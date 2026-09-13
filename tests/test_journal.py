"""Tests for the in-memory change-set engine (apply_ops) and the
append-only journal.jsonl audit trail."""

import json

import pytest

from financials.delete import build_delete_fixes
from financials.fixes import apply_ops
from financials.model import STATUS_ACTUAL, Transaction, save_transactions


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


def _read_journal(path) -> list[dict]:
  with open(path, encoding="utf-8") as fh:
    return [json.loads(line) for line in fh if line.strip()]


def _isolate(tmp_path, monkeypatch):
  """Isolate the engine's store + journal to tmp files."""
  store = tmp_path / "transactions.json"
  journal = tmp_path / "journal.jsonl"
  save_transactions([_transaction()], path=store)

  def fake_load(path=store):
    return [Transaction(**row) for row in json.loads(store.read_text())]

  def fake_save(transactions, path=store):
    store.write_text(
      json.dumps([t.__dict__ for t in transactions], ensure_ascii=False)
    )

  monkeypatch.setattr("financials.fixes.load_transactions", fake_load)
  monkeypatch.setattr("financials.fixes.save_transactions", fake_save)
  monkeypatch.setattr("financials.fixes.journal_file", lambda: journal)
  return store, journal


def test_journal_appends_one_line_per_apply(tmp_path, monkeypatch):
  store, journal = _isolate(tmp_path, monkeypatch)

  apply_ops(build_delete_fixes(_transaction()), source="delete t9001")

  lines = _read_journal(journal)
  assert len(lines) == 1  # append-only: one line per apply
  record = lines[0]
  assert record["source"] == "delete t9001"
  assert record["ts"]
  assert record["fixes"]
  assert record["replay"]["checks"] >= 0
  assert isinstance(record["replay"]["mismatches"], int)


def test_journal_second_apply_appends(tmp_path, monkeypatch):
  """Append-only across applies: line 1 is untouched by line 2."""
  store, journal = _isolate(tmp_path, monkeypatch)

  apply_ops(
    [
      {
        "id": "t9001",
        "field": "description",
        "from": "Kruidenier",
        "to": "Bakker",
        "reason": "rename one",
      }
    ],
    source="edit t9001",
  )
  first = _read_journal(journal)
  apply_ops(
    [
      {
        "id": "t9001",
        "field": "description",
        "from": "Bakker",
        "to": "Slager",
        "reason": "rename two",
      }
    ],
    source="edit t9001",
  )
  lines = _read_journal(journal)
  assert len(lines) == 2
  assert lines[0] == first[0]  # append-only, never rewritten
  assert lines[0]["fixes"][0]["to"] == "Bakker"
  assert lines[1]["fixes"][0]["to"] == "Slager"


def test_journal_not_written_on_guard_failure(tmp_path, monkeypatch):
  store, journal = _isolate(tmp_path, monkeypatch)
  before = store.read_text()

  with pytest.raises(ValueError, match="expected current value"):
    apply_ops(
      [
        {
          "id": "t9001",
          "field": "description",
          "from": "does not match",
          "to": "whatever",
          "reason": "guard must abort",
        }
      ],
      source="edit t9001",
    )
  assert store.read_text() == before  # nothing saved
  assert not journal.exists()  # nothing journaled


def test_apply_ops_executes_field_op(tmp_path, monkeypatch):
  store, journal = _isolate(tmp_path, monkeypatch)

  apply_ops(
    [
      {
        "id": "t9001",
        "field": "description",
        "from": "Kruidenier",
        "to": "Bakker",
        "reason": "rename",
      }
    ],
    source="edit t9001",
  )
  rows = [Transaction(**row) for row in json.loads(store.read_text())]
  assert rows[0].description == "Bakker"
  assert len(_read_journal(journal)) == 1