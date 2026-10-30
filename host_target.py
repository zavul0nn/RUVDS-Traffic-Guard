#!/usr/bin/env python3
"""Root-only Unix bridge restricted to the host's haproxy.service."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile

UNIT = 'haproxy.service'
SOCKET = Path('/run/traffic-guard-target/control.sock')
DROPIN = Path('/etc/systemd/system/haproxy.service.d/zzzz-traffic-guard.conf')
BACKUP = Path('/var/lib/remnanode-traffic-guard/haproxy-policy-before.json')
POLICY = '[Service]\nRestart=no\n'


def install_policy():
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.traffic-guard-', dir=DROPIN.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(POLICY)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, DROPIN)
        legacy = DROPIN.with_name('90-traffic-guard.conf')
        if legacy.exists() and legacy != DROPIN and legacy.read_text() == POLICY:
            legacy.unlink()  # Only remove the exact file owned by the old guard.
        fd = os.open(DROPIN.parent, os.O_RDONLY | os.O_DIRECTORY) if os.name == 'posix' else None
        if fd is not None:
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def systemctl(*args):
    result = subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:500] or 'systemctl failed')
    return result.stdout


def inspect():
    props = dict(line.split('=', 1) for line in systemctl(
        'show', UNIT, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Restart', '-p', 'UnitFileState'
    ).splitlines() if '=' in line)
    if props.get('LoadState') == 'not-found':
        return {'missing': True}
    if props.get('LoadState') != 'loaded':
        raise RuntimeError('HAProxy unit is not loaded: ' + props.get('LoadState', 'unknown'))
    enabled = props.get('UnitFileState') in ('enabled', 'enabled-runtime', 'alias')
    policy = 'no' if props.get('Restart') == 'no' and not enabled else 'always'
    return {'State': {'Running': props.get('ActiveState') in ('active', 'activating', 'reloading', 'deactivating')},
            'HostConfig': {'RestartPolicy': {'Name': policy}}, 'systemd': props}


def execute(action):
    if action not in ('inspect', 'stop', 'start', 'set_restart_no'):
        raise ValueError('Unsupported operation')
    before = inspect()
    if before.get('missing'):
        return before
    if action == 'set_restart_no':
        if not BACKUP.exists():
            BACKUP.parent.mkdir(parents=True, exist_ok=True)
            with BACKUP.open('x') as file:
                json.dump(before['systemd'], file, indent=2)
                file.flush()
                os.fsync(file.fileno())
        install_policy()
        systemctl('disable', UNIT)
        systemctl('daemon-reload')
    elif action in ('stop', 'start'):
        if action == 'start' and before['HostConfig']['RestartPolicy']['Name'] != 'no':
            raise RuntimeError('HAProxy must be managed by guard before start')
        systemctl(action, UNIT)
    after = inspect()
    if action == 'stop' and after.get('State', {}).get('Running'):
        raise RuntimeError('HAProxy is still running')
    if action == 'start' and not after.get('State', {}).get('Running'):
        raise RuntimeError('HAProxy did not stay running')
    if action == 'set_restart_no' and after.get('HostConfig', {}).get('RestartPolicy', {}).get('Name') != 'no':
        raise RuntimeError('HAProxy restart policy could not be disabled')
    return after


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            request = json.loads(self.rfile.readline(1025))
            if not isinstance(request, dict) or set(request) != {'action'}:
                raise ValueError('Invalid request')
            response = {'ok': True, 'result': execute(request['action'])}
        except Exception as error:
            response = {'ok': False, 'error': str(error)}
        self.wfile.write(json.dumps(response).encode() + b'\n')


if __name__ == '__main__':
    os.umask(0o077)
    SOCKET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o600)
        server.serve_forever()
