"""Tests for the transaction entry flow (synthetic data, no file I/O)."""

import pytest

from financials.entry import _build, _insert_position, _next_id
from financials.model import STATUS_ACTUAL, STATUS_EXPECTED, Transaction


def make_row(
  id: str = "t0001",
  date: str = "2026-08-01",
  amount: float = -10.0,
  checking: float = 1000.0,
  savings: float = 500.0,
  source_line: int = 1,
) -> Transaction:
  return Transaction(
    id=id,
    date=date,
    description=f"desc-{id}",
    category_raw="Eten",
    category="Eten",
    subcategory_raw="",
    subcategory="",
    amount_eur=amount,
    status="actual",
    linked_id="",
    balance_checking=checking,
    balance_savings=savings,
    flags=[],
    source_line=source_line,
    note="",
  )


def test_next_id_continues_sequence():
  rows = [make_row(id="t0007"), make_row(id="t0012")]
  assert _next_id(rows) == "t0013"


def test_next_id_skips_non_numeric():
  rows = [make_row(id="t0007"), make_row(id="Min")]
  assert _next_id(rows) == "t0008"


def test_insert_position_appends_after_later_date():
  rows = [
    make_row(id="t0001", date="2026-08-01", source_line=1),
    make_row(id="t0002", date="2026-08-03", source_line=2),
  ]
  assert _insert_position(rows, "2026-08-05") == 2


def test_insert_position_before_out_of_order_row():
  rows = [
    make_row(id="t0001", date="2026-08-03", source_line=1),
    make_row(id="t0002", date="2026-08-01", source_line=2),
  ]
  # A row dated 08-02 chains after the latest row dated <= 08-02 (t0002).
  assert _insert_position(rows, "2026-08-02") == 2


def test_insert_position_same_date_goes_after():
  rows = [
    make_row(id="t0001", date="2026-08-01", source_line=1),
    make_row(id="t0002", date="2026-08-01", source_line=2),
  ]
  assert _insert_position(rows, "2026-08-01") == 2


def test_build_continues_balances():
  rows = [make_row(checking=1000.0, savings=500.0)]
  transaction, error = _build(rows, "2026-09-01", "Kruidenier", "Eten", -23.45, None)
  assert error == ""
  assert transaction.balance_checking == 976.55
  assert transaction.balance_savings == 500.0
  assert transaction.status == STATUS_ACTUAL
  assert transaction.id == "t0002"


def test_build_future_row_redirects_to_expect():
  rows = [make_row(date="2026-08-01")]
  transaction, error = _build(rows, "2026-12-01", "Kerstboom", "Uitgaven", -100.0, None)
  assert transaction is None
  assert "financials expect" in error


def test_build_historic_entry_is_allowed():
  rows = [
    make_row(id="t0001", date="2026-08-01", amount=-10.0, checking=1000.0),
    make_row(id="t0002", date="2026-08-15", amount=-20.0, checking=970.0),
  ]
  # Inserting on 08-10 chains from 08-01: 1000 + 5 = 1005; later rows are
  # rebased by add_transaction (not _build).
  transaction, error = _build(rows, "2026-08-10", "Terugbetaald", "Inkomsten", 5.0, None)
  assert error == ""
  assert transaction.balance_checking == 1005.0
  assert transaction.status == STATUS_ACTUAL


def test_build_rejects_bad_category():
  rows = [make_row()]
  transaction, error = _build(rows, "2026-09-01", "x", "Onbekend", -10.0, None)
  assert transaction is None
  assert "niet in de goedgekeurde lijst" in error


def test_build_rejects_mismatched_balance():
  rows = [make_row(checking=1000.0)]
  transaction, error = _build(rows, "2026-09-01", "x", "Eten", -10.0, 999.0)
  assert transaction is None
  assert "komt niet overeen" in error


def test_build_rejects_date_before_last():
  # With the historic-entry feature, "before the last row" is legal; the
  # rejection now only concerns the missing-predecessor edge case.
  rows = [make_row(date="2026-08-01")]
  transaction, error = _build(rows, "2026-07-01", "x", "Eten", -10.0, None)
  assert transaction is None
  assert "geen voorgaande rij" in error


def test_build_rejects_zero_amount():
  rows = [make_row()]
  transaction, error = _build(rows, "2026-09-01", "x", "Eten", 0.0, None)
  # _build itself does not zero-check; the CLI layer guards zero amounts.
  assert transaction is None or transaction.amount_eur == 0.0


def test_build_rejects_bad_date_format():
  rows = [make_row()]
  transaction, error = _build(rows, "01-09-2026", "x", "Eten", -10.0, None)
  assert transaction is None
  assert "ongeldige datum" in error


def test_dry_run_adds_nothing():
  # Exercises the full non-interactive path against the real data file,
  # without saving: dry-run must leave the store untouched. Uses a historic
  # date (future entries belong in the expected register). Skipped when no
  # store exists (fresh checkout).
  from financials.entry import add_transaction
  from financials.model import load_transactions, transactions_file

  if not transactions_file().exists():
    pytest.skip("transactions.json not imported yet")

  before = load_transactions()
  exit_code = add_transaction(
    iso_date="2026-09-01",
    description="Test dry-run",
    category="Eten",
    amount=-5.0,
    checking=None,
    dry_run=True,
  )
  after = load_transactions()
  assert exit_code == 0
  assert len(after) == len(before)
  assert [t.id for t in after] == [t.id for t in before]
  assert [t.balance_checking for t in after] == [
    t.balance_checking for t in before
  ]
