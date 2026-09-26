"""Journal-native command mutations (roadmap step 6): every mutating
command (add / edit / delete / fixes) builds a mutation, appends it to
the canonical journal, and persists the checkpoint. There is no
intermediate store: the journal IS the store of record, the ledger's
engine owns balance chaining and date ordering, and ledger.json is the
snapshot the next boot hydrates from.

Mutation ids are content hashes (one hash scheme end-to-end); machine
paths stamp a monotonic ts so identical content still yields distinct
ids (the Q8 lesson). Command failures happen BEFORE the append: a
rejected command leaves no journal line, so every line records a
change that actually happened.
"""

from __future__ import annotations

import datetime

from financials.journal import (
  AddMutation,
  ConfirmMutation,
  Date,
  DeleteMutation,
  Journal,
  Ledger,
  Mutation,
  Transaction,
  UpdateMutation,
  checkpoint,
  load_ledger,
)


def _ts_clock():
  """Monotonic microsecond ts generator for machine-minted mutations
  (bootstrap's clock, now the live command clock): identical content in
  a tight loop must still yield distinct ids."""
  last = 0
  while True:
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")
    current = int(stamp.replace("-", "").replace(":", "").replace(".", "").replace("T", ""))
    if current <= last:
      stamp = (now + datetime.timedelta(microseconds=last - current + 1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
      )
      current = int(stamp.replace("-", "").replace(":", "").replace(".", "").replace("T", ""))
    last = current
    yield stamp


_clock = _ts_clock()


def command_journal() -> Journal:
  """The canonical journal. Promotion-aware: before the data promotion
  the migration staging journal is canonical (both files exist; the
  migration one holds the bootstrap chain); after promotion
  migration/journal.jsonl no longer exists and data/journal.jsonl is
  canonical. Resolved through fixes so tests can monkeypatch."""
  from financials.fixes import journal_file, migration_journal_file

  migration = migration_journal_file()
  if migration.exists():
    migration.parent.mkdir(parents=True, exist_ok=True)
    return Journal(migration)
  canonical = journal_file()
  canonical.parent.mkdir(parents=True, exist_ok=True)
  return Journal(canonical)


def append_and_apply(mutation: Mutation, journal: Journal | None = None) -> Ledger:
  """THE single write step: stamp ts, append the mutation, boot the
  ledger (checkpoint + tail replay — the just-appended mutation rides
  the tail), and persist the converged checkpoint. Returns the live
  ledger for feedback (find the applied entry by mutation id)."""
  journal = journal or command_journal()
  if not mutation.ts:
    mutation.ts = next(_clock)
  if not mutation.id:
    mutation.id = mutation.content_hash
  journal.append(mutation)
  ledger = load_ledger(journal)
  checkpoint(journal)
  return ledger


def postings_for(category: str, amount: float) -> dict[str, float]:
  """The posting set for an amount in a category: checking carries the
  amount; a transfer carries the savings mirror (the engine moves both
  balances)."""
  postings = {"checking": round(amount, 2)}
  if category == "Overdracht":
    postings["savings"] = round(-amount, 2)
  return postings


def cmd_add(
  iso_date: str,
  description: str,
  category: str,
  amount: float,
  retires: list[str] | None = None,
  journal: Journal | None = None,
) -> tuple[Mutation, Transaction | None]:
  """Append an add (or confirm, when retires an expected id) and apply
  it. Returns (mutation, applied entry with balances)."""
  if retires:
    mutation: Mutation = ConfirmMutation(
      date=Date(iso_date),
      description=description,
      category=category,
      postings=postings_for(category, amount),
      retires=retires,
    )
  else:
    mutation = AddMutation(
      date=Date(iso_date),
      description=description,
      category=category,
      postings=postings_for(category, amount),
    )
  ledger = append_and_apply(mutation, journal)
  entry, _ = ledger.find(mutation.id or "")
  return mutation, entry


def cmd_update(
  target: str,
  iso_date: str,
  description: str,
  category: str,
  amount: float,
  journal: Journal | None = None,
) -> tuple[Mutation, Transaction | None]:
  """Append an update (full replacement of the mutable fields; the id
  and linked references ride along; date moves re-insert) and apply it.
  Returns (mutation, applied entry); entry is None for an unknown
  target — callers guard that beforehand."""
  mutation = UpdateMutation(
    target=target,
    date=Date(iso_date),
    description=description,
    category=category,
    postings=postings_for(category, amount),
  )
  ledger = append_and_apply(mutation, journal)
  entry, _ = ledger.find(target)
  return mutation, entry


def cmd_delete(target: str, journal: Journal | None = None) -> tuple[Mutation, bool]:
  """Append a delete carrying the victim's full pre-image (read from
  the live ledger BEFORE the append) and apply it. Returns (mutation,
  deleted) — False when the target is unknown (nothing appended)."""
  journal = journal or command_journal()
  ledger = load_ledger(journal)
  victim, _ = ledger.find(target)
  if victim is None:
    return DeleteMutation(  # nothing appended: nothing happened
      date=Date("1970-01-01"),
      description="",
      category="",
      postings={},
      target=target,
    ), False
  mutation = DeleteMutation(
    date=victim.date,
    description=victim.description,
    category=victim.category,
    postings=dict(victim.postings),
    linked=list(victim.linked),
    target=target,
  )
  append_and_apply(mutation, journal)
  return mutation, True


def projected_checking(
  ledger: Ledger, iso_date: str, amount: float
) -> tuple[float | None, Transaction | None]:
  """The projected checking balance for an entry of `amount` dated
  iso_date, chaining from the ledger's date-positioned predecessor (the
  engine's find_parent — the same row the fold will chain from). Returns
  (projection, predecessor); (None, None) when no predecessor exists."""
  from financials.journal import Ledger as _Ledger  # noqa: F401 — typing

  parent, _idx = ledger.find_parent(Date(iso_date))
  if parent is None:
    return None, None
  projected = round(parent.balances.get("checking", 0.0) + amount, 2)
  return projected, parent
