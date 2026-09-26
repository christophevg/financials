# Design Note: Journaled Ledger Migration

Status: agreed design, implementation pending
Date: 2026-09-18
Owner-approved roadmap; this note is its reference.

> **Redaction note (2026-09-26):** all concrete balance values, entry
> counts and merchant/counterparty names that referenced the owner's
> real data have been replaced by generic placeholders — this document
> ships in a public repository and must not carry personal financial
> traces. Verification gates in code assert structure (fold ≡
> checkpoint, presence, preservation), never concrete private values.

## What we agreed (the architecture)

**Sources of truth:**

- **`journal.jsonl`** — append-only journal of committed mutations. Two
  "Opening" add mutations (one per account, the seed balances — ordinary
  transactions; the first posting to an account creates it) + every
  atomic mutation after that. Immutable, never rewritten, never required
  for reads. The audit trail.
- **`ledger.json`** — the persisted consolidated view: entries + computed
  balances, in order, plus `tip` (last applied mutation id). The working
  snapshot; the read path is just "load this file".
- **`expected.json` / `rules.json`** — separate collections for
  hypothetical entries. NOT in the committed ledger: an expected entry is
  a statement about what might happen, a rule is a generator of such
  statements. They compose into the in-memory view as an ephemeral
  overlay, recomputed per view, never persisted into the ledger, never
  touching committed balances. No journal for them — the pre-mutation
  backup is their safety net.

**Derived state:**

- Balances are computed by folding amounts from the openings; the
  checkpoint stores them as first-class snapshot state (accepted —
  cheap per-row rehash guards the rows; fold==checkpoint via doctor is
  the heavy check).
- The in-memory ledger is disposable: `fold(journal)` reproduces it
  exactly. Destroying it loses nothing.

**Mutation protocol (every store, one shared helper):**

1. Backup: copy current file to `backups/<store>.<timestamp>.<ext>`
2. Journal append first (committed mutations only)
3. Apply to in-memory state
4. Persist atomically: temp file + `os.replace`, then prune old backups

Failure direction is always safe: a crash leaves the journal ahead of
the checkpoint (replayable), never the reverse; a failed write leaves
the original + backup intact.

**Mutation ops and idempotence guards (FINAL vocabulary):**

| op | payload | guard |
|---|---|---|
| `add` | id, ts, date, description, category, postings | same id already applied → skip (crash-retry only; identical content with distinct ts = distinct id, both apply) |
| `confirm` | id, ts, retires (the e-entry it lands) | same id already applied → skip |
| `update` | target id, full new field set | unknown target → no-op |
| `delete` | target id + victim pre-image (date/description/category/postings/linked — not balances) | pre-image mismatch → refuse loudly; unknown target → no-op (replay idempotence) |

