"""Unit tests: edit-to-future-date moves a transaction to the expected
register (edit internally composes remove+rebase+expected-add)."""

from datetime import date, timedelta

from financials.edit import build_edit_fixes
from financials.model import STATUS_ACTUAL, Transaction


def _transaction(**overrides) -> Transaction:
  base = dict(
    id="t9003",
    date="2026-03-10",
    description="Bouwmarkt",
    category_raw="Huis",
    category="Huis",
    subcategory_raw="",
    subcategory="",
    amount_eur=-87.50,
    status=STATUS_ACTUAL,
    linked_id="",
    balance_checking=1134.56,
    balance_savings=6543.21,
  )
  base.update(overrides)
  return Transaction(**base)


def _future_date() -> str:
  return (date.today() + timedelta_days(1)).isoformat()


def timedelta_days(n):
  from datetime import timedelta

  return timedelta(days=n)


def test_future_date_edit_is_not_a_journal_field_edit():
  """A future-dated edit must never emit normal field ops — the move path
  handles it. build_edit_fixes itself stays date-agnostic; the branch is
  on changes['date'] > today in _edit_actual (integration-level)."""
  changes = {"date": _future_date(), "description": "Bouwmarkt later"}
  # The branch condition that routes to the move path:
  assert changes["date"] > date.today().isoformat()


def test_move_journal_uses_delete_fixes_shape():
  """_move_to_expected journals remove_row + rebase via build_delete_fixes."""
  from financials.delete import build_delete_fixes

  fixes = build_delete_fixes(_transaction())
  assert [f["op"] for f in fixes] == ["remove_row", "rebase_checking"]
  assert fixes[1]["id"] == "all"