"""Unit tests for the lab-bridge gate API.

The gate logic is split so that the interesting parts are testable without
docker or StarCraft II:

* the noise-band verdict (``gate.evaluate_noise_band``) and the series
  summary (``gate.summarize_series``) are plain functions over plain data,
* the concurrency lock is a unique database row, so it is tested against
  the database.

Run them with::

    python quickstart/manage.py test test_lab --settings=test_lab.quickstart.test_settings
"""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import gate, worktrees
from .models import CustomBot, GateRun, Match, TestGroup

BRIDGE_TOKEN = 'test-bridge-token'


# ---------------------------------------------------------------------------
# Pure logic — noise band
# ---------------------------------------------------------------------------

class NoiseBandTests(SimpleTestCase):
    def test_rule_string_states_the_band(self):
        self.assertEqual(
            gate.noise_band_rule(3),
            'within ±1 game per 3-game series vs baseline',
        )

    def test_equal_wins_pass(self):
        verdict, details = gate.evaluate_noise_band({1: 2}, {1: 2})
        self.assertEqual(verdict, 'pass')
        self.assertTrue(details[0]['within_band'])
        self.assertEqual(details[0]['delta'], 0)

    def test_one_game_more_still_passes(self):
        verdict, _ = gate.evaluate_noise_band({1: 3}, {1: 2})
        self.assertEqual(verdict, 'pass')

    def test_one_game_fewer_still_passes(self):
        verdict, details = gate.evaluate_noise_band({1: 1}, {1: 2})
        self.assertEqual(verdict, 'pass')
        self.assertEqual(details[0]['delta'], -1)

    def test_two_games_more_fails(self):
        verdict, details = gate.evaluate_noise_band({1: 3}, {1: 1})
        self.assertEqual(verdict, 'fail')
        self.assertEqual(details[0]['delta'], 2)
        self.assertFalse(details[0]['within_band'])

    def test_two_games_fewer_fails(self):
        verdict, details = gate.evaluate_noise_band({1: 0}, {1: 2})
        self.assertEqual(verdict, 'fail')
        self.assertEqual(details[0]['delta'], -2)

    def test_one_bad_opponent_fails_the_whole_run(self):
        verdict, details = gate.evaluate_noise_band({1: 3, 2: 0}, {1: 3, 2: 3})
        self.assertEqual(verdict, 'fail')
        by_opponent = {d['opponent_id']: d for d in details}
        self.assertTrue(by_opponent[1]['within_band'])
        self.assertFalse(by_opponent[2]['within_band'])

    def test_opponent_missing_from_one_series_counts_as_zero_wins(self):
        verdict, details = gate.evaluate_noise_band({1: 1}, {1: 1, 2: 0})
        self.assertEqual(verdict, 'pass')
        self.assertEqual(len(details), 2)

    def test_comparison_without_opponents_does_not_pass(self):
        verdict, details = gate.evaluate_noise_band({}, {})
        self.assertEqual(verdict, 'fail')
        self.assertEqual(details, [])

    def test_tolerance_is_configurable(self):
        verdict, _ = gate.evaluate_noise_band({1: 3}, {1: 1}, tolerance=2)
        self.assertEqual(verdict, 'pass')


class SeriesSummaryTests(SimpleTestCase):
    def test_counts_wins_losses_ties_and_durations(self):
        games = [
            {'result': 'Victory', 'duration': 100},
            {'result': 'Defeat', 'duration': 200},
            {'result': 'Crash', 'duration': None},
            {'result': 'Pending', 'duration': None},
            {'result': 'Tie', 'duration': 50},
        ]
        summary = gate.summarize_series(games)
        self.assertEqual(summary['games'], 5)
        self.assertEqual(summary['wins'], 1)
        self.assertEqual(summary['losses'], 2)
        self.assertEqual(summary['ties'], 1)
        self.assertEqual(summary['unfinished'], 1)
        self.assertEqual(summary['durations'], [50, 100, 200])
        self.assertEqual(summary['duration_total'], 350)
        self.assertEqual(summary['duration_median'], 100)

    def test_crashes_are_counted_separately_from_the_other_losses(self):
        games = [
            {'result': 'Victory'},
            {'result': 'Defeat'},
            {'result': 'Crash'},
            {'result': 'Crash'},
        ]
        summary = gate.summarize_series(games)
        self.assertEqual(summary['crashes'], 2)
        self.assertEqual(summary['wins'], 1)
        # Crashes still count as losses (like the results tab counts them),
        # but they are also counted on their own.
        self.assertEqual(summary['losses'], 3)

    def test_a_series_without_crashes(self):
        summary = gate.summarize_series([
            {'result': 'Victory'}, {'result': 'Defeat'},
        ])
        self.assertEqual(summary['crashes'], 0)

    def test_empty_series(self):
        summary = gate.summarize_series([])
        self.assertEqual(summary['games'], 0)
        self.assertEqual(summary['wins'], 0)
        self.assertEqual(summary['crashes'], 0)
        self.assertEqual(summary['durations'], [])
        self.assertIsNone(summary['duration_median'])


