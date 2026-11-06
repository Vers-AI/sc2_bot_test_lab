"""Lab-bridge gate API — a branch series judged against a baseline series.

The gate API is the GitHub-facing surface of the lab.  A caller asks the lab
to play a roster series on a branch; the lab plays the *same* series on a
baseline ref and compares the two with a fixed noise band, so every project
inherits an identical verdict.  The raw numbers are returned as well, so a
caller can apply its own judging without changing the lab.

Everything in this module is generic: callers address bots by their
registered ids and the lab resolves directories, worktrees and maps.

Contents:

* the shared bridge token check,
* the lab-wide concurrency lock (one gate run at a time),
* the noise-band verdict computation and the crash-rate invalidation
  (pure functions, unit tested),
* the background launcher that plays the branch and baseline series.
"""

from __future__ import annotations

import logging
import os
import secrets
import statistics
import threading
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger('test_lab')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Headers that may carry the shared bridge token.
BRIDGE_TOKEN_HEADERS = ('X-Lab-Bridge-Token', 'X-Bridge-Token')

#: A branch series passes when it wins no more than this many games more or
#: fewer than the same series on the baseline ref.
NOISE_BAND_TOLERANCE = 1

#: A series in which one side crashes in more than this share of its games
#: carries no information about the bot's play: a crashed game would
#: otherwise count as a lost game, and two sides that both crashed look
#: like a tie.  Such a run is reported as ``invalid`` instead of pass/fail.
CRASH_RATE_LIMIT = 0.5

#: Defaults for the trigger endpoint.
DEFAULT_GAMES_PER_OPPONENT = 3
DEFAULT_BASELINE = 'master'

#: Upper bound for one series (a sanity guard, not a policy).
MAX_GAMES_PER_OPPONENT = 50

#: How often the watcher thread re-checks a running series.
WATCH_POLL_SECONDS = 15

#: How long the watcher waits before it gives up on a stuck series.
WATCH_MAX_SECONDS = 6 * 60 * 60

#: A game that has been running longer than this (the docker runner gives up
#: after two hours) counts as stale and is recovered.
STALE_RUN_SECONDS = 2 * 60 * 60

#: A run that did not get a single game off the ground after this long is
#: considered dead (e.g. the server died between creating the run and
#: launching the series).
UNSTARTED_RUN_SECONDS = 15 * 60

#: Match results that mean "the game has not produced an outcome yet".
UNFINISHED_RESULTS = ('Pending', 'Queued', '')


class GateBusy(Exception):
    """Raised when another gate run already holds the lab-wide lock."""

    def __init__(self, run_id: int | None):
        super().__init__(f'A gate run is already active (run {run_id})')
        self.run_id = run_id


# ---------------------------------------------------------------------------
# Bridge token
# ---------------------------------------------------------------------------

def get_bridge_token() -> str:
    """Return the configured bridge token (empty when not configured).

    The token is read from the Django setting ``LAB_BRIDGE_TOKEN`` and falls
    back to the ``LAB_BRIDGE_TOKEN`` environment variable, so the app also
    works when it is mounted inside a project that does not define the
    setting.  There is no default value: an unconfigured lab refuses
    bridge calls rather than serving them unauthenticated.
    """
    token = getattr(settings, 'LAB_BRIDGE_TOKEN', '') or ''
    if not token:
        token = os.environ.get('LAB_BRIDGE_TOKEN', '') or ''
    return str(token).strip()


def bridge_token_configured() -> bool:
    """Return True when a bridge token is configured on this lab."""
    return bool(get_bridge_token())


def _token_matches(value: str, expected: str) -> bool:
    """Compare two tokens in constant time."""
    try:
        return secrets.compare_digest(
            value.encode('utf-8'), expected.encode('utf-8'),
        )
    except (AttributeError, UnicodeError):
        return False


def check_bridge_token(request) -> bool:
    """Return True when *request* carries the configured bridge token."""
    expected = get_bridge_token()
    if not expected:
        return False

    for header in BRIDGE_TOKEN_HEADERS:
        value = request.headers.get(header)
        if value and _token_matches(value.strip(), expected):
            return True

    # Also accept "Authorization: Bearer <token>" for callers that can only
    # set the standard authorization header.
    authorization = request.headers.get('Authorization', '') or ''
    if authorization.lower().startswith('bearer '):
        token = authorization[7:].strip()
        if token and _token_matches(token, expected):
            return True

    return False


