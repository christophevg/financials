import datetime
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

"""Journal (append-only mutations) + ledger (applied transactions).

Identity vs integrity (decided 2026-09-18, docs/journaled-ledger.md):
  - a MUTATION's id IS its anti-corruption validator: 16-hex content
    hash over the canonical mutation minus the id (rehash on read)
  - a TRANSACTION's id is assigned ONCE by its creating mutation
    (add | confirm) and never recomputed — updates and balance
    propagation keep it, so linked/retires never need scanning
  - a transaction carries a separate stored `hash` (content hash minus
    the hash field, balances included) guarding its row on disk;
    recomputed by every writer (insert, update, propagate)
  - identical mutation content needs distinct ids (two Q8 rows): every
    mutation carries `ts` (creation time, hashed content) so content
    hashes never collide between two real entries
Ops: add | update | delete | confirm — no opening op (openings are
ordinary add transactions; the first posting to an account creates it).

{"date": "2024-11-24", "description": "Opening", "category": "Inkomsten",
 "postings": {"checking": 2776.47}, "op": "add", "id": "c41fd0e93b6f871a"}
{"date": "2024-11-24", "description": "Opening", "category": "Inkomsten",
 "postings": {"savings": 20000.0}, "op": "add", "id": "9e2ac1b0774410f3"}
{"date": "2024-11-24", "description": "Huur", "category": "Inkomsten",
 "postings": {"checking": 350.0}, "op": "add", "id": "cb2214d53b6f871a"}
...
"""


def Date(date: str) -> datetime.date:
  return datetime.datetime.strptime(date, "%Y-%m-%d").date()


def canonical(payload: dict) -> str:
  """The one frozen canonicalization for content hashing: sorted keys,
  compact separators, non-ASCII kept, float amounts rounded 2-decimal."""
  normalized = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))

  def _round(value):
    if isinstance(value, dict):
      return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
      return [_round(item) for item in value]
    if isinstance(value, float):
      return round(value, 2)
    return value

  return json.dumps(_round(normalized), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class Mutation:
  date: datetime.date  # str accepted at call time; __post_init__ converts
  description: str
  category: str
  ts: str = ""  # mutation creation time (ISO-8601); uniqueness is a
  #               human discipline — mutations are entered manually —
  #               and its purpose is only to make ids distinct for
  #               identical content (two Q8s). It IS hashed content.
  postings: dict[str, float] = field(default_factory=dict)
  id: str | None = None
  target: str = ""
  retires: list[str] = field(default_factory=list)
  linked: list[str] = field(default_factory=list)
  op: str = field(default="", init=False)

  def __post_init__(self):
    if isinstance(self.date, str):
      self.date = Date(self.date)
    for account, amount in list(self.postings.items()):
      self.postings[account] = round(amount, 2)
    if isinstance(self.retires, str):
      self.retires = [self.retires]

  @property
  def content_hash(self) -> str:
    """16-hex content hash over the canonical mutation MINUS the id —
    the id doubles as the row's anti-corruption validator."""
    payload = {key: value for key, value in self.asdict().items() if key != "id"}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:16]

  def asdict(self) -> dict:
    properties = asdict(self)
    properties["date"] = properties["date"].strftime("%Y-%m-%d")
    if not properties.get("op"):
      properties.pop("op", None)
    properties.pop("hash", None)  # Transaction-only; guarded, not content
    return {
      key: value
      for key, value in json.loads(
        json.dumps(properties, ensure_ascii=False, sort_keys=True)
      ).items()
      if value != [] and value != "" and value != {}
    }

  def __str__(self) -> str:
    return json.dumps(self.asdict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

  def apply_on(self, ledger: "Ledger"):
    raise NotImplementedError(f"mutation op {self.op!r} has no apply_on")


@dataclass
class Transaction(Mutation):
  balances: dict[str, float] = field(default_factory=dict)
  hash: str = ""

  def __post_init__(self):
    if isinstance(self.date, str):
      self.date = Date(self.date)
    for account, amount in list(self.postings.items()):
      self.postings[account] = round(amount, 2)
    if isinstance(self.retires, str):
      self.retires = [self.retires]
    if not self.id:
      raise ValueError("a transaction's id is assigned by its creating mutation")

  def rehash(self) -> None:
    """Recompute the row's on-disk guard: content hash minus the hash
    field (includes the id, so id swaps are caught too)."""
    payload = {key: value for key, value in self.asdict().items() if key != "hash"}
    self.hash = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:16]

  def verify(self) -> bool:
    payload = {key: value for key, value in self.asdict().items() if key != "hash"}
    expected = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:16]
    return self.hash == expected

  def row_dict(self) -> dict:
    """The on-disk checkpoint row: the entry dict PLUS its hash guard
    (the row's anti-corruption validator on the checkpoint's disk)."""
    row = self.asdict()
    row["hash"] = self.hash
    return row


