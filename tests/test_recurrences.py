"""Tests: recurring rules — the engine (recurrences.py) and (below) the CLI.

The engine is the migration's existing machinery, previously untested:
expansion is dynamic at view time (rules are NEVER materialized), an
instance is superseded when a real entry covers it (same category, amount
within 0.01, date within ±5 days of the rule's day), and the detector
proposes rules from history — nothing auto-created."""

from datetime import date

import pytest

from financials.config import FinancialsConfig
from financials.model import Transaction
from financials.recurrences import (
  Recurrence,
  _instances_for,
  detect_candidates,
  expand_recurrences,
  instance_id,
  load_recurrences,
  save_recurrences,
)

TODAY = date.today()


def _tx(
  date_: str,
  description: str = "Huur",
  category: str = "Wonen",
  amount: float = -800.0,
) -> Transaction:
  return Transaction(
    id=f"t{date_.replace('-', '')}",
    date=date_,
    description=description,
    category=category,
    amount_eur=amount,
    status="actual",
  )


def _rule(**overrides) -> Recurrence:
  base = {
    "description": "Huur",
    "category": "Wonen",
    "amount": -800.0,
    "frequency": "monthly",
    "day": 1,
    "start": "2026-01-01",
  }
  base.update(overrides)
  return Recurrence(**base)


# --- expansion -----------------------------------------------------------


def test_monthly_expansion_within_window():
  rules = [_rule(day=1, start="2026-01-01")]
  instances = _instances_for(rules[0], date(2026, 1, 1), date(2026, 3, 31))
  assert [d.isoformat() for d in instances] == [
    "2026-01-01",
    "2026-02-01",
    "2026-03-01",
  ]


def test_day_31_skips_short_months():
  rule = _rule(day=31)
  instances = _instances_for(rule, date(2026, 1, 1), date(2026, 4, 30))
  assert [d.isoformat() for d in instances] == ["2026-01-31", "2026-03-31"]


def test_yearly_expansion_uses_month_and_day():
  # explicit start: the _rule default (2026-01-01) would exclude 2025
  rule = _rule(frequency="yearly", month=6, day=15, start="2025-01-01")
  instances = _instances_for(rule, date(2025, 1, 1), date(2027, 12, 31))
  assert [d.isoformat() for d in instances] == ["2025-06-15", "2026-06-15", "2027-06-15"]


def test_yearly_instance_before_the_rules_start_is_excluded():
  # the default _rule start (2026-01-01) excludes the 2025 instance
  rule = _rule(frequency="yearly", month=6, day=15)
  instances = _instances_for(rule, date(2025, 1, 1), date(2027, 12, 31))
  assert [d.isoformat() for d in instances] == ["2026-06-15", "2027-06-15"]


def test_start_and_end_bound_the_instances():
  rule = _rule(day=10, start="2026-02-01", end="2026-03-31")
  instances = _instances_for(rule, date(2026, 1, 1), date(2026, 12, 31))
  # The rule's own start/end bound the instances: January's 10th is
  # before start (excluded), April's after end (excluded).
  assert [d.isoformat() for d in instances] == ["2026-02-10", "2026-03-10"]


def test_paused_rule_expands_nothing():
  rules = [_rule(active=False)]
  assert expand_recurrences(rules, [], date(2026, 1, 1), date(2026, 2, 28)) == []


def test_expansion_reaches_back_to_window_start():
  """The view's backward window: instances between the ledger's anchor
  and today (window_start in the past) expand too — a landed-but-
  unconfirmed rule instance is visible until a real entry supersedes it."""
  rules = [_rule(day=15, start="2026-01-01")]
  virtual = expand_recurrences(rules, [], date(2026, 9, 1), date(2026, 10, 31))
  assert [r.date for r in virtual] == ["2026-09-15", "2026-10-15"]


def test_expansion_produces_virtual_expected_rows():
  rules = [_rule()]
  virtual = expand_recurrences(rules, [], date(2026, 5, 1), date(2026, 5, 31))
  assert len(virtual) == 1
  row = virtual[0]
  assert row.id.startswith("r:")
  assert row.status == "expected"
  assert row.note == "recurrence rule instance"
  assert row.amount_eur == -800.0


# --- superseding ---------------------------------------------------------


def test_superseded_by_matching_real_entry():
  rules = [_rule(day=1)]
  # covering entry: same category, same amount, within ±5 days
  txs = [_tx("2026-05-03")]
  assert expand_recurrences(rules, txs, date(2026, 5, 1), date(2026, 5, 31)) == []


def test_not_superseded_by_wrong_category():
  rules = [_rule()]
  txs = [_tx("2026-05-01", category="Eten")]
  assert len(expand_recurrences(rules, txs, date(2026, 5, 1), date(2026, 5, 31))) == 1


def test_not_superseded_by_wrong_amount():
  rules = [_rule()]
  txs = [_tx("2026-05-01", amount=-750.0)]  # beyond the 0.01 tolerance
  assert len(expand_recurrences(rules, txs, date(2026, 5, 1), date(2026, 5, 31))) == 1


def test_not_superseded_outside_the_day_window():
  rules = [_rule()]
  txs = [_tx("2026-05-10")]  # rule day 1: 9 days away > DAY_WINDOW
  assert len(expand_recurrences(rules, txs, date(2026, 5, 1), date(2026, 5, 31))) == 1


def test_superseding_scoped_to_the_occurrence_year():
  # A same-category/same-amount entry from a DIFFERENT year must not
  # cover this year's instance.
  rules = [_rule()]
  txs = [_tx("2025-05-01")]
  assert len(expand_recurrences(rules, txs, date(2026, 5, 1), date(2026, 5, 31))) == 1


# --- storage -------------------------------------------------------------


def test_roundtrip_save_and_load(tmp_path, monkeypatch):
  path = tmp_path / "recurrences.json"
  monkeypatch.setattr("financials.recurrences.recurrences_file", lambda: path)
  assert load_recurrences() == []  # missing store == empty (no file yet)
  save_recurrences([_rule(), _rule(description="Verzekering", day=15)])
  loaded = load_recurrences()
  assert len(loaded) == 2
  assert loaded[0].description == "Huur"
  assert loaded[1].active is True


