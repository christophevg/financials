"""Regression: a fully-committed group never rendered in the actuals
window — the rollup gate required a member row in `actuals`, but
build_view already hid all members of fully-committed groups
(hidden_committed), so both member and rollup vanished from `list` and
the TUI while the ledger itself held the rows.

The rollup is the display unit: the renderer places it where its rollup
date falls (actuals window or, TUI-only, history) and must not gate on
member presence. build_view itself is unchanged: members hidden, rollup
handed over in `view.rollups`.
"""

from datetime import date, timedelta

from financials import groups as groups_mod
from financials.commands import cmd_add
from financials.journal import Journal
from financials.ledger_view import build_view


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
  _mutation, entry = cmd_add(
    date.today().isoformat(),
    "Winkel",
    "Uitgaven",
    -40.0,
    journal=Journal(journal_path),
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", entry.id)

  # Data level: member hidden, rollup handed over with its balances.
  view = build_view(4, 30, None)
  assert all(t.id != entry.id for t in view.actuals)
  rollup, members = view.rollups[0]
  assert rollup.id == "g0001"
  assert rollup.description == "📁 Mastercard (1)"
  assert rollup.amount_eur == -40.0
  assert rollup.date == date.today().isoformat()
  assert [t.id for t in members] == [entry.id]
  # Balances: the last member's — the rollup mirrors the member's own
  # row (the total landed with it), so compare via the handed-over
  # member row, not the journal mutation shape.
  assert rollup.balance_checking == members[0].balance_checking
  assert rollup.balance_savings == members[0].balance_savings

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
  _mutation, entry = cmd_add(
    (date.today() - timedelta(days=10)).isoformat(),
    "Winkel",
    "Uitgaven",
    -12.34,
    journal=Journal(journal_path),
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", entry.id)

  view = build_view(4, 30, None)
  # List: the rollup date is outside the actuals window — nothing new
  # there (the anchor stays); the rollup lives in view.rollups only.
  assert [t.description for t in view.actuals] == ["Anker"]
  assert view.rollups[0][0].id == "g0001"
  assert view.rollups[0][0].date == (date.today() - timedelta(days=10)).isoformat()
  # TUI band placement mirrors the renderer gates: history carries it.
  window_start = date.today() - timedelta(days=4)
  assert date.fromisoformat(view.rollups[0][0].date) < window_start
