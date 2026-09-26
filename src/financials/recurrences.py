"""Recurring rules: storage, expansion, and superseding (financials recurrence).

Rules are NEVER materialized into the transaction store. They expand
dynamically at view time into virtual expected instances for a period
window; an instance is superseded (skipped) when a real transaction covers
the same period — same category, amount within tolerance, within ± days of
the rule's day-of-month.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from financials.model import Transaction, recurrences_file

AMOUNT_TOLERANCE = 0.01
DAY_WINDOW = 5  # days around the rule day that a covering transaction may sit
RULE_LOOKBACK_DAYS = 35  # the view's grace lookback before the ledger's anchor


@dataclass
class Recurrence:
  description: str
  category: str
  amount: float
  frequency: str  # "monthly" | "yearly" | "weekly" | "biweekly"
  day: int | None = None  # day-of-month for monthly/yearly
  month: int | None = None  # month-of-year for yearly (1-12)
  start: str = ""  # ISO date; instances only from this date on; weekly/biweekly: the phase anchor
  end: str = ""  # ISO date; last instance at or before this date ("" = none)
  active: bool = True
  note: str = ""
  exceptions: list[str] = field(default_factory=list)  # ISO instance dates exempt from expansion


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
  """Rule instance dates within [window_start, window_end], bounded by the
  rule's own start/end."""
  dates: list[date] = []
  start = max(
    window_start,
    date.fromisoformat(rule.start) if rule.start else window_start,
  )
  end = min(window_end, date.fromisoformat(rule.end)) if rule.end else window_end
  if end < start:
    return dates

  def push(year: int, month: int, day: int) -> None:
    try:
      instance = date(year, month, day)
    except ValueError:
      return  # e.g. day 31 in a 30-day month: skipped, next month resumes
    if start <= instance <= end:
      dates.append(instance)

  if rule.frequency == "monthly":
    day = rule.day or 1
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
      push(year, month, day)
      month += 1
      if month > 12:
        year, month = year + 1, 1
  elif rule.frequency == "yearly":
    month = rule.month or 1
    day = rule.day or 1
    for year in range(start.year, end.year + 1):
      push(year, month, day)
  elif rule.frequency in ("weekly", "biweekly"):
    # Weekly/biweekly anchor on the rule's start date (the phase): the
    # first instance IS start (when inside the window), then every k*7
    # days. Deterministic — instance ids are content hashes, so the
    # schedule must be reconstructible from the rule alone.
    if not rule.start:
      return dates  # phase anchor missing: nothing to expand
    anchor = date.fromisoformat(rule.start)
    step = 7 if rule.frequency == "weekly" else 14
    # first anchor multiple >= start
    k = 0
    instance = anchor + timedelta(days=k * step)
    while instance <= end:
      if instance >= start:
        dates.append(instance)
      k += 1
      instance = anchor + timedelta(days=k * step)
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