class CrashRateTests(SimpleTestCase):
    """A series that mostly crashed cannot be judged (see gate.py)."""

    def _summary(self, wins=0, games=3, crashes=0):
        return {'wins': wins, 'games': games, 'crashes': crashes}

    def test_crash_rate_of_a_series(self):
        self.assertEqual(gate.crash_rate(3, 3), 1.0)
        self.assertEqual(gate.crash_rate(1, 4), 0.25)
        self.assertEqual(gate.crash_rate(0, 4), 0.0)

    def test_crash_rate_of_an_unplayed_series(self):
        self.assertEqual(gate.crash_rate(0, 0), 0.0)

    def test_a_crashed_baseline_invalidates_the_verdict(self):
        # The inaugural gate run: the branch played real games, the
        # baseline crashed every single one — which looked like a pass
        # (1 win vs 0 wins is inside the band) but says nothing.
        verdict, details = gate.evaluate_verdict(
            {1: self._summary(wins=1, crashes=0)},
            {1: self._summary(wins=0, crashes=3)},
        )
        self.assertEqual(verdict, 'invalid')
        self.assertTrue(details[0]['crash_invalid'])
        self.assertEqual(details[0]['baseline_crashes'], 3)
        self.assertEqual(details[0]['branch_crashes'], 0)

    def test_a_crashed_branch_invalidates_the_verdict(self):
        verdict, details = gate.evaluate_verdict(
            {1: self._summary(wins=0, crashes=3)},
            {1: self._summary(wins=2, crashes=0)},
        )
        self.assertEqual(verdict, 'invalid')
        self.assertTrue(details[0]['crash_invalid'])

    def test_a_clean_series_is_still_judged(self):
        verdict, details = gate.evaluate_verdict(
            {1: self._summary(wins=2)},
            {1: self._summary(wins=2)},
        )
        self.assertEqual(verdict, 'pass')
        self.assertFalse(details[0]['crash_invalid'])

    def test_a_crash_rate_at_the_limit_is_still_judged(self):
        # One crash in two games is exactly the limit — it does not
        # exceed it, so the band decides.
        verdict, details = gate.evaluate_verdict(
            {1: self._summary(wins=1, games=2, crashes=1)},
            {1: self._summary(wins=0, games=2, crashes=0)},
        )
        self.assertEqual(verdict, 'pass')
        self.assertFalse(details[0]['crash_invalid'])

    def test_one_crash_in_three_games_does_not_invalidate(self):
        verdict, details = gate.evaluate_verdict(
            {1: self._summary(wins=1, games=3, crashes=1)},
            {1: self._summary(wins=0, games=3, crashes=0)},
        )
        self.assertEqual(verdict, 'pass')
        self.assertEqual(details[0]['branch_crash_rate'], 0.3333)

    def test_invalid_wins_over_fail(self):
        # One opponent crashed, another is outside the band: the run
        # cannot be judged at all, so it is invalid — not fail.
        verdict, _ = gate.evaluate_verdict(
            {1: self._summary(wins=3, crashes=0),
             2: self._summary(wins=0, crashes=3)},
            {1: self._summary(wins=0, crashes=0),
             2: self._summary(wins=0, crashes=0)},
        )
        self.assertEqual(verdict, 'invalid')

    def test_invalid_wins_over_pass(self):
        verdict, _ = gate.evaluate_verdict(
            {1: self._summary(wins=1, crashes=0)},
            {1: self._summary(wins=0, crashes=3)},
        )
        self.assertEqual(verdict, 'invalid')

    def test_a_series_without_games_is_not_invalid(self):
        verdict, details = gate.evaluate_verdict(
            {1: self._summary(wins=0, games=0, crashes=0)},
            {1: self._summary(wins=0, games=0, crashes=0)},
        )
        self.assertEqual(verdict, 'pass')
        self.assertFalse(details[0]['crash_invalid'])


# ---------------------------------------------------------------------------
# Worktrees — submodules
# ---------------------------------------------------------------------------

