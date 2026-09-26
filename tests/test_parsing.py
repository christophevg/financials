"""Tests for the TSV parsing helpers."""

from financials.parsing import (
  check_weekday,
  looks_like_date,
  parse_amount,
  parse_date,
  split_category,
)


class TestParseDate:
  def test_standard_date(self):
    assert parse_date("Sun, 24 Nov 2024") == "2024-11-24"

  def test_single_digit_day(self):
    assert parse_date("Wed, 1 Dec 2024") == "2024-12-01"

  def test_all_months(self):
    for text, expected in [
      ("Mon, 3 Feb 2025", "2025-02-03"),
      ("Mon, 3 Mrt 2025", "2025-03-03"),
      ("Mon, 3 Mei 2025", "2025-05-03"),
      ("Mon, 3 Okt 2025", "2025-10-03"),
    ]:
      assert parse_date(text) == expected

  def test_unparsable_returns_none(self):
    assert parse_date("Verhuur") is None
    assert parse_date("") is None
    assert parse_date("Sun, 24 Xyz 2024") is None


class TestParseAmount:
  def test_positive(self):
    assert parse_amount("€ 350,00") == 350.0

  def test_negative(self):
    assert parse_amount("-€ 1.000,00") == -1000.0

  def test_thousands_separator(self):
    assert parse_amount("€ 20.000,00") == 20000.0

  def test_negative_with_thousands(self):
    assert parse_amount("-€ 2.387,00") == -2387.0

  def test_no_decimals(self):
    assert parse_amount("€ 25") == 25.0

  def test_non_breaking_space(self):
    assert parse_amount("-€\xa03,75") == -3.75

  def test_unparsable_returns_none(self):
    assert parse_amount("n/a") is None
    assert parse_amount("") is None


class TestSplitCategory:
  def test_plain(self):
    assert split_category("Eten") == ("Eten", "")

  def test_hierarchical(self):
    assert split_category("Sport: Lopen, Snowworld") == ("Sport", "Lopen, Snowworld")

  def test_whitespace(self):
    assert split_category("  Huis :  Tuin ") == ("Huis", "Tuin")


class TestDateGuards:
  def test_looks_like_date(self):
    assert looks_like_date("Sun, 24 Nov 2024") is True
    assert looks_like_date("Brasserie") is False

  def test_check_weekday_consistent(self):
    assert check_weekday("Sun, 24 Nov 2024") == ""

  def test_check_weekday_mismatch(self):
    assert "mismatch" in check_weekday("Mon, 24 Nov 2024")
