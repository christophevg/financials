# Candidate plan: Swift replatform (macOS menubar app + iOS app)

**Status: DORMANT — candidate plan, NOT activated.**
Brainstormed 2026-09-26 (owner + agent). The owner will decide later
whether to activate; this document records the scoping so the decision
can be made with full context. Nothing in the current Python codebase
changes as a result of this document.

---

## Origin

Two wishes emerged once the journaled-ledger CLI proved stable:

1. A real interactive UI (scrollable ledger view + quick actions),
   replacing the command-by-command CLI experience.
2. A mobile app to record expenses on the go **and consult the full
   ledger (balances, history, expected, rules) offline**, with final
   authority remaining the main ledger on the laptop.

## Hard constraints (owner-stated)

- The main ledger stays on the laptop and is **never exposed over the
  public internet**. No server reachable from outside.
- Sync is **device-to-device** between laptop and phone (same network
  or nearby), the phone works fully autonomously offline otherwise.
- The laptop is the **authority**: on-the-go changes sync back and are
  applied/confirmed as if entered at that point; conflicts are resolved
  in the macOS app.
- The owner wants to **judge every line of code** (as done in Python
  today) — learning Swift/iOS is an explicit goal. Every design must be
  explainable, not just delivered.
- During development, the **free Apple account + 7-day reinstall**
  ritual on the iPhone is acceptable. App Store publication is an
  optional later track, not a launch requirement.

## The decision

**Full replatform to Swift**: one Swift codebase containing the core
(fold, checkpoint, recurrences, expected, forecast) plus three front-ends:

- a Swift CLI (replaces the Python CLI; remains the scripting layer),
- a macOS menubar app (replaces the "TUI" idea; rich viewer + quick
  actions + interactive flows),
- an iOS app (fully equal to the macOS app, autonomous, syncs back).

Python is retired at the end of the migration. There is **no twin
implementation and no drift problem**: a single core runs on both
platforms. This reframing was what dissolved the main objection against
Swift (per-platform reimplementations drifting apart).

### Why the journal architecture makes this viable

- `journal.jsonl` is the data, not the code — implementation-independent
  by design. Swift reads the same journal/ledger/expected/rules files.
- Content-hash ids stay opaque identity tokens; the fold never
  recomputes them, so no hash reimplementation is needed.
- One migration gate: a **fold-equivalence test** (same journal through
  the Python fold and the Swift fold → identical output). After it
  passes and the Swift CLI survives daily use, Python retires.
- **SwiftData is designed out**: stores are files + JSON; the Swift core
  uses `Codable` and plain file I/O. (SwiftData was flagged as the
  agent's weakest training area; it is not needed.)

### Sync design sketch

- The phone accumulates its own journal segment; sync = **merge
  journal segments**; the laptop folds and becomes canonical; the phone
  reconciles against the returned checkpoint. Laptop authority is
  structural, not enforced.
- Transport candidate: **Multipeer Connectivity** (the AirDrop-style
  peer-to-peer framework) — device-to-device, nothing listening on the
  network, nothing internet-exposed. Open detail to verify at design
  time: Bluetooth-only transport between macOS and iOS (training-data
  uncertainty; LAN Wi-Fi via MPC is the reliable path either way).
- Rules (`recurrences.json`) and `expected.json` are small config-like
  stores and ride along in the same sync bundle.
- Conflict policy must be defined (single-user app: rare; e.g. a
  conflict is two divergent segments to reconcile in the macOS app UI;
  the fold defines what "merged" means).

## Milestones

| # | Milestone | Confidence |
|---|-----------|------------|
| M1 | Swift core package (Codable models, fold, checkpoint, recurrences, expected, forecast) + Python↔Swift fold-equivalence golden test. Gated via Makefile target running `swift test`. | High — pure algorithmic code; fully reviewable and headlessly testable. |
| M2 | Swift CLI at parity (view/add/edit/delete/expected/recurrence/report). Python stays production until this passes owner daily use. | High — thin wrapper over the same core. |
| M3 | macOS menubar app: viewer with full CLI capabilities, quick actions, interactive flows as forms/sheets. (Rich scrollable ledger in a real window spawned from the menubar icon, not cramped in the popover.) | Good — `MenuBarExtra` + SwiftUI is a well-covered pattern. |
| M4 | iOS app, fully equal to the macOS app (whole command surface in touch UI — the biggest single chunk of the plan). Slicing (capture + consult first, parity over time) is possible without violating the end state. | Realistic, but the long pole. |
| M5 | Native sync (Multipeer) + macOS conflict-resolution UI. | Good — with the BT-transport detail flagged for verification. |
| M6 | Optional: App Store publication (macOS+iOS) as a separate learning track. | Deferred; requires paid Apple account ($99/yr). |

### Verification split (honest)

- **Logic layer (M1, M2):** agent-implementable and gated — `swift test`
  via Makefile targets, same discipline as `make check` today.
- **UI/device layer (M3–M5):** Xcode lives on the owner's machine —
  agent explains everything, owner runs the simulator/device/signing
  steps (pair work). Signing/provisioning is the classic pain zone;
  expect pair-debugging. macOS free-account dev builds run locally
  indefinitely; the 7-day reinstall ritual bites on the iPhone only.
- **API churn risk:** newest SDK APIs may have moved past training data;
  verify current-API details (researcher agent) at implementation time.

### Learning support

A `c3:swift`-style skill (conventions, patterns, codebase layout) should
be created before/at activation, and expanded where the owner wants to
self-study (e.g. persistence, signing, store deployment).

## Sequencing rule

**Platform decision first, UI work second.** The Python Textual TUI idea
is retired under this plan (the menubar app *is* the "real UI"); it was
never started, so nothing is throwaway there.

## Risks / flags

- **Stalled-migration risk:** rebuilding a stable, daily-use system
  while it stays in production means a long dual-state period. The
  journal schema freeze (below) is what makes the dual state safe.
- **Journal schema freeze (needed from day one):** the on-disk format
  (`journal.jsonl`, ledger checkpoint, expected.json, recurrences.json)
  must be locked while Python keeps appending to it throughout the
  migration. Mostly documentation discipline — the format is already
  stable.
- **SwiftData/Core Data:** not used; if the owner later wants them for
  other projects, that is separate learning.

## Rejected / parked alternatives (for the record)

- **Python Textual TUI:** low risk and quick, but throwaway if Swift
  wins — hence retired under this plan.
- **Briefcase (Python on iOS):** zero-drift (same Python core ships),
  but Toga's UI ceiling is low for a rich financial dashboard; only
  wins if Python-on-iOS packaging itself is the learning goal. Same
  Apple-account situation as native for daily installs.
- **PWA + laptop server (baseweb reuse):** clarified during brainstorm
  = full core reimplementation in JS/TS (same size class as the Swift
  reimpl) + a listening LAN server on the laptop + HTTPS secure-context
  requirements for iOS PWA install/offline + Safari storage-eviction
  caveats. Loses on: server component, web UX ceiling, and a second
  implementation anyway.
- **Capture-only mobile scope:** much smaller, but rejected — the owner
  wants full offline consult of balances/history/expected/rules.

## Open decisions at activation time

- Repo layout: new repo vs. subdirectory in this repo.
- Golden-test harness details (how Python fold and Swift fold are fed
  the same journal in CI/Make).
- Sync transport verification (MPC over Wi-Fi vs. Bluetooth claim) and
  the exact sync bundle format.
- Whether M6 (App Store) is pursued at all.