#!/usr/bin/env python3
"""UTC interface quota controller. Standard library only; Linux runtime."""
import argparse
import copy
import datetime as dt
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import tempfile
import time
from urllib.parse import quote


def log(event, **fields):
    print(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),
                          event=event, **fields)), flush=True)


def utc_day(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat()


def config():
    result = dict(target=os.getenv('TARGET_CONTAINER', 'remnanode'),
                  interface=os.getenv('INTERFACE', 'eth0'),
                  limit=int(os.getenv('LIMIT_BYTES', '145000000000')))
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', result['target']):
        raise ValueError('Invalid target name')
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]+', result['interface']):
        raise ValueError('Invalid interface')
    if result['limit'] <= 0:
        raise ValueError('LIMIT_BYTES must be positive')
    return result


def sample(cfg):
    net = Path('/host/sys/class/net') / cfg['interface']
    for _ in range(3):
        start = time.time()
        result = dict(boot=Path('/host/boot_id').read_text().strip(),
                      ifindex=int((net / 'ifindex').read_text()),
                      counters=[int((net / 'statistics' / name).read_text())
                                for name in ('rx_bytes', 'tx_bytes')],
                      uptime=float(Path('/host/uptime').read_text().split()[0]),
                      wall=time.time())
        # Retry across midnight instead of assigning mixed readings to a day.
        if utc_day(start) == utc_day(result['wall']) and 0 <= result['wall'] - start <= 2:
            result['day'] = utc_day(result['wall'])
            return result
    raise RuntimeError('Could not obtain a stable counter sample')


def initial(cfg, obs, passive=True):
    return dict(version=1, config=cfg, sample=obs, used=0, mode='waiting',
                reason='first_full_utc_day', resume_pending=not passive,
                passive_wait=passive, manual_paused=False, policy_initialized=False,
                accounting_complete=False, node_was_running=False)


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != {'limit_bytes', 'stop_on_limit'}:
        raise ValueError('Invalid quota settings')
    if type(settings['limit_bytes']) is not int or not 1 <= settings['limit_bytes'] <= 9_000_000_000_000_000:
        raise ValueError('Limit must be an integer from 1 to 9000000000000000 bytes')
    if type(settings['stop_on_limit']) is not bool:
        raise ValueError('stop_on_limit must be boolean')


def effective_settings(state):
    return state.get('settings', dict(limit_bytes=state['config']['limit'], stop_on_limit=True))


def validate(s, cfg):
    if s.get('version') != 1 or s.get('config') != cfg:
        raise ValueError('State version/config mismatch')
    if type(s.get('used')) is not int or s['used'] < 0:
        raise ValueError('Invalid used bytes')
    if 'settings' in s:
        validate_settings(s['settings'])
    modes = {'active': None, 'waiting': 'first_full_utc_day', 'hold': 'threshold'}
    if s.get('mode') not in modes or s.get('reason') != modes[s['mode']]:
        raise ValueError('Invalid state mode/reason')
    if type(s.get('resume_pending')) is not bool:
        raise ValueError('Invalid resume flag')
    for key in ('passive_wait', 'manual_paused', 'policy_initialized',
                'accounting_complete', 'node_was_running'):
        if type(s.get(key, False)) is not bool:
            raise ValueError('Invalid ' + key)
    o = s['sample']
    if not isinstance(o['boot'], str) or not o['boot']:
        raise ValueError('Invalid boot ID')
    if type(o['ifindex']) is not int or o['ifindex'] < 1:
        raise ValueError('Invalid interface identity')
    if not isinstance(o['counters'], list) or len(o['counters']) != 2:
        raise ValueError('Invalid counters')
    if any(type(v) is not int or v < 0 for v in o['counters']):
        raise ValueError('Invalid counters')
    for key in ('wall', 'uptime'):
        if type(o[key]) not in (int, float) or not math.isfinite(o[key]) or o[key] < 0:
            raise ValueError('Invalid timestamp')
    if o['day'] != utc_day(o['wall']):
        raise ValueError('State day does not match sample time')


