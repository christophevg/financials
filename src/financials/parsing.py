"""Parsing of the raw cashflow TSV (Dutch euro locale, English weekday dates).

Row shape (tab-separated, no header):
  Sun, 24 Nov 2024 <tab> Winkelen <tab> Uitgaven <tab> € 123,45 <tab> € 2.345,67 <tab> € 12.345,67
"""

import datetime
import re

DATE_RE = re.compile(
  r"^(?P<wd>[A-Za-z]{3}), (?P<day>\d{1,2}) (?P<mon>[A-Za-z]{3,4}) (?P<year>\d{4})$"
)

MONTHS = {
  "jan": 1, "feb": 2, "mrt": 3, "mar": 3, "apr": 4, "mei": 5, "may": 5,
  "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
  "nov": 11, "dec": 12,
}

AMOUNT_RE = re.compile(r"^(?P<sign>-?)€ (?P<digits>[\d.,]+)$")


def parse_date(text: str) -> str | None:
  """Parse 'Sun, 24 Nov 2024' -> '2024-11-24' (ISO), or None."""
  match = DATE_RE.match(text.strip())
  if not match:
    return None
  month = MONTHS.get(match.group("mon").lower())
  if month is None:
    return None
  return f"{match.group('year')}-{month:02d}-{int(match.group('day')):02d}"


def parse_amount(text: str) -> float | None:
  """Parse '-€ 1.234,56' -> -1234.56 (Dutch locale: '.' thousands, ',' decimal)."""
  cleaned = text.replace("\xa0", " ").strip()
  match = AMOUNT_RE.match(cleaned)
  if not match:
    return None
  sign = -1.0 if match.group("sign") else 1.0
  digits = match.group("digits")
  if "," in digits:
    whole, decimals = digits.rsplit(",", 1)
    value = float(f"{whole.replace('.', '')}.{decimals}")
  else:
    value = float(digits.replace(".", ""))
  return sign * value


def split_category(raw: str) -> tuple[str, str]:
  """Split 'Sport: Lopen, Snowworld' -> ('Sport', 'Lopen, Snowworld')."""
  raw = raw.strip()
  if ":" in raw:
    category, _, subcategory = raw.partition(":")
    return category.strip(), subcategory.strip()
  return raw, ""


def read_rows(tsv_path) -> list[tuple[int, list[str]]]:
  """Read all non-empty lines as (1-based line number, cells)."""
  rows = []
  with open(tsv_path, encoding="utf-8") as fh:
    for lineno, line in enumerate(fh, start=1):
      if not line.strip():
        continue
      rows.append((lineno, line.rstrip("\n").split("\t")))
  return rows


def looks_like_date(text: str) -> bool:
  """True when the text has the shape of a dated row's first cell."""
  return bool(DATE_RE.match(text.strip()))


def check_weekday(text: str) -> str:
  """Note when a date-shaped cell's weekday prefix contradicts the date
  ('' when consistent, or when the text is not date-shaped)."""
  match = DATE_RE.match(text.strip())
  if not match:
    return ""
  month = MONTHS.get(match.group("mon").lower())
  if month is None:
    return "unparsable month"
  try:
    actual = datetime.date(
      int(match.group("year")), month, int(match.group("day"))
    ).strftime("%a")
  except ValueError:
    return "invalid calendar date"
  recorded = match.group("wd").capitalize()
  return "" if actual == recorded else f"weekday mismatch: recorded {recorded}, actual {actual}"
