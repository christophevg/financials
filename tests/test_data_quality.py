"""Data-quality gate for data/transactions.json.

These tests run against the actual data file (skipped when it does not exist
yet) and lock in the invariants established during import and cleanup.
"""

import pytest

from financials.importer import _replay_balances
from financials.model import (
  APPROVED_CATEGORIES,
  load_transactions,
  transactions_file,
)

pytestmark = pytest.mark.skipif(
  not transactions_file().exists(), reason="transactions.json not imported yet"
)


def test_every_row_has_a_category():
  transactions = load_transactions()
  empty = [t.id for t in transactions if not t.category]
  assert empty == [], f"rows without category: {empty}"


def test_categories_are_from_the_approved_set():
  transactions = load_transactions()
  unexpected = {t.category for t in transactions} - APPROVED_CATEGORIES
  assert unexpected == set(), f"categories outside the approved set: {unexpected}"


def test_no_unresolved_anomaly_flags():
  transactions = load_transactions()
  problematic = [
    (t.id, t.flags)
    for t in transactions
    if any(
      flag in t.flags
      for flag in ("missing-date", "missing-category", "unparsable-amount", "unparsable-date")
    )
  ]
  assert problematic == [], f"unresolved anomaly flags: {problematic}"


def test_balance_replay_has_no_mismatches():
  transactions = load_transactions()
  _, mismatches, _ = _replay_balances(transactions)
  details = []
  for index, t in enumerate(transactions):
    if "balance-mismatch-savings" in t.flags:
      prev = transactions[index - 1] if index else None
      details.append(
        f"{t.id} ({t.date} {t.description}): recorded savings {t.balance_savings}, "
        f"previous {prev.id if prev else '-'} savings {prev.balance_savings if prev else '-'}, "
        f"amount {t.amount_eur}"
      )
    elif "balance-mismatch-checking" in t.flags:
      details.append(
        f"{t.id} ({t.date} {t.description}): recorded checking {t.balance_checking}, "
        f"amount {t.amount_eur}"
      )
  assert mismatches == 0, f"balance mismatches: {details}"


def test_every_transaction_has_amount_and_balances():
  transactions = load_transactions()
  broken = [
    t.id
    for t in transactions
    if t.amount_eur is None
    or (t.balance_checking is None and not t.note)
    or (t.balance_savings is None and not t.note)
  ]
  # A missing balance is acceptable only when the row's note documents why
  # (e.g. interest booked directly into savings with no checking delta).
  undocumented = [
    t.id
    for t in transactions
    if t.note
    and (t.amount_eur is None or t.balance_checking is None or t.balance_savings is None)
    and "source" not in t.note
  ]
  assert broken == [] and undocumented == [], (
    f"rows missing amount or balances: {broken}; undocumented: {undocumented}"
  )
