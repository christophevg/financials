#!/usr/bin/env python
"""One-time repair: re-mint stale mutation ids in the migration journal.

The step-2 observer bug (2026-09-19): four live confirm rows were
appended by a writer that stamped `ts` into the journal line but minted
the id over content WITHOUT ts. The id doubles as the row's
anti-corruption validator (16-hex content hash over the canonical
mutation minus the id), so those rows now fail fold()'s rehash
verification — the fold refuses loudly.

This script scans the journal, re-derives the id from each mismatched
row's own content (the content itself is never touched), and rewrites
ONLY the affected lines. Everything else stays byte-identical.

Run (Makefile target: migration-remint):
  uv run python scripts/remint_journal_ids.py            # dry-run: report only
  uv run python scripts/remint_journal_ids.py --write    # backup + repair + verify

Verification gate: after writing, the full journal must fold cleanly
(at the bootstrap tip balances); the script refuses to write when the
scan finds nothing, and reports loudly when the post-fold check fails.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from financials.config import data_dir
from financials.journal import fold, remint


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
    "path",
    nargs="?",
    type=Path,
    default=data_dir() / "migration/journal.jsonl",
    help="journal to repair (default: <data_dir>/migration/journal.jsonl)",
  )
  parser.add_argument(
    "--write",
    action="store_true",
    help="actually repair (default: dry-run, report only)",
  )
  args = parser.parse_args(argv)

  if not args.path.exists():
    print(f"{args.path} does not exist — nothing to repair.")
    return 1

  repaired = remint(args.path, dry_run=True)
  if not repaired:
    print("Scan clean: every row's id matches its content hash. Nothing to do.")
    return 0

  print(f"Stale ids found: {len(repaired)} (of the 2026-09-19 writer bug)")
  for number, old_id, new_id in repaired:
    print(f"  - line {number}: {old_id} -> {new_id}")

  if not args.write:
    print("Dry-run only — re-run with --write to apply.")
    return 0

  backup = args.path.with_suffix(
    args.path.suffix
    + "."
    + datetime.now().strftime("%Y%m%d-%H%M%S")
    + ".bak"
  )
  shutil.copy2(args.path, backup)
  print(f"Backup: {backup}")

  remint(args.path)  # writes (scan already reported above)
  print(f"Repaired {len(repaired)} lines.")

  # Verification gate: the repaired journal must fold cleanly.
  try:
    ledger, tip = fold(args.path)
  except ValueError as error:
    print(f"VERIFICATION FAILED — repaired journal does not fold: {error}")
    print(f"Restore from the backup: cp {backup} {args.path}")
    return 1
  entries = ledger.transactions
  tip_entry = ledger.find(tip)[0]
  print(
    f"Fold OK: {len(entries)} entries, tip {tip} "
    f"({', '.join(f'{k}={v:,.2f}' for k, v in sorted(tip_entry.balances.items()))})"
  )
  print("Repair complete. Re-run the suite: make test")
  return 0


if __name__ == "__main__":
  sys.exit(main())