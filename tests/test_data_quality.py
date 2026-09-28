"""Data-quality invariants (SELF-CONTAINED — the suite never reads a
real store; the owner's rule, 2026-09-28). The same six invariants the
former live gate locked are asserted over a synthetic journal built
through the REAL write path (cmd_add/cmd_update/cmd_delete) — testing
that the writers preserve the invariants is stronger than hoping
history stayed clean.

Invariants: categories from the approved set, no empty
category/description, unique ids, checkpoint ≡ fold, contiguous
checking chain, the last entry carries the touched balances.
"""

from __future__ import annotations

import json

import pytest

from financials.commands import cmd_add, cmd_delete, cmd_update
from financials.config import FinancialsConfig, approved_categories
from financials.journal import Journal, checkpoint, fold, load_ledger

# Synthetic categories (personal category names never enter the repo).
_CATEGORIES = ["Inkomsten", "Uitgaven", "Spaar"]


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
  """A small journal built through the real write path, with a
  category set pinned for the whole module (config can be personal)."""
  monkeypatch.setattr(
    "financials.config.get_config",
    lambda: FinancialsConfig(categories=_CATEGORIES, data_dir=tmp_path),
  )
  journal = Journal(tmp_path / "j.jsonl")
  _m1, t1 = cmd_add("2026-01-01", "Salaris", "Inkomsten", 2000.0, journal=journal)
  _m2, t2 = cmd_add("2026-01-02", "Huur", "Uitgaven", -550.0, journal=journal)
  _m3, t3 = cmd_add("2026-01-02", "Boodschappen", "Uitgaven", -65.0, journal=journal)
  cmd_update(t2.id, "2026-01-03", "Huur", "Uitgaven", -575.0, journal=journal)
  cmd_delete(t3.id, journal=journal)
  return load_ledger(journal), journal


def test_categories_are_from_the_approved_set(ledger):
  folded, _journal = ledger
  unexpected = {t.category for t in folded.transactions} - set(
    approved_categories()
  )
  assert unexpected == set(), f"categories outside the approved set: {unexpected}"


def test_every_row_has_a_category_and_description(ledger):
  folded, _journal = ledger
  offenders = [
    (t.id, t.category, t.description)
    for t in folded.transactions
    if not t.category or not t.description
  ]
  assert offenders == []


def test_ids_are_unique(ledger):
  folded, _journal = ledger
  ids = [t.id for t in folded.transactions]
  assert len(ids) == len(set(ids)), "duplicate entry ids in the ledger"


def test_balances_rederive_from_a_fresh_fold(ledger):
  """The loaded checkpoint and a fresh fold of the journal must agree
  exactly (the step-4 convergence invariant)."""
  folded, journal = ledger
  boot = load_ledger(journal)
  refolded, _tip = fold(journal)
  assert [(t.id, t.balances) for t in boot.transactions] == [
    (t.id, t.balances) for t in refolded.transactions
  ]
  # The fixture's own fold must agree too (defense against a fixture
  # regression silently gutting every test in this module).
  assert [(t.id, t.balances) for t in folded.transactions] == [
    (t.id, t.balances) for t in refolded.transactions
  ]


def test_checking_chain_is_contiguous(ledger):
  """Within each date, checking balances must chain: row.balance -
  row.amount == predecessor.balance (the engine's own chaining,
  re-verified independently over the projection)."""
  folded, _journal = ledger
  prev = None
  for t in folded.transactions:
    if prev is not None and t.balances.get("checking") is not None:
      expected = round(prev + t.postings.get("checking", 0.0), 2)
      assert abs(t.balances["checking"] - expected) < 0.005, (
        f"chain break at {t.id} ({t.date}): "
        f"{t.balances['checking']} != {expected}"
      )
    prev = t.balances.get("checking")


def test_tip_matches_surviving_recorded_tip(ledger):
  """The fold's watermark tip is the last journal row's own id (may
  legitimately be a delete row — no entry); balances live on the last
  ENTRY, and the checkpoint agrees with the fold row-by-row."""
  folded, journal = ledger
  refolded, tip = fold(journal)
  last_row = json.loads(journal.path.read_text().splitlines()[-1])
  assert tip == last_row["id"]
  # The last journal row here is a delete → the tip is not an entry.
  assert refolded.find(tip)[0] is None
  last = refolded.transactions[-1]
  assert last.balances.get("checking") is not None
  # Checkpoint ≡ fold (the deleted live gate's core comparison).
  persisted_tip = checkpoint(Journal(journal.path), path=journal.path.parent / "ledger.json")
  assert persisted_tip == tip