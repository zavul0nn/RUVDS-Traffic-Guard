import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import guard
import host_target
from test_guard import CFG, FakeDocker, state, next_sample
import web

HAPROXY = {'kind': 'systemd', 'name': 'haproxy.service'}
DOCKER = {'kind': 'docker', 'name': 'remnanode'}
REAL_DROPIN = host_target.DROPIN


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.props = dict(LoadState='loaded', ActiveState='active', Restart='always', UnitFileState='enabled')
        self.calls = []
        def systemctl(*args):
            self.calls.append(args)
            self.assertIn(args[0], ('show', 'disable', 'daemon-reload', 'stop', 'start'))
            if args[0] == 'show':
                return '\n'.join(k + '=' + v for k, v in self.props.items())
            if args[0] == 'disable':
                self.props['UnitFileState'] = 'disabled'
            if args[0] == 'daemon-reload':
                self.props['Restart'] = 'no'
            if args[0] in ('stop', 'start'):
                self.props['ActiveState'] = 'inactive' if args[0] == 'stop' else 'active'
            return ''
        for name, value in (('systemctl', systemctl), ('DROPIN', self.root/'unit.d'/'guard.conf'),
                            ('BACKUP', self.root/'before.json')):
            mock = patch.object(host_target, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_inspection_is_observational(self):
        result = host_target.execute('inspect')
        self.assertTrue(result['State']['Running'])
        self.assertEqual([args[0] for args in self.calls], ['show', 'show'])
        self.assertFalse(host_target.DROPIN.exists())

    def test_claim_disables_autostart_without_stopping_haproxy(self):
        result = host_target.execute('set_restart_no')
        self.assertTrue(result['State']['Running'])
        self.assertEqual(result['HostConfig']['RestartPolicy']['Name'], 'no')
        self.assertIn('Restart=no', host_target.DROPIN.read_text())
        self.assertEqual(json.loads(host_target.BACKUP.read_text())['Restart'], 'always')
        host_target.execute('set_restart_no')
        self.assertEqual(json.loads(host_target.BACKUP.read_text())['Restart'], 'always')
        self.assertNotIn(('stop', host_target.UNIT), self.calls)

    def test_start_requires_claim_and_explicit_stop_stays_stopped(self):
        with self.assertRaises(RuntimeError):
            host_target.execute('start')
        host_target.execute('set_restart_no')
        self.assertFalse(host_target.execute('stop')['State']['Running'])
        self.assertTrue(host_target.execute('start')['State']['Running'])

    def test_arbitrary_units_and_shell_commands_are_rejected(self):
        for action in ('restart', 'stop ssh', '; touch /tmp/bad', ['stop']):
            with self.subTest(action=action), self.assertRaises(ValueError):
                host_target.execute(action)
        self.assertEqual(self.calls, [])

    def test_missing_service_is_not_created(self):
        self.props['LoadState'] = 'not-found'
        self.assertTrue(host_target.execute('stop')['missing'])
        self.assertFalse(host_target.DROPIN.exists())

    def test_policy_is_applied_after_standard_override(self):
        self.assertGreater(REAL_DROPIN.name, 'override.conf')
        host_target.DROPIN.parent.mkdir()
        override = host_target.DROPIN.with_name('override.conf')
        legacy = host_target.DROPIN.with_name('90-traffic-guard.conf')
        original = '[Service]\nRestart=always\nLimitNOFILE=65536\n'
        override.write_text(original)
        legacy.write_text(host_target.POLICY)
        host_target.execute('set_restart_no')
        self.assertEqual(override.read_text(), original)
        self.assertFalse(legacy.exists())

    def test_customized_legacy_file_is_not_removed(self):
        host_target.DROPIN.parent.mkdir()
        legacy = host_target.DROPIN.with_name('90-traffic-guard.conf')
        legacy.write_text('[Service]\nRestart=on-failure\n')
        host_target.execute('set_restart_no')
        self.assertTrue(legacy.exists())


class TargetControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        (self.data/'commands').mkdir()
        self.old = FakeDocker()
        self.haproxy = FakeDocker(policy='always')
        self.factory = lambda target: self.haproxy if target == HAPROXY else self.old
        self.c = self.controller()
        guard.atomic_json(self.c.state, state())

    def controller(self):
        c = guard.Controller(CFG, self.data, self.old,
                             lambda _: next_sample(c.load()), target_factory=self.factory)
        self.addCleanup(lambda: c.history.db.close() if c.history else None)
        return c

    def send(self, target):
        request_id = web.write_request(self.data/'commands', 'target', target=target)
        self.c.process_commands()
        return json.loads((self.data/'commands'/(request_id+'.result.json')).read_text())

    def test_switch_preserves_quota_and_running_services(self):
        self.assertTrue(self.send(HAPROXY)['ok'])
        self.assertEqual(self.c.load()['used'], 120)
        self.assertTrue(self.old.running)
        self.assertTrue(self.haproxy.running)
        self.assertEqual(self.haproxy.policy, 'no')
        self.assertEqual((self.haproxy.stops, self.haproxy.starts), (0, 0))
        self.assertEqual(self.c.load()['managed_target'], HAPROXY)

    def test_first_wait_switch_does_not_touch_restart_or_stop(self):
        guard.atomic_json(self.c.state, state('waiting'))
        guard.atomic_json(self.c.passive_marker, {'waiting': True})
        self.assertTrue(self.send(HAPROXY)['ok'])
        self.assertEqual(self.haproxy.policy, 'always')
        self.assertTrue(self.haproxy.running)
        self.assertEqual(self.haproxy.stops, 0)
        self.c.shutdown()
        self.assertEqual(self.haproxy.stops, 0)

    def test_switch_above_quota_stops_new_target_and_preserves_resume(self):
        guard.atomic_json(self.c.state, state('hold', 1100))
        self.old.running = False
        self.assertTrue(self.send(HAPROXY)['ok'])
        self.assertFalse(self.haproxy.running)
        self.assertTrue(self.c.load()['resume_pending'])
        self.assertEqual(self.c.load()['used'], 1120)

    def test_monitor_mode_does_not_stop_new_target(self):
        s = state(used=1100)
        s['settings'] = {'limit_bytes':1000, 'stop_on_limit':False}
        guard.atomic_json(self.c.state, s)
        self.assertTrue(self.send(HAPROXY)['ok'])
        self.assertTrue(self.haproxy.running)

    def test_switch_does_not_start_previously_stopped_target(self):
        self.haproxy.running = False
        self.assertTrue(self.send(HAPROXY)['ok'])
        self.assertEqual(self.haproxy.starts, 0)

    def test_restart_and_rearm_keep_selected_target(self):
        self.assertTrue(self.send(HAPROXY)['ok'])
        restarted = self.controller()
        restarted.tick()
        self.assertIs(restarted.docker, self.haproxy)
        restarted.arm(rearm=True)
        self.assertEqual(restarted.load()['managed_target'], HAPROXY)
        self.assertFalse(self.haproxy.running)

    def test_invalid_target_does_not_change_selection(self):
        for target in ({'kind':'systemd','name':'ssh.service'}, {'kind':'docker','name':'anything'}, None):
            self.assertFalse(self.send(target)['ok'])
            self.assertNotIn('managed_target', self.c.load())
            self.assertTrue(self.old.running)

    def test_missing_target_is_rejected_without_fault_or_switch(self):
        self.haproxy.inspect = lambda: (_ for _ in ()).throw(guard.TargetMissing('absent'))
        self.assertFalse(self.send(HAPROXY)['ok'])
        self.assertFalse(self.c.latch.exists())
        self.assertNotIn('managed_target', self.c.load())

    def test_fault_cannot_be_bypassed_by_switch(self):
        guard.atomic_json(self.c.latch, {'error':'test'})
        self.assertFalse(self.send(HAPROXY)['ok'])
        self.assertNotIn('managed_target', self.c.load())

    def test_unreachable_new_target_does_not_stop_old_target(self):
        self.haproxy.inspect = lambda: (_ for _ in ()).throw(ConnectionRefusedError('bridge unavailable'))
        self.assertFalse(self.send(HAPROXY)['ok'])
        self.assertTrue(self.old.running)
        self.assertFalse(self.c.latch.exists())

    def test_rearm_of_damaged_accounting_keeps_selected_target(self):
        self.assertTrue(self.send(HAPROXY)['ok'])
        self.c.state.write_text('broken')
        self.c.reader = lambda _: state()['sample']
        self.c.arm(rearm=True)
        self.assertEqual(self.c.load()['managed_target'], HAPROXY)
        self.assertFalse(self.haproxy.running)
        self.assertTrue(self.old.running)
        self.assertEqual(json.loads(self.c.target_hint.read_text()), HAPROXY)

    def test_initial_haproxy_selection_is_passive(self):
        self.c.state.unlink()
        with patch.dict('os.environ', {'TARGET_MODE':'haproxy'}):
            self.c.reader = lambda _: state()['sample']
            self.c.arm()
        self.c.tick()
        self.assertEqual(self.c.load()['managed_target'], HAPROXY)
        self.assertEqual(self.haproxy.policy, 'always')
        self.assertEqual(self.haproxy.stops, 0)


if __name__ == '__main__':
    unittest.main()