def test_roundtrip_survives_paused_and_yearly(tmp_path, monkeypatch):
  path = tmp_path / "recurrences.json"
  monkeypatch.setattr("financials.recurrences.recurrences_file", lambda: path)
  save_recurrences([_rule(active=False), _rule(frequency="yearly", month=12, day=25)])
  loaded = load_recurrences()
  assert loaded[0].active is False
  assert loaded[1].frequency == "yearly"
  assert loaded[1].month == 12


# --- instance ids & exceptions -------------------------------------------


def test_instance_ids_are_deterministic_content_hashes():
  rule = _rule(day=1)
  occurrence = date(2026, 5, 1)
  id1 = instance_id(rule, occurrence)
  id2 = instance_id(rule, occurrence)
  assert id1 == id2
  # content hash: 16 hex chars with the r: provenance prefix
  assert id1.startswith("r:") and len(id1) == 18
  hex_part = id1[2:]
  assert all(c in "0123456789abcdef" for c in hex_part)
  # content-derived: changing the rule changes future instances' ids
  assert instance_id(_rule(day=2), occurrence) != id1


def test_expansion_skips_exception_dates():
  rule = _rule(day=1)
  rule.exceptions = ["2026-05-01"]
  rows = expand_recurrences([rule], [], date(2026, 5, 1), date(2026, 5, 30))
  assert rows == []


def test_expansion_exceptions_only_affect_their_dates():
  rule = _rule(day=1)
  rule.exceptions = ["2026-05-01"]
  rows = expand_recurrences([rule], [], date(2026, 5, 1), date(2026, 7, 31))
  assert [r.date for r in rows] == ["2026-06-01", "2026-07-01"]


# --- weekly / biweekly -----------------------------------------------------


def test_weekly_instances_anchor_on_start():
  rule = _rule(frequency="weekly")
  rule.start = "2026-10-01"  # a Thursday
  rows = expand_recurrences([rule], [], date(2026, 10, 1), date(2026, 10, 31))
  # anchors: Oct 1 + k*7 -> 01, 08, 15, 22, 29
  assert [r.date for r in rows] == [
    "2026-10-01",
    "2026-10-08",
    "2026-10-15",
    "2026-10-22",
    "2026-10-29",
  ]


def test_biweekly_instances_anchor_on_start():
  rule = _rule(frequency="biweekly")
  rule.start = "2026-10-01"
  rows = expand_recurrences([rule], [], date(2026, 10, 1), date(2026, 11, 30))
  assert [r.date for r in rows] == [
    "2026-10-01",
    "2026-10-15",
    "2026-10-29",
    "2026-11-12",
    "2026-11-26",
  ]


def test_weekly_window_bounds_respect_start():
  rule = _rule(frequency="weekly")
  rule.start = "2026-10-01"
  # window starts AFTER the anchor: first multiple >= window_start
  rows = expand_recurrences([rule], [], date(2026, 10, 5), date(2026, 10, 20))
  assert [r.date for r in rows] == ["2026-10-08", "2026-10-15"]


def test_weekly_without_start_expands_to_nothing():
  rule = _rule(frequency="weekly")
  rule.start = ""
  rows = expand_recurrences([rule], [], date(2026, 10, 1), date(2026, 10, 31))
  assert rows == []
  # and the ids stay content-hashes as with the other frequencies
  rule.start = "2026-10-01"
  rows = expand_recurrences([rule], [], date(2026, 10, 1), date(2026, 10, 8))
  assert all(r.id.startswith("r:") and len(r.id) == 18 for r in rows)


def test_snap_to_weekday_moves_forward_to_named_day():
  # 2026-09-21 is a Monday; 'vr' (Friday) -> 2026-09-25
  assert recurrence_cli._snap_to_weekday("2026-09-21", "vr") == "2026-09-25"
  # same day when the anchor IS the requested weekday
  assert recurrence_cli._snap_to_weekday("2026-09-25", "vr") == "2026-09-25"
  # ISO date passes through unchanged (its own weekday is the anchor)
  assert recurrence_cli._snap_to_weekday("2026-10-01", "") == "2026-10-01"
  # empty anchor snaps from today
  assert recurrence_cli._snap_to_weekday("", "zo") is not None
  # unknown alias: None (caller keeps the base anchor)
  assert recurrence_cli._snap_to_weekday("2026-09-21", "x") is None


def test_add_weekly_with_weekday_flag(monkeypatch, capsys):
  # 2026-09-21 (pinned TODAY) is a Monday; --weekday vr -> start 2026-09-25
  assert (
    recurrence_cli.add_recurrence(
      description="Boodschappen",
      category="Eten",
      amount=-45.0,
      frequency="weekly",
      start="2026-09-21",
      weekday="vr",
    )
    == 0
  )
  rules = load_recurrences()
  assert len(rules) == 1
  assert rules[0].frequency == "weekly"
  assert rules[0].start == "2026-09-25"
  upcoming = ", ".join(recurrence_cli._next_instances(rules[0], count=3))
  # first instance IS the anchor (2026-09-25), then every 7 days
  assert upcoming == "2026-09-25, 2026-10-02, 2026-10-09"
  out = capsys.readouterr().out
  assert "wekelijks" in out


def test_add_weekly_rejected_without_start():
  assert (
    recurrence_cli.add_recurrence(
      description="Boodschappen",
      category="Eten",
      amount=-45.0,
      frequency="biweekly",
      start="",
    )
    == 2
  )
  assert load_recurrences() == []


def test_add_biweekly_prompted_path_gets_start_from_today(monkeypatch, capsys):
  # prompted path (description/category/amount given, frequency prompted):
  # no day-of-month question for biweekly; start defaults to pinned TODAY
  # (anchor/start/end answered via Enter)
  _answers(monkeypatch, ["biweekly", "", "", ""])
  assert (
    recurrence_cli.add_recurrence(description="Stripboeken", category="Eten", amount=-12.0) == 0
  )
  rules = load_recurrences()
  assert len(rules) == 1
  assert rules[0].frequency == "biweekly"
  assert rules[0].start == "2026-09-21"  # pinned TODAY = the anchor
  out = capsys.readouterr().out
  assert "Dag van de maand" not in out
  assert "tweewekelijks (ma)" in out


