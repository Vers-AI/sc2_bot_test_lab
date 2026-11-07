# Lab Bridge — GitHub-facing gate API (v0.1 spec, approved 2026-11-06)

Generic lab infrastructure: any registered SC2 bot repo can ask the lab to play
a gate series for a branch and receive a structured verdict. No bot-specific
names, paths, or logic anywhere in this surface.

## Endpoints

### POST /api/trigger-gate/
Modeled on api_trigger_tests. CSRF-exempt, requires the shared bridge token header.

Input JSON:
- repo_url (str): bot repo whose branch is under test; must match a registered
  test bot's source.
- branch (str): branch to test. Worktree created/reused exactly as the existing
  branch machinery does (aiarena/worktrees/<sanitized_branch>/). The worktree is
  prepared with its submodules checked out (`git submodule update --init
  --recursive`): `git worktree add` does not check out submodules, so a bot that
  imports a pinned submodule would crash on import when it is started from a
  fresh worktree. A submodule checkout that fails fails the trigger with a clear
  error (500) instead of starting a series that can only crash.
  A repo may also provide a prepare script — `scripts/prepare_worktree.sh` or
  `scripts/vendor_lab_deps.sh` (first match wins) — for repos that vendor
  dependencies git ignores (an in-tree engine library, a map analyzer, compiled
  extensions): git restores none of those files in a worktree, so the lab runs
  the repo's own script in the fresh worktree instead of guessing which files
  matter. A prepare script that fails fails the trigger with a clear error (500)
  as well; repos without ignored dependencies need no script and are left
  untouched.
- test_bot_id (int): registered test-subject bot.
- opponent_bot_ids (list[int]): roster opponents for the series.
- games_per_opponent (int, default 3): series length per opponent.
- baseline (str, default "master"): comparison ref.

Returns 200 {"run_id": <test_group_id>} immediately. Returns 409
{"status": "busy", "run_id": <active_run_id>} if a gate is already active.

### GET /api/gate-status/<run_id>/
Returns JSON:
- status: "pending" | "running" | "complete"
- verdict: "pass" | "fail" | "invalid" (present when complete)
- noise_band: the active rule, stated in the response (default: within ±1
  game per games_per_opponent-series vs baseline)
- results: per-opponent record vs baseline: opponent id/name, wins, losses,
  crash counts (branch.crashes / baseline.crashes), the crash rates, durations,
  and the raw game list — raw numbers always exposed so a caller
  may apply custom judging without a lab change.

## Judging
Lab-side (design decision 1A, owner-approved): the noise band
(±1 game per games_per_opponent series, series length stated) is computed by
the lab so every project inherits identical verdicts; raw numbers are also
returned for custom judging.

The band is applied per opponent: the branch series may win at most one game
more and at most one game fewer than the same series on the baseline ref. A
single opponent outside the band fails the run.

Crashed games are counted separately from wins and losses: a crash is a broken
run, not a lost game. If the crash rate of either side of a series is above 50%,
the series says nothing about the bot's play and the verdict is "invalid"
instead of "pass" or "fail" — "invalid" wins over both. Without that rule a
baseline that crashed every game looks exactly like a baseline the branch
merely failed to beat, and the run is judged "pass" although nothing was
played.

## Concurrency
One gate run lab-wide at a time. Enforced server-side with a lock; second
requesters receive 409 + the active run id. This protects the
one-campaign-at-a-time rule regardless of caller.

## Scope (decision 2A)
v0.1: roster-vs-baseline gate only. Scenario runs (DebugSpawn state
manifestation, transition-event judging) are a later, additive extension —
keep the API shape extensible for them.

## Auth
Shared bridge token from lab config, passed as a header by callers (the
GitHub runner job). No per-bot credentials.

## Non-goals / unchanged
Match launching, the Compare tab (gate runs land as normal test groups,
visible in the UI), and the trigger-tests endpoint. The worktree machinery is
unchanged except that it now checks out the submodules of the worktree it
hands out and runs the repo-provided prepare script in it (see the trigger
section).

## Implementation notes (v0.1 clarifications)

These fill in the gaps the spec left open; they do not change the contract
above.

- **Token header**: `X-Lab-Bridge-Token: <token>`. `Authorization: Bearer
  <token>` is accepted as well. The token is read from the Django setting
  `LAB_BRIDGE_TOKEN`, which the standalone setup fills from the
  `LAB_BRIDGE_TOKEN` environment variable. There is no default value: when no
  token is configured the endpoints answer 503 instead of serving bridge calls
  unauthenticated. A missing or wrong token is answered with 401.
- **Run id**: the run id is the id of the branch test group, so a gate run is
  a normal test group in the results UI. The baseline series is played in a
  second test group, linked to the same run.
- **Lock**: the lock is a unique database column on the gate run row
  (`lock_slot`): it holds `1` while the run is active and `NULL` when the
  series is finished. The database therefore refuses a second active run —
  the lock also survives a server restart, unlike an in-process lock. A run
  whose series already finished releases its lock the next time the lab is
  asked for a new one, so a restart cannot wedge the lab.
- **Status**: `pending` while no game has a result yet, `running` while the
  series is being played, `complete` when every game of both series has a
  result. The verdict is only present when the run is complete.
- **Raw results**: `results` is a list with one record per opponent, holding
  the `branch` and the `baseline` summary (wins, losses, ties, unfinished,
  crashes, durations, duration total/median and the full game list) plus the
  win `delta`, whether the opponent stayed inside the band, the crash counts
  of both sides (`branch_crashes` / `baseline_crashes`), their crash rates and
  whether the series was invalidated by them (`crash_invalid`).
- **Crash counts**: `crashes` is the number of games of the series that
  crashed. Crashes also count as losses (that is how the results tab counts
  them), but they are counted on their own so the crash rate can be judged.
  A series with a crash rate above 50% on either side makes the whole run
  `invalid`.
- **Paired maps**: both series play the same maps for the same opponent and
  game index, so the comparison is not polluted by map differences.
- **Worktree submodules**: a worktree is only handed to a match after its
  submodules are checked out (`git submodule update --init --recursive`, with
  a timeout). The checkout is skipped when the submodules are already there,
  so reusing a worktree stays cheap. A checkout that fails aborts the
  trigger — a series that can only crash is worse than no series at all.
- **Worktree prepare step**: some repos vendor dependencies that git ignores
  (an in-tree engine library, a map analyzer, compiled extensions). Git
  restores none of those files in a worktree, so a bot started from a fresh
  worktree crashes on import. The lab does not guess which files a repo needs:
  the repo states it in a prepare script, and the lab runs it. The convention
  is two file names, looked up in this order (first match wins):
  `scripts/prepare_worktree.sh`, `scripts/vendor_lab_deps.sh`. The script is
  run in the worktree (the worktree is its working directory) with a generous
  timeout, and its result is logged. A repo without such a script is left
  untouched — no script is required. A script that fails aborts the trigger
  with the script name and its stderr, so a worktree whose dependencies were
  not prepared never reaches a match.
