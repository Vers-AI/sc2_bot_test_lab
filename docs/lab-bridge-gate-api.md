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
