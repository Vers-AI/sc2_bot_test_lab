# Roadmap

The lab's core is complete and proven: matches run, results and replays are
recorded, suites exist, and the trigger API works. What remains is the layer
that turns a match runner into a **CI/CD system for game bots** — the parts
that judge, report, integrate, and eventually deploy.

Status legend: ✅ done · 🔧 in progress · 📋 planned · 💡 exploratory

---

## Phase 0 — Match engine *(✅ complete)*

- [x] Bot registry (any bot: Python, C++, Java, …), per-bot source paths and overlays
- [x] Four match types: vs Blizzard AI, vs custom bot, vs past version (git A/B), replay continuation
- [x] Match queue with concurrency caps
- [x] Test suites (bundled matchups, e.g. the default 15-variant Blizzard AI suite)
- [x] Tickets + git worktrees for agent-assisted development
- [x] Trigger API (`POST /api/trigger-tests/`), including branch → worktree testing
- [x] Result recording, bot logs, replay capture

## Phase 1 — Benchmarks & regression verdict *(🔧 next)*

The core gap: the lab *records* results but doesn't *judge* them. A bot
regression suite needs a verdict, not a table.

- [ ] Regression verdict engine
      - [ ] Per-suite baseline win-rate (recorded from a reference run)
      - [ ] Pass bands (configurable: e.g. "≥ baseline − 5pp")
      - [ ] Verdict per suite run: `PASS` / `FAIL` / `FLAKY`
      - [ ] Flake policy for nondeterministic games: N-game samples,
            automatic re-run rule for borderline results
      - [ ] Auto-bisect: on FAIL, replay suite against last-good commit
            (past-version matches already exist — this is orchestration)
- [ ] Suite report view
      - [ ] Aggregate win rates per opponent / difficulty / map
      - [ ] Before/after comparison across suite runs
      - [ ] Trends over time
- [ ] Reference-opponent guide (config-only: registering deterministic
      benchmark opponents, recommended N per suite)

## Phase 2 — GitHub-native CI *(📋 planned)*

- [ ] Webhook receiver: verify GitHub signature, parse push/PR events,
      call the existing trigger API (per-repo, per-branch → test bot mapping)
- [ ] Report verdict back to GitHub (commit status / check run)
- [ ] Branch protection recipe: merge only when the suite passes

## Phase 3 — Continuous deployment *(📋 planned)*

- [ ] On-pass action hooks: generic "when a suite passes, fire this
      script/URL" (each bot keeps its own deploy script in its own repo —
      the lab only triggers it)
- [ ] Notification targets (Matrix / Discord / generic webhook) with the
      suite report summary; FAIL → block + notify with evidence
      (bot logs, replay links, verdict)
- [ ] Deployment modes: notify-and-approve first, full auto once the gate
      has a proven track record

## Phase 4 — Beyond SC2 *(💡 exploratory)*

The lab's skeleton — registry → queue → containerized run → verdict →
gate — is game-agnostic. Only the "arena adapter" layer is SC2-specific.

- [ ] Extract a `game adapter` interface from the runner
      (lifecycle: start match → stream state → capture replay → parse result)
- [ ] Second-game harness candidate: BAR (Beyond All Reason) — headless
      dedicated servers, native demo recording, skirmish AI interface
- [ ] Pattern-first, framework-second: build the second harness next to
      this one, then generalize from two real implementations

## Out of scope (deliberately)

- **RL training farms.** Training needs throughput (hundreds of parallel
  fast-reset environments); the lab is built for fidelity (real engine,
  full matches). The lab is the *referee*: training systems plug in as
  upstream producers and promote candidates through the evaluation gate.

---

Per-bot isolation: every match, test group, and suite result is keyed to the
bot under test (`test_bot`), and results are filterable per bot — bots never
share experiment history.