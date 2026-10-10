import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import guard


CFG = dict(target='remnanode', interface='eth0', limit=1000)


def observation(wall=1791583200, uptime=1000, rx=100, tx=200, boot='boot-1', ifindex=2):
    return dict(wall=wall, uptime=uptime, day=guard.utc_day(wall),
                counters=[rx, tx], boot=boot, ifindex=ifindex)


def state(mode='active', used=100):
    s = guard.initial(CFG, observation())
    s.update(mode=mode, used=used, resume_pending=False,
             policy_initialized=True,
             reason={'active': None, 'hold': 'threshold', 'waiting': 'first_full_utc_day'}[mode])
    return s


def next_sample(s, seconds=3, delta=10):
    o = copy.deepcopy(s['sample'])
    o.update(wall=o['wall'] + seconds, uptime=o['uptime'] + seconds)
    o['day'] = guard.utc_day(o['wall'])
    o['counters'][0] += delta
    return o


def boundary_state(mode='active', used=100):
    s = state(mode, used)
    # Precisely two seconds before UTC midnight, using a stable fixture date.
    s['sample'] = observation(wall=1791590398)
    return s


class FakeDocker:
    def __init__(self, running=True, policy='no'):
        self.running, self.policy = running, policy
        self.stops, self.starts = 0, 0

    def inspect(self):
        return {'State': {'Running': self.running}, 'HostConfig': {'RestartPolicy': {'Name': self.policy}}}

    def stop(self):
        self.stops += 1
        self.running = False

    def start(self):
        self.starts += 1
        self.running = True

    def set_restart_no(self):
        self.policy = 'no'


