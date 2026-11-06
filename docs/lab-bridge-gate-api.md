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
  branch machinery does (aiarena/worktrees/<sanitized_branch>/).
- test_bot_id (int): registered test-subject bot.
- opponent_bot_ids (list[int]): roster opponents for the series.
- games_per_opponent (int, default 3): series length per opponent.
- baseline (str, default "master"): comparison ref.

Returns 200 {"run_id": <test_group_id>} immediately. Returns 409
{"status": "busy", "run_id": <active_run_id>} if a gate is already active.

### GET /api/gate-status/<run_id>/
Returns JSON:
- status: "pending" | "running" | "complete"
- verdict: "pass" | "fail" (present when complete)
- noise_band: the active rule, stated in the response (default: within ±1
  game per games_per_opponent-series vs baseline)
- results: per-opponent record vs baseline: opponent id/name, wins, losses,
  durations, and the raw game list — raw numbers always exposed so a caller
  may apply custom judging without a lab change.

## Judging
Lab-side (design decision 1A, owner-approved): the noise band
(±1 game per games_per_opponent series, series length stated) is computed by
the lab so every project inherits identical verdicts; raw numbers are also
returned for custom judging.

The band is applied per opponent: the branch series may win at most one game
more and at most one game fewer than the same series on the baseline ref. A
single opponent outside the band fails the run.

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
Existing worktree machinery, match launching, the Compare tab (gate runs land
as normal test groups, visible in the UI), and the trigger-tests endpoint.

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
  durations, duration total/median and the full game list) plus the win
  `delta` and whether the opponent stayed inside the band.
- **Paired maps**: both series play the same maps for the same opponent and
  game index, so the comparison is not polluted by map differences.