# ---------------------------------------------------------------------------
# Noise band (pure logic)
# ---------------------------------------------------------------------------

def noise_band_rule(
    games_per_opponent: int,
    tolerance: int = NOISE_BAND_TOLERANCE,
) -> str:
    """Return the active noise-band rule, stated for the API response."""
    return (
        f'within ±{tolerance} game per {games_per_opponent}-game series '
        f'vs baseline'
    )


def evaluate_noise_band(
    branch_wins: dict[int, int],
    baseline_wins: dict[int, int],
    tolerance: int = NOISE_BAND_TOLERANCE,
) -> tuple[str, list[dict]]:
    """Compare per-opponent win counts and return ``(verdict, details)``.

    *branch_wins* / *baseline_wins* map an opponent id to the number of
    games the test bot won in that series.

    The verdict is ``'pass'`` when every opponent stays inside the band
    (the branch series wins no more than *tolerance* games more or fewer
    than the baseline series) and ``'fail'`` otherwise.  A comparison
    without any opponent fails — there is no evidence of a pass.
    """
    opponent_ids = sorted(set(branch_wins) | set(baseline_wins))
    details: list[dict] = []
    verdict = 'pass'

    for opponent_id in opponent_ids:
        branch = int(branch_wins.get(opponent_id, 0))
        baseline = int(baseline_wins.get(opponent_id, 0))
        delta = branch - baseline
        within_band = abs(delta) <= tolerance
        if not within_band:
            verdict = 'fail'
        details.append({
            'opponent_id': opponent_id,
            'branch_wins': branch,
            'baseline_wins': baseline,
            'delta': delta,
            'within_band': within_band,
        })

    if not details:
        verdict = 'fail'

    return verdict, details


def evaluate_verdict(
    branch: dict[int, dict],
    baseline: dict[int, dict],
    tolerance: int = NOISE_BAND_TOLERANCE,
) -> tuple[str, list[dict]]:
    """Compare the two series and return ``(verdict, details)``.

    *branch* and *baseline* map an opponent id to a series summary (the
    dict produced by :func:`summarize_series`).

    The verdict is ``'pass'`` when every opponent stays inside the noise
    band, ``'fail'`` when one does not and ``'invalid'`` when a series
    crashed so often that the comparison says nothing about the branch.
    A crashed series would otherwise be counted as a lost series, and a
    series where one side crashed every game looks exactly like a series
    where the branch simply won no more games than the baseline — a
    false pass.  ``'invalid'`` therefore wins over both other verdicts.

    The details carry the crash counts and rates of both sides, so the
    caller can see *why* a run was invalidated.
    """
    branch_wins = {
        opponent_id: int(summary.get('wins', 0))
        for opponent_id, summary in branch.items()
    }
    baseline_wins = {
        opponent_id: int(summary.get('wins', 0))
        for opponent_id, summary in baseline.items()
    }
    verdict, details = evaluate_noise_band(branch_wins, baseline_wins, tolerance)

    for detail in details:
        opponent_id = detail['opponent_id']
        branch_summary = branch.get(opponent_id) or {}
        baseline_summary = baseline.get(opponent_id) or {}

        branch_crashes = int(branch_summary.get('crashes', 0))
        baseline_crashes = int(baseline_summary.get('crashes', 0))
        branch_rate = crash_rate(
            branch_crashes, int(branch_summary.get('games', 0)),
        )
        baseline_rate = crash_rate(
            baseline_crashes, int(baseline_summary.get('games', 0)),
        )
        invalid = (
            series_crashed_out(branch_summary)
            or series_crashed_out(baseline_summary)
        )
        detail.update({
            'branch_crashes': branch_crashes,
            'baseline_crashes': baseline_crashes,
            'branch_crash_rate': round(branch_rate, 4),
            'baseline_crash_rate': round(baseline_rate, 4),
            'crash_invalid': invalid,
        })
        if invalid:
            verdict = 'invalid'

    return verdict, details


