"""Unit tests for the financials edit command (day-to-day row editing)."""

import pytest

from financials.edit import build_edit_fixes, find_entry
from financials.model import STATUS_ACTUAL, STATUS_EXPECTED, Transaction


def _transaction(**overrides) -> Transaction:
  base = dict(
    id="t9002",
    date="2026-03-15",
    description="Kruidenier",
    category_raw="Eten",
    category="Eten",
    subcategory_raw="",
    subcategory="",
    amount_eur=-40.0,
    status=STATUS_ACTUAL,
    linked_id="",
    balance_checking=1234.56,
    balance_savings=6543.21,
  )
  base.update(overrides)
  return Transaction(**base)


def test_build_no_changes_returns_empty():
  assert build_edit_fixes(_transaction(), {}) == []


def test_build_amount_change_appends_rebase():
  fixes = build_edit_fixes(_transaction(), {"amount_eur": -45.0})
  assert [f["field"] for f in fixes if "field" in f] == ["amount_eur"]
  assert fixes[-1]["op"] == "rebase_checking"
  assert all(f.get("op") != "sort_by_date" for f in fixes)


def test_global_ops_carry_id_all():
  """rebase_checking/sort_by_date are global ops — apply_fixes only routes
  them when id=="all" (regression: missing id crashed id-resolution)."""
  fixes = build_edit_fixes(_transaction(), {"amount_eur": -45.0, "date": "2026-03-14"})
  global_ops = [f for f in fixes if "op" in f]
  assert all(f["id"] == "all" for f in global_ops)


def test_build_date_change_appends_sort_and_rebase():
  fixes = build_edit_fixes(_transaction(), {"date": "2026-03-14"})
  assert [f.get("op") for f in fixes if "op" in f] == ["sort_by_date", "rebase_checking"]


def test_build_field_ops_carry_from_guard():
  fixes = build_edit_fixes(
    _transaction(), {"description": "Bakker", "amount_eur": -27.50}
  )
  by_field = {f["field"]: f for f in fixes if "field" in f}
  assert by_field["description"]["from"] == "Kruidenier"
  assert by_field["amount_eur"]["from"] == -40.0
  assert by_field["amount_eur"]["to"] == -27.50


def test_build_overdracht_amount_change_refused():
  with pytest.raises(ValueError):
    build_edit_fixes(_transaction(category="Overdracht"), {"amount_eur": -45.0})


def test_build_overdracht_non_amount_change_allowed():
  fixes = build_edit_fixes(
    _transaction(category="Overdracht"), {"description": "Spaaropname"}
  )
  assert fixes[0]["to"] == "Spaaropname"


def test_build_edit_fixes_shape():
  fixes = build_edit_fixes(
    _transaction(), {"amount_eur": -27.50, "description": "Bakker"}
  )
  ops = [f.get("field", f.get("op")) for f in fixes]
  assert ops == ["amount_eur", "description", "rebase_checking"]
  amounts = [f for f in fixes if f.get("field") == "amount_eur"]
  assert amounts[0]["to"] == -27.50


def test_find_entry_resolves_both_stores():
  actual = _transaction()
  expected = _transaction(
    id="e9007", status="expected", balance_checking=None, balance_savings=None
  )
  assert find_entry("t9002", [actual], [expected]) is actual
  assert find_entry("e9007", [actual], [expected]) is expected
  assert find_entry("t9999", [actual], [expected]) is None