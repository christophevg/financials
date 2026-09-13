"""Quick-add shorthand parser for `financials add "..."`.

Grammar (left to right, whitespace-separated):
  [date] [amount] [Category:]description

- date: ISO (2026-09-12), vandaag/today, morgen/tomorrow, overmorgen,
  gisteren/yesterday, eergisteren, or ±Nd relative days (e.g. -3d).
  Relative days REQUIRE the d suffix — bare "-25" is always an amount.
- amount: signed (+/-) or currency-marked (€/eur) number, comma decimal
  allowed, 0 rejected. A bare unsigned number is NEVER an amount — it
  stays part of the description ("25 jaar jubileum" keeps its text).
- rest: description, optionally "Category: description" where Category
  case-insensitively matches an approved category.

Anything omitted is prompted afterwards by the caller (date defaults to
today via Enter); nothing is ever silently assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from financials.model import APPROVED_CATEGORIES

_DATE_WORDS = {
  "vandaag": 0,
  "today": 0,
  "morgen": 1,
  "tomorrow": 1,
  "overmorgen": 2,
  "gisteren": -1,
  "yesterday": -1,
  "eergisteren": -2,
}

_AMOUNT_RE = re.compile(
  r"^(?P<sign>[+-])?\s*(?P<eur1>€|eur)?\s*(?P<num>\d+(?:[.,]\d{1,2})?)"
  r"\s*(?P<eur2>€|eur)?$",
  re.IGNORECASE,
)


class ShorthandError(ValueError):
  """A quick-add shorthand string could not be parsed."""


@dataclass
class Shorthand:
  """Parsed quick-add parts; any part may be None (then prompted)."""

  date: str | None
  amount: float | None
  category: str | None
  description: str | None


def _canonical_category(text: str) -> str | None:
  lowered = text.strip().lower()
  for approved in APPROVED_CATEGORIES:
    if approved.lower() == lowered:
      return approved
  return None


def parse_date_token(token: str) -> str | None:
  """ISO date, named relative day, or ±Nd relative days; None otherwise.

  Relative days REQUIRE the 'd' suffix (-3d) so bare ±N stays unambiguous
  money ('-25' is an amount, '-3d' is three days ago)."""
  lowered = token.lower()
  if lowered in _DATE_WORDS:
    return (date.today() + timedelta(days=_DATE_WORDS[lowered])).isoformat()
  relative = re.fullmatch(r"([+-])(\d+)d", lowered)
  if relative:
    offset = int(relative.group(2))
    if relative.group(1) == "-":
      offset = -offset
    return (date.today() + timedelta(days=offset)).isoformat()
  try:
    date.fromisoformat(token)
  except ValueError:
    return None
  return token


def parse_amount_token(token: str) -> float | None:
  """Signed/currency-marked amount, or None (bare numbers are not amounts)."""
  match = _AMOUNT_RE.match(token.strip())
  if not match:
    return None
  if not (match.group("sign") or match.group("eur1") or match.group("eur2")):
    return None
  value = round(float(match.group("num").replace(",", ".")), 2)
  if match.group("sign") == "-":
    value = -value
  return value if value != 0 else None


def parse_shorthand(text: str) -> Shorthand:
  """Parse a quick-add string. Raises ShorthandError on malformed input."""
  parts = text.strip().split()
  if not parts:
    raise ShorthandError("lege shorthand — typ bv. \"gisteren -25 Kafe\"")

  iso_date: str | None = None
  parsed_date = parse_date_token(parts[0])
  if parsed_date is not None:
    iso_date = parsed_date
    parts = parts[1:]

  amount: float | None = None
  if parts:
    first = parts[0]
    lowered = first.lower()
    # Sign or € marker in the token ⇒ it *means* to be an amount; a failed
    # parse is then an error, never silently-kept description text.
    if first[0] in "+-" or "€" in lowered or "eur" in lowered:
      parsed_amount = parse_amount_token(first)
      if parsed_amount is None:
        raise ShorthandError(
          f"onbegrijpelijk bedrag {first!r} — gebruik +/- en evt. €, "
          "bv. -25 of +600,50 (0 is niet toegelaten)"
        )
      amount = parsed_amount
      parts = parts[1:]

  rest = " ".join(parts).strip()
  category: str | None = None
  description: str | None = rest or None
  if rest and ":" in rest:
    head, tail = rest.split(":", 1)
    canonical = _canonical_category(head)
    if canonical is not None:
      category = canonical
      description = tail.strip() or None
  return Shorthand(
    date=iso_date, amount=amount, category=category, description=description
  )