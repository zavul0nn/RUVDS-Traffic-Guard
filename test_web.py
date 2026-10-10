import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import guard
from history import History, read_history
import web
from test_guard import CFG, FakeDocker, state, next_sample, boundary_state


class WaitingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.docker = FakeDocker(policy='always')
        self.s = guard.initial(CFG, state()['sample'])
        self.c = guard.Controller(CFG, self.directory, self.docker,
                                  lambda _: next_sample(self.s))
        guard.atomic_json(self.c.state, self.s)
        guard.atomic_json(self.c.passive_marker, {'waiting': True})
        self.addCleanup(lambda: self.c.history.db.close() if self.c.history else None)

    def test_waiting_never_stops_starts_or_changes_policy(self):
        self.c.tick()
        self.assertTrue(self.docker.running)
        self.assertEqual(self.docker.policy, 'always')
        self.assertEqual((self.docker.stops, self.docker.starts), (0, 0))

    def test_waiting_error_does_not_stop(self):
        self.c.state.write_text('broken')
        self.c.emergency(ValueError('broken JSON'))
        self.assertTrue(self.docker.running)
        self.assertFalse(self.c.latch.exists())

    def test_waiting_shutdown_does_not_stop(self):
        self.c.shutdown()
        self.assertTrue(self.docker.running)

    def test_waiting_reboot_rebases_without_activation(self):
        obs = next_sample(self.s)
        obs.update(boot='new-boot', uptime=2, counters=[1, 1])
        new = guard.advance(self.s, obs, CFG, True)
        self.assertEqual(new['mode'], 'waiting')
        self.assertEqual(new['sample']['boot'], 'new-boot')

    def test_waiting_missed_midnight_waits_another_day(self):
        s = boundary_state('waiting')
        s['resume_pending'] = False
        obs = next_sample(s, seconds=91)
        new = guard.advance(s, obs, CFG, True)
        self.assertEqual(new['mode'], 'waiting')
        next_obs = dict(obs, wall=obs['wall'] + 3, uptime=obs['uptime'] + 3)
        self.assertEqual(guard.advance(new, next_obs, CFG, True)['mode'], 'waiting')

    def test_first_midnight_activates_without_restarting_node(self):
        self.s = boundary_state('waiting')
        self.s.update(resume_pending=False, policy_initialized=False)
        guard.atomic_json(self.c.state, self.s)
        self.c.tick()
        self.assertEqual(self.c.load()['mode'], 'active')
        self.assertEqual(self.docker.policy, 'no')
        self.assertEqual((self.docker.stops, self.docker.starts), (0, 0))

    def test_init_without_target_inspection(self):
        self.c.state.unlink()
        self.c.passive_marker.unlink()
        self.docker.inspect = lambda: (_ for _ in ()).throw(guard.TargetMissing())
        self.c.arm()
        self.c.tick()
        self.assertTrue(json.loads((self.directory / 'snapshot.json').read_text())['node_missing'])
        self.assertEqual((self.docker.stops, self.docker.starts), (0, 0))


class CommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.commands = self.directory / 'commands'
        self.commands.mkdir()
        self.docker = FakeDocker()
        self.c = guard.Controller(CFG, self.directory, self.docker,
                                  lambda _: next_sample(self.c.load()))
        guard.atomic_json(self.c.state, state())
        self.addCleanup(lambda: self.c.history.db.close() if self.c.history else None)

    def send(self, action, settings=None):
        request_id = web.write_request(self.commands, action, settings)
        self.c.process_commands()
        return json.loads((self.commands / (request_id + '.result.json')).read_text())

    def test_manual_pause_and_resume(self):
        self.assertTrue(self.send('pause')['ok'])
        self.assertTrue(self.c.load()['manual_paused'])
        self.assertFalse(self.docker.running)
        self.assertTrue(self.send('resume')['ok'])
        self.assertFalse(self.c.load()['manual_paused'])
        self.assertTrue(self.docker.running)

    def test_manual_pause_persists_over_midnight(self):
        s = boundary_state()
        s['manual_paused'] = True
        guard.atomic_json(self.c.state, s)
        self.docker.running = False
        self.c.tick()
        self.assertFalse(self.docker.running)
        self.assertTrue(self.c.load()['manual_paused'])

    def test_resume_cannot_bypass_threshold(self):
        guard.atomic_json(self.c.state, state('hold', 1100))
        self.docker.running = False
        self.assertFalse(self.send('resume')['ok'])
        self.assertFalse(self.docker.running)

    def test_expired_command_does_nothing(self):
        request_id = web.write_request(self.commands, 'pause')
        path = self.commands / (request_id + '.request.json')
        request = json.loads(path.read_text())
        request['at'] = time.time() - 65
        path.write_text(json.dumps(request))
        self.c.process_commands()
        self.assertTrue(self.docker.running)

    def test_rearm_stops_until_next_midnight(self):
        self.c.emergency(RuntimeError('test fault'))
        self.assertTrue(self.send('rearm')['ok'])
        self.assertFalse(self.docker.running)
        self.assertFalse(self.c.load()['passive_wait'])

    def test_lower_limit_applies_immediately_without_resetting_usage(self):
        self.assertTrue(self.send('settings', {'limit_bytes':90, 'stop_on_limit':True})['ok'])
        self.assertEqual(self.c.load()['used'], 120)
        self.assertEqual(self.c.load()['mode'], 'hold')
        self.assertFalse(self.docker.running)

    def test_raise_limit_restarts_only_the_node_stopped_by_quota(self):
        self.send('settings', {'limit_bytes':90, 'stop_on_limit':True})
        self.assertTrue(self.send('settings', {'limit_bytes':500, 'stop_on_limit':True})['ok'])
        self.assertTrue(self.docker.running)
        self.assertEqual(self.c.load()['used'], 140)
        self.assertEqual(self.c.load()['mode'], 'active')

    def test_monitoring_keeps_node_running_above_threshold(self):
        self.assertTrue(self.send('settings', {'limit_bytes':90, 'stop_on_limit':False})['ok'])
        self.assertTrue(self.docker.running)
        self.c.tick()
        self.assertEqual(self.c.load()['used'], 130)
        self.assertEqual(self.c.load()['mode'], 'active')
        self.assertEqual(self.docker.stops, 0)

    def test_enabling_stop_above_limit_stops_now(self):
        self.send('settings', {'limit_bytes':90, 'stop_on_limit':False})
        self.assertTrue(self.send('settings', {'limit_bytes':90, 'stop_on_limit':True})['ok'])
        self.assertFalse(self.docker.running)
        self.assertEqual(self.c.load()['mode'], 'hold')

    def test_monitoring_releases_quota_hold(self):
        self.send('settings', {'limit_bytes':90, 'stop_on_limit':True})
        self.assertTrue(self.send('settings', {'limit_bytes':90, 'stop_on_limit':False})['ok'])
        self.assertTrue(self.docker.running)
        self.assertEqual(self.c.load()['used'], 140)

    def test_policy_changes_preserve_manual_pause(self):
        self.send('pause')
        self.assertTrue(self.send('settings', {'limit_bytes':90, 'stop_on_limit':False})['ok'])
        self.assertFalse(self.docker.running)
        self.assertTrue(self.c.load()['manual_paused'])
        self.assertTrue(self.send('resume')['ok'])
        self.assertTrue(self.docker.running)

    def test_settings_do_not_claim_an_already_stopped_node(self):
        self.docker.running = False
        self.assertTrue(self.send('settings', {'limit_bytes':90, 'stop_on_limit':False})['ok'])
        self.assertFalse(self.docker.running)
        self.assertEqual(self.docker.starts, 0)

    def test_settings_survive_new_controller_and_host_reboot(self):
        policy = {'limit_bytes':90, 'stop_on_limit':False}
        self.send('settings', policy)
        saved = self.c.load()
        from test_guard import observation
        obs = observation(wall=saved['sample']['wall']+60, uptime=5,
                          rx=20, tx=30, boot='boot-2')
        self.docker.running = False
        restarted = guard.Controller(CFG, self.directory, self.docker, lambda _: obs)
        self.addCleanup(lambda: restarted.history.db.close() if restarted.history else None)
        restarted.tick()
        self.assertEqual(restarted.load()['settings'], policy)
        self.assertEqual(restarted.load()['used'], saved['used']+50)
        self.assertTrue(self.docker.running)

    def test_rearm_preserves_saved_policy(self):
        policy = {'limit_bytes':500, 'stop_on_limit':False}
        self.send('settings', policy)
        self.assertTrue(self.send('rearm')['ok'])
        self.assertEqual(self.c.load()['settings'], policy)
        self.assertEqual(self.c.load()['mode'], 'waiting')
        self.assertFalse(self.docker.running)

    def test_invalid_settings_rejected_without_fault_or_changes(self):
        for policy in (None, {}, {'limit_bytes':True, 'stop_on_limit':True},
                       {'limit_bytes':0, 'stop_on_limit':True},
                       {'limit_bytes':1.5, 'stop_on_limit':True},
                       {'limit_bytes':100, 'stop_on_limit':'false'},
                       {'limit_bytes':100, 'stop_on_limit':True, 'target':'other'},
                       {'limit_bytes':9_000_000_000_000_001, 'stop_on_limit':True}):
            with self.subTest(policy=policy):
                self.assertFalse(self.send('settings', policy)['ok'])
                self.assertNotIn('settings', self.c.load())
                self.assertFalse(self.c.latch.exists())
                self.assertTrue(self.docker.running)

    def test_settings_cannot_bypass_fault(self):
        self.c.emergency(RuntimeError('bad state'))
        self.assertFalse(self.send('settings', {'limit_bytes':90, 'stop_on_limit':False})['ok'])
        self.assertTrue(self.c.latch.exists())
        self.assertFalse(self.docker.running)

    def test_settings_during_first_install_do_not_stop_node(self):
        s = guard.initial(CFG, state()['sample'])
        guard.atomic_json(self.c.state, s)
        guard.atomic_json(self.c.passive_marker, {'waiting':True})
        self.assertTrue(self.send('settings', {'limit_bytes':1, 'stop_on_limit':True})['ok'])
        self.assertTrue(self.docker.running)
        self.assertEqual(self.c.load()['mode'], 'waiting')
        self.assertEqual(self.docker.stops, 0)
        self.assertFalse(self.c.latch.exists())


