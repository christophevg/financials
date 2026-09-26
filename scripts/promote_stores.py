#!/usr/bin/env python
"""One-time data promotion (roadmap step 6, phase D1–D6).

Moves the migration staging stores to their canonical locations and
retires the old stores — renamed with a timestamp, never destroyed.
After this, command_journal() resolves to <data_dir>/journal.jsonl (the
staging file no longer exists) and the app boots from the promoted
stores with no further change.

Moves (in order, rollback = copy the backups back):
  D1  data/journal.jsonl        -> backups/journal.jsonl.retired-<ts>   (old audit trail)
  D4  migration/journal.jsonl   -> data/journal.jsonl                   (the event journal)
      migration/ledger.json     -> data/ledger.json                     (its checkpoint)
  D2  data/transactions.json    -> backups/transactions.json.retired-<ts>
  D3  data/expected.json        -> backups/expected.json.retired-<ts>
      migration/expected.json   -> data/expected.json                   (pruned baseline)
  D5  migration/rules.json      deleted ONLY when byte-identical to
      data/recurrences.json (which stays put); refusal otherwise
  D6  migration/ removed when empty

Guards:
  - pre-flight: the staging journal folds cleanly and its tip is recorded;
    the same tip must hold after the move (the move is byte-preserving)
  - expected.json drift check: any row present in the LIVE store but
    missing from the staging baseline REFUSES the promotion loudly
    (rows added after the bootstrap would be lost) — the owner decides
  - post-verify: load_ledger() from the promoted location == the
    pre-flight fold (ids + balances)

Run (Makefile target: migration-promote):
  uv run python scripts/promote_stores.py           # dry-run: report only
  uv run python scripts/promote_stores.py --write   # backups + move + verify
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from financials.config import data_dir
from financials.journal import Journal, fold, load_ledger

TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def _retire(path: Path, backups: Path) -> Path | None:
  """Move a file to backups/<name>.retired-<ts>; returns its new path."""
  if not path.exists():
    return None
  backups.mkdir(parents=True, exist_ok=True)
  target = backups / f"{path.name}.retired-{TIMESTAMP}"
  shutil.move(str(path), target)
  return target


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--write", action="store_true", help="actually promote (default: dry-run)")
  args = parser.parse_args(argv)

  root = data_dir()
  staging = root / "migration"
  backups = root / "backups"

  staging_journal = staging / "journal.jsonl"
  staging_checkpoint = staging / "ledger.json"
  staging_expected = staging / "expected.json"
  staging_rules = staging / "rules.json"

  canonical_journal = root / "journal.jsonl"
  canonical_expected = root / "expected.json"
  canonical_rules = root / "recurrences.json"

  if not staging_journal.exists():
    print(f"{staging_journal} does not exist — already promoted, nothing to do.")
    return 0

  # Pre-flight: the staging journal folds cleanly; record the tip.
  try:
    folded, tip = fold(staging_journal)
  except ValueError as error:
    print(f"PRE-FLIGHT FAILED — staging journal does not fold: {error}")
    return 1
  tip_entry = folded.transactions[-1]
  pre_balances = (
    tip_entry.balances.get("checking", 0.0),
    tip_entry.balances.get("savings", 0.0),
  )
  print(
    f"Pre-flight fold: {len(folded.transactions)} entries, tip {tip[:16]}…, "
    f"balances {pre_balances[0]:,.2f} / {pre_balances[1]:,.2f}"
  )

  # Expected.json drift check: live-only rows would be lost by the
  # promotion (the staging copy is the bootstrap's pruned baseline).
  if staging_expected.exists() and canonical_expected.exists():
    live_rows = json.loads(canonical_expected.read_text(encoding="utf-8"))
    baseline_rows = json.loads(staging_expected.read_text(encoding="utf-8"))
    baseline_ids = {row.get("id") for row in baseline_rows}
    live_only = [row for row in live_rows if row.get("id") not in baseline_ids]
    if live_only:
      print("REFUSED — expected.json drifted since the bootstrap baseline.")
      print(f"{len(live_only)} live row(s) absent from the staging baseline:")
      for row in live_only[:10]:
        print(f"  {row.get('id')}  {row.get('date')}  {row.get('description')}")
      print("Decide their fate first (re-enter after promotion, or merge manually).")
      return 1
    print(
      f"expected.json: baseline holds ({len(baseline_rows)} rows; "
      f"{len(baseline_rows) - len(live_rows)} pruned by the bootstrap)."
    )

  # D5 pre-check: rules disposition. The live store may legitimately be
  # ABSENT (no recurrence CLI yet — load_recurrences treats missing as
  # empty): an empty staging copy then just goes away; a non-empty
  # staging copy is the ONLY copy of the owner's rules and must be
  # PROMOTED, never deleted.
  rules_disposition = "skip"  # staging rules absent
  if staging_rules.exists():
    if canonical_rules.exists():
      if not filecmp.cmp(staging_rules, canonical_rules, shallow=False):
        print("REFUSED — migration/rules.json differs from data/recurrences.json.")
        print("Verify which is authoritative; do NOT delete the staging copy blindly.")
        return 1
      rules_disposition = "delete-identical"
      print("rules.json: staging copy is byte-identical to recurrences.json — safe to delete.")
    else:
      rules_rows = json.loads(staging_rules.read_text(encoding="utf-8"))
      if rules_rows:
        rules_disposition = "promote"
        print(
          f"rules.json: live store absent; staging copy has {len(rules_rows)} rule(s) — "
          "will PROMOTE it to recurrences.json."
        )
      else:
        rules_disposition = "delete-empty"
        print(
          "rules.json: staging copy is empty and the live store is absent "
          "(missing == empty for load_recurrences) — staging copy deleted, nothing promoted."
        )

  if not args.write:
    print("Dry-run — nothing moved. Re-run with --write to promote.")
    return 0

  # D1: retire the old audit trail (the staging journal takes its name).
  moved = _retire(canonical_journal, backups)
  print(f"D1 old audit trail: {'retired -> ' + str(moved) if moved else 'absent (skipped)'}")

  # D4: promote journal + checkpoint.
  shutil.move(str(staging_journal), canonical_journal)
  print(f"D4 journal: {staging_journal} -> {canonical_journal}")
  if staging_checkpoint.exists():
    shutil.move(str(staging_checkpoint), root / "ledger.json")
    print(f"D4 checkpoint: {staging_checkpoint} -> {root / 'ledger.json'}")

  # D2: retire the old actuals store.
  moved = _retire(root / "transactions.json", backups)
  print(f"D2 transactions.json: {'retired -> ' + str(moved) if moved else 'absent (skipped)'}")

  # D3: retire the live expected store, promote the baseline.
  moved = _retire(canonical_expected, backups)
  print(f"D3 expected.json: {'retired -> ' + str(moved) if moved else 'absent (skipped)'}")
  if staging_expected.exists():
    shutil.move(str(staging_expected), canonical_expected)
    print(f"D3 baseline: {staging_expected} -> {canonical_expected}")

  # D5: execute the decided rules disposition.
  if rules_disposition == "delete-identical":
    staging_rules.unlink()
    print("D5 rules.json: staging copy deleted (recurrences.json stays put).")
  elif rules_disposition == "promote":
    shutil.move(str(staging_rules), canonical_rules)
    print(f"D5 rules.json: PROMOTED -> {canonical_rules} (the only copy of your rules).")
  elif rules_disposition == "delete-empty":
    staging_rules.unlink()
    print("D5 rules.json: empty staging copy deleted (live store stays absent).")
  else:
    print("D5 rules.json: staging copy absent — nothing to do.")

  # D6: remove the staging dir when empty.
  if staging.is_dir() and not any(staging.iterdir()):
    staging.rmdir()
    print("D6 migration/: removed (empty).")
  else:
    leftovers = [str(p) for p in staging.iterdir()] if staging.is_dir() else []
    print(f"D6 migration/: kept (not empty: {leftovers})")

  # Post-verify: boot from the promoted location; the fold must agree
  # with the pre-flight fold exactly (ids + balances).
  ledger = load_ledger(Journal(canonical_journal))
  ok = [(t.id, t.balances) for t in ledger.transactions] == [
    (t.id, t.balances) for t in folded.transactions
  ]
  if not ok:
    print("VERIFICATION FAILED — promoted journal does not fold to the pre-flight state.")
    print("Rollback: copy the backups back and restore migration/ from them.")
    return 1
  print(
    f"Promotion verified: boot from {canonical_journal} == pre-flight fold "
    f"({len(ledger.transactions)} entries, tip {tip[:16]}…)."
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())