"""Unit tests for the quick-add shorthand parser."""

from datetime import date, timedelta

import pytest

from financials.shorthand import ShorthandError, parse_date_token, parse_shorthand


def _today() -> str:
  return date.today().isoformat()


def test_example_full():
  s = parse_shorthand("gisteren -25 Kafe")
  assert s.date == (date.today() - timedelta(days=1)).isoformat()
  assert s.amount == -25.0
  assert s.category is None
  assert s.description == "Kafe"


def test_category_colon_convention():
  s = parse_shorthand("-25 Eten: Kafe")
  assert s.category == "Eten"
  assert s.description == "Kafe"
  assert s.amount == -25.0


def test_category_case_insensitive_canonicalized():
  s = parse_shorthand("gisteren -25 eTeN: Kafe")
  assert s.category == "Eten"


def test_non_category_colon_kept_in_description():
  s = parse_shorthand("-25 19:30 aankomst")
  assert s.category is None
  assert s.description == "19:30 aankomst"


def test_bare_number_is_never_an_amount():
  s = parse_shorthand("25 jaar jubileum")
  assert s.amount is None
  assert s.description == "25 jaar jubileum"


def test_currency_marked_amount_single_token():
  assert parse_shorthand("€25 Kafe").amount == 25.0
  assert parse_shorthand("25eur Kafe").amount == 25.0


def test_plus_sign_income():
  s = parse_shorthand(f"{_today()} +600 Spaarstorting")
  assert s.amount == 600.0
  assert s.date == _today()


def test_comma_decimal():
  assert parse_shorthand("-23,45 Kafe").amount == -23.45


def test_zero_amount_rejected():
  with pytest.raises(ShorthandError):
    parse_shorthand("-0 Kafe")


def test_unparseable_amount_raises():
  with pytest.raises(ShorthandError):
    parse_shorthand("- Kafe")


def test_empty_raises():
  with pytest.raises(ShorthandError):
    parse_shorthand("   ")


def test_all_fields_optional_seed():
  s = parse_shorthand("Kafe")
  assert s.date is None and s.amount is None and s.category is None
  assert s.description == "Kafe"


def test_relative_minus_three_days():
  s = parse_shorthand("-3d -25 Kafe")
  assert s.date == (date.today() - timedelta(days=3)).isoformat()
  assert s.amount == -25.0


def test_bare_negative_is_amount_not_date():
  s = parse_shorthand("-25 Kafe")
  assert s.date is None
  assert s.amount == -25.0


def test_zero_amount_rejected_as_error():
  with pytest.raises(ShorthandError):
    parse_shorthand("-0 Kafe")


def test_lone_minus_rejected_as_error():
  with pytest.raises(ShorthandError):
    parse_shorthand("- Kafe")


def test_iso_date_passthrough():
  s = parse_shorthand("2026-09-01 -10 Diensten: Herstelling")
  assert s.date == "2026-09-01"
  assert s.category == "Diensten"


def test_date_token_words():
  assert parse_date_token("vandaag") == _today()
  assert parse_date_token("morgen") == (date.today() + timedelta(days=1)).isoformat()
  assert parse_date_token("overmorgen") == (date.today() + timedelta(days=2)).isoformat()
  assert parse_date_token("eergisteren") == (date.today() - timedelta(days=2)).isoformat()
  assert parse_date_token("Kafe") is None
