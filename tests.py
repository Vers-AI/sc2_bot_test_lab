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

from . import gate
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

    def test_empty_series(self):
        summary = gate.summarize_series([])
        self.assertEqual(summary['games'], 0)
        self.assertEqual(summary['wins'], 0)
        self.assertEqual(summary['durations'], [])
        self.assertIsNone(summary['duration_median'])


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
