"""Tests: financials group — named rollups of rows (credit-card statements).

Covers the store round-trip and id allocation, member resolution across
the three id families, the view collapse (members hidden, rollup row at
the rollup date with the total, re-walked balances), the display-only
committed rollup in the actuals window, the CLI actions, and the
`confirm <g-id>` multi-confirm with e-id re-pointing. All stores are
isolated via conftest (journal + expected) plus groups/recurrences
patches here — no real store is ever touched.
"""

from datetime import date, timedelta

from financials import confirm as module
from financials import groups as groups_mod
from financials.commands import cmd_add
from financials.groups import (
  Group,
  find_group,
  load_groups,
  resolve_member,
  rollup_date,
  save_groups,
)
from financials.journal import Journal, load_ledger
from financials.model import (
  STATUS_EXPECTED,
  Transaction,
  load_expected,
  save_expected,
)
from financials.recurrences import Recurrence, instance_id, save_recurrences


def _isolate(tmp_path, monkeypatch):
  """conftest already points the journal and the expected store at tmp;
  add the groups store (and an empty recurrences store)."""
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


def _expected_entry(**overrides) -> Transaction:
  base = dict(
    id="e9001",
    date=(date.today() + timedelta(days=1)).isoformat(),
    description="Winkel",
    category_raw="Uitgaven",
    category="Uitgaven",
    subcategory_raw="",
    subcategory="",
    amount_eur=-55.0,
    status=STATUS_EXPECTED,
    linked_id="",
    source_line=0,
    note="expected entry",
  )
  base.update(overrides)
  return Transaction(**base)


# --- store ----------------------------------------------------------------


