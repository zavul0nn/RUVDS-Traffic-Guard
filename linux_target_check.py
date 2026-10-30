"""Root-only Linux integration test using an isolated sleep service, never HAProxy."""
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading

import host_target


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run as root on a systemd Linux host')
    os.umask(0o077)
    unit = 'traffic-guard-selftest.service'
    unit_path = Path('/run/systemd/system') / unit
    dropin = unit_path.with_name(unit + '.d') / host_target.DROPIN.name
    override = dropin.with_name('override.conf')
    legacy = dropin.with_name('90-traffic-guard.conf')
    if unit_path.exists() or dropin.parent.exists() or Path('/etc/systemd/system', unit).exists():
        raise RuntimeError('Self-test unit already exists; refusing to overwrite it')
    with tempfile.TemporaryDirectory(prefix='traffic-guard-check-', dir='/run') as directory:
        host_target.UNIT = unit
        host_target.DROPIN = dropin
        host_target.BACKUP = Path(directory) / 'before.json'
        try:
            unit_path.write_text('[Unit]\nDescription=Temporary Traffic Guard self-test\n'
                                 '[Service]\nExecStart=/usr/bin/sleep 600\nRestart=always\n'
                                 '[Install]\nWantedBy=multi-user.target\n')
            dropin.parent.mkdir()
            override.write_text('[Service]\nRestart=always\nLimitNOFILE=65536\n')
            legacy.write_text(host_target.POLICY)
            host_target.systemctl('daemon-reload')
            host_target.systemctl('enable', '--now', unit)
            pid = host_target.systemctl('show', unit, '-p', 'MainPID', '--value').strip()
            address = str(Path(directory) / 'control.sock')
            with socketserver.UnixStreamServer(address, host_target.Handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def request(action):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                        conn.settimeout(30)
                        conn.connect(address)
                        conn.sendall(json.dumps({'action':action}).encode() + b'\n')
                        with conn.makefile('rb') as file:
                            return json.loads(file.readline())
                try:
                    assert request('inspect')['result']['State']['Running']
                    assert not request('restart')['ok']
                    assert request('set_restart_no')['result']['HostConfig']['RestartPolicy']['Name'] == 'no'
                    assert override.read_text() == '[Service]\nRestart=always\nLimitNOFILE=65536\n'
                    assert not legacy.exists()
                    assert host_target.systemctl('show', unit, '-p', 'MainPID', '--value').strip() == pid
                    assert not request('stop')['result']['State']['Running']
                    assert request('start')['result']['State']['Running']
                    assert host_target.BACKUP.exists()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
        finally:
            host_target.systemctl('disable', '--now', unit)
            dropin.unlink(missing_ok=True)
            override.unlink(missing_ok=True)
            legacy.unlink(missing_ok=True)
            if dropin.parent.exists():
                dropin.parent.rmdir()
            unit_path.unlink(missing_ok=True)
            host_target.systemctl('daemon-reload')
    print('Linux target QA passed: conflicting override preserved, legacy policy migrated, Unix socket, policy claim without restart, real systemd stop/start; isolated unit removed.')


if __name__ == '__main__':
    main()
