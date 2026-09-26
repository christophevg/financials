"""Tests: `financials delete` against the journaled ledger (step 6): the
delete is one DeleteMutation carrying the victim's full pre-image; the
engine refuses a mismatched pre-image loudly; unknown targets append
nothing. Expected deletions stay in the register."""

from datetime import date

from financials import delete as module
from financials.commands import cmd_add, cmd_delete
from financials.journal import Journal, load_ledger
from financials.model import (
  STATUS_EXPECTED,
  Transaction,
  find_entry,
  load_expected,
  save_expected,
)


def _journal(tmp_path):
  return Journal(tmp_path / "journal.jsonl")


def _seed(journal):
  cmd_add("2026-01-01", "Opening", "Inkomsten", 1000.0, journal=journal)
  _mutation, row = cmd_add("2026-01-05", "Kruidenier", "Eten", -40.0, journal=journal)
  return row


def test_cmd_delete_reverses_through_tail(tmp_path):
  journal = _journal(tmp_path)
  row = _seed(journal)
  _mutation, added = cmd_add("2026-01-07", "Kafe", "Horeca", -5.0, journal=journal)
  mutation, deleted = cmd_delete(row.id, journal=journal)
  assert deleted is True
  assert mutation.target == row.id
  ledger = load_ledger(journal)
  assert ledger.find(row.id)[0] is None
  # the tail re-derived: Kafe now chains straight from the opening
  kafe = ledger.find(added.id)[0]
  assert kafe.balances == {"checking": 995.0}


def test_cmd_delete_unknown_target_appends_nothing(tmp_path):
  journal = _journal(tmp_path)
  _seed(journal)
  before = len(list(journal))
  mutation, deleted = cmd_delete("d404", journal=journal)
  assert deleted is False
  assert len(list(journal)) == before


def test_delete_confirmation_flow(tmp_path, monkeypatch):
  journal = _journal(tmp_path)
  row = _seed(journal)
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: "ja")
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
  code = module._delete_actual(projected)
  assert code == 0
  assert load_ledger(journal).find(row.id)[0] is None


def test_delete_confirmation_bare_enter_cancels(tmp_path, monkeypatch):
  journal = _journal(tmp_path)
  row = _seed(journal)
  before = len(list(journal))
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: "")
  projected = Transaction(
    id=row.id,
    date=row.date.isoformat(),
    description=row.description,
    category=row.category,
    category_raw=row.category,
    subcategory_raw="",
    subcategory="",
    amount_eur=row.postings.get("checking"),
    status="actual",
    linked_id="",
  )
  code = module._delete_actual(projected)
  assert code == 2
  assert len(list(journal)) == before


def test_delete_expected_from_register(tmp_path, monkeypatch):
  journal = _journal(tmp_path)
  _seed(journal)
  expected_path = tmp_path / "expected.json"
  save_expected(
    [
      Transaction(
        id="e0001",
        date=(date.today()).isoformat(),
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
  monkeypatch.setattr(module, "load_expected", lambda: load_expected(path=expected_path))
  monkeypatch.setattr(module, "save_expected", lambda rows: save_expected(rows, path=expected_path))
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: "ja")
  entry = load_expected(path=expected_path)[0]
  code = module._delete_expected(entry)
  assert code == 0
  assert load_expected(path=expected_path) == []


def test_find_entry_prefix_contract():
  actual: list[Transaction] = []
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
  assert find_entry("e0001", actual, expected).id == "e0001"
  assert find_entry("e9999", actual, expected) is None
  # an e-id never resolves through an actual-looking list
  assert find_entry("e0001", expected, []) is None
