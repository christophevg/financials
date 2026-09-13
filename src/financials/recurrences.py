"""Recurring rules: storage, expansion, and superseding (financials recurrence).

Rules are NEVER materialized into the transaction store. They expand
dynamically at view time into virtual expected instances for a period
window; an instance is superseded (skipped) when a real transaction covers
the same period — same category, amount within tolerance, within ± days of
the rule's day-of-month.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from financials.model import Transaction, recurrences_file

AMOUNT_TOLERANCE = 0.01
DAY_WINDOW = 5  # days around the rule day that a covering transaction may sit


@dataclass
class Recurrence:
  description: str
  category: str
  amount: float
  frequency: str  # "monthly" | "yearly"
  day: int | None = None  # day-of-month for monthly
  month: int | None = None  # month-of-year for yearly (1-12)
  start: str = ""  # ISO date; instances only from this date on
  end: str = ""  # ISO date; last instance at or before this date ("" = none)
  active: bool = True
  note: str = ""


def load_recurrences(path: Path | None = None) -> list[Recurrence]:
  path = path or recurrences_file()
  if not path.exists():
    return []
  with open(path, encoding="utf-8") as fh:
    rows = json.load(fh)
  return [Recurrence(**row) for row in rows]


def save_recurrences(recurrences: list[Recurrence], path: Path | None = None) -> None:
  path = path or recurrences_file()
  path.parent.mkdir(parents=True, exist_ok=True)
  with open(path, "w", encoding="utf-8") as fh:
    json.dump([asdict(r) for r in recurrences], fh, ensure_ascii=False, indent=2)
    fh.write("\n")


def _instances_for(rule: Recurrence, window_start: date, window_end: date) -> list[date]:
  """Rule instance dates within [window_start, window_end]."""
  dates: list[date] = []
  start = date.fromisoformat(rule.start) if rule.start else window_start
  end = min(window_end, date.fromisoformat(rule.end)) if rule.end else window_end
  if end < start:
    return dates

  def push(year: int, month: int, day: int) -> None:
    try:
      instance = date(year, month, day)
    except ValueError:
      return  # e.g. day 31 in a 30-day month: skipped, next month resumes
    if window_start <= instance <= end:
      dates.append(instance)

  if rule.frequency == "monthly":
    day = rule.day or 1
    year, month = window_start.year, window_start.month
    while (year, month) <= (window_end.year, window_end.month):
      push(year, month, day)
      month += 1
      if month > 12:
        year, month = year + 1, 1
  elif rule.frequency == "yearly":
    month = rule.month or 1
    day = rule.day or 1
    for year in range(window_start.year, window_end.year + 1):
      push(year, month, day)
  return sorted(dates)


def _covers(occurrence: date, rule: Recurrence, transactions: list[Transaction]) -> bool:
  """True when a real transaction covers this rule instance's period."""
  window_lo = occurrence - timedelta(days=DAY_WINDOW)
  window_hi = occurrence + timedelta(days=DAY_WINDOW)
  for t in transactions:
    if not t.date or not t.date.startswith(str(occurrence.year)):
      continue
    t_date = date.fromisoformat(t.date)
    if not (window_lo <= t_date <= window_hi):
      continue
    if t.category != rule.category:
      continue
    if t.amount_eur is None or abs(t.amount_eur - rule.amount) > AMOUNT_TOLERANCE:
      continue
    return True
  return False


def expand_recurrences(
  rules: list[Recurrence],
  transactions: list[Transaction],
  window_start: date,
  window_end: date,
) -> list[Transaction]:
  """Virtual expected instances for the window, superseded where covered."""
  virtual: list[Transaction] = []
  for rule in rules:
    if not rule.active:
      continue
    for occurrence in _instances_for(rule, window_start, window_end):
      if _covers(occurrence, rule, transactions):
        continue
      virtual.append(
        Transaction(
          id=f"r:{rule.description[:24]}:{occurrence.isoformat()}",
          date=occurrence.isoformat(),
          description=rule.description,
          category=rule.category,
          category_raw=rule.category,
          subcategory_raw="",
          subcategory="",
          amount_eur=rule.amount,
          status="expected",
          linked_id="",
          balance_checking=None,
          balance_savings=None,
          flags=[],
          source_line=0,
          note="recurrence rule instance",
        )
      )
  return virtual


def detect_candidates(
  transactions: list[Transaction], min_occurrences: int = 3
) -> list[dict]:
  """Propose recurring rules from history: same description+category,
  similar amounts, regular monthly or yearly spacing, at least
  min_occurrences actual occurrences. Returns proposal dicts (not rules)."""
  groups: dict[tuple[str, str], list[Transaction]] = {}
  for t in transactions:
    if not t.date or t.amount_eur in (None, 0.0):
      continue
    if t.description and t.category:
      groups.setdefault((t.description, t.category), []).append(t)

  proposals: list[dict] = []
  for (description, category), rows in groups.items():
    rows = [t for t in rows if t.status == "actual"]
    if len(rows) < min_occurrences:
      continue
    median = sorted(r.amount_eur for r in rows)[len(rows) // 2]
    inliers = [
      t
      for t in rows
      if abs(t.amount_eur - median) <= max(abs(median) * 0.05, 0.5)
    ]
    if len(inliers) < min_occurrences:
      continue
    dates = sorted(date.fromisoformat(t.date) for t in inliers)
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    monthly = [g for g in gaps if 26 <= g <= 38]
    yearly = [g for g in gaps if 360 <= g <= 372]
    if len(monthly) >= min_occurrences - 1:
      proposals.append(
        {
          "description": description,
          "category": category,
          "amount": median,
          "frequency": "monthly",
          "day": dates[-1].day,
          "start": dates[0].isoformat(),
          "occurrences": len(inliers),
        }
      )
    elif len(yearly) >= min_occurrences - 1:
      proposals.append(
        {
          "description": description,
          "category": category,
          "amount": median,
          "frequency": "yearly",
          "month": dates[-1].month,
          "day": dates[-1].day,
          "start": dates[0].isoformat(),
          "occurrences": len(inliers),
        }
      )
  return proposals
