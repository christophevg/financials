"""The TUI (steps 1–3): a scrollable ledger view — Textual App +
DataTable, consuming build_view (the same composition as `list`; one
composition, two renderers) — the step-2 readonly detail dialog, and
the step-3 add dialog (`a`, implemented in tui/add.py).

Keys: up/down (j/k) move the row cursor one row; pageup/pagedown by
page; home/end jump to the first/last row; q or escape quits; `a`
opens the add dialog; enter opens the detail dialog on an entry row.
The DataTable clamps at the first/last row by itself (no offset math
here) and auto-scrolls the viewport when the cursor reaches a screen
edge. The table holds the FULL committed history above the actuals
window; the initial view is positioned on the projection: the PROJECTIE
row at the bottom of the viewport with the 10 rows above it starting at
the top, so the forecast's near future is what you see first.

Row design: every entry row is TWO lines tall — the Datum column
holds the date; the merged second column holds the id (line 1, dim)
and "description / category" (line 2); the amount and the two
balances are their own single-line columns. Section separators stay
one line (label in the merged column). The merged column FILLS: the
other columns stay content-sized and it takes every remaining cell
(fit_columns — DataTable has no flex columns), so the numbers end
flush at the right edge. Rows are mixed-height (add_row height=2);
only the initial-position scroll math needs line offsets — cursor,
keys and dialogs stay row-based.

Step 2: Enter on an entry row opens the readonly DetailScreen (the
row's full data + a provenance status line); Esc/q closes it. The
screen composes from the entry tuple so the editable step reuses it.

Step 3: `a` opens the AddScreen form (native widgets; the validation
and save rules are imported from the CLI modules). On save the table
rebuilds from a fresh build_view and the cursor lands on the newly
added row; a toast confirms the new id.
"""

from __future__ import annotations

from datetime import date, timedelta

from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static
from textual.widgets._data_table import RowDoesNotExist

from financials.ledger_view import ViewRows, _fmt, build_view
from financials.model import Transaction
from financials.tui.add import AddScreen

_DASH = "—"