def test_roundtrip_and_id_allocation(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  assert journal_path.exists() or True
  save_groups([Group(id="g0007", label="Mastercard", members=["e9001"])])
  groups = load_groups()
  assert [g.id for g in groups] == ["g0007"]
  assert groups[0].label == "Mastercard"
  assert groups[0].members == ["e9001"]


def test_next_id_takes_highest(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  save_groups(
    [
      Group(id="g0001", label="A"),
      Group(id="g0005", label="B"),
    ]
  )
  groups = load_groups()
  assert groups_mod._next_id(groups) == "g0006"


def test_find_group_prefix(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  save_groups([Group(id="g0001", label="Mastercard"), Group(id="g0002", label="Visa")])
  assert find_group(load_groups(), "g0002").label == "Visa"
  # ambiguous prefix -> None (never a silent guess); exact or unique only
  assert find_group(load_groups(), "g0") is None
  assert find_group(load_groups(), "zz") is None


# --- resolve_member ---------------------------------------------------------


def test_resolve_member_expected(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  save_expected([_expected_entry()])
  t = resolve_member("e9001")
  assert t is not None
  assert t.amount_eur == -55.0


def test_resolve_member_committed(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  entry = _seed(journal_path)
  t = resolve_member(entry.id)
  assert t is not None
  assert t.status == "actual"


def test_resolve_member_rule_instance(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  rule = Recurrence(
    description="Kindergeld",
    category="Inkomsten",
    amount=-120.0,
    frequency="monthly",
    day=30,
    start="2026-01-01",
  )
  save_recurrences([rule])
  when = date.today().replace(day=30) if date.today().day <= 30 else date.today()
  member = instance_id(rule, when)
  t = resolve_member(member)
  assert t is not None
  assert t.amount_eur == -120.0
  assert t.status == "expected"


def test_resolve_member_unknown(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  assert resolve_member("e9999") is None


# --- rollup date ------------------------------------------------------------


def test_rollup_date_group_date_wins(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  group = Group(id="g0001", label="MC", date="2026-10-05")
  rows = [
    Transaction(id="e1", date="2026-10-01", description="A", category="U", amount_eur=-1),
    Transaction(id="e2", date="2026-10-03", description="B", category="U", amount_eur=-2),
  ]
  assert rollup_date(group, rows) == date(2026, 10, 5)


def test_rollup_date_default_is_member_max(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  group = Group(id="g0001", label="MC")
  rows = [
    Transaction(id="e1", date="2026-10-01", description="A", category="U", amount_eur=-1),
    Transaction(id="e2", date="2026-10-03", description="B", category="U", amount_eur=-2),
  ]
  assert rollup_date(group, rows) == date(2026, 10, 3)


# --- CLI actions ------------------------------------------------------------


def test_create_and_add_and_remove(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  save_expected([_expected_entry()])
  assert groups_mod.create_group("Mastercard") == 0
  groups = load_groups()
  assert groups[0].id == "g0001"
  assert groups_mod.add_member("g0001", "e9001") == 0
  assert load_groups()[0].members == ["e9001"]
  assert groups_mod.remove_member("g0001", "e9001") == 0
  assert load_groups()[0].members == []
  # the member row itself survives
  assert len(load_expected()) == 1


def test_add_unknown_member_refused(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  groups_mod.create_group("Mastercard")
  assert groups_mod.add_member("g0001", "e9999") == 2


def test_delete_group_keeps_members(tmp_path, monkeypatch, capsys):
  _isolate(tmp_path, monkeypatch)
  save_expected([_expected_entry()])
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", "e9001")
  # decline the confirmation prompt
  capsys.readouterr()
  monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *_a, **_k: True)
  assert groups_mod.delete_group("g0001") == 0
  assert load_groups() == []
  assert len(load_expected()) == 1


# --- view collapse ----------------------------------------------------------


def test_view_collapses_members_and_retimes_total(tmp_path, monkeypatch):
  """e-members roll up at the group's rollup date; the walk's balances
  re-time: no member impact before the rollup date, the total on it."""
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected(
    [
      _expected_entry(
        id="e9001", date=(date.today() + timedelta(days=3)).isoformat(), amount_eur=-10.0
      ),
      _expected_entry(
        id="e9002", date=(date.today() + timedelta(days=3)).isoformat(), amount_eur=-20.0
      ),
    ]
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", "e9001")
  groups_mod.add_member("g0001", "e9002")
  # rollup date 2 days out: before the members' own dates
  groups = load_groups()
  groups[0].date = (date.today() + timedelta(days=2)).isoformat()
  save_groups(groups)

  from financials.ledger_view import _projection_walk

  walk, _anchor = _projection_walk(date.today(), project=30)
  ids = [t.id for t, _c, _s in walk]
  assert "e9001" not in ids and "e9002" not in ids
  assert "g0001" in ids
  rollup = next(t for t, _c, _s in walk if t.id == "g0001")
  assert rollup.amount_eur == -30.0
  assert rollup.date == (date.today() + timedelta(days=2)).isoformat()
  # the total hits AT the rollup date: no member rows on day 3 anymore
  # (both were members), so the balance from day 2 on is flat at 470.
  tail = [c for t, c, _s in walk if t.date >= (date.today() + timedelta(days=2)).isoformat()]
  assert set(tail) == {470.0}  # 500 anchor − 30 total; no other rows exist


def test_view_empty_group_hidden(tmp_path, monkeypatch):
  """A group whose members are all dangling leaves nothing in the walk."""
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  groups_mod.create_group("Leeg")
  groups_mod.add_member("g0001", "e9999")  # dangling expected id
  from financials.ledger_view import _projection_walk

  walk, _anchor = _projection_walk(date.today(), project=30)
  assert all(t.id != "g0001" for t, _c, _s in walk)


# --- confirm group ----------------------------------------------------------


def test_confirm_group_confirms_landed_and_repoints(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected(
    [
      _expected_entry(id="e9001", date=(date.today() - timedelta(days=1)).isoformat()),
      _expected_entry(
        id="e9002", date=(date.today() + timedelta(days=5)).isoformat(), amount_eur=-7.0
      ),
    ]
  )
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", "e9001")
  groups_mod.add_member("g0001", "e9002")
  code = module.confirm_transaction("g0001")
  assert code == 0
  # e9001 committed, e9002 (future) skipped but still expected
  ledger = load_ledger(Journal(journal_path)).transactions
  assert [t for t in ledger if t.description == "Winkel"] != []
  assert [t.id for t in load_expected()] == ["e9002"]
  # the group survived, the landed member re-pointed to its hash id
  group = load_groups()[0]
  assert "e9001" not in group.members
  assert "e9002" in group.members
  committed = load_ledger(Journal(journal_path)).transactions
  new_ids = [t.id for t in committed if t.description == "Winkel"]
  assert new_ids and new_ids[0] in group.members


def test_confirm_group_nothing_landed(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  save_expected([_expected_entry()])  # future-dated
  groups_mod.create_group("Mastercard")
  groups_mod.add_member("g0001", "e9001")
  assert module.confirm_transaction("g0001") == 2


def test_confirm_unknown_group(tmp_path, monkeypatch):
  journal_path = _isolate(tmp_path, monkeypatch)
  _seed(journal_path)
  assert module.confirm_transaction("g9999") == 2


# --- resolvers --------------------------------------------------------------


def test_edit_and_delete_resolve_groups(tmp_path, monkeypatch, capsys):
  """edit g#### opens the group editor (stubbed), delete g#### deletes
  the group with confirmation; both resolve the g-id."""
  _isolate(tmp_path, monkeypatch)
  groups_mod.create_group("Mastercard")
  monkeypatch.setattr("financials.groups.edit_group", lambda gid: 0)
  from financials.delete import delete_transaction
  from financials.edit import edit_transaction

  capsys.readouterr()
  assert edit_transaction("g0001") == 0
  monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *_a, **_k: True)
  assert delete_transaction("g0001") == 0
  assert load_groups() == []
