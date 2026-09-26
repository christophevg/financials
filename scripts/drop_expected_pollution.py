#!/usr/bin/env python
"""One-time cleanup: drop the 8 test-pollution rows from expected.json.

2026-09-21: runs of tests/test_edit_move.py before the expected-store
isolation fix appended one expected row per run via the unpatched
add_expected — the test's move signature (Terug / Uitgaven / -30.00 /
today+10 / note "expected entry"). Live ids e0092–e0099 (8 runs).
The promotion's drift guard correctly refused: these rows exist ONLY in
the live store, absent from the bootstrap baseline.

This script removes ONLY rows matching the exact signature below; any
row with the id but different content REFUSES the run (owner data is
never guessed away). Backup before write; post-verify the store parses.

Run:
  uv run python scripts/drop_expected_pollution.py           # report only
  uv run python scripts/drop_expected_pollution.py --write   # backup + drop
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from financials.config import data_dir

POLLUTION_IDS = [f"e{n:04d}" for n in range(92, 100)]
SIGNATURE = {
  "description": "Terug",
  "category": "Uitgaven",
  "amount_eur": -30.0,
  "note": "expected entry",
}


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--write", action="store_true", help="actually drop (default: report)")
  args = parser.parse_args(argv)

  path = data_dir() / "expected.json"
  if not path.exists():
    print(f"{path} does not exist — nothing to clean.")
    return 1
  rows = json.loads(path.read_text(encoding="utf-8"))

  by_id = {row.get("id"): row for row in rows}
  matched, conflicts, missing = [], [], []
  for row_id in POLLUTION_IDS:
    row = by_id.get(row_id)
    if row is None:
      missing.append(row_id)
      continue
    date_ok = row.get("date") in {
      (date.today() + timedelta(days=10)).isoformat(),  # the test's today+10 signature
      "2026-10-01",  # the date those runs actually wrote
    }
    matches = (
      row.get("description") == SIGNATURE["description"]
      and row.get("category") == SIGNATURE["category"]
      and row.get("amount_eur") == SIGNATURE["amount_eur"]
      and row.get("note") == SIGNATURE["note"]
      and date_ok
    )
    if matches:
      matched.append(row)
    else:
      conflicts.append((row_id, row))

  print(f"Scan: {len(matched)} pollution row(s), {len(conflicts)} signature mismatch(es), "
        f"{len(missing)} already absent.")
  for row in matched:
    print(f"  DROP {row.get('id')}  {row.get('date')}  {row.get('description')}  "
          f"{row.get('amount_eur'):+,.2f}")
  for row_id, row in conflicts:
    print(f"  KEEP {row_id} (content differs — NOT removed): "
          f"{row.get('date')} {row.get('description')} [{row.get('category')}] "
          f"{row.get('amount_eur')}")
  if not matched:
    print("Nothing to drop.")
    return 0
  if not args.write:
    print("Report only — re-run with --write to drop the matched rows.")
    return 0

  backup = path.with_name("expected.json.backup-drop-pollution")
  shutil.copy2(path, backup)
  print(f"Backup: {backup}")

  drop_ids = {row.get("id") for row in matched}
  remaining = [row for row in rows if row.get("id") not in drop_ids]
  path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
  print(f"Dropped {len(rows) - len(remaining)} row(s); {len(remaining)} remain.")
  return 0


if __name__ == "__main__":
  sys.exit(main())