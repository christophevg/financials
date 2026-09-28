"""Command-line interface for financials."""

import argparse
from pathlib import Path

from rich.console import Console

from financials.delete import delete_transaction
from financials.edit import edit_transaction
from financials.entry import add_transaction
from financials.expected import add_expected, list_expected, remove_expected
from financials.fixes import apply_all_pending, apply_fixes
from financials.reporting import print_report

console = Console()


def main() -> None:
  parser = argparse.ArgumentParser(
    prog="financials",
    description="Personal cashflow management: journaled ledger, visualize, forecast",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  fix_parser = sub.add_parser(
    "fix",
    help="Apply reviewed cleanup fixes from pending data/cleanup_fixes* files.",
  )
  fix_parser.add_argument(
    "--file",
    default="",
    help="Apply a single fixes file (JSON or TOML) instead of all pending ones.",
  )
  fix_parser.add_argument(
    "--all",
    action="store_true",
    help="Apply all pending data/cleanup_fixes* files, oldest first (journaling as .applied).",
  )

  report_parser = sub.add_parser(
    "report",
    help="Visual report: monthly flows, category breakdown, balance curve.",
  )
  report_parser.add_argument(
    "--months",
    type=int,
    default=None,
    help="Limit to the last N months (default: whole period).",
  )
  report_parser.add_argument(
    "--year",
    type=int,
    default=None,
    help="Limit to a specific year (overrides --months).",
  )
  report_parser.add_argument(
    "--top",
    type=int,
    default=10,
    help="Number of categories in the top lists (default: 10).",
  )

  add_parser = sub.add_parser(
    "add",
    help=(
      "Add a transaction: bare (interactive), fully flagged, or shorthand "
      'like add "gisteren -25 Kafe" (missing fields are prompted). '
      "A future date is stored in the expected register automatically."
    ),
  )
  add_parser.add_argument("--date", default=None, help="ISO date (YYYY-MM-DD)")
  add_parser.add_argument("--description", default=None)
  add_parser.add_argument("--category", default=None)
  add_parser.add_argument(
    "--amount",
    type=float,
    default=None,
    help="Signed amount; positive = income, negative = expense.",
  )
  add_parser.add_argument(
    "--checking",
    type=float,
    default=None,
    help="New checking balance (defaults to the continued balance).",
  )
  add_parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Show what would be added without saving.",
  )
  add_parser.add_argument(
    "shorthand",
    nargs="?",
    default=None,
    metavar="SHORTHAND",
    help='Quick-add: "[date] [amount] [Category:]description" '
    "(date: ISO / vandaag / gisteren / morgen / -3d).",
  )

  expect_parser = sub.add_parser(
    "expect",
    help="Manage one-off future entries (expected register).",
  )
  expect_parser.add_argument("--date", default=None, help="ISO date, must be in the future")
  expect_parser.add_argument("--description", default=None)
  expect_parser.add_argument("--category", default=None)
  expect_parser.add_argument("--amount", type=float, default=None)
  expect_parser.add_argument("--list", action="store_true", help="List all expected entries.")
  expect_parser.add_argument(
    "--remove", metavar="ID", default=None, help="Remove an expected entry by id."
  )

  edit_parser = sub.add_parser(
    "edit",
    help="Edit an existing transaction (t####) or expected entry (e####).",
  )
  edit_parser.add_argument(
    "id",
    nargs="?",
    default=None,
    metavar="ID",
    help="Id from 'financials list' (omit to pick from the last 10 actual rows).",
  )

  delete_parser = sub.add_parser(
    "delete",
    help=(
      "Delete a transaction (t####), expected entry (e####) or rule"
      " instance (r:... = suppress), with confirmation."
    ),
  )
  delete_parser.add_argument(
    "id",
    nargs="?",
    default=None,
    metavar="ID",
    help="Id from 'financials list' (omit to pick from the last 10 actual rows).",
  )

  confirm_parser = sub.add_parser(
    "confirm",
    help=(
      "Confirm an open entry in one go: a landed expected entry (e####) or"
      " rule instance (r:...) commits as-is, zero prompts. Adjust first:"
      " 'financials edit'. Bare confirm shows the OPEN section."
    ),
  )
  confirm_parser.add_argument(
    "id",
    nargs="?",
    default=None,
    metavar="ID",
    help="Expected entry (e####) or rule instance (r:...) from 'financials list'.",
  )

  group_parser = sub.add_parser(
    "group",
    help=(
      "Group rows into one virtual rollup row (credit-card statements):"
      " list | show | create | add | remove | edit | delete. The view"
      " shows the rollup with the member total."
    ),
  )
  group_parser.add_argument(
    "action",
    nargs="?",
    default=None,
    help="list | show | create | add | remove | edit | delete",
  )
  group_parser.add_argument(
    "group_id",
    nargs="?",
    default=None,
    metavar="GROUP",
    help="Group id (g#### or unique prefix); for `create`, the group's name.",
  )
  group_parser.add_argument(
    "member_id",
    nargs="?",
    default=None,
    metavar="ID",
    help="Row id for add/remove (t####, e####, r:...); multiple allowed.",
  )
  group_parser.add_argument(
    "member_ids",
    nargs="*",
    default=[],
    help="Additional row ids for add/remove.",
  )
  group_parser.add_argument(
    "--date",
    default="",
    help="create/edit: rollup date (YYYY-MM-DD; empty = last member's date).",
  )
  group_parser.add_argument(
    "--category",
    default="",
    help="create: category for the rollup row.",
  )
  group_parser.add_argument(
    "--note",
    default="",
    help="create/edit: note for the group.",
  )

  list_parser = sub.add_parser(
    "list",
    help="Ledger: actual transactions of the last X days + projection for Y days.",
  )
  list_parser.add_argument(
    "--days", type=int, default=30, help="Actual history window in days (default 30)."
  )
  list_parser.add_argument(
    "--project", type=int, default=30, help="Projection window in days (default 30)."
  )
  list_parser.add_argument(
    "--filter",
    default=None,
    help="Only show rows whose description, category or id contains this (case-insensitive).",
  )

  tui_parser = sub.add_parser(
    "tui",
    help="Interactive TUI: scrollable ledger view (actuals + openstaand + projection).",
  )
  tui_parser.add_argument(
    "--days",
    type=int,
    default=3,
    help="Actual history window in days (default 3; the projection runs to year end).",
  )

  recurrence_parser = sub.add_parser(
    "recurrence",
    help="Manage recurring rules (the forecast's repeating entries).",
  )
  recurrence_parser.add_argument(
    "action",
    nargs="?",
    default=None,
    metavar="ACTION",
    help="list | add | pause | resume | remove | detect (default: list)",
  )
  recurrence_parser.add_argument("prefix", nargs="?", default=None, metavar="PREFIX")
  recurrence_parser.add_argument("--description", default=None)
  recurrence_parser.add_argument("--category", default=None)
  recurrence_parser.add_argument("--amount", type=float, default=None)
  recurrence_parser.add_argument("--frequency", default=None, help="monthly | yearly (add)")
  recurrence_parser.add_argument("--day", type=int, default=None, help="day of month (1..31)")
  recurrence_parser.add_argument("--month", type=int, default=None, help="month for yearly (1..12)")
  recurrence_parser.add_argument("--start", default=None, help="ISO date; instances from this date")
  recurrence_parser.add_argument("--end", default="", help="ISO date; last instance (optional)")
  recurrence_parser.add_argument(
    "--weekday", default=None, help="weekly/biweekly: ma|di|wo|do|vr|za|zo (anchor weekday)"
  )
  recurrence_parser.add_argument("--min", type=int, default=3, help="detect: min occurrences")
  recurrence_parser.add_argument("--take", type=int, default=None, help="detect: adopt proposal N")
  recurrence_parser.add_argument(
    "--occurrence", default=None, help="edit: instance id (r:-hash) for the occurrence path"
  )

  args = parser.parse_args()

  if args.command == "fix":
    if args.all:
      apply_all_pending()
    elif args.file:
      apply_fixes(Path(args.file))
    else:
      parser.error("nothing to apply: use --file <fixes-file> or --all")
  elif args.command == "report":
    from financials.ledger_view import view_rows

    print_report(view_rows(), args.months, args.year, args.top)
  elif args.command == "add":
    try:
      raise SystemExit(
        add_transaction(
          iso_date=args.date,
          description=args.description,
          category=args.category,
          amount=args.amount,
          checking=args.checking,
          dry_run=args.dry_run,
          shorthand=args.shorthand,
        )
      )
    except KeyboardInterrupt:
      console.print("[yellow]Geannuleerd (Ctrl-C) — niets opgeslagen.[/yellow]")
      raise SystemExit(0) from None
  elif args.command == "expect":
    if args.list:
      raise SystemExit(list_expected())
    if args.remove:
      raise SystemExit(remove_expected(args.remove))
    raise SystemExit(
      add_expected(
        iso_date=args.date,
        description=args.description,
        category=args.category,
        amount=args.amount,
      )
    )
  elif args.command == "edit":
    try:
      raise SystemExit(edit_transaction(args.id))
    except KeyboardInterrupt:
      console.print("[yellow]Geannuleerd (Ctrl-C).[/yellow]")
      raise SystemExit(0) from None
  elif args.command == "delete":
    try:
      raise SystemExit(delete_transaction(args.id))
    except KeyboardInterrupt:
      console.print("[yellow]Geannuleerd (Ctrl-C).[/yellow]")
      raise SystemExit(0) from None
  elif args.command == "confirm":
    from financials.confirm import confirm_transaction

    try:
      raise SystemExit(confirm_transaction(args.id))
    except KeyboardInterrupt:
      console.print("[yellow]Geannuleerd (Ctrl-C).[/yellow]")
      raise SystemExit(0) from None
  elif args.command == "group":
    from financials import groups as groups_cli

    action = args.action or "list"
    if action == "list":
      raise SystemExit(groups_cli.list_groups())
    elif action == "show":
      if not args.group_id:
        group_parser.error("show requires a group id (g####)")
      raise SystemExit(groups_cli.show_group(args.group_id))
    elif action == "create":
      raise SystemExit(
        groups_cli.create_group(
          label=args.group_id,
          date_text=args.date,
          category=args.category,
          note=args.note,
        )
      )
    elif action == "add":
      if not args.group_id or not args.member_id:
        group_parser.error("add requires a group (id or name) and one or more row ids")
      raise SystemExit(groups_cli.add_member(args.group_id, args.member_id, *args.member_ids))
    elif action == "remove":
      if not args.group_id or not args.member_id:
        group_parser.error("remove requires a group (id or name) and one or more row ids")
      raise SystemExit(groups_cli.remove_member(args.group_id, args.member_id, *args.member_ids))
    elif action == "edit":
      if not args.group_id:
        group_parser.error("edit requires a group id (g####)")
      raise SystemExit(groups_cli.edit_group(args.group_id))
    elif action == "delete":
      if not args.group_id:
        group_parser.error("delete requires a group id (g####)")
      raise SystemExit(groups_cli.delete_group(args.group_id))
    else:
      group_parser.error(
        f"unknown action {action!r} — use list | show | create | add | remove | edit | delete"
      )
  elif args.command == "list":
    from financials.ledger_view import print_ledger_view

    print_ledger_view(days=args.days, project=args.project, filter=args.filter)
  elif args.command == "tui":
    from financials.tui import run_tui

    run_tui(days_back=args.days)
  elif args.command == "recurrence":
    from financials import recurrence_cli

    action = args.action or "list"
    if action == "list":
      raise SystemExit(recurrence_cli.list_recurrences())
    elif action == "add":
      raise SystemExit(
        recurrence_cli.add_recurrence(
          description=args.description,
          category=args.category,
          amount=args.amount,
          frequency=args.frequency,
          day=args.day,
          month=args.month,
          start=args.start,
          end=args.end,
          weekday=args.weekday,
        )
      )
    elif action == "pause":
      raise SystemExit(recurrence_cli.pause_recurrence(args.prefix or ""))
    elif action == "resume":
      raise SystemExit(recurrence_cli.resume_recurrence(args.prefix or ""))
    elif action == "remove":
      raise SystemExit(recurrence_cli.remove_recurrence(args.prefix or ""))
    elif action == "edit":
      if args.occurrence:
        raise SystemExit(recurrence_cli.edit_occurrence(args.occurrence))
      raise SystemExit(recurrence_cli.edit_recurrence(args.prefix or ""))
    elif action == "detect":
      if args.take is not None:
        raise SystemExit(recurrence_cli.take_proposal(args.take, min_occurrences=args.min))
      raise SystemExit(recurrence_cli.detect_recurrences(min_occurrences=args.min))
    else:
      recurrence_parser.error(
        f"unknown action {action!r} — use list | add | pause | resume | remove | detect"
      )


if __name__ == "__main__":
  main()
