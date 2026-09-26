"""Rich visual reports over data/transactions.json.

All flow figures exclude Overdracht (internal transfers between the owner's
own accounts). Only actual rows are reported; the count of pending expected
rows is shown as context.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from rich import box
from rich.console import Console
from rich.table import Table

from financials.model import STATUS_ACTUAL

console = Console()

EXCLUDED_FROM_FLOWS = {"Overdracht"}
BAR_WIDTH = 36


def _fmt(value: float, decimals: int = 2) -> str:
  """Dutch-style number formatting: 1.234,56."""
  text = f"{value:,.{decimals}f}"
  return text.translate(str.maketrans(",.", ".,", ""))


def _month_key(iso: str) -> str:
  return iso[:7]


def _month_range(keys: set[str]) -> list[str]:
  """All months between the earliest and latest key, inclusive."""
  start, end = min(keys), max(keys)
  year, month = int(start[:4]), int(start[5:7])
  months = []
  while True:
    key = f"{year:04d}-{month:02d}"
    months.append(key)
    if key == end:
      return months
    month += 1
    if month > 12:
      year, month = year + 1, 1


def _bar(value: float, scale: float) -> str:
  width = round(abs(value) / scale * BAR_WIDTH) if scale else 0
  return "█" * width


def _select(transactions: list, months: int | None, year: int | None) -> list:
  """Actual, dated, non-summary rows, optionally scoped to the last N months
  or a specific year."""
  rows = [
    t for t in transactions if t.status == STATUS_ACTUAL and t.date and "summary row" not in t.note
  ]
  if year is not None:
    rows = [t for t in rows if t.date.startswith(f"{year}-")]
  elif months is not None:
    keys = sorted({_month_key(t.date) for t in rows})
    keep = set(keys[-months:]) if months > 0 else set()
    rows = [t for t in rows if _month_key(t.date) in keep]
  return sorted(rows, key=lambda t: t.date)


def _aggregate_monthly(rows: list) -> list[dict]:
  """Per-month income/expense/net plus end-of-month balances."""
  income: defaultdict[str, float] = defaultdict(float)
  expense: defaultdict[str, float] = defaultdict(float)
  counts: defaultdict[str, int] = defaultdict(int)
  end_balance: dict[str, tuple[float, float]] = {}
  for t in rows:
    key = _month_key(t.date)
    if t.category not in EXCLUDED_FROM_FLOWS and t.amount_eur is not None:
      if t.amount_eur >= 0:
        income[key] += t.amount_eur
      else:
        expense[key] += -t.amount_eur
      counts[key] += 1
    if t.balance_checking is not None or t.balance_savings is not None:
      end_balance[key] = (t.balance_checking, t.balance_savings)
  months = _month_range({key for key in income} | set(end_balance))
  return [
    {
      "month": key,
      "income": income.get(key, 0.0),
      "expense": expense.get(key, 0.0),
      "net": income.get(key, 0.0) - expense.get(key, 0.0),
      "count": counts.get(key, 0),
      "checking": end_balance.get(key, (None, None))[0],
      "savings": end_balance.get(key, (None, None))[1],
    }
    for key in months
  ]


def _print_monthly(agg: list[dict]) -> None:
  table = Table(title="Maandelijks overzicht (zonder Overdracht)", box=box.SIMPLE)
  columns: list[tuple[str, Literal["left", "right"]]] = [
    ("Maand", "left"),
    ("Inkomsten", "right"),
    ("Uitgaven", "right"),
    ("Netto", "right"),
    ("#", "right"),
    ("Eind checking", "right"),
    ("Eind spaar", "right"),
  ]
  for column, justify in columns:
    table.add_column(column, justify=justify)
  for row in agg:
    checking = _fmt(row["checking"]) if row["checking"] is not None else "—"
    savings = _fmt(row["savings"]) if row["savings"] is not None else "—"
    table.add_row(
      row["month"],
      _fmt(row["income"]),
      _fmt(row["expense"]),
      _fmt(row["net"]),
      str(row["count"]),
      checking,
      savings,
    )
  console.print(table)


def _category_table(rows: list, sign: int, title: str, top: int) -> None:
  totals: defaultdict[str, float] = defaultdict(float)
  counts: defaultdict[str, int] = defaultdict(int)
  for t in rows:
    if t.category in EXCLUDED_FROM_FLOWS or t.amount_eur is None:
      continue
    if (t.amount_eur >= 0) == (sign > 0):
      totals[t.category] += abs(t.amount_eur)
      counts[t.category] += 1
  if not totals:
    console.print(f"[dim]Geen {'uitgaven' if sign < 0 else 'inkomsten'} in deze periode.[/dim]")
    return
  grand_total = sum(totals.values())
  ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:top]
  scale = ranked[0][1] if ranked else 1.0
  table = Table(
    title=title,
    box=box.SIMPLE,
  )
  columns: list[tuple[str, Literal["left", "right"]]] = [
    ("Categorie", "left"),
    ("Totaal", "right"),
    ("#", "right"),
    ("Gemiddeld", "right"),
    ("Aandeel", "right"),
    ("", "left"),
  ]
  for column, justify in columns:
    table.add_column(column, justify=justify)
  for category, total in ranked:
    table.add_row(
      category,
      _fmt(total),
      str(counts[category]),
      _fmt(total / counts[category]),
      f"{total / grand_total * 100:.0f}%",
      _bar(total, scale),
    )
  console.print(table)
  console.print(f"[dim]Totaal: € {_fmt(grand_total)}[/dim]")


def _balance_curve(agg: list[dict], months: int | None) -> None:
  show = agg[-months:] if months is not None else agg
  checkings = [r["checking"] for r in show if r["checking"] is not None]
  savings_list = [r["savings"] for r in show if r["savings"] is not None]
  scale_c = max((abs(v) for v in checkings), default=1.0)
  scale_s = max((abs(v) for v in savings_list), default=1.0)
  table = Table(title="Saldo-verloop (maandeindstanden)", box=box.SIMPLE)
  columns: list[tuple[str, Literal["left", "right"]]] = [
    ("Maand", "left"),
    ("Checking", "left"),
    ("€", "right"),
    ("Spaar", "left"),
    ("€", "right"),
  ]
  for column, justify in columns:
    table.add_column(column, justify=justify)
  for row in show:
    checking = row["checking"]
    savings = row["savings"]
    table.add_row(
      row["month"],
      _bar(checking, scale_c) if checking is not None else "—",
      _fmt(checking) if checking is not None else "—",
      _bar(savings, scale_s) if savings is not None else "—",
      _fmt(savings) if savings is not None else "—",
    )
  console.print(table)


def print_report(transactions: list, months: int | None, year: int | None, top: int) -> None:
  rows = _select(transactions, months, year)
  if not rows:
    console.print("[yellow]Geen actuele transacties in deze selectie.[/yellow]")
    return
  scope = year if year is not None else f"laatste {months} maanden" if months else "hele periode"
  console.print(f"[bold]Periode: {rows[0].date} → {rows[-1].date}[/bold] [dim]({scope})[/dim]")
  pending = sum(1 for t in transactions if t.status != STATUS_ACTUAL)
  console.print(
    f"[dim]{len(rows)} actuele transacties; {pending} verwachte (toekomstige) rijen.[/dim]"
  )
  console.print()
  _print_monthly(_aggregate_monthly(rows))
  console.print()
  _category_table(rows, sign=-1, title="Uitgaven per categorie", top=top)
  console.print()
  _category_table(rows, sign=1, title="Inkomsten per categorie", top=top)
  console.print()
  _balance_curve(_aggregate_monthly(rows), months=min(months, 18) if months else 18)
