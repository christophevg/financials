#!/usr/bin/env python
"""One-time bootstrap for the journaled ledger (roadmap step 1).

See docs/journaled-ledger.md. Reads the current stores and emits, into a
STAGING directory (default: <data_dir>/migration — never the live store
root, never overwriting existing outputs without --force):

  events.jsonl   append-only journal: two opening events (seed balances,
                 derived from the first row) + one committed event per
                 actual row, in replay order; NO sequence numbers — the
                 row order IS the order. id = 16-hex content hash over
                 the canonical event minus id (self-validating, idempotent
                 appends, deterministic bootstrap)
  ledger.json    checkpoint: folded entries + computed balances + tip
                 (id of the last journal event — the watermark)
  expected.json  pruned hypotheticals (balances and source_line stripped)
  rules.json     recurrences, carried as-is (they never had balances)

Seed rule (decided 2026-09-18): opening = first row's recorded balance
minus that row's own effect on the account. Worked example t0001
(2024-11-24, Huur, +350,00, recorded 3.126,47 / 20.000,00):
  checking opening = 3126.47 - 350.00 = 2776.47
  savings opening  = 20000.00            (no transfer in the first row)

Op vocabulary (final): add | update | delete | confirm. No opening op
(openings are ordinary add mutations: "Opening", Inkomsten); no
reconcile op — money that moved without a transaction becomes an
explicit correction TRANSACTION (owner policy 2026-09-18: "I will
always create a transaction implementing the mutation"); the bootstrap
derives those correction rows and lists them for review.

Every mutation carries `ts` (creation time, part of the hashed
content): identical content then yields DISTINCT ids (two Q8 rows stay
two rows — the 2026-09-19 duplicate-collapse incident). Machine-minted
rows get a monotonic clock (never repeats); human entries rely on
manual distinctness. Ids are minted through journal.py's Mutation
classes, so bootstrap and engine share one hash scheme.

Verification gate (two-tier):
  HARD:   the folded TIP must equal the recorded tip — the one
          comparison that validates the entire amount chain end-to-end.
          On failure: report, exit non-zero, write NOTHING.
  SOFT:   per-row balance deviations are counted and shown; after the
          correction pass this must be EMPTY — any survivor is an
          inconsistency between corrections and the fold.

Run by the owner (Makefile target: migration-boostrap):
  uv run python scripts/bootstrap_journal.py
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

from financials.config import data_dir
from financials.model import (
  STATUS_ACTUAL,
  SUMMARY_NOTE_MARKER,
  Transaction,
  load_expected,
  load_transactions,
)
from financials.recurrences import load_recurrences

TOLERANCE = 0.005
TRANSFER_CATEGORY = "Overdracht"


def _ts_clock():
  """Monotonic microsecond ts generator for machine-minted mutations.
  1322 rows minted in a tight loop CAN land on the same microsecond;
  identical content + identical ts = identical id (the Q8 lesson), so
  the clock never repeats. Human entries rely on manual distinctness."""
  last = 0
  while True:
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f")
    current = int(stamp.replace("-", "").replace(":", "").replace(".", "").replace("T", ""))
    if current <= last:
      stamp = (now + datetime.timedelta(microseconds=last - current + 1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
      )
      current = last + 1
    last = current
    yield stamp


def _canonical(event: dict) -> str:
  """Canonicalization for hashing (bootstrap-local copy): sorted keys,
  compact separators, non-ASCII kept, float amounts 2-decimal rounded.
  Ids are minted through journal.py's Mutation classes — this helper
  only rounds residual keys of legacy events."""
  normalized = json.loads(json.dumps(event, ensure_ascii=False, sort_keys=True))

  def _round(value):
    if isinstance(value, dict):
      return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
      return [_round(item) for item in value]
    if isinstance(value, float):
      return round(value, 2)
    return value

  return json.dumps(
    _round(normalized), ensure_ascii=False, sort_keys=True, separators=(",", ":")
  )


def _mint_id(event: dict) -> str:
  """Event id = 16-hex content hash over the canonical event MINUS the
  id field (see docs/journaled-ledger.md id spec)."""
  payload = {k: v for k, v in event.items() if k != "id"}
  return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


def _minted(event: dict) -> dict:
  """Event dict with its content-hash id minted in."""
  return {**event, "id": _mint_id(event)}


def _postings_for(t: Transaction) -> dict[str, float]:
  """Postings of one committed row. A transfer is ONE mutation with TWO
  postings: an Overdracht row of amount X moves checking by +amount and
  savings by -amount (the shape _walk and _build already implement)."""
  checking = {"checking": t.amount_eur}
  if t.category == TRANSFER_CATEGORY:
    return {**checking, "savings": round(-(t.amount_eur or 0.0), 2)}
  return checking


def _openings_for_first(first: Transaction) -> list[dict]:
  """Seed openings from the first row: recorded balance minus the row's
  own effect. Emitted as ORDINARY add mutations (no opening op — the
  first posting to an account creates it; category Inkomsten: external
  -> account). Only accounts recorded on the first row get an opening."""
  openings: list[dict] = []
  effect = _postings_for(first)
  if first.balance_checking is not None:
    openings.append(
      {
        "op": "add",
        "date": first.date,
        "description": "Opening",
        "category": "Inkomsten",
        "postings": {
          "checking": round(first.balance_checking - effect.get("checking", 0.0), 2)
        },
      }
    )
  if first.balance_savings is not None:
    openings.append(
      {
        "op": "add",
        "date": first.date,
        "description": "Opening",
        "category": "Inkomsten",
        "postings": {
          "savings": round(first.balance_savings - effect.get("savings", 0.0), 2)
        },
      }
    )
  return openings


def _build_events(
  rows: list[Transaction],
) -> tuple[list[dict], dict[str, str], list[str], list[str]]:
  """Journal mutations + review lists, walking rows in FILE order.

  The recorded balance chain was maintained in file order (every insert
  rebased at insert time), so the evidence pass replays file-order:
  the recorded chain is coherent there and date-inversions produce no
  deltas at all. Per row, the recorded balance deltas decide the
  postings: beyond the convention (Overdracht pairs, otherwise
  checking-only), a savings delta explained by the row's own amount
  RECLASSIFIES the row (pair / target-savings — the mutation triad:
  explicit source/target instead of category sniffing). Unexplained
  deltas become EXPLICIT correction transactions (derived add
  mutations, external -> account; owner policy: no reconcile op
  exists). Returns (mutations, lineage, reclassified, corrections)."""
  mutations: list[dict] = []
  lineage: dict[str, str] = {}
  reclassified: list[str] = []
  corrections: list[str] = []
  clock = _ts_clock()

  openings = _openings_for_first(rows[0])
  for opening in openings:
    mutation = {**opening, "ts": next(clock)}
    mutation = _minted(mutation)
    mutations.append(mutation)
    lineage[mutation["id"]] = "(opening)"
  accounts: dict[str, float] = {}
  for opening in openings:
    for account, amount in opening["postings"].items():
      accounts[account] = round(accounts.get(account, 0.0) + amount, 2)

  for t in rows:
    amount = t.amount_eur or 0.0
    postings = _postings_for(t)
    effect = postings

    # Deltas of the recorded chain vs the replayed state (BEFORE this
    # row's own effects), in the chain's own order. Evidence keys:
    # balance_checking/balance_savings replay under "checking"/"savings".
    left: dict[str, float | None] = {}
    for account, recorded in (
      ("checking", t.balance_checking),
      ("savings", t.balance_savings),
    ):
      if recorded is None or account not in accounts:
        left[account] = None
        continue
      left[account] = round(recorded - accounts[account] - effect.get(account, 0.0), 2)

    final, rule = postings, None
    lc, ls = left["checking"], left["savings"]
    savings_delta = ls is not None and abs(ls) > TOLERANCE
    if (
      savings_delta
      and abs(ls + amount) <= TOLERANCE
      and (lc is None or abs(lc) <= TOLERANCE)
    ):
      # The row also moved savings by -amount: a transfer the category
      # convention missed (e.g. an "Ontsparen" typed as something else).
      final = {**postings, "savings": round(-amount, 2)}
      rule = "pair"
    elif (
      savings_delta
      and lc is not None
      and abs(lc + amount) <= TOLERANCE
      and abs(ls - amount) <= TOLERANCE
    ):
      # The amount belongs to savings entirely; checking did not move
      # (e.g. "Bijsturing Rente Spaarrekening" = Inkomsten -> savings).
      final = {"savings": round(amount, 2)}
      rule = "target-savings"
    if rule is not None:
      reclassified.append(
        f"{t.id} ({t.date} {t.description[:24]!r}): {rule} -> "
        f"{json.dumps(final, ensure_ascii=False)}"
      )

    base = {
      "date": t.date,
      "description": t.description,
      "category": t.category,
      "postings": final,
    }
    if (t.linked_id or "").startswith("e"):
      # Landed expected entry: the crossing is a CONFIRM — it retires
      # the e-row and materializes the actual in one mutation.
      base["op"] = "confirm"
      base["retires"] = t.linked_id
    else:
      base["op"] = "add"
    mutation = _minted({**base, "ts": next(clock)})
    lineage[mutation["id"]] = t.id
    mutations.append(mutation)

    for account, delta_amount in final.items():
      accounts[account] = round(accounts.get(account, 0.0) + delta_amount, 2)

    # Residual bridges: recorded balance not reproduced after this
    # row's (reclassified) effects — money moved that no transaction
    # explains. Owner policy: these become EXPLICIT correction
    # transactions (a real add mutation, external -> account), listed
    # for review. The reconcile op does not exist.
    for account, recorded in (
      ("checking", t.balance_checking),
      ("savings", t.balance_savings),
    ):
      if recorded is None or account not in accounts:
        continue
      excess = round(accounts[account] - recorded, 2)
      if abs(excess) <= TOLERANCE:
        continue
      delta = round(-excess, 2)
      correction = _minted(
        {
          "op": "add",
          "date": t.date,
          "description": f"Saldocorrectie {account} (bij {t.id}: {t.description[:32]})",
          "category": "Inkomsten",
          "postings": {account: delta},
          "ts": next(clock),
        }
      )
      mutations.append(correction)
      lineage[correction["id"]] = t.id
      accounts[account] = round(accounts[account] + delta, 2)
      corrections.append(
        f"{t.id} ({t.date} {t.description[:24]!r}): correction {account} "
        f"{delta:+,.2f} -> {recorded:,.2f}"
      )
  return mutations, lineage, reclassified, corrections


def _fold(mutations: list[dict]) -> tuple[dict[str, dict], dict[str, float], str, list[str]]:
  """Replay the journal. The mutations list IS the append order; replay
  follows it (the bootstrap built it in evidence order: openings, then
  per row: mutation, then its corrections). Returns (entries by id with
  computed balances, account balances at the end, tip id, entry ids in
  order). Openings are ordinary adds; everything is amounts."""
  accounts: dict[str, float] = {}
  entries: dict[str, dict] = {}
  order: list[str] = []
  tip = ""
  for mutation in mutations:
    tip = mutation["id"]
    op = mutation["op"]
    for account, delta_amount in mutation["postings"].items():
      accounts[account] = round(accounts.get(account, 0.0) + delta_amount, 2)
    if op in ("add", "confirm"):
      entry = {
        "id": mutation["id"],
        "date": mutation["date"],
        "description": mutation["description"],
        "category": mutation["category"],
        "postings": mutation["postings"],
        "linked": [mutation["retires"]] if "retires" in mutation else [],
        "balances": {a: v for a, v in sorted(accounts.items())},
      }
      entries[mutation["id"]] = entry
      order.append(mutation["id"])
  return entries, accounts, tip, order


def _mismatches(
  entries: dict[str, dict],
  rows: list[Transaction],
  lineage: dict[str, str],
) -> list[str]:
  """Per-row deviations of computed balances from the recorded chain.
  Compared against the LAST mutation linked to the row (a correction
  appended after the row's own mutation carries the final state)."""
  problems: list[str] = []
  for t in rows:
    jids = [j for j, src in lineage.items() if src == t.id and j in entries]
    if not jids:
      continue
    entry = entries[jids[-1]]
    balances = entry["balances"]
    for field, recorded in (
      ("checking", t.balance_checking),
      ("savings", t.balance_savings),
    ):
      if recorded is None:
        continue
      computed = balances.get(field)
      if computed is None or abs(round(computed, 2) - round(recorded, 2)) > TOLERANCE:
        problems.append(
          f"{t.id} ({t.date} {t.description[:24]!r}): {field} "
          f"recorded {recorded:,.2f} != folded {computed:,.2f} ({jids[-1]})"
        )
  return problems


def _prune_expected(rows: list[Transaction], landed: set[str]) -> list[dict]:
  """Hypotheticals for the new home: no balances, no source_line, and
  landed entries (confirmed into actuals) are excluded entirely."""
  kept = []
  for t in rows:
    if t.id in landed or not t.date:
      continue
    kept.append(
      {
        "id": t.id,
        "date": t.date,
        "description": t.description,
        "category": t.category,
        "subcategory": t.subcategory,
        "amount_eur": t.amount_eur,
        "status": t.status,
        "linked_id": t.linked_id,
        "flags": t.flags,
        "note": t.note,
      }
    )
  return kept


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
    "--out",
    type=Path,
    default=data_dir() / "migration",
    help="staging output directory (default: <data_dir>/migration)",
  )
  parser.add_argument("--force", action="store_true", help="overwrite existing outputs")
  parser.add_argument(
    "--max-report",
    type=int,
    default=10,
    help="max items per report block to print (default 10)",
  )
  args = parser.parse_args(argv)

  rows = [t for t in load_transactions() if SUMMARY_NOTE_MARKER not in t.note]
  dated = [t for t in rows if t.date]
  problems: list[str] = []
  for t in rows:
    if t.status != STATUS_ACTUAL:
      problems.append(f"{t.id}: status {t.status!r} — bootstrap expects actuals only")
    if t.date is None or t.amount_eur is None:
      problems.append(f"{t.id}: missing date or amount")
  if not dated:
    problems.append("no dated rows found")
  if problems:
    print("Bootstrap aborted — store problems:")
    print("\n".join(f"  - {p}" for p in problems[: args.max_report]))
    return 1

  # Evidence basis: FILE order — the recorded chain was maintained in
  # file order, so it is coherent there; date inversions are counted
  # and reported (informational). Same-date ties keep file position.
  inversions: list[str] = []
  for prev, cur in zip(dated, dated[1:], strict=False):
    if cur.date < prev.date:
      inversions.append(f"{prev.id} {prev.date} -> {cur.id} {cur.date}")
  if inversions:
    print(
      f"Legacy date-inversions in store: {len(inversions)} (informational, "
      f"first {min(len(inversions), args.max_report)}):"
    )
    print("\n".join(f"  - {p}" for p in inversions[: args.max_report]))

  first = dated[0]
  if first.balance_checking is None:
    print(f"First row {first.id} has no recorded checking balance — cannot seed.")
    return 1

  mutations, lineage, reclassified, corrections = _build_events(dated)
  entries, accounts, tip_id, order = _fold(mutations)
  mismatches = _mismatches(entries, dated, lineage)

  landed = {t.linked_id for t in dated if t.linked_id.startswith("e")}
  expected_out = _prune_expected(load_expected(), landed)
  rules_out = [asdict(r) for r in load_recurrences()]

  print(
    f"Rows: {len(dated)} committed, {len(expected_out)} expected kept, "
    f"{len(landed)} landed expected retired"
  )
  print(
    f"Mutations: {len(mutations)} ({len(mutations) - len(dated) - len(corrections)} openings + "
    f"{len(dated)} committed + {len(corrections)} corrections)"
  )
  print(f"Folded end balances: {', '.join(f'{k}={v:,.2f}' for k, v in sorted(accounts.items()))}")

  if reclassified:
    print(f"Reclassified rows: {len(reclassified)} (OWNER REVIEW — postings derived "
          f"from recorded balances; first {min(len(reclassified), args.max_report)}):")
    print("\n".join(f"  - {m}" for m in reclassified[: args.max_report]))
  if corrections:
    print(f"Correction transactions: {len(corrections)} (derived add mutations, "
          f"OWNER REVIEW; first {min(len(corrections), args.max_report)}):")
    print("\n".join(f"  - {m}" for m in corrections[: args.max_report]))

  # HARD gate: the folded tip must equal the recorded tip — the one
  # comparison that validates the entire amount chain end-to-end.
  tip = dated[-1]
  tip_checks = [
    ("checking", accounts.get("checking"), tip.balance_checking),
    ("savings", accounts.get("savings"), tip.balance_savings),
  ]
  tip_failures = [
    f"{name}: folded {folded!r} != recorded {recorded:,.2f}"
    for name, folded, recorded in tip_checks
    if folded is None or recorded is None or abs(round(folded, 2) - round(recorded, 2)) > TOLERANCE
  ]
  if tip_failures:
    print("VERIFICATION GATE FAILED — folded tip != recorded tip:")
    print("\n".join(f"  - {m}" for m in tip_failures))
    print("Nothing written. Per the design note: report to owner first.")
    return 1
  print(f"Tip check OK: folded == recorded ({tip.date} {tip.description[:24]!r})")

  # SOFT report: per-row deviations. After corrections this must be
  # empty — a survivor means a correction did not bridge its delta.
  if mismatches:
    print(f"Balance deviations after corrections: {len(mismatches)} "
          f"(UNEXPECTED — corrections should have bridged all deltas; "
          f"first {min(len(mismatches), args.max_report)}):")
    print("\n".join(f"  - {m}" for m in mismatches[: args.max_report]))

  outputs: dict[str, object] = {
    "journal.jsonl": mutations,
    "ledger.json": {
      "tip": tip_id,
      "entries": [entries[e] for e in order],
    },
    "expected.json": expected_out,
    "rules.json": rules_out,
  }
  args.out.mkdir(parents=True, exist_ok=True)
  for name in outputs:
    target = args.out / name
    if target.exists() and not args.force:
      print(f"{target} exists — use --force to overwrite (staging dir, live data untouched)")
      return 1
  for name, payload in outputs.items():
    target = args.out / name
    if name.endswith(".jsonl"):
      lines = [json.dumps(ev, ensure_ascii=False) for ev in payload]  # type: ignore[union-attr]
      target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
      target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
      )
    print(f"wrote {target}")
  print("Bootstrap complete. Verify contents, then wire step 2 (parallel append).")
  return 0


if __name__ == "__main__":
  sys.exit(main())