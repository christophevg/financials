"""Tests: the event journal engine — mutations, fold, checkpoint, boot,
remint — plus live gates on the owner's real journal. The old-path
observer tests (steps 2/5b) were removed at step 6: the write path is
journal-native now (see test_fixes.py for the fix-file engine)."""

import hashlib
import json

import pytest

from financials.journal import (
  AddMutation,
  ConfirmMutation,
  Date,
  DeleteMutation,
  Journal,
  UpdateMutation,
  fold,
)


def _read_journal(path) -> list[dict]:
  with open(path, encoding="utf-8") as fh:
    return [json.loads(line) for line in fh if line.strip()]


def _write_journal(path, mutations) -> Journal:
  """A synthetic journal from mutation objects (append mints content-hash
  ids exactly as the live path does — one hash scheme end-to-end)."""
  path.parent.mkdir(parents=True, exist_ok=True)
  journal = Journal(path)
  for mutation in mutations:
    journal.append(mutation)
  return journal


def test_delete_mutation_preimage_match_reverses():
  """A delete mutation carrying the victim's pre-image reverses the
  row: balances propagate back and the row is gone."""
  from financials.journal import Ledger, Transaction

  ledger = Ledger()
  ledger.insert(
    Transaction(
      id="a1",
      date="2026-01-01",
      description="first",
      category="Eten",
      postings={"checking": -10.0},
    )
  )
  second = ledger.insert(
    Transaction(
      id="a2",
      date="2026-01-05",
      description="second",
      category="Eten",
      postings={"checking": -5.0},
    )
  )
  assert second is not None
  assert second.balances["checking"] == -15.0

  deleted = ledger.apply(
    DeleteMutation(
      date="2026-01-05",
      description="second",
      category="Eten",
      postings={"checking": -5.0},
      target="a2",
    )
  )
  assert deleted is not None
  assert ledger.find("a2")[0] is None
  assert ledger.transactions[-1].balances["checking"] == -10.0


def test_delete_mutation_preimage_mismatch_refuses():
  """A stale/wrong pre-image stops loudly — never a silent delete."""
  from financials.journal import Ledger, Transaction

  ledger = Ledger()
  ledger.insert(
    Transaction(
      id="a1",
      date="2026-01-01",
      description="original",
      category="Eten",
      postings={"checking": -10.0},
    )
  )
  with pytest.raises(ValueError, match="pre-image mismatch"):
    ledger.apply(
      DeleteMutation(
        date="2026-01-01",
        description="DIFFERENT",
        category="Eten",
        postings={"checking": -10.0},
        target="a1",
      )
    )
  assert ledger.find("a1")[0] is not None  # row survives the refusal


# --- Roadmap step 3: the canonical fold (synthetic journals) ------------


def _write_journal(path, mutations) -> Journal:
  """A synthetic journal from mutation objects (append mints content-hash
  ids exactly as the live path does — one hash scheme end-to-end)."""
  path.parent.mkdir(parents=True, exist_ok=True)
  journal = Journal(path)
  for mutation in mutations:
    journal.append(mutation)
  return journal


def test_fold_add_journal(tmp_path):
  """fold(adds) == ledger state: two adds, one a two-posting transfer;
  balances propagate in date order, every row verifies, tip = last id."""
  journal = _write_journal(
    tmp_path / "j.jsonl",
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 1000.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Sparen",
        category="Overdracht",
        postings={"checking": -100.0, "savings": 100.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  ids = [mutation.id for mutation in journal]
  ledger, tip = fold(journal)
  assert [t.id for t in ledger.transactions] == ids
  assert ledger.transactions[0].balances == {"checking": 1000.0}
  assert ledger.transactions[1].balances == {"checking": 900.0, "savings": 100.0}
  assert all(transaction.verify() for transaction in ledger.transactions)
  assert tip == ids[-1]


def test_fold_confirm_and_update(tmp_path):
  """confirm lands a real entry (retires ride along as linked); the landed
  entry's id is the confirm mutation's own id — a later update therefore
  targets THAT id, re-positions by date, and balances re-derive. Tip is
  the update's own id (last applied in journal order, not an entry id)."""
  journal = _write_journal(
    tmp_path / "j.jsonl",
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 500.0},
        ts="2026-01-01T00:00:00",
      ),
      ConfirmMutation(
        date="2026-01-10",
        description="Cadeau",
        category="Inkomsten",
        postings={"checking": 250.0},
        retires="e0042",
        ts="2026-01-10T00:00:00",
      ),
    ],
  )
  landed_id = list(journal)[-1].id
  journal.append(
    UpdateMutation(
      target=landed_id,
      date="2026-01-03",
      description="Cadeau (verplaatst)",
      category="Inkomsten",
      postings={"checking": 250.0},
      ts="2026-01-11T00:00:00",
    )
  )
  update_id = list(journal)[-1].id
  ledger, tip = fold(journal)

  moved = ledger.find(landed_id)[0]
  assert moved is not None
  assert moved.date == Date("2026-01-03")  # update re-inserted earlier
  assert moved.description == "Cadeau (verplaatst)"
  assert moved.linked == ["e0042"]  # retires ride along
  # date order: 01-01 opening, then 01-03 moved cadeau
  assert [t.date for t in ledger.transactions] == [
    Date("2026-01-01"),
    Date("2026-01-03"),
  ]
  assert ledger.transactions[0].balances == {"checking": 500.0}
  assert ledger.transactions[1].balances == {"checking": 750.0}
  assert tip == update_id  # tip is the update mutation's own content id


