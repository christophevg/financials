"""Tests: `financials edit` against the journaled ledger (step 6): actual
edits are one UpdateMutation (id + linked ride along, date moves
re-insert); expected edits stay in the register; edit-to-today confirms;
edit-to-future moves via delete + expected add."""

from datetime import date, timedelta

from financials import edit as module
from financials.commands import cmd_add
from financials.journal import Journal, load_ledger
from financials.model import (
  STATUS_EXPECTED,
  Transaction,
  load_expected,
  save_expected,
)


def _journal(tmp_path):
  return Journal(tmp_path / "journal.jsonl")


def _isolate_expected(tmp_path, monkeypatch):
  expected_path = tmp_path / "expected.json"
  save_expected([], path=expected_path)
  monkeypatch.setattr(module, "load_expected", lambda: load_expected(path=expected_path))
  monkeypatch.setattr(module, "save_expected", lambda rows: save_expected(rows, path=expected_path))
  return expected_path


def _seed(journal):
  cmd_add("2026-01-01", "Opening", "Inkomsten", 1000.0, journal=journal)
  _mutation, row = cmd_add("2026-01-05", "Kruidenier", "Eten", -40.0, journal=journal)
  return row


def test_update_moves_date_and_propagates(tmp_path):
  journal = _journal(tmp_path)
  row = _seed(journal)
  _mutation, moved = module.cmd_update(
    target=row.id,
    iso_date="2026-01-03",
    description="Kruidenier",
    category="Eten",
    amount=-40.0,
    journal=journal,
  )
  assert moved is not None
  assert moved.date.isoformat() == "2026-01-03"
  assert moved.balances["checking"] == 960.0
  # tail re-derived: nothing between the opening and the moved row now
  dates = [t.date.isoformat() for t in load_ledger(journal).transactions]
  assert dates == ["2026-01-01", "2026-01-03"]


def test_update_unknown_target_returns_none(tmp_path):
  journal = _journal(tmp_path)
  _seed(journal)
  _mutation, moved = module.cmd_update(
    target="d404",
    iso_date="2026-01-06",
    description="ghost",
    category="Eten",
    amount=-1.0,
    journal=journal,
  )
  assert moved is None


def test_edit_actual_prompts_and_applies(tmp_path, monkeypatch):
  journal = _journal(tmp_path)
  row = _seed(journal)
  # _collect_changes prompts 3x via Prompt.ask (Datum/Omschrijving/Bedrag,
  # each with a default) — the category prompt is separately patched.
  # Answer in sequence regardless of defaults.
  answers = iter(["2026-01-06", "Kruidenier Zaterdag", "Eten", "-45.00"])
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: next(answers))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  projected = Transaction(
    id=row.id,
    date=row.date.isoformat(),
    description=row.description,
    category_raw=row.category,
    category=row.category,
    subcategory_raw="",
    subcategory="",
    amount_eur=row.postings.get("checking"),
    status="actual",
    linked_id="",
  )
  code = module._edit_actual(projected)
  assert code == 0
  ledger = load_ledger(journal)
  updated = ledger.find(row.id)[0]
  assert updated is not None
  assert updated.description == "Kruidenier Zaterdag"


def test_edit_expected_in_register(tmp_path, monkeypatch):
  journal = _journal(tmp_path)
  _seed(journal)
  expected_path = _isolate_expected(tmp_path, monkeypatch)
  save_expected(
    [
      Transaction(
        id="e0001",
        date=(date.today() + timedelta(days=10)).isoformat(),
        description="Cadeau",
        category_raw="Uitgaven",
        category="Uitgaven",
        subcategory_raw="",
        subcategory="",
        amount_eur=-50.0,
        status=STATUS_EXPECTED,
        linked_id="",
      )
    ],
    path=expected_path,
  )
  entry = load_expected(path=expected_path)[0]
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: k.get("default", ""))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  changes = module._collect_changes(entry, actual=False)
  assert changes is not None
  code = module._edit_expected(entry)
  assert code == 0
  expected = load_expected(path=expected_path)
  assert expected[0].id == "e0001"  # unchanged (all-Enter, future date)


def test_find_entry_prefix_scoping_preserved():
  """The documented id contract survives the model slim-down: e-ids
  resolve only in the expected list, t/other ids only in the given list."""
  actual = [
    Transaction(
      id="t0001",
      date="2026-01-01",
      description="x",
      category_raw="Eten",
      category="Eten",
      subcategory_raw="",
      subcategory="",
      amount_eur=-1.0,
      status="actual",
      linked_id="",
    )
  ]
  expected = [
    Transaction(
      id="e0001",
      date="2026-02-01",
      description="y",
      category_raw="Uitgaven",
      category="Uitgaven",
      subcategory_raw="",
      subcategory="",
      amount_eur=-2.0,
      status=STATUS_EXPECTED,
      linked_id="",
    )
  ]
  from financials.model import find_entry

  assert find_entry("e0001", actual, expected).id == "e0001"
  assert find_entry("t0001", actual, expected).id == "t0001"
  assert find_entry("e0002", actual, expected) is None


# --- amount prompts: 0 as an expected placeholder ---------------------------


def test_ask_amount_expected_allows_zero(monkeypatch):
  """Expected rows: typing 0 is the placeholder flow — reserve the entry,
  fill in the real amount when it lands."""
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: "0")
  assert module._ask_amount(-50.0, allow_zero=True) == 0.0


def test_ask_amount_actual_still_rejects_zero(monkeypatch, capsys):
  """Actual rows: a committed 0,00 actual stays meaningless — 0 is refused
  unless the current value is already 0."""
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: "0")
  assert module._ask_amount(-50.0) is None
  assert "niet toegelaten" in capsys.readouterr().out
