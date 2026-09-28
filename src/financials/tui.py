"""The TUI (step 1): a scrollable ledger view — Textual App + DataTable,
consuming build_view (the same composition as `list`; one composition,
two renderers).

Keys: up/down (j/k) move the row cursor one row; pageup/pagedown by
page; home/end jump to the first/last row; q or escape quits. The
DataTable clamps at the first/last row by itself (no offset math here)
and auto-scrolls the viewport when the cursor reaches a screen edge.
The table holds the FULL committed history: the default window (last
`days_back` days) is only where the cursor anchors on open — scrolling
up walks back through history to the first transaction.
"""

from __future__ import annotations

from datetime import date, timedelta

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from financials.ledger_view import ViewRows, _fmt, build_view
from financials.model import Transaction

_DASH = "—"


def _balance_text(value: float | None, extra_style: str = "") -> Text:
  """A balance cell: None → em-dash; negatives red (the list view's rule)."""
  if value is None:
    return Text(_DASH, style=extra_style or "", justify="right")
  style = extra_style
  if value < 0:
    style = f"{style} red".strip()
  return Text(_fmt(value), style=style, justify="right")


def _amount_text(value: float | None, extra_style: str = "") -> Text:
  """A Verandering cell: the ledger's signed number format."""
  if value is None:
    return Text(_DASH, style=extra_style or "", justify="right")
  return Text(f"{value:+,.2f}", style=extra_style or "", justify="right")


def _row_of(item: Transaction | tuple) -> tuple:
  """Actuals arrive as plain Transactions, open/future as
  (Transaction, checking, savings) walk tuples — normalize to the tuple."""
  if isinstance(item, tuple):
    return item
  return (item, item.balance_checking, item.balance_savings)


class LedgerTable(DataTable):
  """DataTable with the ledger's key map: a row cursor, j/k aliases and
  Home/End jumping the cursor to the first/last row."""

  BINDINGS = [
    Binding("j", "cursor_down", "Down", show=False),
    Binding("k", "cursor_up", "Up", show=False),
    Binding("home", "cursor_top", "First", show=False),
    Binding("end", "cursor_bottom", "Last", show=False),
  ]

  def __init__(self, **kwargs) -> None:
    super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
    self.row_ids: list[str | None] = []

  def action_cursor_top(self) -> None:
    if self.row_count:
      self.move_cursor(row=0)

  def action_cursor_bottom(self) -> None:
    if self.row_count:
      self.move_cursor(row=self.row_count - 1)

  def selected_id(self) -> str | None:
    """The selected row's entry id (None on section separators) — the
    step-2 dialogs address rows through this."""
    if not self.row_count:
      return None
    row = self.cursor_row
    if 0 <= row < len(self.row_ids):
      return self.row_ids[row]
    return None


