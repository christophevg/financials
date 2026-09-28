"""The TUI (step 1+2): a scrollable ledger view — Textual App +
DataTable, consuming build_view (the same composition as `list`; one
composition, two renderers) — plus the step-2 readonly detail dialog.

Keys: up/down (j/k) move the row cursor one row; pageup/pagedown by
page; home/end jump to the first/last row; q or escape quits. The
DataTable clamps at the first/last row by itself (no offset math here)
and auto-scrolls the viewport when the cursor reaches a screen edge.
The table holds the FULL committed history above the actuals window;
the initial view is positioned on the projection: the PROJECTIE row at
the bottom of the viewport with the 10 rows above it starting at the
top, so the forecast's near future is what you see first.

Step 2: Enter on an entry row opens the readonly DetailScreen (the
row's full data + a provenance status line); Esc/q closes it. The
screen composes from the entry tuple so the editable step reuses it.
"""

from __future__ import annotations

from datetime import date, timedelta

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static
from textual.widgets._data_table import RowDoesNotExist

from financials.ledger_view import ViewRows, _fmt, build_view
from financials.model import Transaction

_DASH = "—"

# The initial view: rows above the anchor (PROJECTIE separator) that
# must be visible from the top of the viewport.
_ANCHOR_CONTEXT_ROWS = 10

# Entry provenance for the detail dialog's status line: where the row
# came from in the view's composition.
KIND_COMMITTED = "journaal-geboekt"
KIND_OPEN = "openstaand (geland, niet bevestigd)"
KIND_PROJECTED = "projectie (verwacht door regel)"

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
    dialogs address rows through this."""
    if not self.row_count:
      return None
    row = self.cursor_row
    if 0 <= row < len(self.row_ids):
      return self.row_ids[row]
    return None


class DetailScreen(ModalScreen[None]):
  """Readonly detail dialog for one entry (step 2): the row's full data
  plus a provenance status line. Composed from the entry tuple so the
  next step (editable) reuses this screen. Esc/q dismisses."""

  BINDINGS = [
    Binding("escape", "dismiss_screen", "Terug", show=False),
    Binding("q", "dismiss_screen", "Terug", show=False),
  ]

  DEFAULT_CSS = """
  DetailScreen {
    align: center middle;
  }
  #detail-dialog {
    width: 60%;
    height: auto;
    max-height: 60%;
    border: round $primary;
    background: white;
    padding: 1 2;
  }
  #detail-title {
    text-style: bold;
  }
  #detail-grid {
    layout: grid;
    grid-size: 2 5;
    height: 5;
    grid-rows: 1fr 1fr;
  }
  #balance-grid {
    layout: grid;
    grid-size: 2 2;
    height: 2;
    grid-rows: 1fr 1fr;
    margin-top: 2;
    color: $text-muted;
  }
  #detail-status {
    color: $text-muted;
    margin-top: 0;
  }
  """

  def __init__(self, entry: tuple, kind: str) -> None:
    super().__init__()
    self._entry = entry
    self._kind = kind

  def compose(self) -> ComposeResult:
    t, checking, savings = _row_of(self._entry)
    with Container(id="detail-dialog"):
      yield Static("Transactie", id="detail-title")
      yield Static(self._kind, id="detail-status")
      with Grid(id="detail-grid"):
        yield Static("Id", classes="label")
        yield Static(t.id or _DASH)
        yield Static("Datum", classes="label")
        yield Static(t.date)
        yield Static("Omschrijving", classes="label")
        yield Static(escape(t.description))
        yield Static("Categorie", classes="label")
        yield Static(escape(t.category or _DASH))
        yield Static("Verandering", classes="label")
        yield Static(_amount_text(t.amount_eur))
      with Grid(id="balance-grid"):
        yield Static("Checking", classes="label")
        yield Static(_balance_text(checking))
        yield Static("Spaar", classes="label")
        yield Static(_balance_text(savings))

  def action_dismiss_screen(self) -> None:
    self.dismiss(None)


class LedgerTUI(App[None]):
  """The scrollable ledger (step 1): pinned header, scrollable rows
  filling the terminal height, pinned footer. The table holds the full
  committed history, then OPEN, then the projection to year end (the
  footer's own horizon); the initial view is anchored on the
  projection's start. Enter on an entry row opens its detail dialog."""

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
    self._entries: dict[str, tuple] = {}

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
    OPEN, PROJECTIE."""
    table = self.query_one("#ledger-table", LedgerTable)
    window_start = self._window_start
    self._entries = {}

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
      self._add_entry(table, row, style="", kind=KIND_COMMITTED)

    for t in view.actuals:
      self._add_entry(
        table,
        (t, t.balance_checking, t.balance_savings),
        style="",
        kind=KIND_COMMITTED,
      )
    for rollup, members in view.rollups:
      member_ids = {m.id for m in members}
      if not any(t.id in member_ids for t in view.actuals):
        continue
      self._add_entry(
        table,
        (rollup, rollup.balance_checking, rollup.balance_savings),
        style="",
        kind=KIND_COMMITTED,
      )
    if view.open:
      self._add_section(table, "OPEN", "sep-open")
      for row in view.open:
        self._add_entry(table, row, style="", kind=KIND_OPEN)
    if view.future:
      self._add_section(table, "PROJECTIE", "sep-projection")
      for row in view.future:
        self._add_entry(table, row, style="dim", kind=KIND_PROJECTED)

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

    # Initial position: the PROJECTIE row at the bottom of the viewport
    # with the 10 rows above it starting at the top (clamped at the
    # table top; no PROJECTIE → the table end). The cursor sits on that
    # row, so ↓ continues from what you see.
    anchor = self._initial_anchor_row(table)
    table.call_after_refresh(self._position_at, table, anchor)
    table.focus()

  @staticmethod
  def _initial_anchor_row(table: LedgerTable) -> int:
    """The initial-position anchor row: the PROJECTIE separator's index,
    else the last row (no projection section); 0 on an empty table."""
    if not table.row_count:
      return 0
    try:
      return table.get_row_index("sep-projection")
    except RowDoesNotExist:
      return table.row_count - 1

  def _position_at(self, table: LedgerTable, anchor: int) -> None:
    """Scroll so `anchor` sits at the bottom of the viewport with the
    `_ANCHOR_CONTEXT_ROWS` rows above it starting at the top (clamped
    at the table top; a short table simply shows from row 0), and move
    the row cursor onto the anchor."""
    if not table.row_count:
      return
    anchor = max(0, min(anchor, table.row_count - 1))
    top = max(0, anchor - _ANCHOR_CONTEXT_ROWS)
    table.scroll_to(x=0, y=top, animate=False)
    table.move_cursor(row=anchor, scroll=False)

  def on_data_table_row_selected(
    self, event: DataTable.RowSelected
  ) -> None:
    """Enter on a row: open the readonly detail dialog for the entry
    (no-op on section separators, whose ids aren't in _entries)."""
    row_key = event.row_key.value
    entry = self._entries.get(row_key) if row_key else None
    if entry is None:
      return
    _, _, _, kind = entry
    self.push_screen(DetailScreen(entry[:3], kind))

  def _add_entry(
    self,
    table: LedgerTable,
    row: tuple,
    style: str,
    kind: str,
  ) -> None:
    """One entry → seven styled cells; the row key is the entry id (the
    address step 2 uses); _entries maps it to (entry, checking,
    savings, kind) for the detail dialog."""
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
    if t.id:
      self._entries[t.id] = (t, checking, savings, kind)

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