def summarize_series(games: list[dict]) -> dict:
    """Summarize the raw game records of one series.

    Each game is a dict with at least a ``result`` key; ``duration`` (in
    seconds) is used when present.  Crashed games count as losses, which
    matches how the results tab counts them, and are counted separately
    as ``crashes`` so a series that mostly crashed can be recognized and
    reported as invalid instead of judged.
    """
    wins = losses = ties = unfinished = crashes = 0
    durations: list[int] = []

    for game in games:
        result = (game.get('result') or '').strip()
        if result == 'Victory':
            wins += 1
        elif result == 'Crash':
            crashes += 1
            losses += 1
        elif result == 'Defeat':
            losses += 1
        elif result in UNFINISHED_RESULTS:
            unfinished += 1
        else:
            ties += 1
        duration = game.get('duration')
        if duration:
            durations.append(int(duration))

    durations.sort()
    return {
        'games': len(games),
        'wins': wins,
        'losses': losses,
        'ties': ties,
        'unfinished': unfinished,
        'crashes': crashes,
        'durations': durations,
        'duration_total': sum(durations),
        'duration_median': int(statistics.median(durations)) if durations else None,
        'game_list': games,
    }


def crash_rate(crashes: int, games: int) -> float:
    """Return the share of *games* that crashed (0.0 when nothing was played)."""
    if games <= 0:
        return 0.0
    return crashes / games


def series_crashed_out(summary: dict, limit: float = CRASH_RATE_LIMIT) -> bool:
    """Return True when a series crashed too often to be judged.

    *summary* is a series summary as produced by :func:`summarize_series`.
    """
    games = int(summary.get('games', 0))
    crashes = int(summary.get('crashes', 0))
    return crash_rate(crashes, games) > limit


# ---------------------------------------------------------------------------
# Repository matching
# ---------------------------------------------------------------------------

def normalize_repo_url(url: str) -> str:
    """Normalize a git remote URL to ``host/path`` for comparison.

    Handles the usual spellings of the same repository: ``https://``,
    ``ssh://``, ``git@host:owner/repo.git`` and ``owner/repo``.
    """
    value = (url or '').strip()
    if not value:
        return ''

    if '://' in value:
        value = value.split('://', 1)[1]
    elif ':' in value:
        # scp-style: [user@]host:owner/repo
        value = value.replace(':', '/', 1)

    head, sep, tail = value.partition('/')
    if '@' in head:
        head = head.split('@', 1)[1]
    value = head + sep + tail

    value = value.rstrip('/')
    if value.endswith('.git'):
        value = value[:-4]
    return value.lower()


def _git_remote_url(repo_path: str) -> str:
    """Return the ``origin`` remote URL of the git repo at *repo_path*."""
    import subprocess

    try:
        result = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.url'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if result.returncode != 0:
        return ''
    return result.stdout.strip()


def repo_matches_source(repo_url: str, source_path: str) -> bool:
    """Return True when *repo_url* refers to the repo at *source_path*.

    The registered test bot's ``source_path`` is authoritative — it is what
    the lab actually mounts — so the caller supplied ``repo_url`` is only
    checked against it.  A local path, the git ``origin`` remote, or a
    suffix of either (``owner/repo`` vs ``github.com/owner/repo``) counts
    as a match.
    """
    wanted = normalize_repo_url(repo_url)
    if not wanted or not source_path:
        return False

    if normalize_repo_url(os.path.realpath(source_path)) == wanted:
        return True

    remote = normalize_repo_url(_git_remote_url(source_path))
    if remote:
        if remote == wanted:
            return True
        # "owner/repo" vs "github.com/owner/repo"
        if remote.endswith('/' + wanted) or wanted.endswith('/' + remote):
            return True
        return False

    # No git remote to compare against — fall back to the directory name.
    local_name = os.path.basename(os.path.normpath(source_path)).lower()
    return bool(local_name) and wanted.endswith('/' + local_name)


# ---------------------------------------------------------------------------
# Concurrency lock
# ---------------------------------------------------------------------------

def get_active_run():
    """Return the gate run currently holding the lab-wide lock (or None)."""
    from .models import GateRun

    return GateRun.objects.filter(lock_slot__isnull=False).order_by('pk').first()