def test_seeded_loop_biweekly_asks_anchor_not_day_of_month(monkeypatch):
  # the SEEDED loop is where the anchor question replaces day-of-month
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  _answers(monkeypatch, ["", "", "biweekly", "vr", "", ""])
  assert recurrence_cli.edit_recurrence("hu") == 0
  rules = load_recurrences()
  # behavioral proof: the anchor answer ("vr") snapped start 2026-09-21 →
  # 2026-09-25; had day-of-month been asked instead, the answer sequence
  # would not have aligned and the snap could not have happened.
  assert rules[0].frequency == "biweekly"
  assert rules[0].start == "2026-09-25"


# --- detector weekly / biweekly -------------------------------------------


def test_detect_weekly_from_history():
  # Fridays: 2026-09-04, 11, 18, 25 — gaps of exactly 7
  txs = [_tx(d) for d in ("2026-09-04", "2026-09-11", "2026-09-18", "2026-09-25")]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  proposal = proposals[0]
  assert proposal["frequency"] == "weekly"
  assert proposal["start"] == "2026-09-04"  # first occurrence = phase anchor
  assert proposal["occurrences"] == 4


def test_detect_biweekly_from_history():
  # gaps of exactly 14
  txs = [_tx(d) for d in ("2026-09-01", "2026-09-15", "2026-09-29", "2026-10-13")]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  assert proposals[0]["frequency"] == "biweekly"
  assert proposals[0]["start"] == "2026-09-01"


def test_detect_weekly_survives_one_skipped_week():
  # gaps 7,7,14,7: one skipped week still leaves 3 weekly gaps —
  # weekly wins (3 >= 2); biweekly only sees 1 gap in its band
  txs = [_tx(d) for d in ("2026-09-04", "2026-09-11", "2026-09-18", "2026-10-02", "2026-10-09")]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  assert proposals[0]["frequency"] == "weekly"


def test_detect_weekly_beats_monthly_by_count_not_order():
  # 3 weekly gaps AND 2 monthly gaps in one history: weekly has more
  # gaps in its band and is checked first — proposal is weekly
  txs = [_tx(d) for d in ("2026-09-04", "2026-09-11", "2026-09-18", "2026-10-09", "2026-11-09")]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  assert proposals[0]["frequency"] == "weekly"


# --- Tier B: timely but varying amounts (owner's expected→confirm flow) ----


