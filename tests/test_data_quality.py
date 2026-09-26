"""Data-quality gate for the journaled ledger (live data; skipped when
no journal exists yet). Locks the invariants the fold guarantees and the
owner's hygiene rules: categories canonical, ids unique, balances
re-derivable, checkpoint converged to the journal tip.
"""

import pytest

from financials.config import approved_categories, data_dir
from financials.fixes import journal_file
from financials.journal import fold

_LIVE = data_dir() / "journal.jsonl"

pytestmark = pytest.mark.skipif(not _LIVE.exists(), reason="no journal yet")


@pytest.fixture(autouse=True)
def _live_journal(monkeypatch):
  """Re-point the journal resolvers at the REAL promoted journal (the
  conftest autouse fixture isolates them to tmp; the live gate reads the
  owner's actual data, read-only). Module-attribute references (imported
  at top level) resolve through the module, so patching the module
  attribute is what the code under test sees."""
  monkeypatch.setattr("financials.fixes.journal_file", lambda: _LIVE)
  monkeypatch.setattr("financials.fixes.migration_journal_file", lambda: _LIVE)
  # This module's own top-level binding must follow (functions defined
  # here call the imported name directly). The module is imported as a
  # top-level script module, not via the `tests` package.
  monkeypatch.setattr(f"{__name__}.journal_file", lambda: _LIVE)


def _ledger():
  from financials.journal import load_ledger

  return load_ledger(journal_file())


def test_categories_are_from_the_approved_set():
  unexpected = {t.category for t in _ledger().transactions} - approved_categories()
  assert unexpected == set(), f"categories outside the approved set: {unexpected}"


def test_every_row_has_a_category_and_description():
  offenders = [
    (t.id, t.category, t.description)
    for t in _ledger().transactions
    if not t.category or not t.description
  ]
  assert offenders == []


def test_ids_are_unique():
  ids = [t.id for t in _ledger().transactions]
  assert len(ids) == len(set(ids)), "duplicate entry ids in the ledger"


def test_balances_rederive_from_a_fresh_fold():
  """The committed checkpoint and a fresh fold of the journal must agree
  exactly (the step-4 convergence invariant, re-checked live)."""
  boot = _ledger()
  folded, _tip = fold(_LIVE)
  assert [(t.id, t.balances) for t in boot.transactions] == [
    (t.id, t.balances) for t in folded.transactions
  ]


def test_checking_chain_is_contiguous():
  """Within each date, checking balances must chain: row.balance -
  row.amount == predecessor.balance (the engine's own chaining,
  re-verified independently over the projection)."""
  ledger = _ledger()
  prev = None
  for t in ledger.transactions:
    if prev is not None and t.balances.get("checking") is not None:
      expected = round(prev + t.postings.get("checking", 0.0), 2)
      assert abs(t.balances["checking"] - expected) < 0.005, (
        f"chain break at {t.id} ({t.date}): {t.balances['checking']} != {expected}"
      )
    prev = t.balances.get("checking")


def test_tip_matches_surviving_recorded_tip():
  """The fold's tip carries both account balances (invariant-only since
  2026-09-26: the owner's live balances move with daily use, so a
  hardcoded tip would go stale — and no preservation invariant holds
  across the full history, since savings has genuinely moved before).
  The concrete values live in the private data store, never in code."""
  folded, _tip = fold(_LIVE)
  last = folded.transactions[-1]
  assert last.balances.get("checking") is not None
  assert last.balances.get("savings") is not None