class TransitionTests(unittest.TestCase):
    def test_active_rollover_stays_active(self):
        s = boundary_state()
        o = next_sample(s, delta=20)
        self.assertNotEqual(s['sample']['day'], o['day'])
        new = guard.advance(s, o, CFG, True)
        self.assertEqual((new['mode'], new['used'], new['reason']), ('active', 20, None))
        self.assertEqual(s['used'], 100)  # transition does not mutate input

    def test_hold_still_accounts_background_traffic(self):
        s = state('hold', 1100)
        new = guard.advance(s, next_sample(s, delta=200), CFG, False)
        self.assertEqual((new['mode'], new['used']), ('hold', 1300))

    def test_exact_threshold_stops(self):
        s = state(used=990)
        new = guard.advance(s, next_sample(s), CFG, True)
        self.assertEqual(new['mode'], 'hold')
        self.assertTrue(new['resume_pending'])

    def test_manual_stop_not_claimed(self):
        s = state(used=990)
        new = guard.advance(s, next_sample(s), CFG, False)
        self.assertFalse(new['resume_pending'])

    def test_threshold_resume_intent_survives_midnight(self):
        s = boundary_state('hold', 1100)
        s['resume_pending'] = True
        new = guard.advance(s, next_sample(s), CFG, False)
        self.assertEqual(new['mode'], 'active')
        self.assertTrue(new['resume_pending'])

    def test_large_boundary_delta_does_not_resume(self):
        s = boundary_state('hold', 1100)
        s['resume_pending'] = True
        new = guard.advance(s, next_sample(s, delta=1000), CFG, False)
        self.assertEqual(new['mode'], 'hold')

    def test_initial_wait_ends_at_midnight(self):
        s = boundary_state('waiting')
        s['resume_pending'] = True
        new = guard.advance(s, next_sample(s), CFG, False)
        self.assertEqual(new['mode'], 'active')
        self.assertTrue(new['resume_pending'])

    def test_same_day_gap_keeps_counter_delta(self):
        s = state()
        new = guard.advance(s, next_sample(s, seconds=100, delta=50), CFG, True)
        self.assertEqual(new['used'], 150)

    def test_untrusted_samples_rejected(self):
        for change in ({'boot': 'new'}, {'ifindex': 9}, {'counters': [0, 0]},
                       {'wall': state()['sample']['wall'] - 1},
                       {'uptime': 999}, {'wall': state()['sample']['wall'] + 100}):
            with self.subTest(change=change):
                s = state()
                o = next_sample(s)
                o.update(change)
                o['day'] = guard.utc_day(o['wall'])
                with self.assertRaises(RuntimeError):
                    guard.advance(s, o, CFG, True)

    def test_long_midnight_gap_and_skipped_day_rejected(self):
        for seconds in (91, 86403):
            s = boundary_state()
            with self.assertRaises(RuntimeError):
                guard.advance(s, next_sample(s, seconds=seconds), CFG, False)

    def test_invalid_state_rejected(self):
        for key, value in [('used', -1), ('used', True), ('mode', 'unknown'),
                           ('resume_pending', 1), ('version', 3)]:
            with self.subTest(key=key):
                s = state()
                s[key] = value
                with self.assertRaises(ValueError):
                    guard.advance(s, next_sample(s), CFG, True)

    def test_config_change_rejected(self):
        s = state()
        with self.assertRaises(ValueError):
            guard.advance(s, next_sample(s), dict(CFG, limit=2000), True)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.docker = FakeDocker()
        self.s = state()
        self.c = guard.Controller(CFG, self.directory, self.docker,
                                  lambda _: next_sample(self.s))
        self.addCleanup(lambda: self.c.history.db.close() if self.c.history else None)
        guard.atomic_json(self.c.state, self.s)

    def cycle(self):
        try:
            self.c.tick()
        except Exception as e:
            self.c.emergency(e)

    def test_corrupt_json_stops_and_latches(self):
        self.c.state.write_text('{broken')
        self.cycle()
        self.assertFalse(self.docker.running)
        self.assertTrue(self.c.latch.exists())

    def test_missing_state_stops_instead_of_resetting(self):
        self.c.state.unlink()
        self.cycle()
        self.assertFalse(self.docker.running)
        self.assertFalse(self.c.state.exists())

    def test_disk_failure_still_stops(self):
        with patch.object(guard, 'atomic_json', side_effect=OSError('disk full')):
            self.cycle()
        self.assertFalse(self.docker.running)
        self.assertTrue(self.c.failed)

    def test_restart_policy_violation_stops(self):
        self.docker.policy = 'always'
        self.cycle()
        self.assertFalse(self.docker.running)
        self.assertTrue(self.c.latch.exists())

    def test_fault_survives_process_restart(self):
        self.c.emergency(RuntimeError('bad state'))
        self.docker.running = True
        restarted = guard.Controller(CFG, self.directory, self.docker)
        with self.assertRaises(RuntimeError):
            restarted.tick()
        restarted.emergency(RuntimeError('latched'))
        self.assertFalse(self.docker.running)

    def test_manual_start_during_hold_is_stopped_again(self):
        self.s = state('hold', 1100)
        guard.atomic_json(self.c.state, self.s)
        self.c.tick()
        self.assertFalse(self.docker.running)
        self.docker.running = True
        self.c.tick()
        self.assertEqual(self.docker.stops, 2)

    def test_pending_start_persisted_before_docker_call(self):
        self.s = boundary_state('hold', 1100)
        self.s['resume_pending'] = True
        guard.atomic_json(self.c.state, self.s)
        self.docker.running = False
        start = self.docker.start
        def checked_start():
            disk = self.c.load()
            self.assertTrue(disk['resume_pending'])
            self.assertEqual(disk['mode'], 'active')
            self.assertLess(disk['used'], CFG['limit'])
            start()
        self.docker.start = checked_start
        self.c.tick()
        self.assertTrue(self.docker.running)
        self.assertFalse(self.c.load()['resume_pending'])

    def test_large_boundary_delta_never_starts_container(self):
        self.s = boundary_state('hold', 1100)
        self.s['resume_pending'] = True
        guard.atomic_json(self.c.state, self.s)
        self.c.reader = lambda _: next_sample(self.s, delta=1000)
        self.docker.running = False
        self.c.tick()
        self.assertEqual(self.docker.starts, 0)

    def test_shutdown_stops_and_preserves_resume_intent(self):
        self.c.shutdown()
        self.assertFalse(self.docker.running)
        self.assertTrue(self.c.load()['resume_pending'])

    def test_shutdown_stops_even_if_state_is_broken(self):
        self.c.state.write_text('broken')
        with self.assertRaises(ValueError):
            self.c.shutdown()
        self.assertFalse(self.docker.running)

    def test_init_refuses_overwrite(self):
        with self.assertRaises(RuntimeError):
            self.c.arm()
        self.assertEqual(self.c.load(), self.s)

    def test_rearm_backs_up_and_waits_next_day(self):
        self.c.emergency(RuntimeError('test fault'))
        self.c.arm(rearm=True)
        self.assertEqual(self.c.load()['mode'], 'waiting')
        self.assertTrue(self.c.load()['resume_pending'])
        self.assertFalse(self.c.latch.exists())
        self.assertFalse(self.docker.running)
        self.assertEqual(len(list(self.directory.glob('*-backup-*'))), 2)

    def test_failed_stop_marks_unhealthy_and_retries(self):
        self.docker.stop = Mock(side_effect=TimeoutError('Docker unavailable'))
        self.c.emergency(RuntimeError('test'))
        self.assertFalse(json.loads(self.c.health.read_text())['ok'])
        self.assertTrue(self.c.latch.exists())
        self.cycle()
        self.assertEqual(self.docker.stop.call_count, 2)

    def test_start_failure_latches_and_stops(self):
        self.s = boundary_state('waiting')
        self.s['resume_pending'] = True
        guard.atomic_json(self.c.state, self.s)
        self.docker.start = Mock(side_effect=RuntimeError('start failed'))
        self.cycle()
        self.assertTrue(self.c.latch.exists())
        self.assertFalse(self.docker.running)