@dataclass
class Ledger:
  transactions: list[Transaction] = field(default_factory=list)

  def apply(self, mutation: Mutation):
    return mutation.apply_on(self)

  def aslist(self) -> list[dict]:
    return [transaction.asdict() for transaction in self.transactions]

  def rowlist(self) -> list[dict]:
    """Checkpoint rows: entries plus their hash guards (the persisted
    snapshot carries its own anti-corruption validator)."""
    return [transaction.row_dict() for transaction in self.transactions]

  def hydrate(self, rows: list[dict]) -> None:
    """Rebuild the in-memory ledger from persisted checkpoint rows:
    balances are first-class snapshot state, so no re-fold happens —
    rows carry their own balances; only their integrity is checked.
    Bare rows (bootstrap-era checkpoints without hash guards) are
    accepted; guarded rows failing verify() are refused loudly."""
    self.transactions = []
    for row in rows:
      properties = {key: value for key, value in row.items() if key != "hash"}
      transaction = Transaction(**properties)
      if row.get("hash"):
        transaction.hash = row["hash"]
        if not transaction.verify():
          raise ValueError(
            f"checkpoint row {transaction.id} fails its hash guard — "
            "refusing to hydrate a corrupt checkpoint"
          )
      self.transactions.append(transaction)

  def find(self, id: str):
    return next(
      (
        (transaction, idx)
        for idx, transaction in enumerate(self.transactions)
        if transaction.id == id
      ),
      (None, -1),
    )

  def find_parent(self, date: datetime.date | str):
    anchor: datetime.date = Date(date) if isinstance(date, str) else date
    ancestors = [
      (transaction, idx)
      for idx, transaction in enumerate(self.transactions)
      if transaction.date <= anchor
    ]
    return ancestors[-1] if ancestors else (None, -1)

  def apply_postings(
    self,
    balances: dict[str, float],
    postings: dict[str, float],
  ) -> dict[str, float]:
    new_balances: dict[str, float] = dict(balances)
    for account, amount in postings.items():
      new_balances[account] = round(balances.get(account, 0.0) + amount, 2)
    return new_balances

  def insert(self, transaction: Transaction) -> Transaction:
    """
    idx      0     1          2        3     4     5
    before   Z   parent       X        Y     Z
    after    Z   parent  transaction   X     Y     Z
    """
    parent, idx = self.find_parent(transaction.date)
    balances = parent.balances if parent else {}
    transaction.balances = self.apply_postings(balances, transaction.postings)
    transaction.rehash()
    self.transactions.insert(idx + 1, transaction)
    self.propagate(transaction.postings, idx + 2)
    return transaction

  def propagate(self, changes: dict[str, float], idx: int) -> None:
    while idx < len(self.transactions):
      row = self.transactions[idx]
      row.balances = self.apply_postings(row.balances, changes)
      row.rehash()
      idx += 1

  def update(self, update: Transaction) -> Transaction | None:
    """Update = delete + insert: the two balance-correct primitives,
    composed. The transaction's id (assigned at creation) rides along,
    so linked references elsewhere stay valid; balances re-derive
    through the propagation of both steps."""
    assert update.id is not None
    if not self.delete(update.id):
      return None
    return self.insert(update)

  def delete(self, target: str) -> Transaction | None:
    transaction, idx = self.find(target)
    if not transaction:
      return None
    reverse = {account: round(-amount, 2) for account, amount in transaction.postings.items()}
    del self.transactions[idx]
    self.propagate(reverse, idx)
    return transaction


@dataclass
class AddMutation(Mutation):
  op: str = field(default="add", init=False)

  def apply_on(self, ledger: "Ledger") -> Transaction | None:
    existing, _ = ledger.find(self.id or "")
    if existing:  # idempotent re-apply
      return existing
    return ledger.insert(
      Transaction(
        id=self.id,
        date=self.date,
        description=self.description,
        category=self.category,
        postings=self.postings,
        linked=self.linked,
      )
    )


@dataclass
class ConfirmMutation(AddMutation):
  op: str = field(default="confirm", init=False)

  def apply_on(self, ledger: "Ledger") -> Transaction | None:
    existing, _ = ledger.find(self.id or "")
    if existing:  # idempotent re-apply
      return existing
    return ledger.insert(
      Transaction(
        id=self.id,
        date=self.date,
        description=self.description,
        category=self.category,
        postings=self.postings,
        linked=[*self.retires, *self.linked],
      )
    )


@dataclass
class UpdateMutation(Mutation):
  op: str = field(default="update", init=False)

  def apply_on(self, ledger: "Ledger") -> Transaction | None:
    existing, _ = ledger.find(self.target)
    if not existing:
      return None  # unknown target: no-op (replay idempotence)
    return ledger.update(
      Transaction(
        id=self.target,
        date=self.date,
        description=self.description,
        category=self.category,
        postings=self.postings,
        # linked rides along (decided 2026-09-18: update replaces the
        # mutable fields description/category/postings/date; the id and
        # linked references carry over unchanged)
        linked=existing.linked,
      )
    )