# The initial view: lines above the anchor (PROJECTIE separator) that
# must be visible from the top of the viewport. Line-based because
# entry rows are 2 lines tall (10 lines ≈ 5 rows of recent context)
# and the offset must never cut a row at the viewport top.
_ANCHOR_CONTEXT_LINES = 10

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
  Home/End jumping the cursor to the first/last row — plus the fill
  layout (fit_columns): the entry column takes every remaining cell so
  the numeric columns end flush at the right edge."""

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

  def fit_columns(self) -> None:
    """Layout: every column except the merged Transactie column keeps
    its content width (Datum included — it must count against the
    budget, else the total overflows the viewport and the last column
    is pushed out of sight); the merged column takes every remaining
    cell so the numeric columns end flush at the right edge. DataTable
    has no flex columns — mechanism: pin the fixed columns' widths,
    size the merged column against the table's scrollable width (never
    below its content width), and refit virtual_size."""
    if not self.columns:
      return
    columns = self.ordered_columns
    entry_col = columns[1]
    used = 0
    for col in columns:
      if col is entry_col:
        continue
      col.auto_width = False
      col.width = col.content_width
      used += col.get_render_width(self)
    entry_col.auto_width = False
    # avail already includes the entry column's 2×cell_padding share
    # (every get_render_width carries it) — subtract it so the total
    # render width equals the viewport exactly and no horizontal
    # scrollbar appears.
    entry_col.width = max(
      self.scrollable_content_region.width - used - 2 * self.cell_padding,
      entry_col.content_width,
    )
    self.virtual_size = Size(
      entry_col.get_render_width(self) + used, self.virtual_size.height
    )
    self.scroll_x = 0.0

  def _on_resize(self, event: events.Resize) -> None:
    super()._on_resize(event)
    self.fit_columns()

  def _on_show(self, event: events.Show) -> None:
    super()._on_show(event)
    self.fit_columns()


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
  projection's start. Enter on an entry row opens its detail dialog;
  `a` opens the add dialog (which saves and re-lands the cursor on the
  new row)."""

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
    Binding("a", "add", "Toevoegen"),
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
    # Line heights per table row (entries 2, separators 1) — the
    # line-based scroll math in _position_at.
    self._row_heights: list[int] = []

  def compose(self) -> ComposeResult:
    yield Static("", id="kasboek-header")
    table = LedgerTable(id="ledger-table")
    table.add_column("Datum", key="date")
    table.add_column("Transactie", key="entry")
    table.add_column("Verandering", key="change")
    table.add_column("Checking", key="checking")
    table.add_column("Spaar", key="savings")
    yield table
    yield Static("", id="kasboek-footer")

  def on_mount(self) -> None:
    self._load_view()

  def _load_view(self, focus_id: str | None = None) -> None:
    """(Re)build the composition and render it — on_mount, and after
    every save that changed the ledger (with the cursor re-landed on
    the affected row)."""
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
    self._fill_table(self._view, focus_id=focus_id)

  def _fill_table(self, view: ViewRows, focus_id: str | None = None) -> None:
    """Render the composed view into the table and the pinned strips.
    One continuous list: full committed history (ascending, the TUI's
    extra knob — `list` never renders it), then the actuals window,
    OPEN, PROJECTIE. focus_id lands the cursor on that row after the
    refresh (the post-save behavior); without it the initial anchor
    positioning applies."""
    table = self.query_one("#ledger-table", LedgerTable)
    window_start = self._window_start
    table.clear()  # a post-save rebuild must not duplicate rows
    table.row_ids = []  # clear() doesn't touch the parallel id list
    self._entries = {}
    self._row_heights = []

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
      "↑/↓ of j/k: regel · PgUp/PgDn: pagina · Home/End: begin/eind · "
      "a: nieuw · enter: detail · q: afsluiten"
    )
    lines = [f"[dim]{escape(line)}[/dim]" for line in view.footer]
    lines.append(f"[dim]{hint}[/dim]")
    self.query_one("#kasboek-footer", Static).update("\n".join(lines))

    # Column fit needs the final layout: refit once after refresh, then
    # position (a terminal resize refits via _on_resize).
    table.call_after_refresh(table.fit_columns)
    if focus_id and focus_id in table.row_ids:
      # Post-save landing: the cursor onto the new row (the viewport
      # follows; scroll math stays row-based here).
      table.call_after_refresh(
        self._focus_row, table, table.row_ids.index(focus_id)
      )
    else:
      # Initial position: the PROJECTIE row at the bottom of the
      # viewport with the 10 lines above it starting at the top
      # (clamped at the table top; no PROJECTIE → the table end). The
      # cursor sits on that row, so ↓ continues from what you see.
      anchor = self._initial_anchor_row(table)
      table.call_after_refresh(self._position_at, table, anchor)
    table.focus()

  @staticmethod
  def _focus_row(table: LedgerTable, row: int) -> None:
    """Post-save cursor landing: the cursor on the given row, clamped,
    viewport following it."""
    if table.row_count:
      table.move_cursor(row=max(0, min(row, table.row_count - 1)))

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
    """Scroll so `anchor` sits in view with `_ANCHOR_CONTEXT_LINES`
    lines above it, snapped to a row start so no row is cut at the top
    (clamped at the table top), and move the row cursor onto the
    anchor."""
    if not table.row_count:
      return
    anchor = max(0, min(anchor, table.row_count - 1))
    # Rows are mixed-height (entries 2 lines, separators 1): the scroll
    # offset is a LINE offset — the lines above the anchor minus the
    # context we want to keep visible.
    anchor_line = sum(self._row_heights[:anchor])
    top_line = max(0, anchor_line - _ANCHOR_CONTEXT_LINES)
    # Snap up to the start line of the row containing top_line so the
    # top row is never cut mid-row (shows that full row, never less).
    start = 0
    for height in self._row_heights:
      if start + height > top_line:
        break
      start += height
    table.scroll_to(x=0, y=start, animate=False)
    table.move_cursor(row=anchor, scroll=False)

  def action_add(self) -> None:
    """`a`: open the add dialog — never stacked on an open dialog
    (DetailScreen/AddScreen are ModalScreens; the ledger lives on the
    default screen)."""
    if isinstance(self.screen, ModalScreen):
      return
    self.push_screen(
      AddScreen(today=self._today or date.today()),
      callback=self._on_add_done,
    )

  def _on_add_done(self, new_id: str | None) -> None:
    """The add dialog closed: a toast confirms the save and the table
    rebuilds with the cursor on the new row. None (cancelled) leaves
    everything exactly as it was."""
    if not new_id:
      return
    self.notify(f"Opgeslagen als {new_id}", title="Toegevoegd", timeout=4)
    self._load_view(focus_id=new_id)

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
    """One entry → one TWO-line row: the Datum column plus a merged
    second column (id on line 1, dim; "description / category" on
    line 2); the amount and the two balances are their own single-line
    columns. The row key is the entry id (the address step 2 uses);
    _entries maps it to (entry, checking, savings, kind) for the
    detail dialog."""
    t, checking, savings = _row_of(row)
    base = style or ""
    merged = Text(style=base)
    if t.id:
      merged.append(t.id, style="dim")
    merged.append(f"\n{t.description}")
    if t.category:
      merged.append(f" / {t.category}")
    table.add_row(
      Text(t.date, style=base),
      merged,
      _amount_text(t.amount_eur, base),
      _balance_text(checking, base),
      _balance_text(savings, base),
      key=t.id or None,
      height=2,
    )
    table.row_ids.append(t.id or None)
    self._row_heights.append(2)
    if t.id:
      self._entries[t.id] = (t, checking, savings, kind)

  def _add_section(self, table: LedgerTable, label: str, key: str) -> None:
    """A section-label row (label in the Transactie column); not an
    entry — row_ids keeps None for it."""
    table.add_row("", Text(label, style="bold"), key=key)
    table.row_ids.append(None)
    self._row_heights.append(1)


def run_tui(days_back: int = 3) -> None:
  """Entry point for `financials tui`."""
  LedgerTUI(days_back=days_back).run()