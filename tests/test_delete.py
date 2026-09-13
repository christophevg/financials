"""Unit tests for the financials delete command."""

import json

import pytest

from financials.delete import build_delete_fixes
from financials.model import STATUS_ACTUAL, STATUS_EXPECTED, Transaction


def _transaction(**overrides) -> Transaction:
  base = dict(
    id="t9001",
    date="2026-03-15",
    description="Kruidenier",
    category_raw="Eten",
    category="Eten",
    subcategory_raw="",
    subcategory="",
    amount_eur=-40.0,
    status=STATUS_ACTUAL,
    linked_id="",
    balance_checking=1234.56,
    balance_savings=6543.21,
  )
  base.update(overrides)
  return Transaction(**base)


def test_build_delete_fixes_single_remove_row():
  fixes = build_delete_fixes(_transaction())
  assert [f["op"] for f in fixes] == ["remove_row", "rebase_checking"]
  assert fixes[0]["id"] == "t9001"
  assert all(f.get("reason") for f in fixes)
  # Global op must carry the "all" routing id (apply_fixes convention).
  assert fixes[1]["id"] == "all"


def test_write_journal_roundtrip(tmp_path):
  """The change-set shape is stable: remove_row + rebase, correct routing."""
  fixes = build_delete_fixes(_transaction())
  assert [f["op"] for f in fixes] == ["remove_row", "rebase_checking"]
  assert fixes[0]["id"] == "t9001"
  assert fixes[1]["id"] == "all"


def test_build_delete_fixes_expected_id():
  fixes = build_delete_fixes(
    _transaction(id="e9004", status=STATUS_EXPECTED,
                 balance_checking=None, balance_savings=None)
  )
  assert fixes[0]["id"] == "e9004"


def test_confirmation_rejects_bare_enter(monkeypatch):
  from financials import delete as delete_module

  answers = iter([""])
  monkeypatch.setattr(
    delete_module.Prompt, "ask", lambda *a, **k: next(answers)
  )
  assert delete_module._confirmed(_transaction()) is False


def test_confirmation_accepts_ja(monkeypatch):
  from financials import delete as delete_module

  monkeypatch.setattr(
    delete_module.Prompt, "ask", lambda *a, **k: "ja"
  )
  assert delete_module._confirmed(_transaction()) is True


def test_confirmation_accepts_y(monkeypatch):
  from financials import delete as delete_module

  monkeypatch.setattr(
    delete_module.Prompt, "ask", lambda *a, **k: "y"
  )
  assert delete_module._confirmed(_transaction()) is True


def test_confirmation_rejects_random_text(monkeypatch):
  from financials import delete as delete_module

  monkeypatch.setattr(
    delete_module.Prompt, "ask", lambda *a, **k: "nee"
  )
  assert delete_module._confirmed(_transaction()) is False