def instance_id(rule: Recurrence, occurrence: date) -> str:
  """The virtual instance's content-hash id — deterministic, the SAME
  16-hex scheme as committed rows (with an r: provenance prefix so the
  virtual nature stays visible at a glance). Same rule instance -> same
  id across invocations; editing the RULE changes future instances' ids
  (the id is the manifestation's content, as the owner designed)."""
  payload = {
    "date": occurrence.isoformat(),
    "description": rule.description,
    "category": rule.category,
    "amount": rule.amount,
    "frequency": rule.frequency,
    "day": rule.day,
    "month": rule.month,
  }
  canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
  return "r:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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
      if occurrence.isoformat() in rule.exceptions:
        continue  # edited out: an expected exception replaced this instance
      if _covers(occurrence, rule, transactions):
        continue
      virtual.append(
        Transaction(
          id=instance_id(rule, occurrence),
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
  transactions: list[Transaction],
  min_occurrences: int = 3,
  existing_rules: list[Recurrence] | None = None,
) -> list[dict]:
  """Propose recurring rules from history: same description+category,
  similar amounts (Tier A) or timely-but-varying amounts (Tier B: median
  as nominal, observed range attached), regular weekly/biweekly/monthly/
  yearly spacing, at least min_occurrences actual occurrences. Groups
  already covered by an existing rule are not re-proposed. Returns
  proposal dicts (not rules)."""
  groups: dict[tuple[str, str], list[Transaction]] = {}
  for t in transactions:
    if not t.date or t.amount_eur in (None, 0.0):
      continue
    if t.description and t.category:
      groups.setdefault((t.description, t.category), []).append(t)

  proposals: list[dict] = []
  existing = {(r.description, r.category) for r in (existing_rules or [])}
  for (description, category), rows in groups.items():
    if (description, category) in existing:
      continue  # already formalized as a rule: don't re-propose
    rows = [t for t in rows if t.status == "actual"]
    if len(rows) < min_occurrences:
      continue
    amounts = [r.amount_eur for r in rows if r.amount_eur is not None]
    median = sorted(amounts)[len(amounts) // 2]
    inliers = [
      t
      for t in rows
      if t.amount_eur is not None and abs(t.amount_eur - median) <= max(abs(median) * 0.05, 0.5)
    ]

    # Tier A: constant-amount patterns (amounts within ±5% of the median)
    # — the original detector, checked first and unchanged.
    if len(inliers) >= min_occurrences:
      tier_dates = sorted(date.fromisoformat(t.date) for t in inliers)
      frequency = _classify_frequency(tier_dates, min_occurrences)
      if frequency is not None:
        proposals.append(
          _proposal(description, category, median, frequency, tier_dates, len(inliers))
        )
        continue
      # Tier A's inlier DATES don't classify: fall through to Tier B over
      # ALL rows (the varying amounts may complete a clean pattern).

    # Tier B (owner workflow: expected amount set, then confirmed with the
    # ACTUAL amount — the history is timely but the values differ): classify
    # the dates over ALL occurrences and propose the MEDIAN as the nominal
    # amount. Guardrail: uniform sign (mixed in/out is not one pattern).
    # Deliberately NO magnitude guardrail: budget-style series (owner
    # confirms a fixed budget with the actual spend) vary far more than
    # 2x; a wide spread is surfaced honestly via amount_range instead —
    # proposals are review-only, so a noisy proposal costs a glance.
    signs = {"+" if a > 0 else "-" for a in amounts}
    if len(signs) != 1:
      continue
    tier_dates = sorted(date.fromisoformat(t.date) for t in rows)
    frequency = _classify_frequency(tier_dates, min_occurrences)
    if frequency is None:
      continue
    proposals.append(
      _proposal(
        description,
        category,
        median,
        frequency,
        tier_dates,
        len(rows),
        amount_range=[min(amounts), max(amounts)],
      )
    )
  return proposals


def _classify_frequency(dates: list[date], min_occurrences: int) -> str | None:
  """Classify sorted dates by their gap bands (weekly 5-9, biweekly 11-17,
  monthly 26-38, yearly 360-372); None when no band holds enough gaps.
  Non-overlapping bands, checked most-specific first — a skipped week
  (gap ≈ 14) inside a weekly streak still classifies weekly."""
  gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
  for frequency, lo, hi in (
    ("weekly", 5, 9),
    ("biweekly", 11, 17),
    ("monthly", 26, 38),
    ("yearly", 360, 372),
  ):
    if len([g for g in gaps if lo <= g <= hi]) >= min_occurrences - 1:
      return frequency
  return None


def _proposal(
  description: str,
  category: str,
  amount: float,
  frequency: str,
  dates: list[date],
  occurrences: int,
  amount_range: list[float] | None = None,
) -> dict:
  """One detector proposal dict (frequency-specific fields included)."""
  proposal: dict = {
    "description": description,
    "category": category,
    "amount": amount,
    "frequency": frequency,
    "start": dates[0].isoformat(),  # first occurrence = the phase anchor
    "occurrences": occurrences,
  }
  if frequency == "monthly":
    proposal["day"] = dates[-1].day
  elif frequency == "yearly":
    proposal["month"] = dates[-1].month
    proposal["day"] = dates[-1].day
  if amount_range is not None:
    proposal["amount_range"] = amount_range
  return proposal
