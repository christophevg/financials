"""Tests: edit-to-today-or-past on an expected row confirms it into the
actual register (add with chaining, then expected removal)."""

import functools
from datetime import date, timedelta

from financials import edit as module
from financials.model import (
  STATUS_ACTUAL,
  STATUS_EXPECTED,
  Transaction,
  load_expected,
  load_transactions,
  save_expected,
  save_transactions,
)


def _expected_entry(**overrides) -> Transaction:
  base = dict(
    id="e9001",
    date=(date.today() + timedelta(days=1)).isoformat(),
    description="Cadeau",
    category_raw="Uitgaven",
    category="Uitgaven",
    subcategory_raw="",
    subcategory="",
    amount_eur=-750.0,
    status=STATUS_EXPECTED,
    linked_id="",
    balance_checking=None,
    balance_savings=None,
    flags=[],
    source_line=0,
    note="expected entry",
  )
  base.update(overrides)
  return Transaction(**base)


def _anchor() -> Transaction:
  return Transaction(
    id="t9001",
    date=(date.today() - timedelta(days=1)).isoformat(),
    description="Anker",
    category_raw="Eten",
    category="Eten",
    subcategory_raw="",
    subcategory="",
    amount_eur=-100.0,
    status=STATUS_ACTUAL,
    linked_id="",
    balance_checking=500.0,
    balance_savings=6543.21,
  )


def _isolate(tmp_path, monkeypatch):
  """Point edit.py's store calls at isolated tmp files."""
  transactions_path = tmp_path / "transactions.json"
  expected_path = tmp_path / "expected.json"
  save_transactions([], path=transactions_path)
  save_expected([], path=expected_path)
  monkeypatch.setattr(
    module, "load_transactions", functools.partial(load_transactions, transactions_path)
  )
  monkeypatch.setattr(
    module, "save_transactions", functools.partial(save_transactions, path=transactions_path)
  )
  monkeypatch.setattr(
    module, "load_expected", functools.partial(load_expected, path=expected_path)
  )
  monkeypatch.setattr(
    module, "save_expected", functools.partial(save_expected, path=expected_path)
  )
  return transactions_path, expected_path


def test_confirm_creates_actual_and_removes_expected(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  module.save_transactions([_anchor()])
  module.save_expected([_expected_entry()])

  entry = module.load_expected()[0]
  result = module._confirm_to_actual(entry, {"date": date.today().isoformat()})
  assert result == 0

  transactions = module.load_transactions()
  assert len(transactions) == 2
  confirmed = transactions[-1]
  assert confirmed.description == "Cadeau"
  assert confirmed.amount_eur == -750.0
  assert confirmed.linked_id == "e9001"  # provenance link
  assert confirmed.balance_checking == -250.0  # 500 - 750, chained
  assert module.load_expected() == []  # expected row dropped


def test_confirm_applies_amount_correction(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)
  module.save_transactions([_anchor()])
  module.save_expected([_expected_entry()])

  entry = module.load_expected()[0]
  result = module._confirm_to_actual(
    entry, {"date": date.today().isoformat(), "amount_eur": -700.0}
  )
  assert result == 0
  confirmed = module.load_transactions()[-1]
  assert confirmed.amount_eur == -700.0
  assert confirmed.balance_checking == -200.0  # 500 - 700


def _all_enter(monkeypatch):
  """Stub the prompts so every question is answered with Enter (= default)."""
  monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: k.get("default", ""))
  monkeypatch.setattr(
    module, "ask_category", lambda current="", allow_blank=False: current
  )


def test_confirm_rejects_when_no_chain(tmp_path, monkeypatch):
  _isolate(tmp_path, monkeypatch)  # no transactions: nothing to chain from
  entry = _expected_entry()
  module.save_expected([entry])

  result = module._confirm_to_actual(entry, {"date": date.today().isoformat()})
  assert result == 2
  assert len(module.load_expected()) == 1  # untouched on failure


def test_confirm_without_changes_converts(tmp_path, monkeypatch):
  """All-Enter on a landed (today) expected row confirms it outright."""
  _isolate(tmp_path, monkeypatch)
  _all_enter(monkeypatch)
  module.save_transactions([_anchor()])
  module.save_expected([_expected_entry(date=date.today().isoformat())])

  entry = module.load_expected()[0]
  result = module._edit_expected(entry)
  assert result == 0
  transactions = module.load_transactions()
  assert len(transactions) == 2
  assert transactions[-1].amount_eur == -750.0
  assert transactions[-1].linked_id == "e9001"
  assert module.load_expected() == []