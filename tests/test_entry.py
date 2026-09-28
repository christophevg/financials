"""Tests: `financials add` against the journaled ledger (step 6): every
add appends an AddMutation and the engine applies it — chaining is the
engine's, not the command's."""

from datetime import date, timedelta

from financials import entry as module
from financials.commands import cmd_add, postings_for
from financials.journal import Journal, load_ledger


def _journal(tmp_path):
  return Journal(tmp_path / "journal.jsonl")


def test_postings_for_plain_and_overdracht():
  assert postings_for("Eten", -25.0) == {"checking": -25.0}
  assert postings_for("Overdracht", 100.0) == {"checking": 100.0, "savings": -100.0}


def test_cmd_add_chains_and_dates(tmp_path):
  journal = _journal(tmp_path)
  mutation, first = cmd_add("2026-01-01", "Opening", "Inkomsten", 1000.0, journal=journal)
  assert first is not None
  assert first.balances == {"checking": 1000.0}
  # Sparen = money leaves checking: a NEGATIVE Overdracht amount.
  _mutation, second = cmd_add("2026-01-05", "Sparen", "Overdracht", -100.0, journal=journal)
  assert second is not None
  # The transfer moves both accounts (mirror posting).
  assert second.balances == {"checking": 900.0, "savings": 100.0}
  # Date-positioned insert: a backdated add propagates through the tail.
  # (2026-01-03 chains from the opening; the 01-05 row re-derives.)
  _mutation, backdated = cmd_add("2026-01-03", "Tussin", "Eten", -50.0, journal=journal)
  assert backdated is not None
  assert backdated.balances["checking"] == 950.0
  reloaded = load_ledger(journal)
  tail = [t for t in reloaded.transactions if t.date.isoformat() >= "2026-01-05"]
  assert tail[0].balances["checking"] == 850.0
  # ids are content hashes (no t#### minting anymore)
  assert len(mutation.id) == 16


def test_cmd_add_backdated_overdracht_propagates_savings(tmp_path):
  journal = _journal(tmp_path)
  cmd_add("2026-01-01", "Opening", "Inkomsten", 1000.0, journal=journal)
  cmd_add("2026-01-05", "Sparen", "Overdracht", -200.0, journal=journal)
  _mutation, backdated = cmd_add(
    "2026-01-03", "Sparen eerder", "Overdracht", -100.0, journal=journal
  )
  # 01-03 chains from the opening: -100 out of checking, +100 into savings.
  assert backdated.balances == {"checking": 900.0, "savings": 100.0}
  # The 01-05 row re-derives: another -200 out, +200 in.
  tail = [t for t in load_ledger(journal).transactions if t.date.isoformat() == "2026-01-05"]
  assert tail[0].balances == {"checking": 700.0, "savings": 300.0}


def test_validate_rejects_future_and_category(tmp_path):
  journal = _journal(tmp_path)
  cmd_add("2026-01-01", "Opening", "Inkomsten", 100.0, journal=journal)
  from financials.entry import _validate

  future = (date.today() + timedelta(days=3)).isoformat()
  assert "toekomst" in _validate(future, "x", "Eten", -5.0, load_ledger(journal))
  assert "niet in de goedgekeurde" in _validate(
    "2026-01-02", "x", "Niet-Bestaand", -5.0, load_ledger(journal)
  )
  assert "niet leeg" in _validate("2026-01-02", "  ", "Eten", -5.0, load_ledger(journal))
  assert "niet toegelaten" in _validate("2026-01-02", "x", "Eten", 0.0, load_ledger(journal))
  assert "ongeldige datum" in _validate("2026-13-40", "x", "Eten", -5.0, load_ledger(journal))


def test_add_transaction_flagged_path(monkeypatch, tmp_path):
  journal = _journal(tmp_path)
  monkeypatch.setattr(module, "command_journal", lambda: journal)
  iso = (date.today() - timedelta(days=1)).isoformat()
  code = module.add_transaction(iso_date=iso, description="Test", category="Eten", amount=-12.34)
  assert code == 0
  ledger = load_ledger(journal)
  added = [t for t in ledger.transactions if t.description == "Test"]
  assert len(added) == 1
  assert added[0].postings == {"checking": -12.34}


def test_add_transaction_future_routes_to_expected(monkeypatch, tmp_path):
  journal = _journal(tmp_path)
  monkeypatch.setattr(module, "command_journal", lambda: journal)
  captured = {}

  def fake_expected(iso_date, description, category, amount, dry_run=False):
    captured["args"] = (iso_date, description, category, amount, dry_run)
    return 0

  monkeypatch.setattr(module, "add_expected", fake_expected)
  future = (date.today() + timedelta(days=3)).isoformat()
  code = module.add_transaction(
    iso_date=future, description="Cadeau", category="Uitgaven", amount=-100.0
  )
  assert code == 0
  assert captured["args"][0] == future
  # nothing appended to the actual ledger
  assert all(t.description != "Cadeau" for t in load_ledger(journal).transactions)


# --- expected placeholder: 0 is a legal amount -----------------------------


def test_add_expected_accepts_zero_amount_placeholder(tmp_path, capsys):
  """A 0-amount expected entry is a PLACEHOLDER: reserve the known future
  date now, fill in the real amount when it lands (owner workflow, the
  e0008 case). `financials expect` stores it unchanged."""
  from financials.expected import add_expected
  from financials.model import load_expected

  future = (date.today() + timedelta(days=5)).isoformat()
  code = add_expected(
    iso_date=future, description="Zaai-aarde (placeholder)", category="Huis", amount=0.0
  )
  assert code == 0
  rows = load_expected()
  assert len(rows) == 1
  assert rows[0].amount_eur == 0.0
  assert "Opgeslagen" in capsys.readouterr().out


def test_add_expected_still_requires_an_amount(tmp_path, capsys):
  """amount None stays rejected (bedrag ontbreekt) — the loosening is the
  VALUE 0, not an absent amount."""
  from financials.expected import add_expected

  future = (date.today() + timedelta(days=5)).isoformat()
  code = add_expected(
    iso_date=future,
    description="X",
    category="Eten",
    amount=None,  # type: ignore[arg-type]
  )
  assert code == 2
  assert "bedrag ontbreekt" in capsys.readouterr().out


def test_dry_run_appends_nothing(monkeypatch, tmp_path, capsys):
  journal = _journal(tmp_path)
  monkeypatch.setattr(module, "command_journal", lambda: journal)
  cmd_add("2026-01-01", "Opening", "Inkomsten", 100.0, journal=journal)
  iso = (date.today() - timedelta(days=1)).isoformat()
  code = module.add_transaction(
    iso_date=iso, description="Droog", category="Eten", amount=-1.0, dry_run=True
  )
  assert code == 0
  assert len(list(journal)) == 1  # only the opening line
