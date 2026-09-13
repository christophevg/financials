"""Tests for the importer using a synthetic mini-TSV."""

import pytest

from financials.importer import _build_transaction, _replay_balances
from financials.model import (
  FLAG_BALANCE_CHECKING,
  FLAG_BAD_AMOUNT,
  FLAG_MISSING_CATEGORY,
  FLAG_MISSING_DATE,
  Transaction,
)

TODAY = "2026-01-01"


def test_dated_row_is_fully_parsed():
  t = _build_transaction(
    1,
    7,
    ["Sun, 24 Nov 2024", "Verhuur", "Inkomsten", "€ 800,00", "€ 2.500,00", "€ 12.500,00"],
    TODAY,
  )
  assert t.id == "t0001"
  assert t.date == "2024-11-24"
  assert t.description == "Verhuur"
  assert t.category == "Inkomsten"
  assert t.category_raw == "Inkomsten"
  assert t.subcategory == ""
  assert t.amount_eur == 800.0
  assert t.balance_checking == 2500.0
  assert t.balance_savings == 12500.0
  assert t.status == "actual"
  assert t.source_line == 7
  assert t.flags == []


def test_future_row_is_expected():
  t = _build_transaction(
    1,
    7,
    ["Fri, 24 Dec 2027", "Verhuur", "Inkomsten", "€ 800,00", "€ 2.500,00", "€ 12.500,00"],
    TODAY,
  )
  assert t.status == "expected"


def test_undated_row_is_flagged():
  t = _build_transaction(
    1,
    7,
    ["Brasserie", "Eten", "-€\xa015,60", "€ 2.234,56", "€\xa00,00"],
    TODAY,
  )
  assert t.date == ""
  assert t.description == "Brasserie"
  assert FLAG_MISSING_DATE in t.flags
  assert t.amount_eur == -15.6


def test_undated_row_with_leading_empty_cell():
  # Shape (a): an empty first cell (the absent date), then 5 content cells.
  t = _build_transaction(
    1,
    7,
    ["", "Brasserie", "Eten", "-€\xa015,60", "€\xa02.234,56", "€\xa00,00"],
    TODAY,
  )
  assert t.date == ""
  assert t.description == "Brasserie"
  assert t.category_raw == "Eten"
  assert t.amount_eur == -15.6
  assert t.balance_checking == 2234.56
  assert t.balance_savings == 0.0
  assert FLAG_MISSING_DATE in t.flags
  assert FLAG_BAD_AMOUNT not in t.flags


def test_undated_row_with_junk_in_date_slot():
  # Shape (c): all 6 cells present, but the date cell holds junk ('?').
  t = _build_transaction(
    1,
    7,
    ["?", "Correctie", "???", "-€\xa08,10", "€\xa0456,78", "€\xa00,00"],
    TODAY,
  )
  assert t.date == ""
  assert t.description == "Correctie"
  assert t.category_raw == "???"
  assert t.amount_eur == -8.1
  assert t.balance_checking == 456.78
  assert FLAG_MISSING_DATE in t.flags
  assert FLAG_BAD_AMOUNT not in t.flags
  assert "columns realigned" in t.note

def test_bad_amount_is_flagged():
  t = _build_transaction(
    1,
    7,
    ["Sun, 24 Nov 2024", "?", "Correctie", "???", "€ 456,78", "€ 0,00"],
    TODAY,
  )
  assert FLAG_BAD_AMOUNT in t.flags
  assert t.amount_eur is None


def test_empty_category_is_flagged():
  t = _build_transaction(
    1,
    7,
    ["Sun, 24 Nov 2024", "Apotheek", "", "-€ 4,00", "€ 321,09", "€ 1.250,00"],
    TODAY,
  )
  assert FLAG_MISSING_CATEGORY in t.flags
  assert t.category == ""


def test_short_row_is_padded():
  t = _build_transaction(1, 7, ["Sun, 24 Nov 2024", "Verhuur", "Eten", "-€ 5,00"], TODAY)
  assert t.balance_checking is None
  assert t.balance_savings is None


def test_balance_replay_checks_and_flags():
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Eten", "€ 100,00", "€ 100,00", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sun, 24 Nov 2024", "B", "Eten", "-€ 30,00", "€ 70,00", "€ 0,00"], TODAY),
    _build_transaction(3, 3, ["Mon, 25 Nov 2024", "C", "Eten", "€ 10,00", "€ 80,00", "€ 0,00"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert checks >= 2
  assert mismatches == 0
  assert FLAG_BALANCE_CHECKING not in rows[2].flags


def test_balance_replay_detects_mismatch():
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Eten", "€ 100,00", "€ 100,00", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sun, 24 Nov 2024", "B", "Eten", "-€ 30,00", "€ 999,00", "€ 0,00"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert mismatches == 1
  assert FLAG_BALANCE_CHECKING in rows[1].flags


def test_balance_replay_accepts_savings_transfer():
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Sparen", "-€ 100,00", "€ 100,00", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sun, 24 Nov 2024", "B", "Sparen", "-€ 100,00", "€ 0,00", "€ 100,00"], TODAY),
    _build_transaction(3, 3, ["Mon, 25 Nov 2024", "C", "Eten", "-€ 20,00", "-€ 20,00", "€ 100,00"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert mismatches == 0


def test_rounding_tolerates_cent_rounding():
  # A stored amount rounded to cents must not trip the tolerance check.
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Eten", "€ 0,10", "€ 10,10", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sun, 24 Nov 2024", "B", "Eten", "-€ 10,03", "€ 0,07", "€ 0,00"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert mismatches == 0


def test_savings_interest_is_accepted():
  # Interest booked directly into savings: savings grows by |amount|.
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Eten", "€ 100,00", "€ 100,00", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sun, 24 Nov 2024", "Rente", "Sparen", "€ 105,34", "€ 205,34", "€ 105,34"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert mismatches == 0


def test_expected_rows_are_skipped():
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Eten", "€ 100,00", "€ 100,00", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sat, 24 Dec 2027", "Future", "Eten", "€ 500,00", "€ 999,00", "€ 5,00"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert mismatches == 0
  assert rows[1].flags == []


def test_small_savings_deltas_are_tolerated_as_rounding():
  rows = [
    _build_transaction(1, 1, ["Sat, 23 Nov 2024", "A", "Eten", "€ 100,00", "€ 100,00", "€ 0,00"], TODAY),
    _build_transaction(2, 2, ["Sun, 24 Nov 2024", "B", "Eten", "-€ 10,00", "€ 90,00", "€ 0,03"], TODAY),
  ]
  checks, mismatches, tolerated = _replay_balances(rows)
  assert mismatches == 0
  assert tolerated == 1