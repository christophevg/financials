"""Command-line interface for financials."""

import argparse
from pathlib import Path

from rich.console import Console

from financials.edit import edit_transaction
from financials.entry import add_transaction
from financials.expected import add_expected, list_expected, remove_expected
from financials.fixes import apply_all_pending, apply_fixes
from financials.importer import run_import
from financials.ledger import print_ledger
from financials.model import load_transactions
from financials.reporting import print_report

from financials.delete import delete_transaction

console = Console()


def main() -> None:
  parser = argparse.ArgumentParser(
    prog="financials",
    description="Personal cashflow management: import, cleanup, visualize, forecast",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  import_parser = sub.add_parser(
    "import",
    help="Import cashflow.tsv into data/transactions.json (mechanical normalization only).",
  )
  import_parser.add_argument(
    "--force",
    action="store_true",
    help="Overwrite data/transactions.json even if it already exists.",
  )

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
      'Add a transaction: bare (interactive), fully flagged, or shorthand '
      'like add "gisteren -25 Kafe" (missing fields are prompted). '
      'A future date is stored in the expected register automatically.'
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
    '(date: ISO / vandaag / gisteren / morgen / -3d).',
  )

  expect_parser = sub.add_parser(
    "expect",
    help="Manage one-off future entries (expected register).",
  )
  expect_parser.add_argument("--date", default=None, help="ISO date, must be in the future")
  expect_parser.add_argument("--description", default=None)
  expect_parser.add_argument("--category", default=None)
  expect_parser.add_argument("--amount", type=float, default=None)
  expect_parser.add_argument(
    "--list", action="store_true", help="List all expected entries."
  )
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
    help="Delete a transaction (t####) or expected entry (e####), with confirmation.",
  )
  delete_parser.add_argument(
    "id",
    nargs="?",
    default=None,
    metavar="ID",
    help="Id from 'financials list' (omit to pick from the last 10 actual rows).",
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

  args = parser.parse_args()

  if args.command == "import":
    run_import(force=args.force)
  elif args.command == "fix":
    if args.all:
      apply_all_pending()
    elif args.file:
      apply_fixes(Path(args.file))
    else:
      parser.error("nothing to apply: use --file <fixes-file> or --all")
  elif args.command == "report":
    print_report(load_transactions(), args.months, args.year, args.top)
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
      raise SystemExit(
        console.print("[yellow]Geannuleerd (Ctrl-C) — niets opgeslagen.[/yellow]")
      )
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
      raise SystemExit(
        console.print("[yellow]Geannuleerd (Ctrl-C).[/yellow]")
      )
  elif args.command == "delete":
    try:
      raise SystemExit(delete_transaction(args.id))
    except KeyboardInterrupt:
      raise SystemExit(
        console.print("[yellow]Geannuleerd (Ctrl-C).[/yellow]")
      )
  elif args.command == "list":
    print_ledger(days=args.days, project=args.project)


if __name__ == "__main__":
  main()