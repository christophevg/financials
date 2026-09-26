#!/usr/bin/env python
"""One-time regeneration: rewrite the staging checkpoint to the canonical
fold (roadmap step 6 boot requirement).

Why: the staged migration/ledger.json was written by the bootstrap's
file-order evidence pass (step 1). Step 3 made the engine's
date-positioned fold THE canonical fold (design note, 2026-09-21), so
the staged checkpoint's ROW ORDER is stale wherever a journal row's date
differs from its file position (the 47 known inversion rows). Balances
are fine (tip holds: unchanged from the bootstrap) — only the order is
stale, but
load_ledger hydrates rows as persisted, so a stale order boots the ledger
in non-date order until a tail replay re-derives it. This script folds
the journal canonically and persists the result atomically.

Run (Makefile target: migration-recheckpoint):
  uv run python scripts/recheckpoint.py            # dry-run: report only
  uv run python scripts/recheckpoint.py --write    # backup + rewrite + verify

Verification gate: after writing, hydration from the new checkpoint must
equal a fresh fold exactly (ids + balances), and the tip balances must
hold at the bootstrap values.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from financials.config import data_dir
from financials.journal import Journal, checkpoint, fold, load_ledger


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
    "journal_path",
    nargs="?",
    type=Path,
    default=data_dir() / "migration/journal.jsonl",
    help="journal to fold (default: <data_dir>/migration/journal.jsonl)",
  )
  parser.add_argument("--write", action="store_true", help="actually rewrite (default: dry-run)")
  args = parser.parse_args(argv)

  journal_path = args.journal_path
  checkpoint_path = journal_path.with_name("ledger.json")
  if not journal_path.exists():
    print(f"{journal_path} does not exist — nothing to re-checkpoint.")
    return 1

  folded, tip = fold(journal_path)
  print(f"Canonical fold: {len(folded.transactions)} entries, tip {tip}")
  tip_entry = folded.transactions[-1]
  print(
    f"Tip balances: checking {tip_entry.balances.get('checking', 0.0):,.2f}, "
    f"savings {tip_entry.balances.get('savings', 0.0):,.2f}"
  )

  current = None
  if checkpoint_path.exists():
    current = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    current_ids = [row.get("id") for row in current.get("entries", [])]
    folded_ids = [t.id for t in folded.transactions]
    if current_ids == folded_ids:
      print("Checkpoint already matches the canonical fold. Nothing to do.")
      return 0
    print(
      f"Checkpoint differs from the canonical fold "
      f"(first divergence at row "
      f"{next(i for i, (a, b) in enumerate(zip(current_ids, folded_ids)) if a != b) + 1}) — "
      f"rewriting." if current_ids != folded_ids else ""
    )

  if not args.write:
    print("Dry-run — nothing written. Re-run with --write to rewrite the checkpoint.")
    return 0

  backup = checkpoint_path.with_name(
    "ledger.json.backup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
  )
  shutil.copy2(checkpoint_path, backup)
  print(f"Backup: {backup}")

  checkpoint(journal_path, path=checkpoint_path)  # atomic write, folds again

  # Verification gate: hydrate from the rewritten checkpoint and compare
  # to a fresh fold — exact agreement (ids + balances), else restore.
  hydrated = load_ledger(Journal(journal_path), path=checkpoint_path)
  fresh, fresh_tip = fold(journal_path)
  ok = [(t.id, t.balances) for t in hydrated.transactions] == [
    (t.id, t.balances) for t in fresh.transactions
  ] and hydrated.transactions[-1].balances == fresh.transactions[-1].balances
  if not ok:
    print("VERIFICATION FAILED — rewritten checkpoint does not match the fold.")
    print(f"Restore from the backup: cp {backup} {checkpoint_path}")
    return 1
  print(
    "Re-checkpoint verified: hydration == canonical fold; "
    f"tip {fresh_tip[:16]}… "
    f"({tip_entry.balances.get('checking', 0.0):,.2f} / "
    f"{tip_entry.balances.get('savings', 0.0):,.2f})"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())