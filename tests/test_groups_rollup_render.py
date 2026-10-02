"""Regression: a fully-committed group never rendered in the actuals
window — the rollup gate required a member row in `actuals`, but
build_view already hid all members of fully-committed groups
(hidden_committed), so both member and rollup vanished from `list` and
the TUI while the ledger itself held the rows.

The rollup is the display unit: build_view seats it into the actuals
AT the chain position of the last member in chain order (the point
where the total landed) — the renderer places nothing; whatever sits
after it (e.g. Bankkosten committed after the members) renders below
the rollup, exactly as the ledger orders it. The TUI's history band
keeps its own pre-window rollup placement in view.rollups.
"""

from datetime import date, timedelta

from financials import groups as groups_mod
from financials.commands import cmd_add
from financials.journal import Journal
from financials.ledger_view import build_view, view_rows


def _isolate(tmp_path, monkeypatch):
  """Same store isolation as test_groups._isolate: conftest covers
  journal + expected; groups + recurrences point at tmp here."""
  from financials.groups import save_groups
  from financials.recurrences import save_recurrences

  groups_path = tmp_path / "groups.json"
  rec_path = tmp_path / "recurrences.json"
  save_groups([], path=groups_path)
  save_recurrences([], path=rec_path)
  monkeypatch.setattr("financials.model.groups_file", lambda: groups_path)
  monkeypatch.setattr("financials.recurrences.recurrences_file", lambda: rec_path)
  return tmp_path / "journal.jsonl"


def _seed(journal):
  """One committed anchor row (+500 at today-1) — the balance seed."""
  _mutation, entry = cmd_add(
    (date.today() - timedelta(days=1)).isoformat(),
    "Anker",
    "Inkomsten",
    500.0,
    journal=Journal(journal),
  )
  return entry


def test_fully_committed_group_rollup_renders_in_actuals(tmp_path, monkeypatch, capsys):
  """The owner's repro: member committed today, group over it — `list`
  shows the 📁 rollup, not neither-member-nor-rollup."""
  from financials.ledger_view import print_ledger_view

  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  _mutation, _entry = cmd_add(
    date.today().isoformat(),
    "Winkel",
    "Uitgaven",
    -40.0,
    journal=Journal(journal_path),
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", _entry.id)

  # Data level: member hidden, rollup SEATED into the actuals after the
  # Anker seed (the member's chain position); view.rollups is retired
  # (placement moved into build_view — both renderers consume pre-seated).
  view = build_view(4, 30, None)
  member = next(t for t in view_rows() if t.id == _entry.id)
  assert all(t.id != _entry.id for t in view.actuals)
  assert [t.description for t in view.actuals] == ["Anker", "📁 Mastercard (1)"]
  rollup = view.actuals[1]
  assert rollup.id == "g0001"
  assert rollup.amount_eur == -40.0
  assert rollup.date == date.today().isoformat()
  assert view.rollups == []
  # Balances: the seat member's — the rollup mirrors the member's own row
  # (the total landed with it).
  assert rollup.balance_checking == member.balance_checking
  assert rollup.balance_savings == member.balance_savings

  # Renderer level (the actual bug): the rollup row prints, the member
  # is collapsed away.
  print_ledger_view(4, 30, None)
  out = capsys.readouterr().out
  # capsys renders at 80 cols (the label wraps mid-word): match tokens.
  assert "g0001" in out
  assert "Mastercard" in out
  assert "-40.00" in out
  assert "Winkel" not in out  # the member stays collapsed


def test_fully_committed_group_rollup_older_than_window(tmp_path, monkeypatch):
  """A fully-committed group older than the actuals window: no rollup in
  the list actuals, and the TUI's history band carries it (window gate
  on the rollup date, not the members)."""
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  _mutation, _entry = cmd_add(
    (date.today() - timedelta(days=10)).isoformat(),
    "Winkel",
    "Uitgaven",
    -12.34,
    journal=Journal(journal_path),
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", _entry.id)

  view = build_view(4, 30, None)
  member = next(t for t in view_rows() if t.id == _entry.id)
  # List: the rollup date is outside the actuals window — the rollup
  # bands into history (the TUI's band), the list shows only the anchor.
  assert [t.description for t in view.actuals] == ["Anker"]
  assert view.rollups == []
  assert [t.description for t in view.history] == ["📁 Mastercard (1)"]
  assert view.history[0].id == "g0001"
  assert view.history[0].date == (date.today() - timedelta(days=10)).isoformat()
  # TUI band placement mirrors the renderer gates: history carries it.
  window_start = date.today() - timedelta(days=4)
  assert date.fromisoformat(view.history[0].date) < window_start
  # The seat member's balance: the total landed with it.
  assert view.history[0].balance_checking == member.balance_checking


def test_rollup_seat_respects_member_chain_position(tmp_path, monkeypatch, capsys):
  """The owner's ordering report: a NON-member row (Bankkosten) committed
  on the same date but chained AFTER the group's members must render
  BELOW the rollup — the rollup takes the member's seat in the chain,
  not the bottom of the list. Before the fix the rollup was appended
  after all actuals, displaying an earlier chain balance under a later
  balance and reading as if Bankkosten's balance were already stale."""
  from financials.ledger_view import print_ledger_view

  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  # Members committed first (chain positions 2..3), then the non-member
  # (chain position 4) — same date, so chain order decides.
  _m, member_a_journal = cmd_add(
    date.today().isoformat(),
    "Winkel",
    "Uitgaven",
    -20.0,
    journal=Journal(journal_path),
  )
  _m, member_b_journal = cmd_add(
    date.today().isoformat(),
    "Cafe",
    "Uitgaven",
    -30.0,
    journal=Journal(journal_path),
  )
  _m, after = cmd_add(
    date.today().isoformat(),
    "Bankkosten",
    "Uitgaven",
    -4.25,
    journal=Journal(journal_path),
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", member_a_journal.id, member_b_journal.id)

  view = build_view(4, 30, None)
  members = {t.id: t for t in view_rows()}
  member_b = members[member_b_journal.id]
  # Seat = last member's chain position; the seed + non-member frame it.
  assert [t.description for t in view.actuals] == [
    "Anker",
    "📁 Mastercard (2)",
    "Bankkosten",
  ]
  rollup = view.actuals[1]
  assert rollup.id == "g0001"
  assert rollup.amount_eur == -50.0
  assert rollup.balance_checking == member_b.balance_checking

  print_ledger_view(4, 30, None)
  out = capsys.readouterr().out
  # The 80-col capsys width truncates descriptions ("Bankkost…"), so
  # assert on prefixes; the point is the ORDER (rollup above Bankkosten).
  assert out.upper().index("MASTERCARD") < out.index("Bankkost")
