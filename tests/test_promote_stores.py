"""Tests: the data-promotion script (step 6, D1–D6) — exercised against a
synthetic data dir; the real promotion is owner-run (their data dir is
outside the agent sandbox). Locks the guards: expected-drift refusal,
rules byte-identity refusal, order-independent post-verification, and
that the staging dir disappears when everything promoted."""

import json

import pytest

from financials.journal import AddMutation, Journal


@pytest.fixture()
def fake_data_dir(tmp_path, monkeypatch):
  """A synthetic <data_dir> with the promotion's starting state."""
  from financials import config

  root = tmp_path / "data"
  staging = root / "migration"
  staging.mkdir(parents=True)

  journal_path = staging / "journal.jsonl"
  journal = Journal(journal_path)
  journal.append(
    AddMutation(
      date="2026-01-01",
      description="Opening",
      category="Inkomsten",
      postings={"checking": 1000.0, "savings": 900.0},
      ts="2026-01-01T00:00:00",
    )
  )

  (staging / "ledger.json").write_text(json.dumps({"tip": "", "entries": []}))
  (staging / "expected.json").write_text(json.dumps([{"id": "e0001", "date": "2026-01-01"}]))
  (staging / "rules.json").write_text(json.dumps({"frequency": "monthly"}))

  (root / "journal.jsonl").write_text('{"op": "add", "description": "old audit"}\n')
  (root / "transactions.json").write_text(json.dumps([{"id": "t0001"}]))
  (root / "expected.json").write_text(json.dumps([{"id": "e0001", "date": "2026-01-01"}]))
  (root / "recurrences.json").write_text(json.dumps({"frequency": "monthly"}))

  monkeypatch.setattr(config, "data_dir", lambda: root)
  return root


def _run(fake_data_dir, argv):
  import importlib.util
  import sys
  from pathlib import Path

  module_path = Path(__file__).resolve().parent.parent / "scripts" / "promote_stores.py"
  spec = importlib.util.spec_from_file_location("promote_stores", module_path)
  module = importlib.util.module_from_spec(spec)
  sys.modules["promote_stores"] = module
  spec.loader.exec_module(module)
  return module.main(argv)


def test_dry_run_moves_nothing(fake_data_dir):
  assert _run(fake_data_dir, []) == 0
  assert (fake_data_dir / "migration/journal.jsonl").exists()
  assert (fake_data_dir / "transactions.json").exists()


def test_promotion_moves_and_verifies(fake_data_dir):
  assert _run(fake_data_dir, ["--write"]) == 0
  # promoted
  assert (fake_data_dir / "journal.jsonl").exists()
  assert (fake_data_dir / "ledger.json").exists()
  assert (fake_data_dir / "expected.json").exists()
  # retired
  backups = fake_data_dir / "backups"
  assert list(backups.glob("journal.jsonl.retired-*"))
  assert list(backups.glob("transactions.json.retired-*"))
  assert list(backups.glob("expected.json.retired-*"))
  # staging gone
  assert not (fake_data_dir / "migration").exists()
  # recurrences stayed put
  assert (fake_data_dir / "recurrences.json").exists()
  # second run: nothing to do
  assert _run(fake_data_dir, ["--write"]) == 0


def test_promotion_refuses_on_expected_drift(fake_data_dir):
  # A live-only expected row appeared after the bootstrap baseline.
  (fake_data_dir / "expected.json").write_text(
    json.dumps([{"id": "e0001", "date": "2026-01-01"}, {"id": "e0002", "date": "2026-02-01"}])
  )
  assert _run(fake_data_dir, ["--write"]) == 1
  # nothing moved
  assert (fake_data_dir / "migration/journal.jsonl").exists()
  assert (fake_data_dir / "transactions.json").exists()


def test_promotion_refuses_on_rules_divergence(fake_data_dir):
  (fake_data_dir / "migration/rules.json").write_text(json.dumps({"frequency": "yearly"}))
  assert _run(fake_data_dir, ["--write"]) == 1
  assert (fake_data_dir / "migration/rules.json").exists()


def test_promotion_promotes_rules_when_live_store_absent(fake_data_dir):
  # No recurrences.json (no CLI yet, store never created); the staging
  # copy holds rules — it is the ONLY copy and must be promoted.
  (fake_data_dir / "recurrences.json").unlink()
  (fake_data_dir / "migration/rules.json").write_text(
    json.dumps([{"description": "Huur", "category": "Wonen", "amount": 800.0}])
  )
  assert _run(fake_data_dir, ["--write"]) == 0
  assert (fake_data_dir / "recurrences.json").exists()
  assert "Huur" in (fake_data_dir / "recurrences.json").read_text()


def test_promotion_deletes_empty_rules_when_live_store_absent(fake_data_dir):
  (fake_data_dir / "recurrences.json").unlink()
  (fake_data_dir / "migration/rules.json").write_text("[]")
  assert _run(fake_data_dir, ["--write"]) == 0
  assert not (fake_data_dir / "recurrences.json").exists()
  assert not (fake_data_dir / "migration").exists()


def test_promotion_promotes_dict_rules_when_live_store_absent(fake_data_dir):
  # (Was: REFUSED on missing recurrences.json — redesigned: missing ==
  # empty for load_recurrences, so a non-empty staging copy promotes.)
  (fake_data_dir / "recurrences.json").unlink()
  assert _run(fake_data_dir, ["--write"]) == 0
  assert (fake_data_dir / "recurrences.json").exists()
  assert "monthly" in (fake_data_dir / "recurrences.json").read_text()