def test_fold_delete_with_preimage(tmp_path):
  """A delete line whose pre-image matches removes the row, reverses its
  balance through the tail, and advances the tip to the delete's own id."""
  journal = _write_journal(
    tmp_path / "j.jsonl",
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Kruidenier",
        category="Eten",
        postings={"checking": -40.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  victim_id = list(journal)[-1].id
  journal.append(
    DeleteMutation(
      date="2026-01-05",
      description="Kruidenier",
      category="Eten",
      postings={"checking": -40.0},
      target=victim_id,
      ts="2026-01-06T00:00:00",
    )
  )
  delete_id = list(journal)[-1].id
  assert delete_id != victim_id  # the delete line has its own identity

  ledger, tip = fold(journal)
  assert ledger.find(victim_id)[0] is None
  assert len(ledger.transactions) == 1
  assert ledger.transactions[0].balances == {"checking": 100.0}
  assert tip == delete_id


def test_fold_add_dedupe_guard(tmp_path):
  """Re-applying the same add (identical content + ts → identical id) is
  a no-op: one entry, tip stays that mutation's id."""
  path = tmp_path / "j.jsonl"
  journal = _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
    ],
  )
  first_id = list(journal)[-1].id
  journal.append(
    AddMutation(
      date="2026-01-01",
      description="Opening",
      category="Inkomsten",
      postings={"checking": 100.0},
      ts="2026-01-01T00:00:00",
    )
  )
  assert len(_read_journal(path)) == 2  # two lines, same id

  ledger, tip = fold(journal)
  assert len(ledger.transactions) == 1
  assert ledger.transactions[0].id == first_id
  assert tip == first_id


def test_fold_unknown_targets_are_noops(tmp_path):
  """update/delete on an unknown target apply as no-ops (replay
  idempotence): no error, no entry, tip still advances to that line."""
  journal = _write_journal(
    tmp_path / "j.jsonl",
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      UpdateMutation(
        target="t404",
        date="2026-01-02",
        description="ghost",
        category="Eten",
        postings={"checking": -1.0},
        ts="2026-01-02T00:00:00",
      ),
      DeleteMutation(
        date="2026-01-03",
        description="ghost",
        category="Eten",
        postings={"checking": -1.0},
        target="t404",
        ts="2026-01-03T00:00:00",
      ),
    ],
  )
  last_id = list(journal)[-1].id
  ledger, tip = fold(journal)
  assert len(ledger.transactions) == 1
  assert ledger.transactions[0].balances == {"checking": 100.0}
  assert tip == last_id