def acquire_gate_lock(test_group, **fields):
    """Create the gate run for *test_group* and take the lab-wide lock.

    The lock is a unique database column, so exactly one gate run can hold
    it at a time — even across server restarts.  Raises :class:`GateBusy`
    when another run is already active.
    """
    from .models import GateRun

    try:
        with transaction.atomic():
            return GateRun.objects.create(
                test_group=test_group, lock_slot=1, **fields,
            )
    except IntegrityError:
        active = get_active_run()
        raise GateBusy(active.pk if active else None)


def release_gate_lock(run, status: str = 'complete') -> None:
    """Release the lab-wide lock held by *run*."""
    from .models import GateRun

    GateRun.objects.filter(pk=run.pk).update(
        lock_slot=None, status=status, updated_at=timezone.now(),
    )
    run.lock_slot = None
    run.status = status


def _run_is_stale_unstarted(run) -> bool:
    """Return True when a run never got a game off the ground."""
    if _series_matches(run.test_group_id) or _series_matches(run.baseline_group_id):
        return False
    created = run.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (timezone.now() - created).total_seconds() > UNSTARTED_RUN_SECONDS


def reconcile_active_run():
    """Return the active gate run, releasing the lock if it already finished.

    A server restart can kill the watcher thread that normally releases the
    lock, so the lock is re-checked against the match records before a new
    run is allowed to start.  A run whose games are stuck (their docker
    process is gone) is recovered first, so a crashed lab does not stay
    busy forever.
    """
    run = get_active_run()
    if run is None:
        return None
    repair_stale_run(run)
    if compute_run_status(run) == 'complete':
        release_gate_lock(run)
        return None
    if _run_is_stale_unstarted(run):
        logger.warning(
            'Gate run %s never started a game, releasing the lock', run.pk,
        )
        release_gate_lock(run)
        return None
    return run


# ---------------------------------------------------------------------------
# Run status
# ---------------------------------------------------------------------------

def _is_finished(match) -> bool:
    return (match.result or '') not in UNFINISHED_RESULTS


def _unfinished_matches(group_ids) -> list:
    from .models import Match

    return list(
        Match.objects.filter(
            test_group_id__in=[gid for gid in group_ids if gid],
            result__in=['Pending', 'Queued'],
        )
    )


def _docker_alive(match) -> bool:
    """Return True when the docker process of a match is still running."""
    from . import aiarena_runner

    pid_file = os.path.join(aiarena_runner.get_run_dir(match.id), 'docker.pid')
    if not os.path.isfile(pid_file):
        return False
    try:
        with open(pid_file) as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return False
    return aiarena_runner._is_process_running(pid)


def repair_stale_run(run) -> bool:
    """Recover the games of a run that lost their watcher.

    A server restart (or a killed thread) can leave a match without a
    result.  When such a game is older than the docker match timeout and
    its docker process is gone, the result is collected — or the game is
    marked as crashed — so the run can finish and the lab-wide lock is
    released.

    Returns True when at least one game was recovered.
    """
    from . import aiarena_runner

    matches = _unfinished_matches([run.test_group_id, run.baseline_group_id])
    if not matches:
        return False

    now = timezone.now()
    repaired = False
    for match in matches:
        started = match.start_timestamp
        if started is None:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if (now - started).total_seconds() < STALE_RUN_SECONDS:
            continue  # too young to be stale
        if not os.path.isdir(aiarena_runner.get_run_dir(match.id)):
            # The game never got a docker run directory — it never started.
            match.result = 'Crash'
            match.end_timestamp = now
            match.save(update_fields=['result', 'end_timestamp'])
            repaired = True
            continue
        if _docker_alive(match):
            continue  # the game is still running
        if aiarena_runner.collect_match_result(match.id) is not None:
            repaired = True

    return repaired


def _series_matches(group_id: int | None) -> list:
    from .models import Match

    if not group_id:
        return []
    return list(
        Match.objects
        .filter(test_group_id=group_id)
        .select_related('opponent_bot')
        .order_by('id')
    )


def compute_run_status(run) -> str:
    """Return ``'pending'``, ``'running'`` or ``'complete'`` for *run*."""
    matches = _series_matches(run.test_group_id)
    matches += _series_matches(run.baseline_group_id)
    if not matches:
        return 'pending'

    finished = [m for m in matches if _is_finished(m)]
    if not finished:
        return 'pending'
    if len(finished) < len(matches):
        return 'running'

    expected = run.expected_matches or 0
    if expected and len(matches) < expected:
        # Not every game was launched (yet).
        return 'running'

    return 'complete'


