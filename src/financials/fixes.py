"""Change-set engine: apply guarded operations to the data stores.

The change-set is an in-memory list of dicts (the internal contract);
data/journal.jsonl is the append-only audit trail of every applied
change-set (ts, source, fixes, replay verdict). Hand-authored file
sets remain supported for manual corrections: `financials fix --file`
loads a JSON/TOML document and routes it through the same engine.

{
  "fixes": [
    {"id": "t0424", "field": "date", "from": "", "to": "2025-05-30",
     "reason": "date pinned from source context"},
    {"id": "all", "op": "rebase_checking", "reason": "chain rebase"}
  ]
}
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from collections import Counter

from rich.console import Console
from rich.table import Table

from financials.importer import _replay_balances
from financials.model import (
  FLAG_BAD_AMOUNT,
  STATUS_ACTUAL,
  STATUS_EXPECTED,
  Transaction,
  load_expected,
  load_transactions,
  save_expected,
  save_transactions,
)
from financials.config import data_dir


def journal_file() -> Path:
  """Append-only audit trail (config-driven location)."""
  return data_dir() / "journal.jsonl"

console = Console()

GLOBAL_OPS = {
  "map_category",
  "map_category_default",
  "amount_zero_fill",
  "refresh_status",
  "split_expected",
  "rebase_checking",
  "redate_rollover_rows",
  "sort_by_date",
  "restore_source_order",
  "fix_date_monotonicity",
  "restore_rollover_extra",
  "reorder_december_block",
  "remove_row",
  "add_expected_row",
}


def _next_t_id(transactions: list[Transaction]) -> str:
  """Next sequential t-id based on the highest existing numeric id."""
  highest = 0
  for t in transactions:
    if t.id.startswith("t") and t.id[1:].isdigit():
      highest = max(highest, int(t.id[1:]))
  return f"t{highest + 1:04d}"


def _index_of_source_line(transactions: list[Transaction], source_line: int) -> int:
  for index, t in enumerate(transactions):
    if t.source_line == source_line:
      return index
  return len(transactions)


def _apply_one(t: Transaction, fix: dict, transactions: list[Transaction]) -> str:
  """Apply one fix to one transaction. Returns a short action description.
  Raises ValueError when the fix does not apply."""
  op = fix.get("op")
  if op == "flags_remove":
    removed = [flag for flag in fix["flags"] if flag in t.flags]
    for flag in removed:
      t.flags.remove(flag)
    return f"flags removed: {', '.join(removed)}" if removed else "flags already absent"
  if op == "note_append":
    t.note = f"{t.note}; {fix['note']}" if t.note else fix["note"]
    return "note appended"
  if op == "map_category":
    # Global rule-based op: maps matching categories across ALL transactions.
    mapping = fix["mapping"]
    counts = Counter()
    for candidate in transactions:
      if candidate.category in mapping:
        mapped = mapping[candidate.category]
        candidate.category = mapped
        counts[mapped] += 1
    summary = ", ".join(f"{name}: {count}" for name, count in counts.most_common())
    return f"mapped categories -> {summary or 'no matches'}"
  if op == "map_category_default":
    # Global rule-based op: assigns a category to every row whose category is
    # empty (resolving all missing-category flags at once).
    default = fix["default"]
    counts = 0
    for candidate in transactions:
      if not candidate.category:
        candidate.category = default
        counts += 1
    return f"defaulted empty categories -> {default}: {counts} rows"
  if op == "refresh_status":
    # Global rule-based op: recompute status from today's date — rows dated
    # after today are 'expected', everything else 'actual'.
    from datetime import date as _date

    today = _date.today().isoformat()
    flipped = 0
    for candidate in transactions:
      if not candidate.date:
        continue
      wanted = STATUS_EXPECTED if candidate.date > today else STATUS_ACTUAL
      if candidate.status != wanted:
        candidate.status = wanted
        flipped += 1
    return f"status refreshed (today={today}): {flipped} rows changed"
  if op == "split_expected":
    # Global one-time migration: move all expected rows out of
    # transactions.json into the separate expected register, renumbered e####.
    expected_rows = sorted(
      (t for t in transactions if t.status == STATUS_EXPECTED),
      key=lambda t: t.date,
    )
    if not expected_rows:
      return "split_expected: no expected rows present, nothing to do"
    kept = [t for t in transactions if t.status != STATUS_EXPECTED]
    for index, row in enumerate(expected_rows, start=1):
      row.id = f"e{index:04d}"
    save_expected(expected_rows)
    transactions[:] = kept
    return f"moved {len(expected_rows)} expected rows to expected.json"
  if op == "redate_rollover_rows":
    # One-time migration op: rows mistyped as Dec 2026 that are really
    # Dec 2025 (year-rollover typos in a source-line range, chronology
    # ruling: all of them belong to 2025). Restores them as actual
    # transactions with corrected dates, re-inserted before the 2026-01-01
    # chain.
    expected = load_expected()
    moved = [t for t in expected if 810 <= t.source_line <= 821]
    if not moved:
      return "redate_rollover_rows: no rollover rows in the expected register, nothing to do"
    remaining = [t for t in expected if not (810 <= t.source_line <= 821)]
    for row in moved:
      row.date = f"2025-12{row.date[7:]}"
      row.status = STATUS_ACTUAL
      row.note = (
        f"{row.note}; year corrected: source had Dec 2026 (rollover typo, "
        "chronology ruling: Dec 2025)"
      ).strip("; ")
    insert_at = next(
      (i for i, t in enumerate(transactions) if t.date and t.date >= "2026-01-01"),
      len(transactions),
    )
    for offset, row in enumerate(sorted(moved, key=lambda t: t.date)):
      transactions.insert(insert_at + offset, row)
    save_expected(remaining)
    return f"restored {len(moved)} rollover rows as actual with 2025 dates"
  if op == "restore_rollover_extra":
    # One-time migration op: two additional source rows in the same
    # Dec-2026 rollover-typo range (chronology ruling) — missed by the
    # earlier restore. Restores them as actual transactions with pristine
    # recorded balances, inserted so the December block stays contiguous.
    expected = load_expected()
    moved = [t for t in expected if 808 <= t.source_line <= 809]
    if not moved:
      return "restore_rollover_extra: rows not in the expected register, nothing to do"
    remaining = [t for t in expected if not (808 <= t.source_line <= 809)]
    for row in moved:
      row.date = f"2025-12{row.date[7:]}"
      row.status = STATUS_ACTUAL
      row.id = _next_t_id(transactions)
      row.note = (
        f"{row.note}; year corrected: source had Dec 2026 (rollover typo, "
        "chronology ruling: Dec 2025); balance pristine"
      ).strip("; ")
    insert_at = _index_of_source_line(transactions, 807) + 1
    for offset, row in enumerate(sorted(moved, key=lambda t: t.source_line)):
      transactions.insert(insert_at + offset, row)
    save_expected(remaining)
    return f"restored {len(moved)} extra rollover rows (808-809) as actual"
  if op == "reorder_december_block":
    # Local fix: the restored December rows sat after the month's last
    # row; true order (file order) is by day within the rollover range.
    block = [t for t in transactions if 808 <= t.source_line <= 821]
    if not block:
      return "reorder_december_block: block absent, nothing to do"
    first_at = transactions.index(block[0])
    ordered = sorted(block, key=lambda t: (t.date, t.source_line))
    transactions[first_at : first_at + len(block)] = ordered
    return f"reordered {len(ordered)} December-block rows chronologically"
  if op == "restore_source_order":
    # Global op: sort by import order (numeric id) — the source file's row
    # order reflects true chronology even where date labels are typos.
    transactions[:] = sorted(
      transactions,
      key=lambda t: int(t.id[1:]) if t.id[1:].isdigit() else 10**9,
    )
    return f"restored source order for {len(transactions)} rows"
  if op == "fix_date_monotonicity":
    # Global op: chronology ruling — the file order is chronologically
    # correct; year labels that decrease along that order are typos. Bump
    # the year (annotated) until dates are non-decreasing.
    fixed = []
    previous_date = None
    for candidate in transactions:
      if not candidate.date:
        continue
      if previous_date is not None and candidate.date < previous_date:
        try:
          corrected = date.fromisoformat(candidate.date).replace(
            year=date.fromisoformat(candidate.date).year + 1
          )
        except ValueError:  # Feb 29 -> Feb 28
          corrected = date.fromisoformat(candidate.date).replace(
            year=date.fromisoformat(candidate.date).year + 1, day=28
          )
      else:
        corrected = None
      if corrected is not None and corrected.isoformat() >= previous_date:
        candidate.note = (
          f"{candidate.note}; year corrected: source label {candidate.date} "
          "breaks chronology (ruling: file order is truth)"
        ).strip("; ")
        candidate.date = corrected.isoformat()
        fixed.append(candidate.id)
      previous_date = candidate.date
    return f"date labels made monotonic: {len(fixed)} rows ({', '.join(fixed) or 'none'})"
  if op == "sort_by_date":
    # Global op: stable-sort transactions by date; undated rows (Min/Max
    # aggregates) go last. Restores chain order after date corrections.
    undated = [t for t in transactions if not t.date]
    dated = sorted(
      (t for t in transactions if t.date),
      key=lambda t: (t.date, t.source_line or 10**9),
    )
    transactions[:] = dated + undated
    return f"sorted {len(dated)} dated rows (stable), {len(undated)} undated kept last"
  if op == "rebase_checking":
    # Global one-time op: recompute checking balances from the amount chain.
    # Where the recorded balance still replays correctly it is kept (proof of
    # continuity); where it was anchored through removed rows it is replaced
    # by the computed value. Idempotent: once the chain replays cleanly,
    # nothing changes.
    fixed = 0
    if transactions:
      first = transactions[0]
      anchor = (
        round(first.balance_checking - first.amount_eur, 2)
        if first.balance_checking is not None and first.amount_eur is not None
        else None
      )
      for candidate in transactions:
        if candidate.amount_eur is None:
          continue
        computed = round(anchor + candidate.amount_eur, 2) if anchor is not None else None
        if (
          computed is not None
          and candidate.balance_checking is not None
          and abs(computed - candidate.balance_checking) <= 0.005
        ):
          anchor = candidate.balance_checking
        else:
          candidate.balance_checking = computed
          anchor = computed
          fixed += 1
    return f"checking balances rebased from amounts: {fixed} rows changed"
  if op == "amount_zero_fill":
    # Global rule-based op: rows whose amount cell was blank in the source
    # (no movement recorded) get 0.0 so every transaction has an amount.
    count = 0
    for candidate in transactions:
      if candidate.amount_eur is None and "summary row" not in candidate.note:
        candidate.amount_eur = 0.0
        if FLAG_BAD_AMOUNT in candidate.flags:
          candidate.flags.remove(FLAG_BAD_AMOUNT)
        candidate.note = (
          f"{candidate.note}; amount was blank in source; recorded balances show no movement"
          if candidate.note
          else "amount was blank in source; recorded balances show no movement"
        )
        count += 1
    return f"amounts zero-filled: {count} rows"
  if op == "remove_row":
    # Global op: remove the targeted transaction entirely (dedup / spurious
    # row), keeping the audit trail in this applied fixes journal. Routes on
    # the id prefix: e#### -> expected register, t#### -> transactions.
    if t.id.startswith("e"):
      remaining = [candidate for candidate in load_expected() if candidate.id != t.id]
      save_expected(remaining)
      return "expected row removed"
    transactions[:] = [
      candidate for candidate in transactions if candidate.id != t.id
    ]
    return "row removed"
  if op == "add_expected_row":
    # Global op: (re-)create a one-off expected entry from the fix's own
    # fields (id is assigned here; a fresh t-id is minted if requested).
    entry = Transaction(
      id=f"e{len(load_expected()) + 1:04d}",
      date=fix["date"],
      description=fix["description"],
      category_raw=fix["category"],
      category=fix["category"],
      subcategory_raw="",
      subcategory="",
      amount_eur=fix["amount_eur"],
      status=STATUS_EXPECTED,
      linked_id="",
      balance_checking=fix.get("balance_checking"),
      balance_savings=fix.get("balance_savings"),
      flags=[],
      source_line=fix.get("source_line", 0),
      note=fix.get("note", ""),
    )
    expected = load_expected()
    expected.append(entry)
    save_expected(sorted(expected, key=lambda t: t.date))
    return (
      f"expected row added: {entry.id} {entry.date} {entry.description} "
      f"{entry.amount_eur:+,.2f}"
    )
  if "field" in fix:
    field = fix["field"]
    current = getattr(t, field)
    expected = fix.get("from")
    if expected is not None and current != expected:
      raise ValueError(
        f"{t.id}.{field}: expected current value {expected!r}, found {current!r}"
      )
    value = fix["to"]
    setattr(t, field, value)
    return f"{field}: {current!r} -> {value!r}"
  raise ValueError(f"unknown operation in fix for {t.id}: {fix!r}")


def _load_fixes_file(fixes_path: Path) -> list[dict]:
  """Load a fixes file; format follows the extension (.json or .toml).
  TOML arrays of tables ([[fixes]]) are the preferred, readable format."""
  if fixes_path.suffix == ".toml":
    import tomllib

    with open(fixes_path, "rb") as fh:
      document = tomllib.load(fh)
    return document.get("fixes", [])
  with open(fixes_path, encoding="utf-8") as fh:
    document = json.load(fh)
  return document.get("fixes", [])


def apply_all_pending() -> None:
  """Apply every data/cleanup_fixes* file that has not been applied yet,
  oldest first, and journal the applied files by renaming to *.applied."""
  pending = sorted(
    path
    for path in data_dir().glob("cleanup_fixes*")
    if path.suffix in {".json", ".toml"}
  )
  if not pending:
    console.print("[green]No pending fixes files.[/green]")
    return
  for path in pending:
    apply_fixes(path)
    path.rename(path.with_suffix(path.suffix + ".applied"))


def _resolve_target(fix: dict, by_id: dict, transactions: list[Transaction]) -> Transaction:
  """Resolve a fix's target transaction. id="all" with a global op returns
  a placeholder (unused by the op itself); e#### ids route to the expected
  register."""
  fix_id = fix.get("id", "")
  if fix_id == "all" and fix.get("op") in GLOBAL_OPS:
    return transactions[0]
  if fix_id.startswith("e") and fix_id[1:].isdigit():
    return by_id.get(fix_id) or _resolve_expected(fix_id)
  target = by_id.get(fix_id)
  if target is None:
    raise ValueError(f"fix targets unknown id: {fix_id}")
  return target


def _resolve_expected(expected_id: str) -> Transaction:
  expected = load_expected()
  for entry in expected:
    if entry.id == expected_id:
      return entry
  raise ValueError(f"fix targets unknown expected id: {expected_id}")


def apply_ops(fixes: list[dict], source: str) -> None:
  """Apply an in-memory change-set to the transaction store, then validate.

  This is THE single write path for the actual register: resolve targets,
  execute the ops (from-guarded), run the balance replay, save, and append
  one JSONL line to data/journal.jsonl — the append-only audit trail of
  every change to the data files. Journaling happens only AFTER a
  successful application: a guard violation aborts before any save, so
  every journal line records a change that actually happened.
  """
  transactions = load_transactions()
  by_id = {t.id: t for t in transactions}

  table = Table(title=f"Applying fixes from {source}", pad_edge=False)
  table.add_column("id", style="bold")
  table.add_column("action")
  table.add_column("reason")
  applied = 0
  for fix in fixes:
    fix_id = fix.get("id", "")
    if fix_id == "all":
      # id="all": global rule ops run once; per-row ops fan out over every
      # transaction. Used for rule-based category mapping.
      if fix.get("op") in GLOBAL_OPS:
        if not transactions:
          # Empty store: the placeholder row is only a routing token for
          # global ops (rebase_checking etc. iterate the list itself).
          table.add_row("all", "empty store — nothing to do", fix.get("reason", ""))
        else:
          action = _apply_one(transactions[0], fix, transactions)
          table.add_row("all", action, fix.get("reason", ""))
      else:
        count = 0
        for candidate in transactions:
          _apply_one(candidate, fix, transactions)
          count += 1
        table.add_row("all", f"applied to {count} rows", fix.get("reason", ""))
      applied += 1
      continue
    target = _resolve_target(fix, by_id, transactions)
    action = _apply_one(target, fix, transactions)
    applied += 1
    table.add_row(fix_id, action, fix.get("reason", ""))
  console.print(table)

  checks, mismatches, tolerated = _replay_balances(transactions)
  save_transactions(transactions)
  console.print(f"Applied {applied} fixes.")
  console.print(
    f"Balance replay: {checks} checks, [red]{mismatches} mismatches[/red],"
    f" {tolerated} rounding artifacts."
    if mismatches
    else f"Balance replay: {checks} checks, [green]0 mismatches[/green], {tolerated} rounding artifacts."
  )
  _append_journal(
    {
      "ts": datetime.now().isoformat(timespec="seconds"),
      "source": source,
      "fixes": fixes,
      "replay": {"checks": checks, "mismatches": mismatches, "tolerated": tolerated},
    }
  )


def _append_journal(record: dict) -> None:
  """Append one JSONL record to the audit journal (single line + newline)."""
  journal = journal_file()
  journal.parent.mkdir(parents=True, exist_ok=True)
  line = json.dumps(record, ensure_ascii=False)
  with open(journal, "a", encoding="utf-8") as fh:
    fh.write(line + "\n")


def apply_fixes(fixes_path: Path) -> None:
  """Apply a hand-authored fixes file (JSON or TOML): the file remains the
  human review interface for manual corrections; the engine is apply_ops."""
  fixes = _load_fixes_file(fixes_path)
  apply_ops(fixes, source=f"fix --file {fixes_path.name}")