def test_fold_refuses_corrupt_row(tmp_path):
  """A tampered journal row (content edited, id kept) stops the fold
  loudly — the id IS the row's anti-corruption validator."""
  path = tmp_path / "j.jsonl"
  _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Sparen",
        category="Overdracht",
        postings={"checking": -100.0, "savings": 100.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  lines = path.read_text().strip().splitlines()
  row = json.loads(lines[0])
  row["description"] = "TAMPERED"
  lines[0] = json.dumps(row, ensure_ascii=False, sort_keys=True)
  path.write_text("\n".join(lines) + "\n", encoding="utf-8")

  with pytest.raises(ValueError, match="fails its content hash"):
    fold(path)


# --- Live validation: the real bootstrap journal ------------------------


def test_fold_of_real_bootstrap_journal():
  """Fold the owner's real bootstrap journal through the engine (skipped
  when the staging file does not exist). The step-1 gate validated the
  tip with the bootstrap's own file-order fold; this proves the ENGINE's
  fold — including per-row rehash verification with the engine's hash —
  agrees on all bootstrap rows: bootstrap-hash scheme == engine-hash
  scheme (balance values are not stated — see the redaction note).
  Skips with a pointer to the repair while the journal still carries the
  four 2026-09-19 writer-bug rows (ids minted before ts was hashed
  content): run scripts/remint_journal_ids.py, then this test hard-runs."""
  from financials.config import data_dir
  from financials.journal import remint

  path = data_dir() / "journal.jsonl"
  if not path.exists():
    pytest.skip("promoted journal not found")
  if remint(path, dry_run=True):
    pytest.skip("journal still has stale ids — run scripts/remint_journal_ids.py first")
  ledger, tip = fold(path)
  # The bootstrap pinned the milestone row count and balances; the owner
  # keeps USING the app, so the journal legitimately grows. Invariant-only
  # since 2026-09-26: every committed row folds, the tip is the last
  # applied mutation's entry, and both account balances are intact at the
  # tip (the historical milestone values are not restated in code).
  assert len(ledger.transactions) >= 1322
  tip_entry = ledger.find(tip)[0]
  assert tip_entry is not None
  assert tip_entry.balances.get("checking") is not None
  assert tip_entry.balances.get("savings") is not None


def test_checkpoint_agrees_with_fold_on_real_journal(tmp_path):
  """Step-4 live gate (skips when the staging journal is absent): the
  persisted checkpoint agrees with the fold — same tip, same balances at
  the tip, same entry count. Fold == checkpoint, verified on real data;
  written to tmp so the suite never touches the owner's staging dir."""
  from financials.config import data_dir
  from financials.journal import checkpoint, fold

  path = data_dir() / "journal.jsonl"
  if not path.exists():
    pytest.skip("promoted journal not found")
  folded, fold_tip = fold(path)
  persisted_tip = checkpoint(Journal(path), path=tmp_path / "ledger.json")
  assert persisted_tip == fold_tip
  persisted = json.loads((tmp_path / "ledger.json").read_text())
  assert len(persisted["entries"]) == len(folded.transactions)
  tip_entry = folded.find(fold_tip)[0]
  assert tip_entry is not None
  # Invariant-only since 2026-09-26 (the owner's live balances move as
  # they use the app): the persisted checkpoint agrees with the fold on
  # the FULL date-ordered ledger — the tip id and every entry's balances,
  # compared row-by-row. Positional alignment is correct: both sides are
  # date-ordered (checkpoint entries serialize in date order; fold chains
  # by date), so a backdated tip row does NOT pair with entries[-1] (the
  # 2026-09-26 lesson: entries[-1] was a LATER-dated row, the tip was the
  # backdated confirm — same data, unlike rows, false divergence).
  assert persisted["tip"] == fold_tip
  for persisted_row, folded_row in zip(persisted["entries"], folded.transactions, strict=True):
    assert persisted_row["id"] == folded_row.id
    assert persisted_row["balances"] == folded_row.balances


def test_remint_repairs_mismatched_ids_only(tmp_path):
  """remint rewrites ONLY the hash-mismatched lines (re-deriving the id
  from the row's own content) and leaves every other line byte-identical;
  a second run is a no-op; dry-run writes nothing."""
  from financials.journal import remint

  path = tmp_path / "j.jsonl"
  _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      ConfirmMutation(
        date="2026-01-10",
        description="Boodschappen",
        category="Eten",
        postings={"checking": -87.5},
        retires="e0092",
        ts="2026-09-19T19:29:15.241470",
      ),
    ],
  )
  lines = path.read_text().strip().splitlines()
  # Simulate the writer bug: mint the confirm's id WITHOUT its ts.
  row = json.loads(lines[1])
  payload = {key: value for key, value in row.items() if key != "id" and key != "ts"}
  from financials.journal import canonical

  row["id"] = hashlib.sha256(canonical(payload).encode()).hexdigest()[:16]
  lines[1] = json.dumps(row, ensure_ascii=False, sort_keys=True)
  stale_id = row["id"]
  path.write_text("\n".join(lines) + "\n", encoding="utf-8")
  before = path.read_text().splitlines()

  repaired = remint(path)  # writes
  assert len(repaired) == 1
  number, old_id, _new_id = repaired[0]
  assert old_id == stale_id

  after = path.read_text().splitlines()
  assert after[0] == before[0]  # healthy line byte-identical
  assert len(after) == 2
  assert json.loads(after[1])["id"] != stale_id  # re-minted
  # The repaired row now passes the fold's verification.
  ledger, _tip = fold(path)
  assert ledger.find(json.loads(after[1])["id"])[0] is not None
  # Idempotent: a second run finds nothing to do.
  assert remint(path, dry_run=True) == []
  # Dry-run never writes: simulate another stale row, check content untouched.
  row = json.loads(after[1])
  row["id"] = "deadbeefdeadbeef"
  after[1] = json.dumps(row, ensure_ascii=False, sort_keys=True)
  path.write_text("\n".join(after) + "\n", encoding="utf-8")
  snapshot = path.read_text()
  assert len(remint(path, dry_run=True)) == 1
  assert path.read_text() == snapshot  # dry-run wrote nothing