def refresh_run_state(run) -> str:
    """Recompute the status of *run* and release the lock when it is done."""
    from .models import GateRun

    status = compute_run_status(run)
    if status == 'complete':
        if run.lock_slot is not None:
            release_gate_lock(run, status='complete')
        elif run.status != 'complete':
            GateRun.objects.filter(pk=run.pk).update(
                status='complete', updated_at=timezone.now(),
            )
            run.status = 'complete'
    elif run.status != status:
        GateRun.objects.filter(pk=run.pk).update(
            status=status, updated_at=timezone.now(),
        )
        run.status = status
    return status


# ---------------------------------------------------------------------------
# Status payload
# ---------------------------------------------------------------------------

def _game_record(match) -> dict:
    opponent = match.opponent_bot
    return {
        'match_id': match.id,
        'result': match.result,
        'map': match.map_name,
        'duration': match.duration_in_game_time,
        'opponent_id': match.opponent_bot_id,
        'opponent_name': opponent.name if opponent else '',
        'started_at': match.start_timestamp.isoformat() if match.start_timestamp else None,
        'ended_at': match.end_timestamp.isoformat() if match.end_timestamp else None,
    }


def build_gate_status(run) -> dict:
    """Build the JSON payload of the gate-status endpoint."""
    from .models import CustomBot

    status = compute_run_status(run)
    branch_matches = _series_matches(run.test_group_id)
    baseline_matches = _series_matches(run.baseline_group_id)

    branch_by_opponent: dict[int, list] = {}
    for match in branch_matches:
        branch_by_opponent.setdefault(match.opponent_bot_id, []).append(match)
    baseline_by_opponent: dict[int, list] = {}
    for match in baseline_matches:
        baseline_by_opponent.setdefault(match.opponent_bot_id, []).append(match)

    opponent_ids = list(run.opponent_bot_ids or [])
    for opponent_id in sorted(
        set(branch_by_opponent) | set(baseline_by_opponent),
        key=lambda value: (value is None, value),
    ):
        if opponent_id is None:
            continue
        if opponent_id not in opponent_ids:
            opponent_ids.append(opponent_id)

    results = []
    branch_summaries: dict[int, dict] = {}
    baseline_summaries: dict[int, dict] = {}

    for opponent_id in opponent_ids:
        opponent = CustomBot.objects.filter(id=opponent_id).first()
        branch_summary = summarize_series([
            _game_record(m) for m in branch_by_opponent.get(opponent_id, [])
        ])
        baseline_summary = summarize_series([
            _game_record(m) for m in baseline_by_opponent.get(opponent_id, [])
        ])
        branch_summaries[opponent_id] = branch_summary
        baseline_summaries[opponent_id] = baseline_summary
        results.append({
            'opponent_id': opponent_id,
            'opponent_name': opponent.name if opponent else '',
            'branch': branch_summary,
            'baseline': baseline_summary,
        })

    verdict, details = evaluate_verdict(branch_summaries, baseline_summaries)
    by_opponent = {detail['opponent_id']: detail for detail in details}
    for record in results:
        record.update(by_opponent.get(record['opponent_id'], {}))

    test_bot = run.test_bot
    payload = {
        'run_id': run.pk,
        'status': status,
        'noise_band': noise_band_rule(run.games_per_opponent),
        'branch': run.branch,
        'baseline': run.baseline,
        'games_per_opponent': run.games_per_opponent,
        'test_bot': {
            'id': test_bot.id if test_bot else None,
            'name': test_bot.name if test_bot else '',
        },
        'games_finished': sum(
            1 for m in branch_matches + baseline_matches if _is_finished(m)
        ),
        'games_expected': run.expected_matches or 0,
        'results': results,
    }

    if status == 'complete':
        payload['verdict'] = verdict

    return payload


# ---------------------------------------------------------------------------
# Launching the series
# ---------------------------------------------------------------------------

def _select_maps(opponent_count: int, games_per_opponent: int) -> list[list[str]]:
    """Return the map per opponent and game index.

    Both series play the same maps, so the branch and baseline series are
    paired and the comparison is not polluted by map differences.
    """
    from .views import MAP_LIST

    maps = []
    for opponent_index in range(opponent_count):
        row = []
        for game_index in range(games_per_opponent):
            index = (opponent_index * games_per_opponent + game_index) % len(MAP_LIST)
            row.append(MAP_LIST[index])
        maps.append(row)
    return maps