class AdapterTests(unittest.TestCase):
    def test_uses_daemon_api_version(self):
        docker = guard.Docker('remnanode')
        docker.request = Mock(side_effect=[{'ApiVersion': '1.99'}, {'State': {'Running': False}}])
        docker.inspect()
        self.assertEqual(docker.request.call_args_list[0].args, ('GET', '/version'))
        self.assertEqual(docker.request.call_args_list[1].args,
                         ('GET', '/v1.99/containers/remnanode/json'))

    def test_stop_checks_running_after_api_success(self):
        docker = guard.Docker('remnanode')
        docker.prefix = '/v1.45'
        docker.request = Mock(side_effect=[None, {'State': {'Running': True}}])
        with self.assertRaisesRegex(RuntimeError, 'still running'):
            docker.stop()

    def test_start_rechecks_restart_policy(self):
        docker = guard.Docker('remnanode')
        docker.inspect = Mock(return_value=FakeDocker(policy='always').inspect())
        docker.request = Mock()
        with self.assertRaises(RuntimeError):
            docker.start()
        docker.request.assert_not_called()

    def test_http_errors_not_treated_as_success_and_connection_closed(self):
        for status in (404, 500, 403):
            with self.subTest(status=status), patch.object(guard, 'UnixHTTP') as http:
                conn = http.return_value
                conn.getresponse.return_value.status = status
                conn.getresponse.return_value.read.return_value = b'{"message":"failed"}'
                with self.assertRaises(RuntimeError):
                    guard.Docker('remnanode').request('POST', '/test')
                conn.close.assert_called_once()

    def test_already_stopped_response_is_success(self):
        with patch.object(guard, 'UnixHTTP') as http:
            conn = http.return_value
            conn.getresponse.return_value.status = 304
            conn.getresponse.return_value.read.return_value = b''
            self.assertIsNone(guard.Docker('remnanode').request('POST', '/test'))
            conn.close.assert_called_once()

    def test_http_timeout_closes_connection(self):
        with patch.object(guard, 'UnixHTTP') as http:
            http.return_value.request.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                guard.Docker('remnanode').request('GET', '/version')
            http.return_value.close.assert_called_once()

    def test_counter_read_retries_midnight(self):
        def read(path):
            return {'boot_id': 'boot', 'ifindex': '2', 'rx_bytes': '100',
                    'tx_bytes': '200', 'uptime': '1000.0 0'}[path.name]
        midnight = 1791590400
        with patch.object(Path, 'read_text', read), patch.object(
                guard.time, 'time', side_effect=[midnight - .001, midnight + .001,
                                                midnight + .002, midnight + .003]):
            result = guard.sample(CFG)
        self.assertEqual(result['day'], guard.utc_day(midnight))
        self.assertEqual(result['wall'], midnight + .003)


if __name__ == '__main__':
    unittest.main()