def advance(state, obs, cfg, running):
    """Pure transition: no I/O. Entire boundary delta belongs to the new day."""
    validate(state, cfg)
    s = copy.deepcopy(state)
    old = s['sample']
    policy = effective_settings(s)
    limit, enforce = policy['limit_bytes'], policy['stop_on_limit']
    if s['mode'] == 'hold' and (not enforce or s['used'] < limit):
        s.update(mode='active', reason=None)
    if s['mode'] == 'waiting' and s.get('passive_wait', False):
        # Installation is observational only. A reboot/gap restarts observation,
        # never stops a node before its first trustworthy full UTC day.
        elapsed = obs['uptime'] - old['uptime']
        wall_elapsed = obs['wall'] - old['wall']
        continuous = (obs['boot'] == old['boot'] and obs['ifindex'] == old['ifindex']
                      and all(c >= p for c, p in zip(obs['counters'], old['counters']))
                      and elapsed >= 0 and wall_elapsed >= 0
                      and abs(elapsed - wall_elapsed) <= 5)
        days = (dt.date.fromisoformat(obs['day']) - dt.date.fromisoformat(old['day'])).days
        delta = sum(c - p for c, p in zip(obs['counters'], old['counters'])) if continuous else 0
        if continuous and days == 1 and elapsed <= 90:
            s.update(mode='active', reason=None, used=delta, passive_wait=False,
                     accounting_complete=True)
        else:
            s['used'] = s['used'] + delta if days == 0 else delta
        s['sample'] = obs
        if enforce and s['mode'] == 'active' and s['used'] >= limit:
            s.update(mode='hold', reason='threshold', resume_pending=running)
        validate(s, cfg)
        return s
    if obs['boot'] != old['boot']:
        # New kernel counters contain ALL traffic since boot. Never subtract
        # previous-boot counters or count an already persisted boot twice.
        days = (dt.date.fromisoformat(obs['day']) - dt.date.fromisoformat(old['day'])).days
        boot_at = obs['wall'] - obs['uptime']
        if days < 0 or obs['wall'] < old['wall'] or boot_at < old['wall'] - 5:
            raise RuntimeError('Invalid reboot chronology or wall clock')
        delta = sum(obs['counters'])
        if days == 0:
            s['used'] += delta
        else:
            # A boot before midnight conservatively charges its entire traffic
            # to today, avoiding an unobservable division at the boundary.
            s.update(used=delta, mode='active', reason=None)
        s.update(sample=obs, accounting_complete=False)
        if not s.get('manual_paused', False):
            s['resume_pending'] = (s['resume_pending'] or
                                   s.get('node_was_running', False) or running)
        if enforce and s['mode'] == 'active' and s['used'] >= limit:
            s.update(mode='hold', reason='threshold')
        validate(s, cfg)
        return s
    if obs['ifindex'] != old['ifindex']:
        raise RuntimeError('Interface replacement')
    if any(c < p for c, p in zip(obs['counters'], old['counters'])):
        raise RuntimeError('Network counters reset')
    elapsed = obs['uptime'] - old['uptime']
    wall_elapsed = obs['wall'] - old['wall']
    if elapsed < 0 or wall_elapsed < 0 or abs(elapsed - wall_elapsed) > 5:
        raise RuntimeError('Wall clock jump or invalid uptime')
    delta = sum(c - p for c, p in zip(obs['counters'], old['counters']))
    if obs['day'] != old['day']:
        days = (dt.date.fromisoformat(obs['day']) - dt.date.fromisoformat(old['day'])).days
        if days != 1 or elapsed > 90:
            raise RuntimeError('Missing trustworthy sample at UTC rollover')
        s['used'] = delta
        s['mode'], s['reason'] = 'active', None
        s['accounting_complete'] = True
    else:
        # Account in EVERY mode, including stopped-container background traffic.
        s['used'] += delta
    s['sample'] = obs
    if enforce and s['mode'] == 'active' and s['used'] >= limit:
        s.update(mode='hold', reason='threshold',
                 resume_pending=running or s['resume_pending'])
    validate(s, cfg)
    return s


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    os.chmod(temp, 0o640)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        if os.name == 'posix':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


class TargetMissing(RuntimeError):
    pass


