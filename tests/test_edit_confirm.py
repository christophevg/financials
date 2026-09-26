"""Tests: expected-row confirm via edit (step 6): edit-to-today-or-past
on an expected row appends a ConfirmMutation (the e-row retires and the
actual lands in one mutation) and drops the expected row."""

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


def _isolate(tmp_path, monkeypatch):
  journal = Journal(tmp_path / "journal.jsonl")
  expected_path = tmp_path / "expected.json"
  save_expected([], path=expected_path)
  monkeypatch.setattr(module, "command_journal", lambda: journal)
  monkeypatch.setattr(module, "load_expected", lambda: load_expected(path=expected_path))
  monkeypatch.setattr(module, "save_expected", lambda rows: save_expected(rows, path=expected_path))
  return journal, expected_path


def _expected_entry(**overrides) -> Transaction:
  base = dict(
    id="e9001",
    date=(date.today() - timedelta(days=1)).isoformat(),
    description="Cadeau",
    category_raw="Uitgaven",
    category="Uitgaven",
    subcategory_raw="",
    subcategory="",
    amount_eur=-750.0,
    status=STATUS_EXPECTED,
    linked_id="",
    source_line=0,
    note="expected entry",
  )
  base.update(overrides)
  return Transaction(**base)


def _seed(journal):
  return cmd_add(
    (date.today() - timedelta(days=1)).isoformat(),
    "Anker",
    "Inkomsten",
    500.0,
    journal=journal,
  )


def _collect_prompt(**overrides):
  """A Prompt.ask mock matching _collect_changes' 4 prompts. Sequence:
  1. Datum (default = current date), 2. Omschrijving (default = current),
  3. Bedrag (default = current amount). Category is monkeypatched
  separately. Overrides: {prompt_substring: answer} — when the prompt
  text contains the substring the override is returned, else the
  default (Enter keeps the value)."""

  def ask(*args, **kwargs):
    default = kwargs.get("default", "")
    for needle, answer in overrides.items():
      if needle in str(args):
        return answer
    return default

  return ask


def test_confirm_creates_actual_and_removes_expected(tmp_path, monkeypatch):
  journal, expected_path = _isolate(tmp_path, monkeypatch)
  _seed(journal)
  save_expected([_expected_entry()], path=expected_path)
  # all-Enter confirm (defaults keep values; date already past)
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: k.get("default", ""))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  entry = load_expected(path=expected_path)[0]
  code = module._edit_expected(entry)
  assert code == 0
  ledger = load_ledger(journal)
  confirmed = [t for t in ledger.transactions if t.description == "Cadeau"]
  assert len(confirmed) == 1
  assert confirmed[0].postings == {"checking": -750.0}
  assert confirmed[0].linked == ["e9001"]
  assert load_expected(path=expected_path) == []


def test_confirm_applies_amount_correction(tmp_path, monkeypatch):
  journal, expected_path = _isolate(tmp_path, monkeypatch)
  _seed(journal)
  save_expected([_expected_entry()], path=expected_path)
  # date moves to today, amount corrected: -760.00
  monkeypatch.setattr(
    module.Prompt, "ask", _collect_prompt(Datum=date.today().isoformat(), Bedrag="-760.00")
  )
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  entry = load_expected(path=expected_path)[0]
  code = module._edit_expected(entry)
  assert code == 0
  confirmed = [t for t in load_ledger(journal).transactions if t.description == "Cadeau"]
  assert confirmed[0].postings == {"checking": -760.0}


def test_confirm_without_changes_converts(tmp_path, monkeypatch):
  journal, expected_path = _isolate(tmp_path, monkeypatch)
  _seed(journal)
  save_expected(
    [
      _expected_entry(
        date=(date.today() - timedelta(days=1)).isoformat(),
      )
    ],
    path=expected_path,
  )
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: k.get("default", ""))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  entry = load_expected(path=expected_path)[0]
  code = module._edit_expected(entry)
  assert code == 0
  assert load_expected(path=expected_path) == []
  confirmed = [t for t in load_ledger(journal).transactions if t.description == "Cadeau"]
  assert len(confirmed) == 1


def test_confirm_backdated_chains_from_predecessor(tmp_path, monkeypatch):
  journal, expected_path = _isolate(tmp_path, monkeypatch)
  _seed(journal)  # anker: +500 at today-1
  cmd_add(
    (date.today() - timedelta(days=2)).isoformat(),
    "Vroeger",
    "Eten",
    -100.0,
    journal=journal,
  )
  save_expected(
    [
      _expected_entry(
        date=(date.today() - timedelta(days=3)).isoformat(),
        amount_eur=-200.0,
      )
    ],
    path=expected_path,
  )
  monkeypatch.setattr(module.Prompt, "ask", _collect_prompt(Bedrag="-200.00"))
  monkeypatch.setattr(module, "ask_category", lambda current="", allow_blank=False: current)
  entry = load_expected(path=expected_path)[0]
  code = module._edit_expected(entry)
  assert code == 0
  confirmed = [t for t in load_ledger(journal).transactions if t.description == "Cadeau"]
  # Date order: Cadeau (d-3, from empty → 0-200 = -200) → Vroeger (d-2,
  # -100 → -300) → Anker (d-1, +500 → 200). The confirm chains from an
  # EMPTY ledger (no rows on d-3 yet): starting balance 0.0, not 500.
  assert confirmed[0].balances["checking"] == -200.0
