"""Shared test fixtures: journal + expected-store isolation.

Every test runs with the canonical journal resolved through
`financials.fixes.journal_file` pointed at a temporary directory — so
running the suite never appends to the owner's real journal — and with
the expected store pointed at a tmp file too: fix flows legitimately
READ the expected register (e.g. add_expected_row's id-uniqueness
check), and reading the owner's real store would make tests depend on
live data (or crash on its row shape). A test that wants the live
stores overrides these fixtures explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_journals(tmp_path, monkeypatch):
  """Point both journals (and the expected store) at tmp for every test,
  unless overridden."""
  monkeypatch.setattr("financials.fixes.journal_file", lambda: tmp_path / "journal.jsonl")
  monkeypatch.setattr(
    "financials.fixes.migration_journal_file",
    lambda: tmp_path / "migration" / "journal.jsonl",
  )
  monkeypatch.setattr("financials.model.expected_file", lambda: tmp_path / "expected.json")