- NO `opening` op (openings are ordinary adds; account creation is
  emergent — the first posting to an account creates it). NO
  `reconcile` op — corrections are entered by the owner as real
  transactions (policy: "I will always create a transaction
  implementing the mutation"). Both were removed after live testing.
- **Every mutation carries `ts`** (creation time, part of the hashed
  content): identical content then yields distinct ids — the Q8 lesson
  (2026-09-19): a content-only id collapsed two identical rows at fold
  time. Human entries rely on manual distinctness; machine-minted rows
  use a monotonic clock that never repeats.
- **Identity vs integrity (decided 2026-09-18):** a mutation's id IS
  its anti-corruption validator (16-hex content hash over the
  canonical mutation minus the id, rehashed on read). A transaction's
  id is assigned once by its creating mutation (add/confirm) and never
  recomputed — updates and balance propagation keep it, so `linked`
  references never need scanning. Ledger rows carry a separate `hash`
  (content minus the hash field) guarding the row on disk.
- A transfer is ONE mutation with two postings (`checking -500`,
  `savings +500`) — never two mutations. Postings/balances are flat
  account→amount dicts; one net effect per account per mutation.
- The current `journal.jsonl` audit trail is superseded: the mutation
  journal IS the audit trail. Old file is retired at step 6, not before.

## Composition (what a view command does)

```
in-memory view = committed ledger (from ledger.json)
                 ⊕ expected entries (overlay, window-expanded)
                 ⊕ rule expansions (overlay, window-expanded, superseded
                   per period by real entries)
```

Rules are never materialized. Expected one-offs live outside the
committed store and project from the tip of the fact chain. (Same
semantics as today's design — confirmed correct — new home.)

## The roadmap

Owner constraint: the current codebase is stable for day-to-day use;
defer all unrelated bug fixes during migration. The new machinery runs
in PARALLEL with the old until step 6 — every step is reversible, the
old path stays the source of truth serving views until step 5.

### 1. Bootstrap — `scripts/bootstrap_journal.py` (one-time)

- **DECIDED (2026-09-18):** openings = earliest recorded balances; all
  later recorded balances become the verification oracle.
- Seed derivation: `opening = first row's recorded balance − that row's
  own effect on the account`. Worked example (t0001, 2024-11-24, Huur,
  +350,00, recorded 3.126,47 / 20.000,00):
  - Opening add: `{"op": "add", "ts": ..., "date": "2024-11-24",
    "description": "Opening", "category": "Inkomsten", "postings":
    {"checking": 2776.47}}` (3126.47 − 350.00)
  - Second opening: same shape with `"postings": {"savings": 20000.00}`
    (no transfer in t0001 → the recorded savings IS the pre-balance)
  - Then `{"op": "add", "date": "2024-11-24", "description": "Huur",
    ..., "postings": {"checking": 350.00}}`
  - First replay then yields 3.126,47 = t0001's recorded balance — the
    chain's first recorded balance is the first verification checkpoint.
  - General rule for savings openings: the first recorded savings value
    when no transfer precedes it; if the first row IS a transfer, apply
    the minus-effect to that account as well.
- Read current `transactions.json`; emit `journal.jsonl`:
  - two "Opening" add mutations (per the seed rule above),
  - one `add` mutation per actual transaction (in file order),
  - `confirm` mutations for landed expected entries (carrying
    `retires` = the e-id) so the expected store's history stays
    coherent,
  - explicit correction transactions for the residual bridges
    ("Saldocorrectie …", listed for owner review).
- Emit `ledger.json` (checkpoint: tip + entries) + `expected.json`/
  `rules.json` in the new locations — **pruned**: expected/rules carry
  no balances (or other ledger-derived fields) in the new home; the
  migration strips them rather than carrying them over.
- **Verification gate:** run fold over the new journal; assert the
  computed tip equals the recorded tip (HARD — the full amount chain
  validates through this one comparison) and report per-row deviations
  (must be empty after corrections). The bootstrap does NOT proceed on
  HARD failure — report to owner first. Fallback if the earliest seed
  fails the gate: pin at a bank-statement date the owner trusts
  (entries before it keep no computed balances).
- Rollback: delete the new files; nothing else was touched.

### 2. Parallel append (stub)

- Add `apply(mutation)` to the store layer. Called from every mutating
  command path (add/edit/delete/fixes/expect), AFTER the current
  mutation succeeded — a pure observer, return value ignored, wrapped
  so it can never break the old path.
- Initially a stub (validates JSON, appends one line, prints nothing).
- **Verification gate:** existing suite green; journal grows one line
  per mutation; a deliberately broken stub must not break commands
  (proves the parallel path is truly non-load-bearing).

### 3. Implement apply()

- Real fold: add/confirm/update/delete per the FINAL op table
  (`src/financials/journal.py` already implements the mutation classes,
  `apply_on` handlers, dedupe guard, ledger insert/propagate/update/
  delete, and content-hash id minting — extracted, not rebuilt).
- **Verification gate:** fold(journal) == ledger state, asserted in
  tests with synthetic journals (each op, each guard).

### 4. Close the loop

- Persist ledger.json after every applied mutation (atomic write +
  watermark = last applied seq).
- **Verification gate:** kill -9 a command mid-mutation (or simulate):
  next command detects watermark gap, replays tail, converges.

### 5. Load on boot

- Every command boots from ledger.json + tail replay; expected/rules
  overlay composes on top.
- **Verification gate:** old and new pipelines produce identical view
  output on the owner's real data (shadow-compare in tests).

### 5b. Switch views

- Rewire view commands to the in-memory ledger. Old pipeline stops
  being the source of truth; keep it callable behind a flag until 6.
- **Verification gate:** owner's daily commands against real data;
  owner sign-off before 6.

### 6. Dispose the old path

- Remove the legacy stores and mutation paths made redundant. The
  current audit `journal.jsonl` retires here (superseded by the event
  journal). Deletion list presented for approval BEFORE removal
  (deletion discipline).
- Rollback: git history + backups folder.

### 7. Harden

- The shared `persist()` helper (backup → atomic write → prune) is
  actually introduced here if not already; ensure expected/rules
  mutations route through it. Doctor: chain replay validation,
  watermark agreement, id uniqueness, journal/checkpoint agreement.

## Explicit non-goals (from the design discussion)

- Expected/rules data is migrated **pruned**: balances (and other
  ledger-derived fields) are stripped at bootstrap — hypotheticals carry
  no computed balances in the new model.
- No statistical forecasting; rules expand, one-offs land, period.
- No daemon/session model; per-invocation process, ledger.json is the
  shared state.

## Open decision points (owner decides during implementation)

1. ~~`reconcile` strictness~~ — **DECIDED 2026-09-18**: the reconcile op
   does not exist; corrections are owner-entered transactions.
2. ~~`amend` shape~~ — **DECIDED via journal.py `update`**: full
   replacement of the mutable fields (description, category, postings,
   date); date moves re-insert. Id and `linked` ride along.
3. Backup retention N (default proposed: 20 per store) — still open.
4. ~~Bootstrap start date for openings~~ — **DECIDED 2026-09-18**:
   earliest recorded balances as the two openings (see worked example
   in step 1); all later recorded balances = verification oracle;
   fallback = pinned bank-statement date if the gate fails.
5. ~~Op vocabulary~~ — **DECIDED 2026-09-19**: `add | confirm | update |
   delete` (no opening, no reconcile — see the FINAL op table).

## Migration-relevant context from the current codebase (2026-09-18)

- Chain-tip convention just fixed and committed (5774ee7): `chain_last()`
  in entry.py — highest `(date, source_line or 10**9)`, full ties by
  list position. Added rows carry `source_line=0` and rank above
  imported rows. Used by `_build`, `_prompt`, `_anchor_row` (ledger).
  The new model makes this convention largely moot (no stored chain to
  mis-seed) — listed so a fresh session knows the recent history and
  doesn't re-litigate it.
- Suite at 130 tests, `make check` green (lint → mypy → test).
- Data files under `~/.financials/data`: transactions.json (~1.4k rows,
  first row t0001 2024-11-24 Huur +350,00, recorded 3.126,47/20.000,00),
  expected.json, recurrences.json, journal.jsonl (current audit trail,
  retired at step 6), cleanup_fixes*.
- `rebase_checking` (fixes.py) already implements the replay rule
  "keep what replays correctly, replace what doesn't" — reusable
  reference for the fold. `linked_id` on Transaction anticipates the
  confirm pairing. `_walk`/`_ledger_rows`/`_footer_lines` (ledger.py)
  are the existing read-time projection the overlay must compose with.

## Step 1 executed (2026-09-18) — bootstrap results & decisions

`scripts/bootstrap_journal.py` is implemented and PASSED its gate
(staging output in `~/.financials/data/migration/`:
events.jsonl N = 2 openings + committed rows + 3 reconciles;
ledger.json tip <last-event-hash>; expected.json pruned; rules.json
as-is).

- **Journal id spec (DECIDED 2026-09-18, supersedes seq/watermark):**
  events carry NO sequence numbers — the row order IS the order, and
  appends are O(1) (no read-before-write). `id` = 16-hex sha256 over
  the CANONICAL event minus the id field. Canonicalization is ONE
  frozen function (sorted keys, compact separators, ensure_ascii=False,
  amounts 2-decimal rounded) — defined once, versioned here. Properties:
  per-row self-validation (doctor rehashes), idempotent appends
  (re-append ⇒ same id ⇒ dedupe by id-set), deterministic bootstrap.
  Structure damage (delete/reorder) is NOT the hash's job: the
  checkpoint's `tip` id + backup path own it. Rejected: position-in-hash
  (appends would need a read to know the index — kills O(1) append);
  chain-hash (records the upgrade path: one concat, verification
  replays the chain, append stays O(1) — adopt only if the reorder
  blind spot ever matters).
- Replay-order decision (owner): fold replays in (date, file-position)
  order — "balances only mean something today; historic balances are
  mere indicators." The EVIDENCE pass walks FILE order (the recorded
  chain was maintained in file order; it is coherent there).
- 47 legacy date-inversions: informational, tolerated.
- Verification gate is two-tier: HARD = folded tip == recorded tip
  (validates the whole amount chain end-to-end);
  SOFT = historic per-row deviations — 397 under the pre-hash draft
  (date-order fold), **3 after the file-order fold: exactly the 3
  pinned events, by construction**.
- Checking chain: fully derived from amounts, ZERO reconcile pins.
- Mutation triad decided (owner): every transaction is a mutation
  source -> target with defaults (external -> checking); Overdracht
  demotes from mechanism to label; the bootstrap derives postings from
  recorded-balance evidence. Future: config rules set source/target
  from category (e.g. Sparen -> checking->savings); explicit flags
  override. Precedence: hard defaults < config rules < explicit flags.
- 3 genuine reconcile pins (unexplained savings facts, each with its
  source row for future promotion to explicit postings):
  t0175 rente bijsturing +105,34 (would become Inkomsten --target
  savings), t0295 Ontsparen -> 0,00, t0472 WIT -> 0,03.
- Reclassification pass found ZERO amount-explainable hidden movers —
  the earlier 12 candidates were degenerate 0-delta false positives
  (now suppressed: rules require a non-zero savings delta).
- **Final event schema (owner, 2026-09-18):** journal events carry
  NO provenance and NO derived-markers — no `source`/t-id/origin, no
  `derived` flag (a correction is a real transaction; its provenance
  lives in the description text "Saldocorrectie … (bij t0175 …)",
  human-readable, not a structural field). Confirm events reference
  the retired e-entry via **`retires`** (`target` is the update-op's
  target id). Account keys unified 2026-09-19: `savings` everywhere
  (events, postings, balances, checks). Op vocabulary FINAL per
  2026-09-19: `add | confirm | update | delete` — no opening op
  (openings are ordinary adds), no reconcile op at all, not even
  historically. The 3 historical bridges are explicit correction
  TRANSACTIONS (add mutations, external -> account): t0175 rente
  bijsturing +105,34, t0295 +25,87, t0472 +0,03. POLICY: the owner
  enters a transaction implementing any needed correction; no code
  ever synthesizes balance pins.
- **ts on every mutation (2026-09-19):** identical content + identical
  ts = identical id (content hash), so a content-only id COLLAPSED two
  identical Q8 rows at fold time (found by owner's replay test). Fix:
  `ts` (creation time) is part of the hashed content — identical
  content with distinct ts stays two rows. Machine-minted rows use a
  monotonic microsecond clock (never repeats); human entries rely on
  manual distinctness. The `apply_on` dedupe guard is thereby
  harmless-by-construction (fires only on a true same-id re-append).
- **Step 1 verified end-to-end (2026-09-19):** journal.py replayed the
  bootstrap's journal.jsonl → correct final balances.
  The engine folds date-positioned (vs bootstrap's file-order evidence
  pass) and the tip still holds — first live validation across the 47
  inversion rows.
- **Checkpoint entry schema (owner, 2026-09-18):** entries carry
  `linked` as a LIST (future-proof: one transaction may link several
  others) and `postings`/`balances` as FLAT DICTS `{account: amount}`
  (one net effect per account per event — access is a get, not a walk;
  accounts are data, not schema: a third account needs no code change).
  Checkpoint top level: `{"tip": <last APPLIED event id>, "entries":
  [...]}` — tip is the fold position (amends/tombstones append without
  minting entries, so it is NOT derivable from entries); `accounts`
  dropped as redundant (entries always carry fresh balances).
- Account keys in events/postings: unified to "checking"/"savings"
  (2026-09-19; the store's legacy balance fields are
  balance_checking/balance_savings — bootstrap maps them).
- Next: step 2 (parallel append stub) per the roadmap below.

## Step 3 executed (2026-09-21) — the canonical fold

`fold()` extracted in `src/financials/journal.py` (the fold already
existed in the `__main__` debug block and the bootstrap's evidence pass;
this is the canonical version): replays a journal into a `Ledger`,
returns `(ledger, tip)`. Verification gate: synthetic-journal tests for
every op and guard (suite now 139 tests + 1 live gate).

- **Fold-order reconciliation (DECIDED 2026-09-21, closes the open
  point):** the CANONICAL checkpoint fold is the engine's
  date-positioned insert; the bootstrap's file-order fold remains the
  one-time evidence pass inside the script only. The tip is the last
  APPLIED mutation in JOURNAL order — entry order and journal order are
  independent. Both folds agreed on the real data.
- **fold() verifies before applying:** every row is rehash-checked
  BEFORE apply — the id IS the row's anti-corruption validator, so a
  tampered row stops the fold loudly (never a silently wrong
  checkpoint). First live run caught 4 real stale-id rows (below).
- **Update carries linked (engine fix):** the synthetic gate exposed
  that `UpdateMutation.apply_on` dropped `linked` when the mutation
  carried none — the design (open point 2, decided 2026-09-18) says id
  and `linked` ride along. `apply_on` now looks up the live row and
  carries its `linked` through; unknown target stays a no-op.
- **INCIDENT (repaired): 4 stale observer ids (2026-09-19 writer bug).**
  The intermediate step-2 writer stamped `ts` into the journal line but
  minted the id over content WITHOUT ts (the final code mints after
  stamping; one of the four rows was written by the intermediate state).
  The four rows (four confirm entries, 2026-09-10..14) carry ids
  matching neither with-ts nor without-ts rehash. Content is correct
  (balances tip out exactly); only the ids are stale. Owner-approved
  repair (2026-09-21): re-mint the ids from each row's own content —
  `remint()` in journal.py rewrites ONLY the mismatched lines (everything
  else byte-identical, dry-run default, timestamped backup, post-fold
  verification gate); `scripts/remint_journal_ids.py` (Makefile:
  `migration-remint`) is the one-time runner. The live fold test skips
  with a pointer to the script until the owner runs it, then hard-runs.
  Original ids remain traceable via the old audit journal.jsonl.
  REPAIR EXECUTED 2026-09-21: owner ran the script (dry-run: 4 rows;
  --write applied with timestamped backup + fold gate; re-scan clean).
  Live fold gate now hard-runs green — 140 passed, 0 skipped. Step 3
  fully closed.
- **Live fold gate added:** `test_fold_of_real_bootstrap_journal` folds
  the real staging journal (skips when absent) and asserts the full
  bootstrap entry set folds clean — proving bootstrap-hash ≡
  engine-hash on all real rows once the repair lands.

## Step 4 executed (2026-09-21) — the checkpoint closes the loop

`checkpoint(journal, path=None)` in journal.py: folds the journal
(per-row hash-verified) and writes ledger.json — {"tip", "entries"} —
atomically (temp + os.replace). Wired into `_observe` after the journal
append, so every mutation that observes also persists; the whole
wrapper stays best-effort (non-load-bearing until step 5/6). Suite now
144 tests, make check green.

- **Watermark = tip (DECIDED 2026-09-21):** the roadmap's "last applied
  seq" predates the id-spec (no sequence numbers — the row order IS the
  order); the checkpoint tip IS the watermark, so a crash that leaves
  the journal ahead is detected by the next persist, whose fold replays
  the tail and converges the checkpoint. Convergence is structural:
  checkpoint() always folds the WHOLE journal, so any gap heals on the
  next persist — no separate tail-replay mechanism needed.
- **Failure direction verified:** kill-9 simulation test — mutation
  journaled, persist never ran; the next persist folds the tail and the
  checkpoint converges to the journal tip. Also verified through the
  real command path (apply_ops -> _observe -> append + persist), with a
  pre-existing gap replayed by the command's own persist.
- **Persist default path:** journal.jsonl's sibling ledger.json (the
  bootstrap's staging layout); tests pass explicit tmp paths.
- **RECORDED GAP (for step 5/6):** field-op edits (the apply_ops
  field-op branch) are still UNOBSERVED — the step-2 projection covers
  only remove_row/add_row/add_expected_row. An edit via apply_ops
  appends the old-path audit line but no mutation and no persist. The
  entry/edit/fixes funnel (entry.add_transaction, edit e→actual
  confirm, apply_ops row ops) observes; field edits don't. Step 5
  closes this when views move to the ledger (edits become update
  mutations) — noted so it is not forgotten.
- **Live parity gate added:** test_checkpoint_agrees_with_fold_on_
  real_journal — persists the real checkpoint to tmp and asserts tip +
  balances + entry count agree with the fold; runs on every suite run
  against the owner's real data.
- Next: step 5 (load on boot: every command boots from ledger.json +
  tail replay; expected/rules overlay composes; shadow-compare old vs
  new view output on real data).

## Step 5 executed (2026-09-21) — boot from checkpoint + tail replay

`load_ledger(journal, path=None)` in journal.py: hydrate the checkpoint
(hash-guard-verified) → walk the journal, hash-verify every row, skip to
the watermark, apply the tail → converged in-memory ledger. Checkpoint
rows now carry the on-disk hash guard (`row_dict`/`rowlist`; hydrate
verifies when present, accepts bare bootstrap-era rows). `checkpoint()`
persists guarded rows from now on. Suite: 149 tests, make check green.

- **Boot semantics (per the design):** no checkpoint → self-heal by full
  fold + persist (the in-memory ledger is disposable); gap → tail replay
  beyond the watermark (hydrated+replayed ledger == full fold, asserted);
  truncated/reordered journal (tip never appears) → REFUSE LOUDLY
  (structure damage is the tip + backups' job). Corrupt row → refuse.
- **Shadow-compare gate (headline, on real data):**
  test_shadow_compare_real_data — old pipeline (load_transactions,
  dated non-summary rows = the bootstrap's evidence basis, 1317 rows)
  vs new pipeline (load_ledger on the real staging journal+checkpoint):
  1:1 IN-ORDER pairs on (date, description, category, postings) — exact
  match; transfers carry both postings; the 2 openings + 3 corrections
  are structural (skipped). New count = old + 5 asserted. Balance
  COLUMNS are not parity targets (step-1 ruling: computed indicators —
  and the tip balances are separately pinned by the step-3/4 gates).
- **RECORDED GAP CARRIES FORWARD (step 5b/6):** field-op edits via
  apply_ops remain unobserved (see step-4 note); views still boot from
  the OLD pipeline (load_transactions) — step 5b rewires them.
- Next: step 5b (switch views: rewire view commands to the in-memory
  ledger; old pipeline callable behind a flag until 6; gate = owner's
  daily commands against real data + owner sign-off).

## Step 5b executed (2026-09-21) — the view switch (decision: hash ids)

OWNER DECISION (asked & answered): the owner-facing id IS the journal's
content-hash id — `list` shows it, `edit`/`delete` accept full or
unambiguous-prefix hex ids; friendly refs are cosmetic, deferred. "Start
with what works."

`src/financials/ledger_view.py` (new): the new pipeline's view layer —
`load_ledger_view()` (boot: checkpoint + tail replay), `view_rows()`
(ledger → old-model Transaction projection: id=hash, amount=checking
posting, balances=computed chain, raw category fields mirror canonical),
`pair_rows()` + `hash_target_map()` (positional hash↔t-id pairing,
structural rows skipped — same rule the step-5 shadow gate proved),
`resolve_hash_id()` (full/prefix, ambiguity raises loud),
`print_ledger_view()` (list on the new pipeline: same table/projection/
footer as old print_ledger; structural rows excluded from the actuals
window; anchor = chain-last non-structural row).

- **cli.py:** `list` + `report` now default to the NEW pipeline;
  `--legacy` flag keeps the old pipeline callable until step 6.
- **find_entry (model.py):** hash-shaped ids (4–16 hex) resolve through
  the pairing map after the t/e prefix branches (legacy ids still win);
  ambiguity raises loud.
- **Observer completed (the step-4 recorded gap CLOSED):**
  - remove_row's DeleteMutation now targets the victim's JOURNAL id via
    the pairing map (latent divergence fixed: previously targeted the
    t-id, which the fold would no-op).
  - field-op edits (apply_ops) now project the FINAL row state as one
    UpdateMutation per edited row (idempotent, final-state semantics —
    the guard "expected current value" keeps from-guarding).
  - add_row projects real content; add_expected_row projects NOTHING
    (expected entries are overlay, not committed facts — design).
  - Unmappable victim (not yet in the ledger) → no projection (loud
    no-op deletes are worse than silence).
  - checkpoint persist DECOUPLED from projections: apply_ops persists
    after every mutating command (even with no projection) — the step-4
    gap-convergence property no longer depends on observation.
- **Engine tests neutralize the pairing bridge** (`_isolate` monkeypatches
  hash_target_map → {}): the positional pairing is only valid when both
  pipelines hold the same rows; arbitrary synthetic stores would pair
  falsely. Bridge behavior gets explicit-map tests + real-data gates.
- **Balance parity at correction anchors is BY DESIGN different:** the
  ledger's computed chain shows the pre-correction value at anchor rows;
  the following Saldocorrectie restores agreement (step-1 design: no
  reconcile op, corrections are explicit transactions). The step-5 gate
  pins the TIP; the 5b view gate skips anchor rows for
  balance parity (content compared fully).
- **Live gates (real data, in suite):** test_view_rows_match_old_pipeline
  (1317 rows content+balance parity, anchors exempted),
  test_hash_target_map_pairs_real_data (1:1, both directions),
  test_find_entry_resolves_hash_id; observer tests with explicit maps
  (field edit → UpdateMutation with journal-id target; delete → journal-id
  target, fold REMOVES). CLI smoke via make run: new `list` renders
  identical content + identical footer projections as --legacy.
- Suite: 154 tests, make check green.
- Next: step 6 (dispose the old path — requires OWNER SIGN-OFF; the
  deletion list gets presented for approval; old pipeline's `--legacy`
  flag removed last).

## Step 6 executed (2026-09-21) — the write-path switch + the migration is COMPLETE

- `commands.py` cmd_add/cmd_update/cmd_delete construct real mutations,
  append, engine applies. Fix field-ops → UpdateMutations; global ops
  rejected loudly. `--legacy` flag + old print_ledger retired.
- Data PROMOTED (scripts/promote_stores.py, owner-run): journal.jsonl +
  ledger.json canonical at data/, old stores retired to backups/
  (*.retired-20260921-170535); staging rules.json was EMPTY (live store
  absent — missing == empty for load_recurrences; D5 redesigned: absent
  live store + non-empty staging copy promotes, divergence refuses).
  Post-verify: boot from promoted location == pre-flight fold.
- Deletion lists C1–C8 executed (last: importer.py + test_importer.py,
  commit b824646). Reference healing: README/AGENTS.md describe the
  journaled ledger.
- Post-promotion bug (caught by suite): load_expected crashed on the
  pruned baseline shape → Transaction raw fields/amount/status default.
  conftest isolates the expected store (test pollution class closed).
- Live gates re-pointed at data/journal.jsonl (post-promotion they had
  kept pointing at migration/ and skipped); shadow gate retired with the
  old store. Final: 118 passed, 0 skipped. Commits: 01ec1d9, b08a2a3,
  be4fecf, 3e4df27, 75a03d8, b824646, 4889e10.
- Owner CONFIRMED daily use post-promotion. Migration COMPLETE; next:
  phase 7 — recurrence CLI (add/list/pause/resume/detect) on the existing
  recurrences.py machinery (zero tests today — the phase starts there).