class Docker:
    def __init__(self, target):
        self.target = quote(target, safe='')
        self.prefix = None

    def request(self, method, path, accepted=(200, 204, 304)):
        conn = UnixHTTP('localhost', timeout=12)
        try:
            conn.request(method, path, body=b'' if method == 'POST' else None)
            response = conn.getresponse()
            body = response.read()
            if response.status == 404 and '/containers/' in path:
                raise TargetMissing('Target container does not exist')
            if response.status not in accepted:
                raise RuntimeError(f'Docker {method} {path}: {response.status} {body[:300]!r}')
            return json.loads(body) if body else None
        finally:
            conn.close()

    def path(self, suffix):
        if self.prefix is None:
            # Use the daemon's API version, including newer minimum API versions.
            version = self.request('GET', '/version')['ApiVersion']
            if not re.fullmatch(r'\d+\.\d+', version):
                raise ValueError('Invalid Docker API version')
            self.prefix = '/v' + version
        return f'{self.prefix}/containers/{self.target}/{suffix}'

    def inspect(self):
        return self.request('GET', self.path('json'))

    def stop(self):
        # Independent of state parsing or writes; idempotent, with verification.
        try:
            self.request('POST', self.path('stop?t=2'))
        except TargetMissing:
            return
        if self.inspect()['State']['Running']:
            raise RuntimeError('Target still running after stop')

    def start(self):
        info = self.inspect()
        if info['HostConfig']['RestartPolicy']['Name'] != 'no':
            raise RuntimeError('Target restart policy must be no')
        self.request('POST', self.path('start'))
        if not self.inspect()['State']['Running']:
            raise RuntimeError('Target did not stay running')

    def set_restart_no(self):
        conn = UnixHTTP('localhost', timeout=12)
        try:
            conn.request('POST', self.path('update'),
                         body=json.dumps({'RestartPolicy': {'Name': 'no'}}),
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                raise RuntimeError(f'Docker update failed: {response.status} {body[:200]!r}')
        finally:
            conn.close()


class Controller:
    def __init__(self, cfg, directory, docker, reader=sample):
        self.cfg, self.directory, self.docker, self.reader = cfg, directory, docker, reader
        self.state = directory / 'state.json'
        self.latch = directory / 'fault.json'
        self.health = directory / 'health.json'
        self.passive_marker = directory / 'passive-wait.json'
        self.failed = False
        self.last_state = None
        self.history = None

    def is_passive(self):
        if not self.passive_marker.exists():
            return False
        try:
            s = self.load()
            return s['mode'] == 'waiting' and s.get('passive_wait', False)
        except Exception:
            return True

    def load(self):
        s = json.loads(self.state.read_text())
        validate(s, self.cfg)
        return s

    def tick(self):
        if self.failed or self.latch.exists():
            raise RuntimeError('Fault latched; inspect logs and explicitly rearm')
        s = self.load()
        missing = False
        try:
            info = self.docker.inspect()
        except TargetMissing:
            missing = True
            info = {'State': {'Running': False}, 'HostConfig': {'RestartPolicy': {'Name': 'no'}}}
        passive = s['mode'] == 'waiting' and s.get('passive_wait', False)
        if not passive and info['HostConfig']['RestartPolicy']['Name'] != 'no':
            if not s.get('policy_initialized', True) and not missing:
                self.docker.set_restart_no()
                s['policy_initialized'] = True
            else:
                raise RuntimeError('Target restart policy must remain no')
        new = advance(s, self.reader(self.cfg), self.cfg, info['State']['Running'])
        new['node_was_running'] = info['State']['Running'] or new['resume_pending']
        if passive and new['mode'] != 'waiting':
            self.passive_marker.unlink(missing_ok=True)
            if not missing and info['HostConfig']['RestartPolicy']['Name'] != 'no':
                self.docker.set_restart_no()
        if not missing and new['mode'] != 'waiting':
            new['policy_initialized'] = True
        atomic_json(self.state, new)  # Persist intent BEFORE start/stop.
        node_running = info['State']['Running']
        if new.get('manual_paused', False) or new['mode'] == 'hold' or (new['mode'] == 'waiting' and not new.get('passive_wait', False)):
            if info['State']['Running']:
                self.docker.stop()
                node_running = False
                new['node_was_running'] = False
                atomic_json(self.state, new)
        elif new['mode'] == 'active' and new['resume_pending'] and not missing:
            self.docker.start()
            node_running = True
            new['resume_pending'] = False
            new['node_was_running'] = True
            atomic_json(self.state, new)
        if s['sample']['boot'] != new['sample']['boot']:
            log('host_reboot', used=new['used'], day=new['sample']['day'])
        if (s['mode'], s['reason'], s['sample']['day']) != (new['mode'], new['reason'], new['sample']['day']):
            log('transition', mode=new['mode'], reason=new['reason'], used=new['used'])
        atomic_json(self.health, dict(ok=True, at=time.time(), mode=new['mode']))
        previous = self.last_state or s
        seconds = new['sample']['uptime'] - previous['sample']['uptime']
        rate = [max(0, c - p) / seconds if seconds > 0 and previous['sample']['boot'] == new['sample']['boot'] else 0
                for c, p in zip(new['sample']['counters'], previous['sample']['counters'])]
        atomic_json(self.directory / 'snapshot.json', dict(at=time.time(), state=new,
                    node_running=None if missing else node_running, node_missing=missing,
                    rate_rx=rate[0], rate_tx=rate[1], fault=None))
        self.last_state = copy.deepcopy(new)
        self.record_history(s, new)

    def record_history(self, old, new):
        try:
            if self.history is None:
                from history import History
                self.history = History(self.directory / 'history.sqlite3')
            self.history.record(old, new)
        except Exception as error:
            # Telemetry is secondary; it must never reset or bypass the quota.
            log('telemetry_error', error=str(error))

    def emergency(self, error):
        if self.is_passive():
            log('waiting_observation_error', error=str(error))
            try:
                atomic_json(self.health, dict(ok=False, at=time.time(), error=str(error)))
            except Exception:
                pass
            return
        self.failed = True
        log('fault', error=str(error))
        # Stop FIRST: a full/read-only disk must not prevent Docker stop.
        node_running = None
        try:
            self.docker.stop()
            node_running = False
        except Exception as stop_error:
            log('CRITICAL_STOP_FAILED', error=str(stop_error))
        try:
            if not self.latch.exists():
                atomic_json(self.latch, dict(at=time.time(), error=str(error)))
            atomic_json(self.health, dict(ok=False, at=time.time(), error=str(error)))
            snapshot = dict(at=time.time(), fault=str(error), node_running=node_running)
            try:
                snapshot['state'] = self.load()
            except Exception:
                pass
            atomic_json(self.directory / 'snapshot.json', snapshot)
        except Exception as write_error:
            log('CRITICAL_STATE_WRITE_FAILED', error=str(write_error))

    def arm(self, rearm=False):
        if not rearm and (self.state.exists() or self.latch.exists()):
            raise RuntimeError('State exists; refusing to overwrite')
        saved_settings = None
        if rearm and self.state.exists():
            try:
                saved_settings = self.load().get('settings')
            except (ValueError, KeyError):
                pass  # Recovery of damaged state still needs to be possible.
        if rearm:
            info = self.docker.inspect()
            if info['HostConfig']['RestartPolicy']['Name'] != 'no':
                self.docker.set_restart_no()
            self.docker.stop()
        if self.state.exists():
            backup = self.directory / ('state-backup-' + str(time.time_ns()) + '.json')
            backup.write_bytes(self.state.read_bytes())
        if self.latch.exists():
            backup = self.directory / ('fault-backup-' + str(time.time_ns()) + '.json')
            backup.write_bytes(self.latch.read_bytes())
        if not rearm:
            atomic_json(self.passive_marker, {'waiting': True})
        else:
            self.passive_marker.unlink(missing_ok=True)
        new = initial(self.cfg, self.reader(self.cfg), passive=not rearm)
        if saved_settings is not None:
            new['settings'] = saved_settings
        atomic_json(self.state, new)
        self.latch.unlink(missing_ok=True)
        self.health.unlink(missing_ok=True)
        self.failed = False
        log('armed', mode='waiting', passive=not rearm,
            message='Traffic before installation is unknown; waiting for next UTC midnight')

    def shutdown(self):
        if self.is_passive():
            self.health.unlink(missing_ok=True)
            return
        s = None
        try:
            if not self.failed and not self.latch.exists():
                s = self.load()
                if s['mode'] == 'active' and self.docker.inspect()['State']['Running']:
                    s['resume_pending'] = True
                    s['node_was_running'] = True
                    atomic_json(self.state, s)
        finally:
            try:
                self.docker.stop()
            finally:
                self.health.unlink(missing_ok=True)

        if s is not None:
            try:
                # Stop has finished: include traffic since the last poll in a
                # final durable checkpoint before graceful host shutdown.
                new = advance(s, self.reader(self.cfg), self.cfg, False)
                atomic_json(self.state, new)
                self.record_history(s, new)
            except Exception as error:
                self.emergency(error)
                raise

    def process_commands(self):
        directory = self.directory / 'commands'
        if not directory.is_dir():
            return
        for path in sorted(directory.glob('*.request.json'))[:10]:
            request_id = path.name.split('.')[0]
            if not re.fullmatch(r'[0-9a-f]{32}', request_id):
                path.unlink(missing_ok=True)
                continue
            result = dict(id=request_id, at=time.time(), ok=False)
            try:
                req = json.loads(path.read_text())
                if not 0 <= time.time() - req['at'] < 60:
                    raise ValueError('Command expired')
                action = req['action']
                if action == 'pause':
                    # Explicit user request, also available during passive waiting.
                    self.docker.stop()
                    s = self.load()
                    s.update(manual_paused=True, resume_pending=False)
                    atomic_json(self.state, s)
                elif action == 'resume':
                    try:
                        self.docker.inspect()
                    except TargetMissing:
                        raise ValueError('Target container does not exist')
                    self.tick()  # Refresh the accounting before making a decision.
                    s = self.load()
                    policy = effective_settings(s)
                    if s['mode'] != 'active' or (policy['stop_on_limit'] and s['used'] >= policy['limit_bytes']):
                        raise ValueError('Start forbidden: no trusted remaining quota')
                    s.update(manual_paused=False, resume_pending=True)
                    atomic_json(self.state, s)
                    self.tick()
                elif action == 'rearm':
                    self.arm(rearm=True)
                    self.tick()
                elif action == 'settings':
                    settings = req.get('settings')
                    validate_settings(settings)
                    if self.failed or self.latch.exists():
                        raise ValueError('Resolve the accounting fault before changing settings')
                    self.tick()  # Keep all consumed bytes when editing a limit.
                    s = self.load()
                    s['settings'] = settings
                    atomic_json(self.state, s)
                    self.tick()  # Apply the new policy immediately, without restart.
                else:
                    raise ValueError('Unknown action')
                result['ok'] = True
                log('user_command', action=action)
                try:
                    from history import History
                    if self.history is None:
                        self.history = History(self.directory / 'history.sqlite3')
                    self.history.event(self.load(), 'user_' + action)
                except Exception as error:
                    log('telemetry_error', error=str(error))
            except Exception as error:
                result['error'] = str(error)
                # Rejected user commands do not themselves invalidate accounting.
                if not isinstance(error, ValueError):
                    self.emergency(error)
            finally:
                atomic_json(directory / (request_id + '.result.json'), result)
                path.unlink(missing_ok=True)
        for path in directory.glob('*.result.json'):
            if time.time() - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'init', 'rearm', 'status', 'health'))
    args = parser.parse_args()
    os.umask(0o027)
    directory = Path(os.getenv('STATE_DIR', '/data'))
    if args.command == 'health':
        try:
            h = json.loads((directory / 'health.json').read_text())
            age = time.time() - h['at']
            return 0 if h['ok'] and 0 <= age < 30 and not (directory / 'fault.json').exists() else 1
        except Exception:
            return 1
    cfg = config()
    controller = Controller(cfg, directory, Docker(cfg['target']))
    if args.command == 'status':
        for name in ('state.json', 'fault.json', 'health.json'):
            path = directory / name
            if path.exists():
                print(f'{name}:\n{path.read_text()}')
        return 0
    import fcntl
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'guard.lock').open('a') as lock:
        # A second controller must exit without stopping the first one's target.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command in ('init', 'rearm'):
            controller.arm(rearm=args.command == 'rearm')
            return 0
        stopping = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopping.set())
        log('started', **cfg)
        try:
            while not stopping.is_set():
                try:
                    controller.tick()
                except Exception as error:
                    controller.emergency(error)
                try:
                    controller.process_commands()
                except Exception as error:
                    log('command_queue_error', error=str(error))
                stopping.wait(3)
        finally:
            # Intentional shutdown removes protection, so stop the target too.
            controller.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