@dataclass
class DeleteMutation(Mutation):
  """Delete = the victim's full pre-image + target id. date/description/
  category/postings/linked are the VICTIM's content at deletion time
  (the pre-image), not the deletion's own execution data. The fold
  verifies the pre-image against the live row before reversing: a
  mismatch stops loudly (the journal claims a delete that does not
  match reality — replaying it silently would corrupt the chain). One
  line therefore reconstructs the deleted item AND proves it deleted
  the right thing."""

  op: str = field(default="delete", init=False)

  def apply_on(self, ledger: "Ledger") -> Transaction | None:
    transaction, _ = ledger.find(self.target)
    if not transaction:
      return None  # unknown target: nothing to delete (replay idempotence)
    preimage = {
      "date": self.date.strftime("%Y-%m-%d") if isinstance(self.date, datetime.date) else self.date,
      "description": self.description,
      "category": self.category,
      "postings": self.postings,
      "linked": self.linked,
    }
    live = {
      "date": transaction.date.strftime("%Y-%m-%d"),
      "description": transaction.description,
      "category": transaction.category,
      "postings": transaction.postings,
      "linked": transaction.linked,
    }
    if canonical(preimage) != canonical(live):
      raise ValueError(
        f"delete {self.target}: pre-image mismatch — journal snapshot "
        "does not match the live row; refusing to delete"
      )
    return ledger.delete(self.target)


class Journal:
  MUTATIONS = {
    "add": AddMutation,
    "confirm": ConfirmMutation,
    "update": UpdateMutation,
    "delete": DeleteMutation,
  }

  def __init__(self, path: Path | str):
    self.path = path if isinstance(path, Path) else Path(path)

  def __iter__(self):
    if not self.path.exists():
      return  # a fresh journal: no rows (self-heal boot starts empty)
    with self.path.open() as fp:
      for line in fp:
        if not line.strip():
          continue
        properties = json.loads(line)
        op = properties.pop("op", None)
        cls = self.MUTATIONS.get(op)
        if cls is None:
          raise NotImplementedError(f"mutation op {op!r} is not implemented")
        yield cls(**properties)

  def append(self, mutation: Mutation) -> None:
    if not mutation.id:
      mutation.id = mutation.content_hash
    with self.path.open("a") as fp:
      fp.write(str(mutation) + "\n")

  def verify(self) -> list[str]:
    """Rehash every mutation row; returns the ids that fail (the
    journal's per-row anti-corruption check)."""
    return [mutation.id or "?" for mutation in self if mutation.content_hash != mutation.id]


def fold(source: Journal | Path | str) -> tuple[Ledger, str]:
  """THE canonical fold (roadmap step 3): replay a journal into a ledger,
  returning (ledger, tip).

  Entries are positioned by DATE (the engine's date-positioned insert —
  the canonical checkpoint fold, decided 2026-09-21; the bootstrap's
  file-order fold stays the one-time evidence pass inside the script).
  The tip is the last APPLIED mutation in JOURNAL order — entry order
  and journal order are independent. Every row is rehash-verified
  BEFORE it is applied: the id IS the row's anti-corruption validator,
  so a tampered row stops the fold loudly instead of silently producing
  a wrong checkpoint."""

  journal = source if isinstance(source, Journal) else Journal(source)
  ledger = Ledger()
  tip = ""
  for mutation in journal:
    if mutation.id and mutation.content_hash != mutation.id:
      raise ValueError(
        f"journal row {mutation.id!r} fails its content hash "
        f"(rehashed {mutation.content_hash}) — refusing to fold a corrupt journal: {mutation}"
      )
    ledger.apply(mutation)
    tip = mutation.id or tip
  return ledger, tip


def remint(source: Journal | Path | str, dry_run: bool = False) -> list[tuple[int, str, str]]:
  """Re-mint the ids of rows whose stored id does not match their content
  hash (repair for the 2026-09-19 writer bug: ts was written into the
  line but the id was minted before ts was hashed content — four live
  observer rows).

  Only the affected lines are rewritten (byte-preserving everything
  else); the content itself is never touched — the id doubles as the
  anti-corruption validator, so a re-mint re-derives it from the row's
  own content. Returns the repaired rows as (line number, old id, new
  id). Dry-run reports without writing."""
  journal = source if isinstance(source, Journal) else Journal(source)
  path = journal.path
  lines = path.read_text(encoding="utf-8").splitlines()
  repaired: list[tuple[int, str, str]] = []
  for number, line in enumerate(lines, start=1):
    if not line.strip():
      continue
    properties = json.loads(line)
    op = properties.pop("op", None)
    cls = Journal.MUTATIONS.get(op)
    if cls is None:
      raise NotImplementedError(f"mutation op {op!r} is not implemented")
    mutation = cls(**properties)
    if mutation.id and mutation.content_hash != mutation.id:
      new_id = mutation.content_hash
      properties["id"] = new_id
      properties["op"] = op
      ordered = {key: properties[key] for key in sorted(properties)}
      lines[number - 1] = json.dumps(ordered, ensure_ascii=False, sort_keys=True)
      repaired.append((number, mutation.id, new_id))
  if repaired and not dry_run:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
  return repaired