def test_detect_tier_b_varying_amounts_proposes_median():
  # every Friday, amounts vary around -45: Tier A drops this group
  # (±5% band), Tier B classifies the dates and proposes the median
  txs = [
    _tx("2026-09-04", amount=-45.0),
    _tx("2026-09-11", amount=-38.0),
    _tx("2026-09-18", amount=-52.0),
    _tx("2026-09-25", amount=-41.0),
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  proposal = proposals[0]
  assert proposal["frequency"] == "weekly"
  # median = upper-middle element for even counts (existing convention)
  assert proposal["amount"] == -41.0
  assert proposal["amount_range"] == [-52.0, -38.0]
  assert proposal["start"] == "2026-09-04"
  assert proposal["occurrences"] == 4


def test_detect_tier_b_monthly_pattern():
  txs = [
    _tx("2026-07-01", amount=-900.0),
    _tx("2026-08-01", amount=-830.0),
    _tx("2026-09-01", amount=-940.0),
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  assert proposals[0]["frequency"] == "monthly"
  assert proposals[0]["amount"] == -900.0  # median, not the mean


def test_detect_tier_b_allows_wild_amount_spread():
  # owner's budget flow: fixed budget confirmed with actual spend —
  # magnitudes far beyond 2x; the spread is surfaced in amount_range
  txs = [
    _tx("2026-06-26", amount=-138.53),
    _tx("2026-07-10", amount=-79.37),
    _tx("2026-07-17", amount=-314.96),
    _tx("2026-07-24", amount=-56.68),
    _tx("2026-08-08", amount=-73.23),
    _tx("2026-08-14", amount=-96.48),
    _tx("2026-08-19", amount=-96.72),
    _tx("2026-09-04", amount=-95.47),
    _tx("2026-09-07", amount=-122.53),
    _tx("2026-09-16", amount=-40.37),
    _tx("2026-09-19", amount=-103.52),
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  proposal = proposals[0]
  assert proposal["frequency"] == "weekly"  # catch-up pairs outvote biweekly
  assert proposal["amount"] == -96.48  # median of the 11 magnitudes
  assert proposal["amount_range"] == [-314.96, -40.37]
  assert proposal["start"] == "2026-06-26"
  assert proposal["occurrences"] == 11


def test_detect_tier_b_rejects_mixed_signs():
  txs = [
    _tx("2026-09-04", amount=-45.0),
    _tx("2026-09-11", amount=-40.0),
    _tx("2026-09-18", amount=45.0),  # a refund, not an occurrence
    _tx("2026-09-25", amount=-42.0),
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert proposals == []


def test_detect_tier_a_still_preferred_over_tier_b():
  # mixed: 3 tight rows (Tier A signal) + 1 outlier row; Tier A must
  # win with the tight dates, NOT Tier B over all 4 (the outlier's date
  # gap would blur the pattern)
  txs = [
    _tx("2026-09-04", amount=-45.0),
    _tx("2026-09-11", amount=-45.5),  # within 5% of median
    _tx("2026-09-18", amount=-44.0),
    _tx("2026-09-25", amount=-600.0),  # outlier amount
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  proposal = proposals[0]
  assert proposal["occurrences"] == 3  # the inliers, not all 4 rows
  assert proposal["amount"] == -45.0
  assert "amount_range" not in proposal  # Tier A: constant-amount proposal


def test_detect_skips_groups_already_covered_by_rules():
  txs = [
    _tx("2026-09-04", amount=-45.0),
    _tx("2026-09-11", amount=-45.0),
    _tx("2026-09-18", amount=-45.0),
  ]
  rules = [_rule(description="Huur", category="Wonen")]
  proposals = detect_candidates(txs, min_occurrences=3, existing_rules=rules)
  assert proposals == []  # same description+category: already formalized


def test_detect_tier_b_when_tier_a_dates_dont_classify():
  # the owner-report shape: a few confirmed-at-expected rows + varying
  # actuals. The tight rows' DATES are irregular (no band holds enough
  # gaps), so Tier A's inlier dates fail to classify — the fix falls
  # through to Tier B over ALL rows (old code skipped Tier B entirely).
  txs = [
    _tx("2026-08-07", amount=-45.0),
    _tx("2026-08-14", amount=-45.2),
    _tx("2026-09-04", amount=-45.0),
    _tx("2026-09-11", amount=-38.0),
    _tx("2026-09-18", amount=-52.0),
    _tx("2026-09-25", amount=-41.0),
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  proposal = proposals[0]
  assert proposal["frequency"] == "weekly"
  assert proposal["occurrences"] == 6  # ALL rows, not Tier A's 3
  assert "amount_range" in proposal  # Tier B proposal


# --- detector ------------------------------------------------------------


def test_detect_monthly_from_history():
  txs = [_tx(f"2026-0{m}-05") for m in (1, 2, 3, 4)]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  proposal = proposals[0]
  assert proposal["description"] == "Huur"
  assert proposal["frequency"] == "monthly"
  assert proposal["amount"] == -800.0
  assert proposal["day"] == 5


def test_detect_ignores_amount_outliers():
  txs = [_tx("2026-01-05"), _tx("2026-02-05"), _tx("2026-03-05", amount=-795.0)]
  # -795 is within 5% of the median (-800): 3 inliers, still a proposal.
  proposals = detect_candidates(txs, min_occurrences=3)
  assert proposals and proposals[0]["amount"] == -800.0


def test_detect_rejects_irregular_spacing():
  txs = [_tx("2026-01-05"), _tx("2026-02-05"), _tx("2026-04-20")]  # 74-day gap
  assert detect_candidates(txs, min_occurrences=3) == []


def test_detect_yearly_spacing():
  txs = [
    _tx("2024-12-24", description="Kerstmis", category="Cadeaus", amount=-100.0),
    _tx("2025-12-24", description="Kerstmis", category="Cadeaus", amount=-100.0),
    _tx("2026-12-24", description="Kerstmis", category="Cadeaus", amount=-100.0),
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  assert proposals[0]["frequency"] == "yearly"
  assert proposals[0]["month"] == 12


def test_detect_requires_min_occurrences():
  txs = [_tx("2026-01-05"), _tx("2026-02-05")]
  assert detect_candidates(txs, min_occurrences=3) == []


# --- CLI (recurrence_cli) ------------------------------------------------


from financials import recurrence_cli  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_rules(tmp_path, monkeypatch):
  """Point the rules store at tmp, pin 'today' for deterministic
  upcoming-instance projections, and pin a KNOWN category set: the live
  config's categories are the OWNER's personal set (~/.financials.toml) —
  a hermetic suite must not depend on them. Category PROMPTING is also
  neutralized (questionary reads stdin, which pytest capture forbids):
  tests that exercise category interaction override ask_category."""
  monkeypatch.setattr(
    "financials.recurrences.recurrences_file", lambda: tmp_path / "recurrences.json"
  )
  monkeypatch.setattr(recurrence_cli, "TODAY", lambda: date(2026, 9, 21))
  # Pin a KNOWN category set at the config source: the live config's
  # categories are the OWNER's personal set (~/.financials.toml) — a
  # hermetic suite must not depend on them. All use sites resolve the set
  # at CALL time via config.approved_categories() → get_config(), so
  # patching the cached-config accessor covers every module at once (no
  # per-module late-binding traps). Category prompting is neutralized too
  # (questionary reads stdin, which pytest capture forbids).
  monkeypatch.setattr(
    "financials.config.get_config",
    lambda: FinancialsConfig(categories=["Wonen", "Eten", "Inkomsten"], data_dir=tmp_path),
  )
  monkeypatch.setattr(recurrence_cli, "ask_category", lambda current="": "Wonen")


def test_add_and_list_roundtrip(capsys):
  assert (
    recurrence_cli.add_recurrence(
      description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
    )
    == 0
  )
  assert recurrence_cli.list_recurrences() == 0
  out = capsys.readouterr().out
  assert "Huur" in out
  assert "monthly (dag 1)" in out
  assert "actief" in out
  # next 3 instances from the pinned today (2026-09-21)
  assert "2026-10-01" in out
  assert "2026-11-01" in out


def test_add_defaults_day_and_start_from_pinned_today(monkeypatch):
  # frequency omitted -> the interactive path fills it; Enter = monthly;
  # day/start/end answered via Enter -> defaults from pinned today
  _answers(monkeypatch, ["monthly", "", "", ""])
  recurrence_cli.add_recurrence(description="Internet", category="Wonen", amount=-55.0)
  rules = load_recurrences()
  assert len(rules) == 1
  assert rules[0].frequency == "monthly"
  assert rules[0].day == 21  # pinned today's day-of-month
  assert rules[0].start == "2026-09-21"


def test_add_rejects_unapproved_category():
  assert (
    recurrence_cli.add_recurrence(
      description="X", category="NietBestaand", amount=-10.0, frequency="monthly"
    )
    == 2
  )
  assert load_recurrences() == []


def test_add_rejects_zero_amount():
  assert (
    recurrence_cli.add_recurrence(
      description="X", category="Wonen", amount=0.0, frequency="monthly"
    )
    == 2
  )
  assert load_recurrences() == []


def test_add_yearly_requires_month():
  assert (
    recurrence_cli.add_recurrence(
      description="X", category="Wonen", amount=-10.0, frequency="yearly", day=15
    )
    == 2
  )
  assert load_recurrences() == []


def test_add_yearly_prompts_for_day_and_month(monkeypatch):
  # the reported gap: interactive add with 'yearly' silently took today's
  # day-of-month and then hard-failed on the month; add now asks both
  # (seeded-loop parity). Order matches the seeded loop: freq -> day ->
  # month -> start -> end (Enter keeps the pinned-today defaults).
  _answers(monkeypatch, ["yearly", "15", "12", "", ""])
  assert (
    recurrence_cli.add_recurrence(description="Woningpolis", category="Wonen", amount=-893.93) == 0
  )
  rules = load_recurrences()
  assert rules[0].frequency == "yearly"
  assert rules[0].month == 12
  assert rules[0].day == 15  # the typed day, not today's (21)
  assert rules[0].start == "2026-09-21"  # Enter: pinned today's default
  assert recurrence_cli._next_instances(rules[0], count=1) == ["2026-12-15"]


def test_add_yearly_default_day_and_month_are_pinned_today(monkeypatch):
  # Enter everywhere: defaults = pinned TODAY's day (21) and month (9)
  _answers(monkeypatch, ["yearly", "", "", "", ""])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 0
  rules = load_recurrences()
  assert rules[0].day == 21
  assert rules[0].month == 9
  assert rules[0].end == ""


def test_add_yearly_day_prompt_q_aborts(monkeypatch):
  _answers(monkeypatch, ["yearly", "q"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []


def test_add_yearly_rejects_out_of_range_day_answer(monkeypatch, capsys):
  _answers(monkeypatch, ["yearly", "35"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []
  assert "Ongeldige dag" in capsys.readouterr().out


def test_add_yearly_month_prompt_q_aborts(monkeypatch):
  # day answered via Enter (default 21), then q at the month question
  _answers(monkeypatch, ["yearly", "", "q"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []


def test_add_yearly_rejects_out_of_range_month_answer(monkeypatch, capsys):
  _answers(monkeypatch, ["yearly", "", "13"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []
  assert "Ongeldige maand" in capsys.readouterr().out


def test_add_monthly_prompts_for_day(monkeypatch):
  # monthly gets the same day question (seeded-loop parity)
  _answers(monkeypatch, ["monthly", "5", "", ""])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 0
  rules = load_recurrences()
  assert rules[0].frequency == "monthly"
  assert rules[0].day == 5
  assert recurrence_cli._next_instances(rules[0], count=1) == ["2026-10-05"]


def test_add_monthly_day_prompt_q_aborts(monkeypatch):
  _answers(monkeypatch, ["monthly", "q"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []


def test_add_monthly_rejects_out_of_range_day_answer(monkeypatch, capsys):
  _answers(monkeypatch, ["monthly", "0"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []
  assert "Ongeldige dag" in capsys.readouterr().out


def test_add_monthly_typed_start_and_end(monkeypatch):
  # start/end parity: a typed ISO start replaces the today-default and an
  # ISO end is recorded; the first instance lands on the start itself.
  _answers(monkeypatch, ["monthly", "1", "2026-10-01", "2027-09-30"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 0
  rules = load_recurrences()
  assert rules[0].start == "2026-10-01"
  assert rules[0].end == "2027-09-30"
  assert recurrence_cli._next_instances(rules[0], count=1) == ["2026-10-01"]


def test_add_monthly_start_prompt_q_aborts(monkeypatch):
  _answers(monkeypatch, ["monthly", "1", "q"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []


def test_add_monthly_end_prompt_q_aborts(monkeypatch):
  _answers(monkeypatch, ["monthly", "1", "", "q"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []


def test_add_monthly_invalid_start_answer_rejected_by_validation(monkeypatch, capsys):
  # a non-ISO start passes the prompt (no parse there, like the seeded
  # loop) and is caught by _validate before saving
  _answers(monkeypatch, ["monthly", "1", "nonsense"])
  assert recurrence_cli.add_recurrence(description="X", category="Wonen", amount=-10.0) == 2
  assert load_recurrences() == []
  assert "ongeldige start-datum" in capsys.readouterr().out


def test_add_weekly_prompted_path_asks_weekday_anchor(monkeypatch):
  # the sibling gap: interactive add with weekly used to silently anchor
  # on today's weekday; the anchor question now snaps the start forward.
  # "vr" on 2026-09-21 (pinned TODAY, a Monday) → start 2026-09-25.
  _answers(monkeypatch, ["weekly", "vr", "", ""])
  assert (
    recurrence_cli.add_recurrence(description="Boodschappen", category="Eten", amount=-45.0) == 0
  )
  rules = load_recurrences()
  assert rules[0].frequency == "weekly"
  assert rules[0].start == "2026-09-25"


def test_add_weekly_typed_start_is_snapped_to_anchor(monkeypatch):
  # typed start 2026-09-22 (a Tuesday) + anchor "vr" -> snapped to Friday
  _answers(monkeypatch, ["weekly", "vr", "2026-09-22", ""])
  assert recurrence_cli.add_recurrence(description="X", category="Eten", amount=-45.0) == 0
  assert load_recurrences()[0].start == "2026-09-25"


def test_add_end_before_start_rejected():
  assert (
    recurrence_cli.add_recurrence(
      description="X",
      category="Wonen",
      amount=-10.0,
      frequency="monthly",
      start="2026-09-01",
      end="2026-08-01",
    )
    == 2
  )
  assert load_recurrences() == []


def test_pause_and_resume_by_prefix():
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  assert recurrence_cli.pause_recurrence("hu") == 0
  assert load_recurrences()[0].active is False
  assert recurrence_cli.resume_recurrence("hu") == 0
  assert load_recurrences()[0].active is True


def test_pause_unknown_and_ambiguous_prefix(capsys, monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  recurrence_cli.add_recurrence(
    description="Huisverzekering", category="Wonen", amount=-30.0, frequency="monthly"
  )
  monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "")
  # ambiguous: two candidates -> pick list; empty answer declines
  assert recurrence_cli.pause_recurrence("hu") == 2
  assert "niet eenduidig" in capsys.readouterr().out
  assert recurrence_cli.pause_recurrence("bestaatniet") == 2
  # exact prefix resolves unambiguously
  assert recurrence_cli.pause_recurrence("huisverz") == 0
  assert load_recurrences()[0].active is True


def test_pause_already_paused_is_noop_with_notice(capsys):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  assert recurrence_cli.pause_recurrence("hu") == 0
  assert recurrence_cli.pause_recurrence("hu") == 0
  assert "al gepauzeerd" in capsys.readouterr().out


def test_remove_needs_confirmation(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "nee")
  assert recurrence_cli.remove_recurrence("hu") == 0
  assert len(load_recurrences()) == 1  # declined: kept
  monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "ja")
  assert recurrence_cli.remove_recurrence("hu") == 0
  assert load_recurrences() == []


def test_resolve_ambiguous_prefix_can_pick_from_list(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  recurrence_cli.add_recurrence(
    description="Huisverzekering", category="Wonen", amount=-30.0, frequency="monthly"
  )
  monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: "2")
  assert recurrence_cli.pause_recurrence("hu") == 0
  rules = load_recurrences()
  assert [r.active for r in rules] == [True, False]  # Huisverzekering paused


def test_list_empty_shows_hint(capsys):
  assert recurrence_cli.list_recurrences() == 0
  out = capsys.readouterr().out
  assert "Geen terugkerende regels" in out
  assert "recurrence add" in out


def test_list_marks_paused_rule(capsys):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  recurrence_cli.pause_recurrence("hu")
  capsys.readouterr()  # clear
  assert recurrence_cli.list_recurrences() == 0
  assert "gepauzeerd" in capsys.readouterr().out


def test_paused_rule_shows_no_upcoming_instances(capsys):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly"
  )
  recurrence_cli.pause_recurrence("hu")
  capsys.readouterr()
  recurrence_cli.list_recurrences()
  out = capsys.readouterr().out
  # the upcoming column shows the no-instance dash
  assert "—" in out


def test_detect_lists_proposals_but_creates_nothing(capsys):
  # The detector runs on the real committed ledger; nothing to assert on
  # its content, but it must NOT create any rule.
  assert recurrence_cli.detect_recurrences(min_occurrences=3) == 0
  assert load_recurrences() == []
  out = capsys.readouterr().out
  assert "Niets is aangemaakt" in out or "Geen kandidaat" in out


# --- edit (rule / occurrence) --------------------------------------------


def _answers(monkeypatch, answers: list[str]):
  """Prompt.ask stub returning the given answers in order (repeats the
  last one when exhausted). An empty-string answer means 'Enter' — which
  for rich means the PROMPT'S DEFAULT, so the stub returns kwargs'
  default in that case (mimicking real Prompt.ask behavior)."""
  index = {"i": 0}

  def ask(*args, **kwargs):
    answer = answers[min(index["i"], len(answers) - 1)]
    index["i"] += 1
    if answer == "":
      return kwargs.get("default", "")
    return answer

  monkeypatch.setattr("rich.prompt.Prompt.ask", ask)


def test_edit_rule_keeps_values_on_enter(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  _answers(monkeypatch, ["", "", "", "", "", "", ""])  # Enter everywhere
  assert recurrence_cli.edit_recurrence("hu") == 0
  rules = load_recurrences()
  assert len(rules) == 1
  assert rules[0].description == "Huur"
  assert rules[0].amount == -800.0


def test_edit_rule_changes_typed_values(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  # description kept, amount changed (-850), rest kept — the loop asks
  # description, amount, frequency, day, start, end (category via the
  # patched ask_category, no slot)
  _answers(monkeypatch, ["", "-850", "", "", "", ""])
  assert recurrence_cli.edit_recurrence("hu") == 0
  rules = load_recurrences()
  assert rules[0].amount == -850.0
  assert rules[0].description == "Huur"
  assert rules[0].day == 1


def test_edit_rule_q_aborts_without_change(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  _answers(monkeypatch, ["q"])
  assert recurrence_cli.edit_recurrence("hu") == 2
  assert load_recurrences()[0].amount == -800.0


def test_edit_occurrence_option_2_creates_expected_exception(monkeypatch, tmp_path, capsys):
  from financials.expected import load_expected
  from financials.recurrences import instance_id

  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  occurrence = date(2026, 10, 1)
  instance_hash = instance_id(rule, occurrence)

  # choice 2, then the slim occurrence loop (datum FIRST): datum Enter
  # (keeps 2026-10-01), omschrijving Enter, amount -700, rest none —
  # category via the patched ask_category, no slot; frequency/day/month/
  # end are never asked.
  _answers(monkeypatch, ["2", "", "", "-700"])
  assert recurrence_cli.edit_occurrence(instance_hash) == 0
  # expected entry created from the edited values
  expected_rows = load_expected()
  assert len(expected_rows) == 1
  assert expected_rows[0].date == "2026-10-01"
  assert expected_rows[0].amount_eur == -700.0
  # the instance is exempted from further expansion
  assert load_recurrences()[0].exceptions == ["2026-10-01"]
  out = capsys.readouterr().out
  assert "uitzondering" in out


def test_edit_occurrence_option_2_moves_the_datum(monkeypatch, capsys):
  """Datum is the FIRST question of the slim occurrence loop (moving it
  is the usual reason to edit a single instance) and typing a new date
  moves the expected row; the ORIGINAL instance date joins the rule's
  exceptions, so the projection shows only the replacement."""
  from financials.expected import load_expected
  from financials.recurrences import instance_id

  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))

  # choice 2, then the slim loop: datum 2026-10-05, omschrijving Enter
  # (category via the patched ask_category, no slot; bedrag keeps via
  # repeated last answer "")
  _answers(monkeypatch, ["2", "2026-10-05", ""])
  assert recurrence_cli.edit_occurrence(instance_hash) == 0
  expected_rows = load_expected()
  assert len(expected_rows) == 1
  assert expected_rows[0].date == "2026-10-05"  # moved, not the rule's day 1
  assert expected_rows[0].amount_eur == -800.0
  assert load_recurrences()[0].exceptions == ["2026-10-01"]
  out = capsys.readouterr().out
  assert "uitzondering" in out


def test_edit_occurrence_option_2_drops_dead_questions(monkeypatch, capsys):
  """Frequentie, dag and end are never asked (they are discarded for a
  one-off exception): the walk ends at Bedrag — exactly 4 Prompt.ask
  calls (menu, datum, omschrijving, bedrag; category is the no-slot
  stub). Proven by counting, not by answer slots."""
  from financials.expected import load_expected
  from financials.recurrences import instance_id

  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  calls = {"n": 0}

  def counting_ask(*args, **kwargs):
    calls["n"] += 1
    if calls["n"] == 1:
      return "2"  # the occurrence-edit menu
    return kwargs.get("default", "")  # Enter everywhere → all values kept

  monkeypatch.setattr("rich.prompt.Prompt.ask", counting_ask)
  assert recurrence_cli.edit_occurrence(instance_hash) == 0
  assert calls["n"] == 4
  assert load_expected()[0].date == "2026-10-01"
  assert load_expected()[0].amount_eur == -800.0
  assert load_recurrences()[0].exceptions == ["2026-10-01"]


def test_edit_occurrence_unknown_id_is_a_noop_2(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  _answers(monkeypatch, [""])
  assert recurrence_cli.edit_occurrence("r:0000000000000000") == 2
  assert len(load_recurrences()) == 1
  assert load_recurrences()[0].exceptions == []


def test_edit_occurrence_choice_1_edits_the_rule(monkeypatch):
  from financials.recurrences import instance_id

  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  # choice 1 -> the rule's seeded loop: change amount to -900, rest Enter
  _answers(monkeypatch, ["1", "", "-900", "", "", "", "", ""])
  assert recurrence_cli.edit_occurrence(instance_hash) == 0
  rules = load_recurrences()
  assert len(rules) == 1
  assert rules[0].amount == -900.0
  assert rules[0].exceptions == []  # no exception recorded on the rule path


# --- delete-wired suppression (delete <r-id> -> exceptions) ---------------


def test_suppress_occurrence_appends_exception(monkeypatch, capsys):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["ja"])  # delete's confirmation
  assert recurrence_cli.suppress_occurrence(instance_hash) == 0
  rules = load_recurrences()
  assert len(rules) == 1  # the rule survives
  assert rules[0].exceptions == ["2026-10-01"]
  out = capsys.readouterr().out
  assert "onderdrukt" in out
  # behavioral proof: the suppressed instance no longer projects; November does
  assert recurrence_cli._next_instances(rules[0], count=2) == ["2026-11-01", "2026-12-01"]


def test_suppress_occurrence_is_idempotent(monkeypatch, capsys):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["ja"])
  assert recurrence_cli.suppress_occurrence(instance_hash) == 0
  # second time: no confirmation asked (idempotent no-op with notice)
  _answers(monkeypatch, ["ja"])
  assert recurrence_cli.suppress_occurrence(instance_hash) == 0
  assert load_recurrences()[0].exceptions == ["2026-10-01"]
  assert "al onderdrukt" in capsys.readouterr().out


def test_suppress_occurrence_declined_changes_nothing(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["nee"])
  assert recurrence_cli.suppress_occurrence(instance_hash) == 2
  assert load_recurrences()[0].exceptions == []


def test_suppress_unknown_id_is_a_noop_2(monkeypatch):
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  assert recurrence_cli.suppress_occurrence("r:0000000000000000") == 2
  assert len(load_recurrences()) == 1
  assert load_recurrences()[0].exceptions == []


def test_suppress_suppressed_date_still_resolves(monkeypatch):
  # _find_instance must not skip suppressed instances: expansion filters
  # the DATE, the date itself still exists, so a re-delete stays possible
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["ja"])
  assert recurrence_cli.suppress_occurrence(instance_hash) == 0
  _answers(monkeypatch, ["ja"])
  assert recurrence_cli.suppress_occurrence(instance_hash) == 0  # resolves + no-op


def test_find_instance_resolves_before_an_out_of_order_anchor(monkeypatch, tmp_path):
  """Regression (owner report, 2026-09-26): the resolver window matches
  the view's open section — [anchor − 35d, +400d] — so an instance BEFORE
  the out-of-order-committed anchor (later-dated row first) stays
  addressable for edit/delete."""
  from financials.commands import cmd_add
  from financials.journal import Journal

  recurrence_cli.add_recurrence(
    description="Huur",
    category="Wonen",
    amount=-800.0,
    frequency="monthly",
    day=1,
    start="2026-08-01",  # explicit: the default start is the pinned TODAY
  )
  rule = load_recurrences()[0]
  # The anchor: a LATER-dated committed row (pinned TODAY is 2026-09-21).
  journal = Journal(tmp_path / "journal.jsonl")
  monkeypatch.setattr("financials.fixes.journal_file", lambda: journal.path)
  monkeypatch.setattr("financials.fixes.migration_journal_file", lambda: tmp_path / "mig.jsonl")
  cmd_add("2026-09-20", "Later", "Wonen", -10.0, journal=journal)
  # The September 1st instance predates the anchor: still resolvable.
  instance_hash = instance_id(rule, date(2026, 9, 1))
  assert recurrence_cli._find_instance(instance_hash) is not None


def test_delete_r_id_suppresses_instance(monkeypatch, tmp_path, capsys):
  # end-to-end through the delete entry point: committed miss → expected
  # miss → rule-instance hit → suppression
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["ja"])
  import financials.delete as delete_module

  # No patching needed: the autouse fixture pins get_config ->
  # data_dir=tmp_path, routing the journal, expected store and rules
  # store to isolated tmp paths (absent files → empty ledger/expected).
  assert delete_module.delete_transaction(instance_hash) == 0
  rules = load_recurrences()
  assert rules[0].exceptions == ["2026-10-01"]
  assert "onderdrukt" in capsys.readouterr().out


def test_delete_unknown_r_id_falls_through_to_error(monkeypatch, tmp_path):
  # committed miss → expected miss → rule-instance miss → unknown id
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  _answers(monkeypatch, [""])
  import financials.delete as delete_module

  assert delete_module.delete_transaction("r:0000000000000000") == 2
  assert load_recurrences()[0].exceptions == []


def test_delete_r_id_declined_is_cancel_not_unknown(monkeypatch, capsys):
  # the owner's typo case: a declined confirmation inside suppression is
  # a CANCEL — exit 2, nothing suppressed, and NO "Onbekende id" line
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["js"])  # typo for 'ja' -> declined
  import financials.delete as delete_module

  assert delete_module.delete_transaction(instance_hash) == 2
  assert load_recurrences()[0].exceptions == []
  out = capsys.readouterr().out
  assert "Geannuleerd" in out
  assert "Onbekende id" not in out


def test_edit_r_id_declined_is_cancel_not_unknown(monkeypatch, capsys):
  # same conflation existed pre-existing in edit: declining inside the
  # occurrence edit (choice q at the menu) must NOT print "Onbekende id"
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))
  _answers(monkeypatch, ["q"])  # cancel at the occurrence-edit menu
  import financials.edit as edit_module

  # edit_occurrence's menu-cancel convention is exit 0 (pre-existing);
  # the fix is purely about the message: no false "Onbekende id"
  assert edit_module.edit_transaction(instance_hash) == 0
  assert load_recurrences()[0].exceptions == []
  out = capsys.readouterr().out
  assert "Geannuleerd" in out
  assert "Onbekende id" not in out


def test_confirm_landed_instance_lands_the_actual(monkeypatch, capsys):
  """The OPEN-section confirmation workflow: edit on a landed instance
  offers option 2 — confirm as actual. All-Enter commits the instance's
  own values as a real transaction (AddMutation); a covering commit
  supersedes the instance (no exception recorded)."""
  recurrence_cli.add_recurrence(
    description="Huur",
    category="Wonen",
    amount=-800.0,
    frequency="monthly",
    day=1,
    start="2026-08-01",  # explicit: the default start is the pinned TODAY
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 9, 1))  # landed (before TODAY 09-21)
  # menu "2", then all-Enter through datum/omschrijving/bedrag; the
  # category prompt is the fixture's ask_category stub (no slot).
  _answers(monkeypatch, ["2", "", "", ""])
  assert recurrence_cli.edit_occurrence(instance_hash) == 0
  out = capsys.readouterr().out
  assert "Bevestigd: 2026-09-01" in out
  # the actual landed in the journal at the instance's date
  from financials.commands import command_journal
  from financials.journal import load_ledger

  ledger = load_ledger(command_journal())
  landed = [t for t in ledger.transactions if t.date == date(2026, 9, 1)]
  assert len(landed) == 1
  assert landed[0].description == "Huur"
  assert landed[0].postings == {"checking": -800.0}
  # a covering commit supersedes: NO exception
  assert load_recurrences()[0].exceptions == []


def test_confirm_landed_instance_with_drifted_amount_excepts(monkeypatch, capsys):
  """A drifted commit (amount beyond ±0.01) does NOT supersede — the
  instance would linger in OPEN, so the flow records the occurrence as an
  exception instead."""
  recurrence_cli.add_recurrence(
    description="Huur",
    category="Wonen",
    amount=-800.0,
    frequency="monthly",
    day=1,
    start="2026-08-01",  # explicit: the default start is the pinned TODAY
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 9, 1))
  # menu "2", datum Enter, omschrijving Enter, bedrag -750 (drifted)
  _answers(monkeypatch, ["2", "", "", "-750"])
  assert recurrence_cli.edit_occurrence(instance_hash) == 0
  rules = load_recurrences()
  assert rules[0].exceptions == ["2026-09-01"]
  from financials.commands import command_journal
  from financials.journal import load_ledger

  ledger = load_ledger(command_journal())
  landed = [
    t for t in ledger.transactions if t.description == "Huur" and t.date == date(2026, 9, 1)
  ]
  assert len(landed) == 1
  assert landed[0].postings == {"checking": -750.0}


def test_confirm_landed_instance_q_aborts(monkeypatch, capsys):
  """'q' at the first prompt aborts: nothing committed, no exception."""
  recurrence_cli.add_recurrence(
    description="Huur",
    category="Wonen",
    amount=-800.0,
    frequency="monthly",
    day=1,
    start="2026-08-01",  # explicit: the default start is the pinned TODAY
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 9, 1))
  _answers(monkeypatch, ["2", "q"])
  # cancel inside the confirm prompts is exit 2 (declined-action
  # convention, same as suppression's decline) — nothing committed
  assert recurrence_cli.edit_occurrence(instance_hash) == 2
  out = capsys.readouterr().out
  assert "Geannuleerd" in out
  assert "Bevestigd" not in out
  from financials.commands import command_journal
  from financials.journal import load_ledger

  ledger = load_ledger(command_journal())
  assert all(t.description != "Huur" for t in ledger.transactions)
  assert load_recurrences()[0].exceptions == []


def test_confirm_menu_not_offered_for_future_instance(monkeypatch, capsys):
  """A future instance keeps the original menu (1) regel / (2) expected-
  uitzondering — the landed-confirm option is only for date <= today.
  Behavioral proof (the prompt stub bypasses rich's rendering, so the
  menu text is not in the output): answering '2' on a FUTURE instance
  enters the seeded-exception loop, never the confirm flow."""
  recurrence_cli.add_recurrence(
    description="Huur", category="Wonen", amount=-800.0, frequency="monthly", day=1
  )
  rule = load_recurrences()[0]
  instance_hash = instance_id(rule, date(2026, 10, 1))  # future (TODAY 09-21)
  _answers(monkeypatch, ["2", "q"])  # '2' at the future menu, then abort its loop
  assert recurrence_cli.edit_occurrence(instance_hash) == 2
  out = capsys.readouterr().out
  assert "Bevestigen van" not in out  # the confirm flow was never entered


def test_take_proposal_routes_through_seeded_loop(monkeypatch, tmp_path, capsys):
  """--take must NOT silently adopt: the seeded loop has to show each
  detected value (Enter accepts, typing replaces, q aborts)."""
  from financials.model import Transaction
  from financials.recurrences import detect_candidates, load_recurrences

  txs = [
    Transaction(
      id=f"t{i:04d}",
      date=f"2026-0{m}-05",
      description="Huur",
      category_raw="Wonen",
      category="Wonen",
      subcategory_raw="",
      subcategory="",
      amount_eur=-800.0,
      status="actual",
      linked_id="",
      balance_checking=None,
      balance_savings=None,
      flags=[],
      source_line=i,
      note="",
    )
    for i, m in enumerate((1, 2, 3), start=1)
  ]
  proposals = detect_candidates(txs, min_occurrences=3)
  assert len(proposals) == 1
  # the detector reads view_rows() -> the committed ledger; the ledger is
  # isolated in these tests, so stub the ledger source to the synthetic rows
  monkeypatch.setattr(
    "financials.ledger_view.view_rows",
    lambda: txs,
  )
  # q at the first prompt aborts: nothing saved
  _answers(monkeypatch, ["q"])
  assert recurrence_cli.take_proposal(1) == 2
  assert load_recurrences() == []
  # Enter-all accepts the proposal as-is
  _answers(monkeypatch, ["", "", "", "", "", "", ""])
  assert recurrence_cli.take_proposal(1) == 0
  rules = load_recurrences()
  assert len(rules) == 1
  assert rules[0].description == "Huur"
  assert rules[0].amount == -800.0
