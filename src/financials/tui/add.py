"""The TUI's step-3 add dialog: a native Textual form (modal screen)
for `financials tui`'s `a` key.

One source of truth for the RULES: validation, projection and the save
mutations are imported from the CLI modules (entry._validate /
_validate_amount, commands.projected_checking / cmd_add,
expected.add_expected) — only the PROMPTING is native widgets. The
same dispatch as the CLI applies: a future date saves to the expected
register instead of the journal (no balance chaining — expected rows
have no balances), and the CLI's future-date rejection inside
entry._validate is bypassed by dispatching BEFORE it (CLI parity:
entry.add_transaction dispatches before _validate too).

Form: Datum / Omschrijving / Categorie / Bedrag / checking-saldo
confirmation. Enter walks the fields (Tab works natively too); on the
saldo field Enter submits. Esc cancels (dismiss(None)).

The category is a FUZZY AUTOCOMPLETE (textual-autocomplete), not free
text: the approved categories (config-driven, call time) are offered
in a dropdown the moment the field takes focus — an empty input shows
ALL of them (questionary-style), typing narrows fuzzily. Enter
completes the highlighted match and advances to the next field (the
library intercepts Enter/Tab while the dropdown is visible; a mouse
click completes too). Esc with the dropdown open only hides it — a
second Esc cancels the dialog. Enter on a hidden dropdown accepts an
EXACT approved category; anything else is refused inline (submit
re-validates every value, so a Tab-past is caught at the gate too).
A bare 'q' in any field cancels (CLI parity).

The saldo confirmation shows the live projection for the typed
date+amount; blank accepts the projection (the data-quality gate is
"you saw the number", the typed value is never stored — balances are
engine-computed). Enter-driven — no per-keystroke ledger boots.

Tests: pilot-pressable. Validation errors render inline (a Static
line) instead of rich console output.
"""

from __future__ import annotations

from datetime import date

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Input, Static
from textual_autocomplete import AutoComplete

from financials.commands import command_journal, projected_checking
from financials.config import approved_categories
from financials.entry import _validate, _validate_amount
from financials.journal import load_ledger

_ABORT = "q"  # a bare 'q' in a field cancels the dialog (CLI parity)

_LABELS = {
  "add-date": "Datum",
  "add-description": "Omschrijving",
  "add-category": "Categorie",
  "add-amount": "Bedrag",
  "add-checking": "Checking-saldo",
}

_FIELD_ORDER = [
  "add-date",
  "add-description",
  "add-category",
  "add-amount",
  "add-checking",
]


class _Field(Input):
  """An add-form field: escape cancels the dialog. A plain Input blurs
  on escape — this form wants one-press cancel from anywhere (the
  subclass binding overrides Input's own escape binding)."""

  BINDINGS = [Binding("escape", "cancel_dialog", "Annuleren", show=False)]

  def action_cancel_dialog(self) -> None:
    screen = self.screen
    if isinstance(screen, AddScreen):
      screen.action_cancel()


class CategoryAutocomplete(AutoComplete):
  """The category dropdown: fuzzy matches over the approved category
  list (config-driven, call time) while the category field is focused.
  The base class hides the dropdown on an empty search string — the
  category field should offer the FULL list the moment it takes focus
  (questionary style), so visibility is simply: any candidate → show.
  Completing (Enter, Tab or click) advances the form walk to the next
  field."""

  def should_show_dropdown(self, search_string: str) -> bool:
    return self.option_list.option_count > 0

  def _handle_focus_change(self, has_focus: bool) -> None:
    """Focus alone must OPEN the dropdown (the base class only
    rebuilds options on focus — showing is left to a later
    Input.Changed, i.e. the list stays hidden until the user types a
    character). Routing the focus event through _handle_target_update
    gives align + rebuild + show: the full list is offered the moment
    the field takes focus (questionary style)."""
    if has_focus:
      self._handle_target_update()
    else:
      self.action_hide()

  def post_completion(self) -> None:
    super().post_completion()  # the library hides the dropdown
    screen = self.screen
    if isinstance(screen, AddScreen):
      screen._focus_next("add-category")


