"""Tests: financials confirm — the zero-prompt confirm of open entries.

Covers the resolver branches (committed no-op, expected commit,
rule-instance commit), the future-dated refusals, the unknown id, the
drifted-commit exception recording, and the bare picker. All stores are
isolated via conftest (journal + expected) and a recurrences_file patch
here — no real store is ever touched.
"""

from datetime import date, timedelta

from financials import confirm as module
from financials.commands import cmd_add
from financials.journal import Journal, load_ledger
from financials.model import (
  STATUS_EXPECTED,
  Transaction,
  load_expected,
  save_expected,
)
from financials.recurrence_cli import _find_instance
from financials.recurrences import (
  Recurrence,
  _instances_for,
  instance_id,
  load_recurrences,
  save_recurrences,
)


def _isolate(tmp_path, monkeypatch):
  """conftest already points the journal and the expected store at tmp;
  add the recurrences store (not covered by conftest)."""
  rec_path = tmp_path / "recurrences.json"
  save_recurrences([], path=rec_path)
  monkeypatch.setattr("financials.recurrences.recurrences_file", lambda: rec_path)
  return tmp_path / "journal.jsonl"


def _seed(journal):
  """One committed anchor row (+500 at today-1) — the balance seed."""
  _mutation, entry = cmd_add(
    (date.today() - timedelta(days=1)).isoformat(),
    "Anker",
    "Inkomsten",
    500.0,
    journal=Journal(journal),
  )
  return entry


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


# --- expected (e####) -----------------------------------------------------


def test_confirm_expected_landed(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected([_expected_entry()])
  code = module.confirm_transaction("e9001")
  assert code == 0
  confirmed = [
    t for t in load_ledger(Journal(journal_path)).transactions if t.description == "Cadeau"
  ]
  assert len(confirmed) == 1
  assert confirmed[0].postings == {"checking": -750.0}
  assert confirmed[0].linked == ["e9001"]
  assert load_expected() == []


def test_confirm_expected_future_refused(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected([_expected_entry(id="e9002", date=(date.today() + timedelta(days=3)).isoformat())])
  code = module.confirm_transaction("e9002")
  assert code == 2
  # untouched: still expected, nothing committed
  assert len(load_expected()) == 1
  committed = load_ledger(Journal(journal_path)).transactions
  assert [t for t in committed if t.description == "Cadeau"] == []


def test_confirm_unknown_expected_id(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected([_expected_entry()])
  assert module.confirm_transaction("e9999") == 2


def test_confirm_committed_id_is_noop(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  entry = _seed(journal_path)
  before = len(load_ledger(Journal(journal_path)).transactions)
  assert module.confirm_transaction(entry.id) == 0
  assert len(load_ledger(Journal(journal_path)).transactions) == before


def test_confirm_committed_description_is_unknown(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  assert module.confirm_transaction("Anker") == 2


# --- rule instances (r:-hash) ----------------------------------------------


def _monthly_rule(**overrides) -> Recurrence:
  base = dict(
    description="Huur",
    category="Huis",
    amount=-500.0,
    frequency="monthly",
    day=5,
    month=None,
    start=f"{date.today().year}-01-05",
    end="",
  )
  base.update(overrides)
  return Recurrence(**base)


def _landed_instance(rule: Recurrence) -> date:
  """The latest rule instance on or before today (the test's landed one)."""
  occurrences = [
    d
    for d in _instances_for(rule, date.today() - timedelta(days=60), date.today())
    if d <= date.today()
  ]
  assert occurrences, "expected a landed instance for the test rule"
  return occurrences[-1]


def test_confirm_rule_instance_landed(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_recurrences([_monthly_rule()])
  rule = load_recurrences()[0]
  occurrence = _landed_instance(rule)
  code = module.confirm_transaction(instance_id(rule, occurrence))
  assert code == 0
  confirmed = [
    t for t in load_ledger(Journal(journal_path)).transactions if t.description == "Huur"
  ]
  assert len(confirmed) == 1
  assert confirmed[0].postings == {"checking": -500.0}
  # a covering commit supersedes: no exception was recorded
  assert load_recurrences()[0].exceptions == []


def test_confirm_rule_instance_future_refused(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  future = (date.today() + timedelta(days=3)).isoformat()
  save_recurrences([_monthly_rule(description="Str", frequency="weekly", day=None, start=future)])
  rule = load_recurrences()[0]
  rid = instance_id(rule, date.fromisoformat(future))
  assert _find_instance(rid) is not None  # resolvable, but...
  code = module.confirm_transaction(rid)
  assert code == 2  # ...refused: not money yet
  committed = load_ledger(Journal(journal_path)).transactions
  assert [t for t in committed if t.description == "Str"] == []
  assert load_recurrences()[0].exceptions == []


def test_commit_instance_drifted_records_exception(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_recurrences([_monthly_rule()])
  rule = load_recurrences()[0]
  occurrence = _landed_instance(rule)
  from financials.recurrence_cli import commit_instance

  # drifted amount: outside the superseding tolerance -> exception
  code = commit_instance(rule, occurrence, occurrence, "Huur", "Huis", -410.0)
  assert code == 0
  assert occurrence.isoformat() in load_recurrences()[0].exceptions


# --- bare picker ------------------------------------------------------------


def test_bare_confirm_without_open_rows(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  assert module.confirm_transaction(None) == 2


def test_bare_confirm_picks_from_open(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected([_expected_entry()])
  # the picker shows the backlog; typing the id confirms it
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: "e9001")
  code = module.confirm_transaction(None)
  assert code == 0
  assert load_expected() == []
  assert [t for t in load_ledger(Journal(journal_path)).transactions if t.description == "Cadeau"]