class HistoryTests(unittest.TestCase):
    def test_background_traffic_and_partial_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            history = History(path)
            try:
                old = state('hold', 1100)
                new = guard.advance(old, next_sample(old, delta=50), CFG, False)
                history.record(old, new)
                data = read_history(path, new['sample']['wall'])
                self.assertEqual(data['days'][0]['used'], 1150)
                self.assertEqual(data['days'][0]['rx'], 50)
            finally:
                history.db.close()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = web.password_record('test-password-very-long')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.commands = self.data / 'commands'
        self.commands.mkdir()
        self.prefix = 'test-hidden-path-0123456789'
        self.server = web.Panel(('127.0.0.1', 0), self.prefix, self.auth,
                                self.data, self.commands, Path(__file__).parent / 'static')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = ''
        self.csrf = ''
        guard.atomic_json(self.data / 'health.json', {'ok': True, 'at': time.time()})
        guard.atomic_json(self.data / 'snapshot.json', {'at': time.time(), 'state': state(), 'node_running': True})

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, route, payload=None, headers=None):
        host = f'127.0.0.1:{self.server.server_port}'
        conn = http.client.HTTPConnection(host, timeout=5)
        default = {'Host': host, 'Origin': 'http://' + host, 'Cookie': self.cookie,
                   'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'}
        default.update(headers or {})
        conn.request(method, route, json.dumps(payload) if payload is not None else None, default)
        response = conn.getresponse()
        status, result_headers, body = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return status, result_headers, body

    def login(self):
        status, headers, _ = self.request('POST', '/' + self.prefix + '/api/login', {'password': 'test-password-very-long'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        status, _, body = self.request('GET', '/' + self.prefix + '/api/status')
        self.assertEqual(status, 200)
        self.csrf = json.loads(body)['csrf']

    def test_wrong_paths_uniform_404_without_redirect(self):
        for route in ('/', '/admin', '/robots.txt', '/' + self.prefix + 'x', '/style.css'):
            status, headers, body = self.request('GET', route)
            self.assertEqual(status, 404)
            self.assertEqual(body, b'Not found\n')
            self.assertNotIn('Location', headers)
            self.assertNotIn(self.prefix, body.decode())

    def test_login_page_does_not_require_slash(self):
        status, headers, body = self.request('GET', '/' + self.prefix)
        self.assertEqual(status, 200)
        self.assertIn(b'login-form', body)
        self.assertNotIn(b'__BASE__', body)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')

    def test_api_requires_authentication(self):
        status, _, _ = self.request('GET', '/' + self.prefix + '/api/status')
        self.assertEqual(status, 401)

    def test_password_not_stored_plaintext(self):
        self.assertNotIn('test-password-very-long', json.dumps(self.auth))
        self.assertFalse(web.password_matches('bad-password', self.auth))

    def test_login_sets_safe_cookie_and_dashboard(self):
        self.login()
        status, _, body = self.request('GET', '/' + self.prefix + '/')
        self.assertEqual(status, 200)
        self.assertIn(b'quota-ruler', body)

    def test_wrong_password_and_rate_limit(self):
        for _ in range(8):
            status, _, _ = self.request('POST', '/' + self.prefix + '/api/login', {'password': 'bad'})
            self.assertEqual(status, 401)
        status, _, _ = self.request('POST', '/' + self.prefix + '/api/login', {'password': 'test-password-very-long'})
        self.assertEqual(status, 429)

    def test_csrf_and_origin_checks(self):
        self.login()
        for headers in ({'X-CSRF-Token': ''}, {'Origin': 'http://evil.invalid'}):
            status, _, _ = self.request('POST', '/' + self.prefix + '/api/action', {'action': 'pause'}, headers)
            self.assertEqual(status, 403)
        self.assertEqual(len(list(self.commands.iterdir())), 0)

    def test_authenticated_action_writes_bounded_command(self):
        self.login()
        status, _, body = self.request('POST', '/' + self.prefix + '/api/action', {'action': 'pause'})
        self.assertEqual(status, 202)
        request_id = json.loads(body)['id']
        self.assertTrue((self.commands / (request_id + '.request.json')).exists())

    def test_rearm_requires_explicit_confirmation(self):
        self.login()
        status, _, _ = self.request('POST', '/' + self.prefix + '/api/action', {'action': 'rearm'})
        self.assertEqual(status, 400)

    def test_authenticated_settings_command_contains_validated_policy(self):
        self.login()
        policy = {'limit_bytes':145_500_000_000, 'stop_on_limit':False}
        status, _, body = self.request('POST', '/' + self.prefix + '/api/action',
                                       {'action':'settings', 'settings':policy})
        self.assertEqual(status, 202)
        request = json.loads((self.commands / (json.loads(body)['id'] + '.request.json')).read_text())
        self.assertEqual(request['settings'], policy)

    def test_invalid_http_settings_never_enter_queue(self):
        self.login()
        for policy in ({}, {'limit_bytes':-1, 'stop_on_limit':True},
                       {'limit_bytes':100, 'stop_on_limit':1},
                       {'limit_bytes':'145', 'stop_on_limit':False}):
            with self.subTest(policy=policy):
                status, _, _ = self.request('POST', '/' + self.prefix + '/api/action',
                                            {'action':'settings', 'settings':policy})
                self.assertEqual(status, 400)
        self.assertFalse(list(self.commands.iterdir()))

    def test_settings_require_csrf_and_same_origin(self):
        self.login()
        for headers in ({'X-CSRF-Token':''}, {'Origin':'http://evil.invalid'}):
            status, _, _ = self.request('POST', '/' + self.prefix + '/api/action',
                                        {'action':'settings', 'settings':{'limit_bytes':100, 'stop_on_limit':False}}, headers)
            self.assertEqual(status, 403)
        self.assertFalse(list(self.commands.iterdir()))

    def test_expired_session_rejected(self):
        self.login()
        token = self.cookie.split('=', 1)[1]
        self.server.sessions[token]['last'] -= 1801
        status, _, _ = self.request('GET', '/' + self.prefix + '/api/status')
        self.assertEqual(status, 401)

    def test_stale_controller_rejects_command(self):
        self.login()
        guard.atomic_json(self.data / 'health.json', {'ok': True, 'at': time.time() - 31})
        status, _, _ = self.request('POST', '/' + self.prefix + '/api/action', {'action': 'pause'})
        self.assertEqual(status, 503)

    def test_logout_invalidates_session(self):
        self.login()
        status, _, _ = self.request('POST', '/' + self.prefix + '/api/logout', {})
        self.assertEqual(status, 200)
        status, _, _ = self.request('GET', '/' + self.prefix + '/api/status')
        self.assertEqual(status, 401)

    def test_corrupt_fault_file_does_not_hide_recovery_controls(self):
        self.login()
        (self.data / 'fault.json').write_text('broken')
        status, _, body = self.request('GET', '/' + self.prefix + '/api/status')
        self.assertEqual(status, 200)
        self.assertIn('csrf', json.loads(body))
        self.assertIn('error', json.loads(body)['fault'])


if __name__ == '__main__':
    unittest.main()