class AddScreen(ModalScreen[str | None]):
  """The add form. dismiss(value): the new entry id on save, None on
  cancel — the app's callback lands the cursor on the saved row."""

  BINDINGS = [
    Binding("escape", "cancel", "Annuleren", show=False),
  ]

  DEFAULT_CSS = """
  AddScreen {
    align: center middle;
  }
  #add-dialog {
    width: 64;
    height: auto;
    border: round $primary;
    background: white;
    padding: 1 2;
  }
  #add-title {
    text-style: bold;
    margin-bottom: 1;
  }
  #add-grid {
    layout: grid;
    grid-size: 2;
    grid-columns: 1fr 3fr;
    height: auto;
  }
  #add-grid Static {
    padding: 1 1 0 0;
    color: $text-muted;
  }
  #add-grid Input {
    width: 100%;
  }
  #add-category-ac {
    position: absolute;
  }
  #add-category-ac AutoCompleteList {
    max-height: 5;
  }
  #add-error {
    height: auto;
    margin-top: 1;
    color: $error;
  }
  #add-hint {
    height: auto;
    margin-top: 1;
    color: $text-muted;
  }
  #add-buttons {
    height: auto;
    margin-top: 1;
  }
  #add-buttons Static {
    color: $text-muted;
  }
  """

  def __init__(self, today: date | None = None) -> None:
    super().__init__()
    self._today = today or date.today()
    self._projection: float | None = None

  def compose(self) -> ComposeResult:
    category_field: _Field | None = None
    with Grid(id="add-dialog"):
      yield Static("Nieuwe transactie", id="add-title")
      with Grid(id="add-grid"):
        for field_id in _FIELD_ORDER:
          yield Static(_LABELS[field_id], classes="label")
          field = _Field(
            id=field_id,
            value=self._today.isoformat() if field_id == "add-date" else "",
          )
          if field_id == "add-category":
            category_field = field
          yield field
      yield Static("", id="add-error")
      yield Static("", id="add-hint")
      with Horizontal(id="add-buttons"):
        yield Static("enter: volgend/vastleggen · esc: annuleren")
    # The category autocomplete floats OVER the form (a screen-level
    # overlay widget, positioned at the input's cursor) — a grid child
    # here would consume a cell and shift every later field pair (the
    # hand-rolled attempt's bug).
    assert category_field is not None
    yield CategoryAutocomplete(
      category_field,
      candidates=sorted(approved_categories()),
      id="add-category-ac",
    )

  def on_mount(self) -> None:
    """Focus starts on the first field (the date is pre-filled)."""
    self.query_one("#add-date", _Field).focus()

  # --- field walking ----------------------------------------------------------

  @property
  def _fields(self) -> dict[str, _Field]:
    return {fid: self.query_one(f"#{fid}", _Field) for fid in _FIELD_ORDER}

  def _field_values(self) -> dict[str, str]:
    return {fid: inp.value.strip() for fid, inp in self._fields.items()}

  def _focus_next(self, field_id: str) -> None:
    idx = _FIELD_ORDER.index(field_id)
    self._fields[_FIELD_ORDER[min(idx + 1, len(_FIELD_ORDER) - 1)]].focus()

  def _advance(self, current: _Field) -> None:
    """Enter: refresh what later fields depend on, then to the next
    field; on the last field (saldo), submit. A bare 'q' cancels (CLI
    parity)."""
    if current.value.strip() == _ABORT:
      self.dismiss(None)
      return
    fid = current.id or ""
    if fid in ("add-date", "add-amount"):
      self._refresh_projection()
    if fid == "add-checking":
      self._submit()
      return
    if fid == "add-category":
      self._accept_category()
      return
    self._focus_next(fid)

  def on_input_submitted(self, event: Input.Submitted) -> None:
    """Enter on an input: walk forward / submit. (When the category
    dropdown is visible, the library intercepts Enter first — see
    CategoryAutocomplete.post_completion.)"""
    if isinstance(event.input, _Field):
      self._advance(event.input)
    event.stop()

  # --- the category accept ------------------------------------------------------

  def _accept_category(self) -> None:
    """Enter on the category field with the dropdown hidden: an exact
    approved category is accepted and the walk advances; anything else
    → inline error, stay (a non-approved category can never be walked
    past on Enter)."""
    field = self._fields["add-category"]
    typed = field.value.strip()
    if typed in approved_categories():
      self._error("")
      self._focus_next("add-category")
      return
    self._error(f"categorie {typed!r} is niet in de goedgekeurde lijst")

  # --- live projection ---------------------------------------------------------

  def _set_hint(self, text: str) -> None:
    self.query_one("#add-hint", Static).update(escape(text) if text else "")

  def _refresh_projection(self) -> None:
    """Recompute the projected checking balance from the typed date +
    amount (the same predecessor the engine chains from) and reflect
    it in the saldo field's placeholder. A future date clears it (the
    expected register has no balance chaining). Enter-driven — no
    per-keystroke ledger boots."""
    values = self._field_values()
    self._projection = None
    checking_input = self._fields["add-checking"]
    try:
      is_future = date.fromisoformat(values["add-date"]) > self._today
    except ValueError:
      is_future = False
    amount = _validate_amount(values["add-amount"])
    if is_future:
      checking_input.placeholder = "— expected-register (geen saldo-doorvoer)"
      self._set_hint("Toekomstige datum — opgeslagen in het expected-register.")
      return
    if amount is None:
      checking_input.placeholder = "projectie verschijnt hier"
      self._set_hint("")
      return
    ledger = load_ledger(command_journal())
    projected, _parent = projected_checking(ledger, values["add-date"], amount)
    if projected is None:
      checking_input.placeholder = "geen voorgaande rij — geen doorzetting"
      self._set_hint("")
      return
    self._projection = projected
    checking_input.placeholder = f"{projected:,.2f}  (leeg = accepteer)"
    self._set_hint("")

  # --- validation + save ------------------------------------------------------

  def _error(self, message: str) -> None:
    """Render an inline validation error."""
    self.query_one("#add-error", Static).update(escape(message))

  def _submit(self) -> None:
    """Validate and save. The same dispatch as the CLI: a future date
    → expected register (its own validation applies; errors are shown
    inline because add_expected's console output is invisible here),
    otherwise entry._validate (the CLI's rules) → cmd_add. On success:
    dismiss(new_id)."""
    values = self._field_values()
    iso_date = values["add-date"]
    description = values["add-description"]
    category = values["add-category"]

    amount = _validate_amount(values["add-amount"])
    if amount is None:
      self._error("bedrag ongeldig of 0 (niet toegelaten)")
      self._fields["add-amount"].focus()
      return

    try:
      date.fromisoformat(iso_date)
    except ValueError:
      self._error(f"ongeldige datum: {iso_date!r}")
      self._fields["add-date"].focus()
      return

    if iso_date > self._today.isoformat():
      # Future → expected register. add_expected re-validates but
      # reports on the console (invisible in the TUI) — pre-check the
      # same rules inline first.
      if not description:
        self._error("omschrijving mag niet leeg zijn")
        self._fields["add-description"].focus()
        return
      if category not in approved_categories():
        self._error(f"categorie {category!r} is niet in de goedgekeurde lijst")
        self._fields["add-category"].focus()
        return
      from financials.expected import add_expected

      rc = add_expected(
        iso_date=iso_date, description=description,
        category=category, amount=amount,
      )
      if rc != 0:
        self._error("expected-register weigerde de rij")
        return
      self.dismiss(_expected_id(iso_date, description))
      return

    ledger = load_ledger(command_journal())
    error = _validate(iso_date, description, category, amount, ledger)
    if error:
      self._error(error)
      return

    # Saldo confirmation: blank accepts the projection; a typed value
    # must match it within a cent (the CLI's gate — the number itself
    # is never stored, the engine computes balances). Computed FRESH
    # here (not from the placeholder) so Tab-navigation can't skip the
    # gate with a stale value; skipped when no predecessor exists.
    projected, _parent = projected_checking(ledger, iso_date, amount)
    if projected is not None and values["add-checking"]:
      typed_value = None
      try:
        typed_value = round(float(values["add-checking"].replace(",", ".")), 2)
      except ValueError:
        pass
      if typed_value is None or abs(typed_value - projected) >= 0.005:
        self._error(f"checking-saldo komt niet overeen met {projected:,.2f}")
        self._fields["add-checking"].focus()
        return

    from financials.commands import cmd_add

    _mutation, entry = cmd_add(
      iso_date=iso_date, description=description,
      category=category, amount=amount,
    )
    self.dismiss(entry.id if entry else None)

  def action_cancel(self) -> None:
    self.dismiss(None)


def _expected_id(iso_date: str, description: str) -> str | None:
  """The just-saved expected row's id (expected-store lookup by
  date+description; None when not found — the app then re-lands the
  cursor on its default anchor)."""
  from financials.model import load_expected

  for t in load_expected():
    if t.date == iso_date and t.description == description.strip():
      return t.id
  return None