# --- Roadmap step 4: the checkpoint (persist + watermark) ---------------


def test_checkpoint_persists_ledger_json(tmp_path):
  """checkpoint folds the journal and writes sibling ledger.json
  {"tip", "entries"} atomically; the tip matches the last journal id and
  entries carry the folded balances."""
  from financials.journal import checkpoint

  path = tmp_path / "journal.jsonl"
  journal = _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 1000.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Kruidenier",
        category="Eten",
        postings={"checking": -40.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  last_id = list(journal)[-1].id

  tip = checkpoint(journal)
  assert tip == last_id
  persisted = json.loads((tmp_path / "ledger.json").read_text())
  assert set(persisted) == {"tip", "entries"}
  assert persisted["tip"] == last_id
  assert len(persisted["entries"]) == 2
  assert persisted["entries"][-1]["balances"] == {"checking": 960.0}
  assert not (tmp_path / "ledger.tmp.json").exists()  # atomic swap, no litter


def test_checkpoint_converges_after_simulated_kill9(tmp_path):
  """Kill-9 simulation: a mutation was journaled but the process died
  before persisting — the checkpoint lags the journal (the safe failure
  direction). The next persist folds the tail and converges the
  checkpoint to the journal tip."""
  from financials.journal import checkpoint

  path = tmp_path / "journal.jsonl"
  journal = _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
    ],
  )
  persisted_tip = checkpoint(journal)

  # The crash: a mutation appended WITHOUT a following checkpoint.
  crashed = AddMutation(
    date="2026-01-05",
    description="Kruidenier",
    category="Eten",
    postings={"checking": -40.0},
    ts="2026-01-05T00:00:00",
  )
  journal.append(crashed)
  crashed_id = crashed.id
  stale = json.loads((tmp_path / "ledger.json").read_text())
  assert stale["tip"] == persisted_tip  # checkpoint lags the journal

  tip = checkpoint(journal)  # next command's persist
  assert tip == crashed_id  # the gap replayed, watermark advanced
  converged = json.loads((tmp_path / "ledger.json").read_text())
  assert converged["tip"] == crashed_id
  assert converged["entries"][-1]["balances"] == {"checking": 60.0}


def test_checkpoint_gap_converges_through_command_path(tmp_path, monkeypatch):
  """The crash gap converges through the real command path: a command's
  append_and_apply boots (checkpoint + tail replay) and persists, so a
  checkpoint left stale by a simulated crash is brought to the journal
  tip by the next command."""
  from financials.commands import cmd_add

  journal_path = tmp_path / "journal.jsonl"
  gap = _write_journal(
    journal_path,
    [
      AddMutation(
        date="2026-02-01",
        description="Lunch",
        category="Eten",
        postings={"checking": -25.0},
        ts="2026-02-01T00:00:00",
      ),
    ],
  )
  from financials.journal import checkpoint

  assert checkpoint(gap)  # checkpoint exists, but lags after the crash below
  gap_tip = list(gap)[-1].id
  crashed = AddMutation(
    date="2026-02-05",
    description="Bakker",
    category="Eten",
    postings={"checking": -5.0},
    ts="2026-02-05T00:00:00",
  )
  Journal(journal_path).append(crashed)  # killed before persisting
  stale = json.loads((journal_path.parent / "ledger.json").read_text())
  assert stale["tip"] == gap_tip

  # The next command boots + persists: the gap replays, the tip advances.
  _mutation, entry = cmd_add("2026-02-06", "Kafe", "Horeca", -3.0, journal=gap)
  live_tip = json.loads((journal_path.parent / "ledger.json").read_text())["tip"]
  assert live_tip == entry.id  # the command's own mutation
  assert live_tip != stale["tip"]
  converged = json.loads((journal_path.parent / "ledger.json").read_text())
  assert len(converged["entries"]) == 3
  assert converged["entries"][-1]["balances"] == {"checking": -33.0}