class LedgerTUI(App[None]):
  """The scrollable ledger (step 1): pinned header, scrollable rows
  filling the terminal height, pinned footer. The table holds the full
  committed history, then OPEN, then the projection to year end (the
  footer's own horizon); the cursor anchors on the default window."""

  CSS = """
  #kasboek-header {
    height: auto;
    padding: 0 1;
  }
  #ledger-table {
    height: 1fr;
  }
  #kasboek-footer {
    height: auto;
    dock: bottom;
    padding: 0 1;
  }
  """

  BINDINGS = [
    Binding("q", "quit", "Afsluiten"),
    Binding("escape", "quit", "Afsluiten", show=False),
  ]

  theme = "textual-light"

  def __init__(self, days_back: int = 3, today: date | None = None) -> None:
    super().__init__()
    self._days_back = days_back
    self._today = today
    self._window_start: str = ""
    self._view: ViewRows | None = None

  def compose(self) -> ComposeResult:
    yield Static("", id="kasboek-header")
    table = LedgerTable(id="ledger-table")
    table.add_column("Id", key="id")
    table.add_column("Datum", key="date")
    table.add_column("Omschrijving", key="description")
    table.add_column("Categorie", key="category")
    table.add_column("Verandering", key="change")
    table.add_column("Checking", key="checking")
    table.add_column("Spaar", key="savings")
    yield table
    yield Static("", id="kasboek-footer")

  def on_mount(self) -> None:
    today = self._today or date.today()
    self._window_start = (today - timedelta(days=self._days_back)).isoformat()
    self._view = build_view(
      days=self._days_back,
      project=0,
      days_back=self._days_back,
      projection_horizon=date(today.year, 12, 31),
      today=today,
    )
    assert self._view is not None
    self._fill_table(self._view)

  def _fill_table(self, view: ViewRows) -> None:
    """Render the composed view into the table and the pinned strips.
    One continuous list: full committed history (ascending, the TUI's
    extra knob — `list` never renders it), then the actuals window,
    OPEN, PROJECTIE. No history separator: the default `days_back`
    window is simply where the cursor anchors; scrolling up from it
    walks back through history to the first transaction."""
    table = self.query_one("#ledger-table", LedgerTable)
    window_start = self._window_start

    history_rows: list[tuple] = [
      (t, t.balance_checking, t.balance_savings) for t in view.history
    ]
    for rollup, members in view.rollups:
      member_ids = {m.id for m in members}
      if any(t.id in member_ids for t in view.actuals):
        continue  # renders in/after the actuals window (list semantics)
      if rollup.date < window_start:
        history_rows.append(
          (rollup, rollup.balance_checking, rollup.balance_savings)
        )
    history_rows.sort(key=lambda row: (row[0].date, row[0].id))
    for row in history_rows:
      self._add_entry(table, row, style="")

    first_actual_index = len(history_rows)
    for t in view.actuals:
      self._add_entry(table, (t, t.balance_checking, t.balance_savings), style="")
    for rollup, members in view.rollups:
      member_ids = {m.id for m in members}
      if not any(t.id in member_ids for t in view.actuals):
        continue
      self._add_entry(
        table,
        (rollup, rollup.balance_checking, rollup.balance_savings),
        style="",
      )
    if view.open:
      self._add_section(table, "OPEN", "sep-open")
      for row in view.open:
        self._add_entry(table, row, style="")
    if view.future:
      self._add_section(table, "PROJECTIE", "sep-projection")
      for row in view.future:
        self._add_entry(table, row, style="dim")

    count = len(table.row_ids)
    if count:
      header = f"[bold]Kasboek[/bold] [dim]— journaal-geboekt · {count} rijen[/dim]"
    else:
      header = "[bold]Kasboek[/bold] [dim]— geen rijen in deze vensters.[/dim]"
    self.query_one("#kasboek-header", Static).update(header)

    hint = (
      "↑/↓ of j/k: regel · PgUp/PgDn: pagina · Home/End: begin/eind · q: afsluiten"
    )
    lines = [f"[dim]{escape(line)}[/dim]" for line in view.footer]
    lines.append(f"[dim]{hint}[/dim]")
    self.query_one("#kasboek-footer", Static).update("\n".join(lines))

    # Anchor the cursor on the first actual row: the view opens on the
    # default window (history above is out of sight but one ↑ away,
    # down to the first transaction). move_cursor clamps and scrolls
    # the cursor row into view.
    if table.row_count:
      table.move_cursor(row=min(first_actual_index, table.row_count - 1))
    table.focus()

  def _add_entry(
    self,
    table: LedgerTable,
    row: tuple,
    style: str,
  ) -> None:
    """One entry → seven styled cells; the row key is the entry id (the
    address step 2 uses)."""
    t, checking, savings = _row_of(row)
    table.add_row(
      Text(t.id, style=f"{style} dim".strip()),
      Text(t.date, style=style or ""),
      Text(t.description, style=style or ""),
      Text(t.category, style=style or ""),
      _amount_text(t.amount_eur, style),
      _balance_text(checking, style),
      _balance_text(savings, style),
      key=t.id or None,
    )
    table.row_ids.append(t.id or None)

  def _add_section(self, table: LedgerTable, label: str, key: str) -> None:
    """A section-label row (label in the Omschrijving column, as `list`
    renders it); not an entry — row_ids keeps None for it."""
    table.add_row(
      "",
      "",
      Text(label, style="bold"),
      "",
      "",
      "",
      "",
      key=key,
    )
    table.row_ids.append(None)


def run_tui(days_back: int = 3) -> None:
  """Entry point for `financials tui`."""
  LedgerTUI(days_back=days_back).run()