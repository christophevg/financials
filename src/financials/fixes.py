"""Change-set engine (roadmap step 6): apply reviewed fix files against
the journaled ledger.

The change-set is an in-memory list of dicts (the internal contract);
`financials fix --file` loads a JSON/TOML document and routes it through
this engine. Field ops re-point at the committed ledger: each op becomes
one UpdateMutation carrying the target's FINAL row state (idempotent,
final-state semantics — the from-guard still prevents stale edits from
clobbering newer values). remove_row emits a DeleteMutation targeting
the victim's journal id (pre-image from the ledger). add_expected_row
and e-targeted remove_row keep operating on the expected register
(hypotheticals, overlay — never committed facts).

Every mutation is applied through the single write step
(commands.append_and_apply): append → boot (replay) → checkpoint. A
guard violation aborts BEFORE any append, so every journal line records
a change that actually happened.

{
  "fixes": [
    {"id": "d9906804515c73c2", "field": "description", "from": "Terug",
     "to": "Terug: parking", "reason": "typo"},
    {"id": "e0004", "op": "remove_row", "reason": "spurious"}
  ]
}
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from financials.commands import (
  append_and_apply,
  command_journal,
)
from financials.journal import (
  Date,
  Journal,
  Ledger,
  UpdateMutation,
  load_ledger,
)
from financials.journal import Transaction as LedgerTransaction
from financials.model import (
  load_expected,
  save_expected,
)

console = Console()

GLOBAL_OPS = {
  "remove_row",
  "add_expected_row",
}


def _resolve_ledger_target(fix_id: str, ledger: Ledger) -> LedgerTransaction | None:
  """Resolve a fix's target in the committed ledger: full hash id or
  unambiguous prefix. Raises on ambiguity; None when absent."""
  if not fix_id:
    raise ValueError("fix has no id")
  matches = [t for t in ledger.transactions if t.id and (t.id == fix_id or t.id.startswith(fix_id))]
  if len(matches) > 1:
    raise ValueError(f"ambiguous id prefix in fix: {fix_id} ({len(matches)} matches)")
  return matches[0] if matches else None


def _project_transaction(t: LedgerTransaction) -> dict:
  """The ledger-shaped projection of a committed row (the mutation
  content a field-op's final state carries): date/description/category/
  postings."""
  return {
    "date": t.date,
    "description": t.description,
    "category": t.category,
    "postings": t.postings,
  }


def _current_field(target: LedgerTransaction, field: str):
  """The target's current value for the fix's field (ledger-row shape)."""
  mapping = {
    "date": target.date,
    "description": target.description,
    "category": target.category,
    "amount": target.postings.get("checking", 0.0),
    "savings": target.postings.get("savings"),
  }
  if field not in mapping:
    raise ValueError(f"unsupported field for ledger fix: {field!r}")
  return mapping[field]


def replace_field(target: LedgerTransaction, field: str, value) -> LedgerTransaction:
  """The row with one field replaced (the update's final state)."""
  from dataclasses import replace as _replace

  if field == "amount":
    postings = dict(target.postings)
    postings["checking"] = round(value, 2)
    if target.category == "Overdracht":
      postings["savings"] = round(-value, 2)
    return _replace(target, postings=postings)
  if field in {"date", "description", "category"}:
    return _replace(target, **{field: value})
  raise ValueError(f"unsupported field for ledger fix: {field!r}")


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


def apply_ops(fixes: list[dict], source: str, ledger: Journal | None = None) -> Ledger:
  """Apply a change-set (fix-file ops or the delete/edit commands' ops)
  to the committed ledger + expected register. THE single write path:
  every op becomes a mutation appended via commands.append_and_apply
  (append → boot replay → checkpoint). A guard violation aborts before
  any append, so every journal line records a change that happened.
  Returns the live ledger (post-application)."""
  journal = ledger or command_journal()
  table = Table(title=f"Applying fixes from {source}", pad_edge=False)
  table.add_column("id", style="bold")
  table.add_column("action")
  table.add_column("reason")
  applied = 0
  for fix in fixes:
    fix_id = fix.get("id", "")
    op = fix.get("op", "")
    if "field" in fix:
      raise ValueError("field fixes must be applied through apply_field_ops (ledger boot)")
    if fix_id == "all":
      raise ValueError(f"unsupported global op for the journaled ledger: {op!r}")
    if op == "remove_row":
      committed = load_ledger(journal)
      target = _resolve_ledger_target(fix_id, committed)
      if target is None:
        # e#### routing: expected register (overlay, not committed).
        expected = load_expected()
        if any(x.id == fix_id for x in expected):
          save_expected([x for x in expected if x.id != fix_id])
          table.add_row(fix_id, "expected row removed", fix.get("reason", ""))
          applied += 1
          continue
        raise ValueError(f"fix targets unknown id: {fix_id}")
      from financials.journal import DeleteMutation

      delete = DeleteMutation(
        date=target.date,  # already a date (the ledger row's own)
        description=target.description,
        category=target.category,
        postings=dict(target.postings),
        target=target.id or "",
      )
      append_and_apply(delete, journal)
      table.add_row(fix_id, "row removed", fix.get("reason", ""))
      applied += 1
    elif op == "add_expected_row":
      from financials.expected import add_expected_row_from_fix

      entry_id = add_expected_row_from_fix(fix)
      table.add_row(fix_id, f"expected row added: {entry_id}", fix.get("reason", ""))
      applied += 1
    else:
      raise ValueError(f"unknown operation in fix for {fix_id}: {op!r}")
  console.print(table)
  console.print(f"Applied {applied} fixes.")
  return load_ledger(journal)


def apply_field_ops(fixes: list[dict], source: str, ledger: Journal | None = None) -> Ledger:
  """Apply field fixes (id+field+from+to) to the committed ledger: each
  becomes one UpdateMutation with the final row state (from-guarded).
  Returns the live ledger."""
  journal = ledger or command_journal()
  table = Table(title=f"Applying fixes from {source}", pad_edge=False)
  table.add_column("id", style="bold")
  table.add_column("action")
  table.add_column("reason")
  live = load_ledger(journal)
  applied = 0
  for fix in fixes:
    if "field" not in fix:
      raise ValueError(f"non-field fix in field application: {fix!r}")
    fix_id = fix.get("id", "")
    if fix_id == "all":
      raise ValueError("unsupported global op for the journaled ledger: field fix on 'all'")
    target = _resolve_ledger_target(fix_id, live)
    if target is None:
      raise ValueError(f"fix targets unknown id: {fix_id}")
    field = fix["field"]
    current = _current_field(target, field)
    expected_value = fix.get("from")
    if expected_value is not None and current != expected_value:
      raise ValueError(
        f"{fix_id}.{field}: expected current value {expected_value!r}, found {current!r}"
      )
    value = fix["to"]
    if field == "date":
      value = Date(value)  # accept ISO string in the fixes file
    projected = _project_transaction(replace_field(target, field, value))
    update = UpdateMutation(
      target=target.id or "",
      date=projected["date"],  # already a date
      description=projected["description"],
      category=projected["category"],
      postings=projected["postings"],
    )
    live = append_and_apply(update, journal)
    table.add_row(fix_id, f"{field}: {current!r} -> {value!r}", fix.get("reason", ""))
    applied += 1
  console.print(table)
  console.print(f"Applied {applied} fixes.")
  return live


def apply_all_pending() -> None:
  """Apply every data/cleanup_fixes* file that has not been applied yet,
  oldest first, and journal the applied files by renaming to *.applied."""
  pending = sorted(
    path for path in data_dir().glob("cleanup_fixes*") if path.suffix in {".json", ".toml"}
  )
  if not pending:
    console.print("[green]No pending fixes files.[/green]")
    return
  for path in pending:
    apply_fixes(path)
    path.rename(path.with_suffix(path.suffix + ".applied"))


def journal_file() -> Path:
  """The canonical journal (config-driven location). Before the data
  promotion the migration staging journal shadows it (commands.py and
  ledger_view.py resolve the canonical journal through this module)."""
  return data_dir() / "journal.jsonl"


def migration_journal_file() -> Path:
  """The migration staging journal (step 2's parallel append; retired as
  canonical once promotion moves it to journal_file())."""
  return data_dir() / "migration/journal.jsonl"


def apply_fixes(fixes_path: Path) -> None:
  """Apply a hand-authored fixes file against the journaled ledger:
  field ops route to apply_field_ops (one UpdateMutation each), row ops
  (remove_row/add_expected_row) route through apply_ops."""
  fixes = _load_fixes_file(fixes_path)
  field_fixes = [fix for fix in fixes if "field" in fix]
  row_fixes = [fix for fix in fixes if "field" not in fix]
  if field_fixes:
    apply_field_ops(field_fixes, source=fixes_path.name)
  if row_fixes:
    apply_ops(row_fixes, source=fixes_path.name)


# data_dir import kept at module bottom to avoid the circular import
# through commands (commands → fixes → commands).
from financials.config import data_dir  # noqa: E402  (module-level re-export)

__all__ = [
  "GLOBAL_OPS",
  "apply_all_pending",
  "apply_field_ops",
  "apply_fixes",
  "apply_ops",
  "journal_file",
  "migration_journal_file",
]