# --- Roadmap step 5: boot from checkpoint + tail replay -----------------


def test_hydrate_verifies_hash_guards(tmp_path):
  """Hydrate rebuilds from checkpoint rows (no re-fold — balances are
  snapshot state); guarded rows failing verify() refuse loudly; bare
  rows (no hash field) are accepted as-is."""
  from financials.journal import Ledger, Transaction

  ledger = Ledger()
  transaction = ledger.insert(
    Transaction(
      id="a1",
      date="2026-01-01",
      description="first",
      category="Eten",
      postings={"checking": -10.0},
    )
  )
  assert transaction is not None
  rows = ledger.rowlist()
  assert rows[0]["hash"]  # rowlist carries the guard

  rebuilt = Ledger()
  rebuilt.hydrate(rows)
  assert rebuilt.transactions[0].verify()
  assert rebuilt.transactions[0].balances == {"checking": -10.0}

  # Tampered guard: refusal.
  bad = json.loads(json.dumps(rows))
  bad[0]["hash"] = "deadbeefdeadbeef"
  with pytest.raises(ValueError, match="fails its hash guard"):
    Ledger().hydrate(bad)

  # Bare row (bootstrap-era, no guard): accepted.
  bare = [{key: value for key, value in rows[0].items() if key != "hash"}]
  Ledger().hydrate(bare)


def test_boot_self_heals_without_checkpoint(tmp_path):
  """No ledger.json: load_ledger folds the journal (self-heal), persists
  the checkpoint, and returns the ledger; the next boot hydrates."""
  from financials.journal import load_ledger

  path = tmp_path / "journal.jsonl"
  journal = _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Kruidenier",
        category="Eten",
        postings={"checking": -40.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  ledger = load_ledger(journal)
  assert [t.id for t in ledger.transactions] == [m.id for m in journal]
  assert ledger.transactions[-1].balances == {"checking": 60.0}
  assert (tmp_path / "ledger.json").exists()  # checkpoint persisted

  # Second boot: hydrate path (same state, no fold).
  ledger2 = load_ledger(journal)
  assert [t.id for t in ledger2.transactions] == [t.id for t in ledger.transactions]
  assert ledger2.transactions[-1].balances == {"checking": 60.0}


def test_boot_replays_tail_beyond_watermark(tmp_path):
  """Checkpoint at the gap tip + crash row in the journal: boot hydrates,
  replays only the tail beyond the watermark, and converges — the
  hydrated+replayed ledger equals the full fold."""
  from financials.journal import load_ledger

  path = tmp_path / "journal.jsonl"
  journal = _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Kruidenier",
        category="Eten",
        postings={"checking": -40.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  from financials.journal import checkpoint

  checkpoint(journal)
  crashed = AddMutation(
    date="2026-01-07",
    description="Bakker",
    category="Eten",
    postings={"checking": -5.0},
    ts="2026-01-07T00:00:00",
  )
  journal.append(crashed)  # killed before persisting

  ledger = load_ledger(journal)  # boot: hydrate + tail replay
  assert ledger.transactions[-1].id == crashed.id
  assert ledger.transactions[-1].balances == {"checking": 55.0}
  # Parity with the full fold (the ground truth).
  folded, fold_tip = fold(journal)
  assert ledger.aslist() == folded.aslist()
  assert fold_tip == crashed.id


def test_boot_refuses_truncated_journal(tmp_path):
  """Structure damage: the checkpoint tip never appears in the journal
  (truncation) — boot refuses loudly; the tip + backups own this."""

  path = tmp_path / "journal.jsonl"
  journal = _write_journal(
    path,
    [
      AddMutation(
        date="2026-01-01",
        description="Opening",
        category="Inkomsten",
        postings={"checking": 100.0},
        ts="2026-01-01T00:00:00",
      ),
      AddMutation(
        date="2026-01-05",
        description="Kruidenier",
        category="Eten",
        postings={"checking": -40.0},
        ts="2026-01-05T00:00:00",
      ),
    ],
  )
  from financials.journal import checkpoint

  checkpoint(journal)
  # Truncate: drop the last journal line (the checkpoint's tip).
  lines = path.read_text().strip().splitlines()
  path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