class WorktreeSubmoduleTests(SimpleTestCase):
    """A fresh worktree has no submodules; the bot would crash on import."""

    def setUp(self):
        self.base_dir = tempfile.mkdtemp(prefix='gate-worktrees-')
        self.addCleanup(shutil.rmtree, self.base_dir, True)

        self.repo_dir = os.path.join(self.base_dir, 'repo')
        self.sub_dir = os.path.join(self.base_dir, 'sub')
        os.makedirs(self.repo_dir)
        os.makedirs(self.sub_dir)

        # The submodule of the test repo lives next to it, so git has to
        # be allowed to clone over the file transport (the default is to
        # refuse it).  The env vars are inherited by every git call the
        # lab makes.
        env_patcher = mock.patch.dict(os.environ, {
            'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': 'protocol.file.allow',
            'GIT_CONFIG_VALUE_0': 'always',
            'GIT_AUTHOR_NAME': 'Test',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        # The submodule itself.
        self._git('init', '-q', self.sub_dir)
        with open(os.path.join(self.sub_dir, 'engine.py'), 'w') as handle:
            handle.write('# engine\n')
        self._git('add', '.', cwd=self.sub_dir)
        self._git('commit', '-q', '-m', 'sub', cwd=self.sub_dir)

        # The repo that pins it.
        self._git('init', '-q', self.repo_dir)
        with open(os.path.join(self.repo_dir, 'run.py'), 'w') as handle:
            handle.write('# bot\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'init')
        self._git('submodule', 'add', '-q', self.sub_dir, 'vendorlib')
        self._git('commit', '-q', '-m', 'add submodule')
        self._git('branch', 'feature/x')

        base_patcher = mock.patch.object(
            worktrees, 'WORKTREE_BASE_DIR', os.path.join(self.base_dir, 'worktrees'),
        )
        base_patcher.start()
        self.addCleanup(base_patcher.stop)

    def _git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ['git', *args],
            cwd=cwd or self.repo_dir,
            capture_output=True,
            text=True,
            check=check,
        )

    def test_a_new_worktree_gets_its_submodule_checked_out(self):
        worktree_path = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')
        self.assertTrue(os.path.isdir(worktree_path))
        # The submodule is checked out, so the bot can import it.
        self.assertTrue(
            os.path.isfile(os.path.join(worktree_path, 'vendorlib', 'engine.py'))
        )

    def test_a_reused_worktree_missing_its_submodule_is_completed(self):
        # A worktree that exists but lost its submodule (e.g. it was
        # created before the submodule existed) is completed.
        worktree_path = os.path.join(self.base_dir, 'worktrees', 'feature_x')
        self._git('worktree', 'add', '-q', worktree_path, 'feature/x')
        self.assertFalse(
            os.path.exists(os.path.join(worktree_path, 'vendorlib', 'engine.py'))
        )

        result = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')
        self.assertEqual(result, worktree_path)
        self.assertTrue(
            os.path.isfile(os.path.join(worktree_path, 'vendorlib', 'engine.py'))
        )

    def test_a_failed_submodule_checkout_raises_a_clear_error(self):
        # A repo whose branch pins a submodule that cannot be cloned.
        broken_dir = os.path.join(self.base_dir, 'broken')
        os.makedirs(broken_dir)
        self._git('init', '-q', broken_dir)
        with open(os.path.join(broken_dir, 'run.py'), 'w') as handle:
            handle.write('# bot\n')
        self._git('add', '.', cwd=broken_dir)
        self._git('commit', '-q', '-m', 'init', cwd=broken_dir)
        self._git('submodule', 'add', '-q', self.sub_dir, 'vendorlib', cwd=broken_dir)
        self._git('commit', '-q', '-m', 'add submodule', cwd=broken_dir)
        self._git('checkout', '-q', '-b', 'feature/broken', cwd=broken_dir)

        gitmodules = os.path.join(broken_dir, '.gitmodules')
        with open(gitmodules) as handle:
            content = handle.read()
        with open(gitmodules, 'w') as handle:
            handle.write(
                content.replace(self.sub_dir, os.path.join(self.base_dir, 'gone'))
            )
        self._git('commit', '-q', '-am', 'break the submodule url', cwd=broken_dir)
        # The url of an added submodule is also cached in the config of
        # the repo — drop it, so the broken one is used.
        self._git('config', '--unset', 'submodule.vendorlib.url', cwd=broken_dir)
        # The branch must not be checked out anywhere, so the lab has to
        # create a worktree for it.
        self._git('checkout', '-q', '-', cwd=broken_dir)

        with self.assertRaises(worktrees.SubmoduleError) as ctx:
            worktrees.get_or_create_worktree(broken_dir, 'feature/broken')
        self.assertIn('vendorlib', str(ctx.exception))

    def test_a_repo_without_submodules_still_works(self):
        plain_dir = os.path.join(self.base_dir, 'plain')
        os.makedirs(plain_dir)
        self._git('init', '-q', plain_dir)
        with open(os.path.join(plain_dir, 'run.py'), 'w') as handle:
            handle.write('# bot\n')
        self._git('add', '.', cwd=plain_dir)
        self._git('commit', '-q', '-m', 'init', cwd=plain_dir)
        self._git('branch', 'feature/y', cwd=plain_dir)

        worktree_path = worktrees.get_or_create_worktree(plain_dir, 'feature/y')
        self.assertTrue(os.path.isdir(worktree_path))
        self.assertFalse(worktrees.ensure_submodules(worktree_path))


class WorktreePrepareScriptTests(SimpleTestCase):
    """A repo may provide a prepare script for its worktrees.

    Repos that vendor gitignored dependencies (an in-tree engine library, a
    map analyzer, compiled extensions) cannot run from a worktree: git
    restores none of those files.  The lab must not guess which files
    matter — the repo states it in a prepare script, and the lab runs it.
    """

    def setUp(self):
        self.base_dir = tempfile.mkdtemp(prefix='gate-prepare-')
        self.addCleanup(shutil.rmtree, self.base_dir, True)

        env_patcher = mock.patch.dict(os.environ, {
            'GIT_AUTHOR_NAME': 'Test',
            'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test',
            'GIT_COMMITTER_EMAIL': 'test@example.invalid',
        })
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self.repo_dir = os.path.join(self.base_dir, 'repo')
        os.makedirs(self.repo_dir)
        self._git('init', '-q', self.repo_dir)
        with open(os.path.join(self.repo_dir, 'run.py'), 'w') as handle:
            handle.write('# bot\n')
        self._git('add', '.')
        self._git('commit', '-q', '-m', 'init')
        self._git('branch', 'feature/x')

        base_patcher = mock.patch.object(
            worktrees, 'WORKTREE_BASE_DIR', os.path.join(self.base_dir, 'worktrees'),
        )
        base_patcher.start()
        self.addCleanup(base_patcher.stop)

    def _git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ['git', *args],
            cwd=cwd or self.repo_dir,
            capture_output=True,
            text=True,
            check=check,
        )

    def _write_script(self, relative_path, body):
        """Add a prepare script to the repo (and to the branch under test)."""
        path = os.path.join(self.repo_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        self._git('add', '.')
        self._git('commit', '-q', '-m', f'add {relative_path}')
        # The branch under test gets the script as well.
        self._git('branch', '-f', 'feature/x')

    def test_the_prepare_script_runs_in_the_worktree(self):
        self._write_script(
            'scripts/vendor_lab_deps.sh',
            '#!/bin/sh\npwd > prepare_cwd.txt\ntouch vendored_marker\n',
        )

        worktree_path = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')

        # The vendored file the script created is there, so the bot can
        # import it from the worktree.
        self.assertTrue(
            os.path.isfile(os.path.join(worktree_path, 'vendored_marker'))
        )
        # The script ran with the worktree as its working directory.
        with open(os.path.join(worktree_path, 'prepare_cwd.txt')) as handle:
            self.assertEqual(
                os.path.realpath(handle.read().strip()),
                os.path.realpath(worktree_path),
            )

    def test_the_first_conventional_script_wins(self):
        self._write_script(
            'scripts/vendor_lab_deps.sh',
            '#!/bin/sh\ntouch vendor_marker\n',
        )
        self._write_script(
            'scripts/prepare_worktree.sh',
            '#!/bin/sh\ntouch prepare_marker\n',
        )

        worktree_path = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')

        self.assertTrue(
            os.path.isfile(os.path.join(worktree_path, 'prepare_marker'))
        )
        self.assertFalse(
            os.path.exists(os.path.join(worktree_path, 'vendor_marker'))
        )

    def test_a_reused_worktree_is_prepared_again(self):
        self._write_script(
            'scripts/vendor_lab_deps.sh',
            '#!/bin/sh\ntouch vendored_marker\n',
        )

        worktree_path = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')
        marker = os.path.join(worktree_path, 'vendored_marker')
        self.assertTrue(os.path.isfile(marker))

        # The worktree is reused the second time — and prepared again, so
        # the vendored files are there even when they went missing.
        os.remove(marker)
        again = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')

        self.assertEqual(again, worktree_path)
        self.assertTrue(os.path.isfile(marker))

    def test_a_failing_prepare_script_raises_a_clear_error(self):
        self._write_script(
            'scripts/vendor_lab_deps.sh',
            '#!/bin/sh\necho "vendor download failed" >&2\nexit 3\n',
        )

        with self.assertRaises(worktrees.PrepareScriptError) as ctx:
            worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')

        message = str(ctx.exception)
        self.assertIn('vendor_lab_deps.sh', message)
        self.assertIn('vendor download failed', message)

    def test_a_repo_without_a_prepare_script_is_used_as_it_is(self):
        worktree_path = worktrees.get_or_create_worktree(self.repo_dir, 'feature/x')

        self.assertTrue(os.path.isdir(worktree_path))
        self.assertIsNone(worktrees.run_prepare_script(worktree_path))


class RepoUrlTests(SimpleTestCase):
    def test_normalizes_the_usual_spellings(self):
        self.assertEqual(
            gate.normalize_repo_url('https://github.com/owner/repo.git'),
            'github.com/owner/repo',
        )
        self.assertEqual(
            gate.normalize_repo_url('git@github.com:owner/repo.git'),
            'github.com/owner/repo',
        )
        self.assertEqual(
            gate.normalize_repo_url('ssh://git@github.com/owner/repo'),
            'github.com/owner/repo',
        )
        self.assertEqual(
            gate.normalize_repo_url('https://github.com/owner/repo/'),
            'github.com/owner/repo',
        )

    def test_owner_repo_matches_the_full_remote(self):
        with mock.patch(
            'test_lab.gate._git_remote_url',
            return_value='https://github.com/owner/repo.git',
        ):
            self.assertTrue(
                gate.repo_matches_source('owner/repo', '/srv/bots/repo')
            )

    def test_a_different_repo_does_not_match(self):
        with mock.patch(
            'test_lab.gate._git_remote_url',
            return_value='https://github.com/owner/repo.git',
        ):
            self.assertFalse(
                gate.repo_matches_source(
                    'owner/other', '/srv/bots/repo',
                )
            )

    def test_the_local_path_matches_itself(self):
        self.assertTrue(
            gate.repo_matches_source('/srv/bots/repo', '/srv/bots/repo')
        )


# ---------------------------------------------------------------------------
# Concurrency lock
# ---------------------------------------------------------------------------

class GateLockTests(TestCase):
    def setUp(self):
        self.group = TestGroup.objects.create(description='gate test', branch='feature/x')

    def _lock(self, group):
        return gate.acquire_gate_lock(
            group, branch='feature/x', baseline='master',
            games_per_opponent=3, opponent_bot_ids=[], expected_matches=0,
        )

    def test_the_first_run_takes_the_lock(self):
        run = self._lock(self.group)
        self.assertEqual(run.pk, self.group.id)
        self.assertEqual(run.lock_slot, 1)
        self.assertEqual(gate.get_active_run().pk, run.pk)

    def test_a_second_run_is_refused_while_the_first_is_active(self):
        run = self._lock(self.group)
        other = TestGroup.objects.create(description='gate test 2', branch='feature/y')
        with self.assertRaises(gate.GateBusy) as ctx:
            self._lock(other)
        self.assertEqual(ctx.exception.run_id, run.pk)

    def test_releasing_the_lock_allows_the_next_run(self):
        run = self._lock(self.group)
        gate.release_gate_lock(run)
        self.assertIsNone(gate.get_active_run())

        other = TestGroup.objects.create(description='gate test 2', branch='feature/y')
        second = self._lock(other)
        self.assertEqual(gate.get_active_run().pk, second.pk)

    def test_a_finished_series_releases_a_stale_lock(self):
        run = self._lock(self.group)
        Match.objects.create(
            test_group=self.group,
            start_timestamp=gate.timezone.now(),
            end_timestamp=gate.timezone.now(),
            map_name='TestMap',
            opponent_race='Terran',
            result='Victory',
        )
        # The lock survives a server restart, so it is re-checked against
        # the match records before a new run may start.
        self.assertIsNone(gate.reconcile_active_run())
        self.assertIsNone(gate.get_active_run())
        run.refresh_from_db()
        self.assertIsNone(run.lock_slot)

    def test_an_unfinished_series_keeps_the_lock(self):
        self._lock(self.group)
        Match.objects.create(
            test_group=self.group,
            start_timestamp=gate.timezone.now(),
            map_name='TestMap',
            opponent_race='Terran',
            result='Pending',
        )
        active = gate.reconcile_active_run()
        self.assertIsNotNone(active)
        self.assertIsNotNone(gate.get_active_run())

    def test_a_run_that_never_started_releases_the_lock(self):
        run = self._lock(self.group)
        GateRun.objects.filter(pk=run.pk).update(
            created_at=gate.timezone.now() - timedelta(hours=1),
        )
        self.assertIsNone(gate.reconcile_active_run())
        self.assertIsNone(gate.get_active_run())

    def test_a_stuck_series_is_recovered_and_releases_the_lock(self):
        self._lock(self.group)
        match = Match.objects.create(
            test_group=self.group,
            start_timestamp=gate.timezone.now() - timedelta(hours=3),
            map_name='TestMap',
            opponent_race='Terran',
            result='Pending',
        )
        # The game never started (no docker run directory) — it is recovered
        # so the lab is not left busy forever.
        self.assertIsNone(gate.reconcile_active_run())
        match.refresh_from_db()
        self.assertEqual(match.result, 'Crash')
        self.assertIsNone(gate.get_active_run())


class RunStatusTests(TestCase):
    def setUp(self):
        self.group = TestGroup.objects.create(description='gate test', branch='feature/x')
        self.run = GateRun.objects.create(
            test_group=self.group,
            branch='feature/x',
            baseline='master',
            games_per_opponent=2,
            opponent_bot_ids=[],
            expected_matches=2,
        )

    def _match(self, result):
        return Match.objects.create(
            test_group=self.group,
            start_timestamp=gate.timezone.now(),
            map_name='TestMap',
            opponent_race='Terran',
            result=result,
        )

    def test_pending_without_games(self):
        self.assertEqual(gate.compute_run_status(self.run), 'pending')

    def test_pending_while_no_game_finished(self):
        self._match('Pending')
        self.assertEqual(gate.compute_run_status(self.run), 'pending')

    def test_running_while_some_games_finished(self):
        self._match('Victory')
        self._match('Pending')
        self.assertEqual(gate.compute_run_status(self.run), 'running')

    def test_complete_when_every_game_finished(self):
        self._match('Victory')
        self._match('Defeat')
        self.assertEqual(gate.compute_run_status(self.run), 'complete')


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@override_settings(LAB_BRIDGE_TOKEN=BRIDGE_TOKEN)
class GateApiTests(TestCase):
    def setUp(self):
        self.repo_dir = tempfile.mkdtemp(prefix='gate-repo-')
        self.addCleanup(shutil.rmtree, self.repo_dir, True)
        self._init_repo()

        self.test_bot = CustomBot.objects.create(
            name='SubjectBot', race='Terran', bot_type='aiarena',
            is_test_subject=True, source_path=self.repo_dir,
        )
        self.opponent = CustomBot.objects.create(
            name='OpponentBot', race='Zerg', bot_type='aiarena',
        )

    def _init_repo(self):
        """Create a small git repo so the repo_url check has something to read."""
        env = {
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid',
            'PATH': os.environ.get('PATH', ''),
            'HOME': self.repo_dir,
        }
        subprocess.run(['git', 'init', '-q', self.repo_dir], check=True, env=env)
        subprocess.run(
            ['git', '-C', self.repo_dir, 'remote', 'add', 'origin',
             'https://example.invalid/owner/repo.git'],
            check=True, env=env,
        )
        with open(os.path.join(self.repo_dir, 'README.md'), 'w') as handle:
            handle.write('test\n')
        subprocess.run(['git', '-C', self.repo_dir, 'add', '.'], check=True, env=env)
        subprocess.run(
            ['git', '-C', self.repo_dir, 'commit', '-q', '-m', 'init'],
            check=True, env=env,
        )
        subprocess.run(
            ['git', '-C', self.repo_dir, 'checkout', '-q', '-b', 'feature/x'],
            check=True, env=env,
        )

    def _payload(self, **overrides):
        payload = {
            'repo_url': 'https://example.invalid/owner/repo.git',
            'branch': 'feature/x',
            'test_bot_id': self.test_bot.id,
            'opponent_bot_ids': [self.opponent.id],
            'games_per_opponent': 2,
            'baseline': 'master',
        }
        payload.update(overrides)
        return payload

    def _post(self, payload=None, token=BRIDGE_TOKEN):
        headers = {}
        if token is not None:
            headers['HTTP_X_LAB_BRIDGE_TOKEN'] = token
        return self.client.post(
            reverse('api_trigger_gate'),
            data=json.dumps(payload if payload is not None else self._payload()),
            content_type='application/json',
            **headers,
        )

    def _trigger(self, **overrides):
        """Trigger a gate run without touching git or docker."""
        with mock.patch(
            'test_lab.worktrees.get_or_create_worktree',
            return_value=self.repo_dir,
        ), mock.patch('test_lab.gate.launch_gate_series') as launch:
            response = self._post(**overrides)
        return response, launch

    # -- auth ------------------------------------------------------------

    def test_trigger_without_a_token_is_refused(self):
        response = self._post(token=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['status'], 'error')

    def test_trigger_with_a_wrong_token_is_refused(self):
        response = self._post(token='nope')
        self.assertEqual(response.status_code, 401)

    def test_status_without_a_token_is_refused(self):
        response = self.client.get(reverse('api_gate_status', args=[1]))
        self.assertEqual(response.status_code, 401)

    def test_an_unconfigured_lab_refuses_bridge_calls(self):
        with override_settings(LAB_BRIDGE_TOKEN=''):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('LAB_BRIDGE_TOKEN', None)
                response = self._post()
        self.assertEqual(response.status_code, 503)

    # -- trigger ---------------------------------------------------------

    def test_trigger_returns_the_run_id_immediately(self):
        response, launch = self._trigger()
        self.assertEqual(response.status_code, 200)
        run_id = response.json()['run_id']
        self.assertEqual(run_id, TestGroup.objects.get(branch='feature/x').id)
        self.assertTrue(GateRun.objects.filter(pk=run_id).exists())
        # The series is played in the background, not in the request.
        launch.assert_called_once()

    def test_trigger_creates_a_test_group_per_series(self):
        response, _ = self._trigger()
        run = GateRun.objects.get(pk=response.json()['run_id'])
        self.assertEqual(run.test_group.branch, 'feature/x')
        self.assertIsNotNone(run.baseline_group)
        self.assertEqual(run.baseline_group.branch, 'master')
        self.assertEqual(run.expected_matches, 2 * 1 * 2)
        self.assertEqual(run.games_per_opponent, 2)
        self.assertEqual(run.opponent_bot_ids, [self.opponent.id])

    def test_a_second_trigger_while_active_is_busy(self):
        response, _ = self._trigger()
        run_id = response.json()['run_id']

        busy, _ = self._trigger()
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json(), {'status': 'busy', 'run_id': run_id})

    def test_trigger_rejects_an_unknown_test_bot(self):
        response = self._post(self._payload(test_bot_id=9999))
        self.assertEqual(response.status_code, 404)

    def test_trigger_rejects_an_unknown_opponent(self):
        response = self._post(self._payload(opponent_bot_ids=[9999]))
        self.assertEqual(response.status_code, 404)

    def test_trigger_rejects_a_foreign_repo_url(self):
        response = self._post(self._payload(repo_url='https://example.invalid/other/repo.git'))
        self.assertEqual(response.status_code, 400)

    def test_trigger_rejects_an_empty_opponent_roster(self):
        response = self._post(self._payload(opponent_bot_ids=[]))
        self.assertEqual(response.status_code, 400)

    def test_trigger_rejects_a_branch_equal_to_the_baseline(self):
        response = self._post(self._payload(baseline='feature/x'))
        self.assertEqual(response.status_code, 400)

    def test_trigger_rejects_a_silly_series_length(self):
        response = self._post(self._payload(games_per_opponent=0))
        self.assertEqual(response.status_code, 400)

    def test_a_failing_prepare_script_fails_the_trigger(self):
        # The worktree could not be prepared (the repo-provided prepare
        # script failed), so no series is started — a series that can only
        # crash is worse than no series at all.
        error = worktrees.PrepareScriptError(
            'The prepare script scripts/vendor_lab_deps.sh failed in '
            '/tmp/worktree (exit code 1): vendor download failed'
        )
        with mock.patch(
            'test_lab.worktrees.get_or_create_worktree',
            side_effect=error,
        ), mock.patch('test_lab.gate.launch_gate_series') as launch:
            response = self._post()

        self.assertEqual(response.status_code, 500)
        self.assertIn('vendor download failed', response.json()['message'])
        launch.assert_not_called()

    def test_trigger_rejects_invalid_json(self):
        response = self.client.post(
            reverse('api_trigger_gate'),
            data='not json',
            content_type='application/json',
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        self.assertEqual(response.status_code, 400)

    # -- status ----------------------------------------------------------

    def _finished_run(self, branch_results, baseline_results):
        """Create a finished run and return its id."""
        branch_group = TestGroup.objects.create(
            description='gate: branch', branch='feature/x',
        )
        baseline_group = TestGroup.objects.create(
            description='gate: baseline', branch='master',
        )
        run = GateRun.objects.create(
            test_group=branch_group,
            baseline_group=baseline_group,
            test_bot=self.test_bot,
            branch='feature/x',
            baseline='master',
            games_per_opponent=len(branch_results),
            opponent_bot_ids=[self.opponent.id],
            expected_matches=len(branch_results) + len(baseline_results),
        )
        for group, results in (
            (branch_group, branch_results),
            (baseline_group, baseline_results),
        ):
            for result in results:
                Match.objects.create(
                    test_group=group,
                    start_timestamp=gate.timezone.now(),
                    end_timestamp=gate.timezone.now(),
                    map_name='TestMap',
                    opponent_race='Zerg',
                    opponent_bot=self.opponent,
                    result=result,
                    duration_in_game_time=100,
                )
        return run.pk

    def test_status_of_an_unknown_run(self):
        response = self.client.get(
            reverse('api_gate_status', args=[4242]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        self.assertEqual(response.status_code, 404)

    def test_status_reports_a_pass_within_the_noise_band(self):
        run_id = self._finished_run(
            ['Victory', 'Defeat'], ['Defeat', 'Defeat'],
        )
        response = self.client.get(
            reverse('api_gate_status', args=[run_id]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'complete')
        self.assertEqual(payload['verdict'], 'pass')
        self.assertEqual(
            payload['noise_band'],
            'within ±1 game per 2-game series vs baseline',
        )

    def test_status_reports_a_fail_outside_the_noise_band(self):
        run_id = self._finished_run(
            ['Victory', 'Victory'], ['Defeat', 'Defeat'],
        )
        response = self.client.get(
            reverse('api_gate_status', args=[run_id]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        payload = response.json()
        self.assertEqual(payload['status'], 'complete')
        self.assertEqual(payload['verdict'], 'fail')

    def test_status_reports_invalid_when_a_series_crashed(self):
        # The inaugural gate run: the branch won one game, the baseline
        # crashed every game.  Inside the band, so it used to be judged
        # "pass" — a false pass, because the baseline never played.
        run_id = self._finished_run(
            ['Victory', 'Defeat', 'Tie'], ['Crash', 'Crash', 'Crash'],
        )
        response = self.client.get(
            reverse('api_gate_status', args=[run_id]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        payload = response.json()
        self.assertEqual(payload['status'], 'complete')
        self.assertEqual(payload['verdict'], 'invalid')

    def test_status_reports_the_crash_counts_of_both_series(self):
        run_id = self._finished_run(
            ['Victory', 'Crash'], ['Crash', 'Crash'],
        )
        response = self.client.get(
            reverse('api_gate_status', args=[run_id]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        payload = response.json()
        record = payload['results'][0]
        self.assertEqual(record['branch']['crashes'], 1)
        self.assertEqual(record['baseline']['crashes'], 2)
        self.assertEqual(record['branch_crashes'], 1)
        self.assertEqual(record['baseline_crashes'], 2)
        self.assertTrue(record['crash_invalid'])

    def test_status_exposes_the_raw_numbers_of_both_series(self):
        run_id = self._finished_run(
            ['Victory', 'Defeat'], ['Defeat', 'Defeat'],
        )
        response = self.client.get(
            reverse('api_gate_status', args=[run_id]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        payload = response.json()
        self.assertEqual(len(payload['results']), 1)
        record = payload['results'][0]
        self.assertEqual(record['opponent_id'], self.opponent.id)
        self.assertEqual(record['opponent_name'], 'OpponentBot')
        self.assertEqual(record['branch']['wins'], 1)
        self.assertEqual(record['branch']['losses'], 1)
        self.assertEqual(record['branch']['durations'], [100, 100])
        self.assertEqual(len(record['branch']['game_list']), 2)
        self.assertEqual(record['baseline']['wins'], 0)
        self.assertEqual(record['baseline']['losses'], 2)
        self.assertEqual(len(record['baseline']['game_list']), 2)
        self.assertEqual(record['delta'], 1)
        self.assertTrue(record['within_band'])

    def test_status_of_a_running_run_has_no_verdict(self):
        branch_group = TestGroup.objects.create(
            description='gate: branch', branch='feature/x',
        )
        run = GateRun.objects.create(
            test_group=branch_group,
            test_bot=self.test_bot,
            branch='feature/x',
            baseline='master',
            games_per_opponent=2,
            opponent_bot_ids=[self.opponent.id],
            expected_matches=4,
        )
        Match.objects.create(
            test_group=branch_group,
            start_timestamp=gate.timezone.now(),
            map_name='TestMap',
            opponent_race='Zerg',
            opponent_bot=self.opponent,
            result='Pending',
        )
        response = self.client.get(
            reverse('api_gate_status', args=[run.pk]),
            HTTP_X_LAB_BRIDGE_TOKEN=BRIDGE_TOKEN,
        )
        payload = response.json()
        self.assertEqual(payload['status'], 'pending')
        self.assertNotIn('verdict', payload)
        self.assertEqual(payload['games_expected'], 4)
        self.assertEqual(payload['games_finished'], 0)