def checkpoint(journal: Journal | Path | str, path: Path | str | None = None) -> str:
  """Roadmap step 4: persist the checkpoint. Folds the journal (per-row
  hash-verified, see fold) and writes ledger.json — {"tip", "entries"} —
  atomically (temp file + os.replace, so a crash mid-write leaves the
  old checkpoint intact and the journal still ahead of the checkpoint:
  the failure direction is always replayable).

  The watermark IS the tip: the checkpoint's tip records the fold
  position, so a crash that leaves the journal ahead is detected on the
  next persist — folding replays the tail and the checkpoint converges.
  Returns the persisted tip. Default path: journal.jsonl's sibling
  ledger.json (the bootstrap's staging layout)."""
  journal = journal if isinstance(journal, Journal) else Journal(journal)
  ledger, tip = fold(journal)
  target = Path(path) if path else journal.path.with_name("ledger.json")
  payload = json.dumps(
    {"tip": tip, "entries": ledger.rowlist()},
    ensure_ascii=False,
    indent=2,
  )
  temp = target.with_name(target.stem + ".tmp" + target.suffix)
  temp.write_text(payload + "\n", encoding="utf-8")
  os.replace(temp, target)
  return tip


def load_ledger(journal: Journal | Path | str, path: Path | str | None = None) -> Ledger:
  """Roadmap step 5: boot. Rebuild the in-memory ledger from the
  checkpoint (hydrate, hash-guard-verified) and bring it to the journal
  tip by applying the journal tail beyond the checkpoint's watermark —
  the tail replay, per the design. Converges when a gap is healed:
  a crash left the journal ahead, the replay catches up and the next
  persist writes the converged checkpoint.

  Self-healing when no checkpoint exists: a full fold replaces it (the
  in-memory ledger is disposable — fold(journal) reproduces it exactly).
  Structural damage (the checkpoint tip never appears in the journal —
  truncation or reorder) is NOT the hash's job: refuse loudly, the
  tip + backup path own it. Default checkpoint path: journal.jsonl's
  sibling ledger.json."""
  journal = journal if isinstance(journal, Journal) else Journal(journal)
  checkpoint_path = Path(path) if path else journal.path.with_name("ledger.json")
  ledger = Ledger()
  if not checkpoint_path.exists():
    # No checkpoint: self-heal by full fold (and persist it, so the next
    # boot hydrates instead of folding).
    ledger, tip = fold(journal)
    checkpoint(journal, checkpoint_path)
    return ledger
  persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
  ledger.hydrate(persisted.get("entries", []))
  watermark = persisted.get("tip", "")
  if not watermark:
    # Empty checkpoint (empty journal at checkpoint time): the tip
    # appears vacuously; a non-empty journal behind an empty checkpoint
    # means the journal arrived AFTER the crash-gap persist — the whole
    # journal replays (seen stays False, treated as all-tail).
    for mutation in journal:
      if mutation.id and mutation.content_hash != mutation.id:
        raise ValueError(
          f"journal row {mutation.id!r} fails its content hash "
          f"(rehashed {mutation.content_hash}) — refusing to boot a corrupt journal: {mutation}"
        )
      ledger.apply(mutation)
    return ledger
  seen = False
  for mutation in journal:
    if mutation.id and mutation.content_hash != mutation.id:
      raise ValueError(
        f"journal row {mutation.id!r} fails its content hash "
        f"(rehashed {mutation.content_hash}) — refusing to boot a corrupt journal: {mutation}"
      )
    if not seen:
      if mutation.id == watermark:
        seen = True
      continue  # before the watermark: already folded into the checkpoint
    ledger.apply(mutation)  # tail beyond the watermark: replay it
  if not seen:
    raise ValueError(
      f"checkpoint tip {watermark!r} never appears in the journal — "
      "truncated or reordered journal (structure damage is the tip's + "
      "backup's job); refusing to boot"
    )
  return ledger


if __name__ == "__main__":

  def show(path: str | Path) -> None:
    ledger, tip = fold(path)
    print(json.dumps({"tip": tip, "entries": ledger.aslist()}, indent=2))

  show(sys.argv[1] if len(sys.argv) > 1 else "test_events.jsonl")