def _mark_failed_launches(group_ids) -> None:
    """Mark games whose launch failed as crashed.

    ``start_custom_bot_match`` creates the match record before it builds
    the docker run directory, so a failed launch leaves a match without a
    result.  Without this the run would never reach a terminal state.
    """
    from . import aiarena_runner
    from .models import Match

    for group_id in group_ids:
        pending = Match.objects.filter(
            test_group_id=group_id, result__in=['Pending', 'Queued'],
        )
        for match in pending:
            if os.path.isdir(aiarena_runner.get_run_dir(match.id)):
                continue
            match.result = 'Crash'
            match.end_timestamp = timezone.now()
            match.save(update_fields=['result', 'end_timestamp'])
            logger.warning(
                'Gate run: match %d never started, marked as Crash', match.id,
            )


def _run_gate_series(
    run_id: int,
    test_bot_id: int,
    opponent_ids: list[int],
    games_per_opponent: int,
    branch_group_id: int,
    baseline_group_id: int,
    branch_source: str | None,
    baseline_source: str | None,
) -> None:
    """Play the branch series and the baseline series, then watch them."""
    from . import views
    from .models import CustomBot, GateRun

    try:
        test_bot = CustomBot.objects.get(id=test_bot_id)
        opponents = list(CustomBot.objects.filter(id__in=opponent_ids))
        opponents.sort(key=lambda bot: opponent_ids.index(bot.id))
    except CustomBot.DoesNotExist:
        logger.error('Gate run %s: bot vanished before launch', run_id)
        return

    maps = _select_maps(len(opponents), games_per_opponent)

    for game_index in range(games_per_opponent):
        for opponent_index, opponent in enumerate(opponents):
            map_name = maps[opponent_index][game_index]
            for group_id, source in (
                (branch_group_id, branch_source),
                (baseline_group_id, baseline_source),
            ):
                try:
                    views.start_custom_bot_match(
                        opponent,
                        test_bot=test_bot,
                        test_group_id=group_id,
                        source_override=source,
                        map_name=map_name,
                    )
                except Exception:
                    logger.exception(
                        'Gate run %s: failed to launch %s vs %s (group %s)',
                        run_id, test_bot.name, opponent.name, group_id,
                    )

    _mark_failed_launches([branch_group_id, baseline_group_id])

    # The series is complete once every launched game has a result.
    deadline = time.monotonic() + WATCH_MAX_SECONDS
    while True:
        run = GateRun.objects.filter(pk=run_id).first()
        if run is None:
            return
        repair_stale_run(run)
        status = refresh_run_state(run)
        if status == 'complete':
            logger.info('Gate run %s: series complete', run_id)
            return
        if time.monotonic() > deadline:
            unfinished = _unfinished_matches([branch_group_id, baseline_group_id])
            if not any(_docker_alive(match) for match in unfinished):
                # Nothing is playing any more — the series is dead.
                logger.warning(
                    'Gate run %s: no game is running any more, giving up',
                    run_id,
                )
                for match in unfinished:
                    match.result = 'Crash'
                    match.end_timestamp = timezone.now()
                    match.save(update_fields=['result', 'end_timestamp'])
                continue
            # Games are still playing — keep waiting.
            deadline = time.monotonic() + WATCH_MAX_SECONDS
        time.sleep(WATCH_POLL_SECONDS)


def launch_gate_series(
    run,
    test_bot,
    opponents: list,
    games_per_opponent: int,
    branch_group_id: int,
    baseline_group_id: int,
    branch_source: str | None,
    baseline_source: str | None,
) -> None:
    """Start the branch and baseline series in the background.

    The games are played by the regular match machinery (docker runs in
    background threads), so the trigger endpoint can answer immediately.
    """
    thread = threading.Thread(
        target=_run_gate_series,
        args=(
            run.pk,
            test_bot.id,
            [opponent.id for opponent in opponents],
            games_per_opponent,
            branch_group_id,
            baseline_group_id,
            branch_source,
            baseline_source,
        ),
        daemon=True,
    )
    thread.start()
