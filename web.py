#!/usr/bin/env python3
"""HTTP control panel: no Docker access, authenticated bounded IPC commands."""
import base64
import csv
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit

from history import read_history


def password_record(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12 to 256 characters')
    salt = secrets.token_bytes(24)
    rounds = 600000
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, rounds)
    return dict(salt=base64.b64encode(salt).decode(), rounds=rounds,
                digest=base64.b64encode(digest).decode())


def password_matches(password, record):
    if not isinstance(password, str) or len(password) > 256:
        return False
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(),
                               base64.b64decode(record['salt']), record['rounds'])
    return hmac.compare_digest(digest, base64.b64decode(record['digest']))


def write_request(directory, action):
    request_id = secrets.token_hex(16)
    path = directory / (request_id + '.request.json')
    temp = directory / (request_id + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, 'w') as file:
        json.dump(dict(action=action, at=time.time()), file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    return request_id


class Panel(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, prefix, auth, data, commands, assets):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{20,96}', prefix):
            raise ValueError('WEB_PATH must contain 20-96 URL-safe characters')
        self.prefix = '/' + prefix
        self.auth, self.data, self.commands, self.assets = auth, data, commands, assets
        self.sessions = {}
        self.attempts = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def session(self, token):
        now = time.time()
        with self.lock:
            for key in list(self.sessions):
                item = self.sessions[key]
                if now - item['last'] > 1800 or now - item['created'] > 43200:
                    del self.sessions[key]
            item = self.sessions.get(token)
            if item:
                item['last'] = now
            return dict(item) if item else None

    def allow_login(self, ip):
        now = time.time()
        with self.lock:
            self.attempts = {key: value for key, value in self.attempts.items() if now - value[0] < 900}
            if ip not in self.attempts and len(self.attempts) >= 4096:
                return False
            since, count = self.attempts.get(ip, (now, 0))
            if count >= 8:
                return False
            self.attempts[ip] = (since, count + 1)
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = ''
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *_):
        # Never put the hidden path, password or session in access logs.
        pass

    def respond(self, status, body, content_type='application/json; charset=utf-8', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response_only(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def not_found(self):
        self.respond(404, 'Not found\n', 'text/plain; charset=utf-8')

    def route(self):
        path = urlsplit(self.path).path
        if path == self.server.prefix:
            return '/'
        if path.startswith(self.server.prefix + '/'):
            return path[len(self.server.prefix):]
        return None

    def token(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            return cookies['guard_session'].value if 'guard_session' in cookies else ''
        except Exception:
            return ''

    def origin_ok(self):
        # CSRF headers also require an exact same-origin request. No CORS enabled.
        host = self.headers.get('Host', '')
        if not re.fullmatch(r'[a-zA-Z0-9.\[\]:-]{1,255}', host):
            return False
        return self.headers.get('Origin') == 'http://' + host

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = self.route()
        if route is None:
            self.not_found()
            return
        session = self.server.session(self.token())
        assets = {'/': ('index.html' if session else 'login.html', 'text/html; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8'),
                  '/login.js': ('login.js', 'text/javascript; charset=utf-8')}
        if session:
            assets['/app.js'] = ('app.js', 'text/javascript; charset=utf-8')
        if route in assets:
            name, mime = assets[route]
            # Rewrite relative asset URLs also for a URL without trailing slash.
            body = (self.server.assets / name).read_bytes()
            if name.endswith('.html'):
                body = body.replace(b'__BASE__', (self.server.prefix + '/').encode())
            self.respond(200, body, mime)
            return
        if not session:
            self.respond(401, {'error': 'Войдите в панель'})
            return
        try:
            if route == '/api/status':
                result = dict(csrf=session['csrf'])
                for name, key in [('snapshot.json', 'snapshot'), ('health.json', 'health'), ('fault.json', 'fault')]:
                    try:
                        result[key] = json.loads((self.server.data / name).read_text())
                    except FileNotFoundError:
                        result[key] = None
                    except (ValueError, OSError):
                        result[key] = {'error': 'Повреждён файл аварийного состояния'} if key == 'fault' else None
                if result['snapshot'] is None:
                    try:
                        saved = json.loads((self.server.data / 'state.json').read_text())
                        result['snapshot'] = dict(at=saved['sample']['wall'], state=saved, node_running=None)
                    except (ValueError, OSError, KeyError):
                        pass
                self.respond(200, result)
            elif route in ('/api/history', '/api/export'):
                result = read_history(self.server.data / 'history.sqlite3', time.time())
                if route == '/api/export':
                    output = io.StringIO()
                    writer = csv.DictWriter(output, fieldnames=['day', 'used', 'rx', 'tx', 'complete', 'updated'])
                    writer.writeheader()
                    writer.writerows(result['days'])
                    self.respond(200, output.getvalue(), 'text/csv; charset=utf-8',
                                 {'Content-Disposition': 'attachment; filename="traffic-history.csv"'})
                else:
                    self.respond(200, result)
            elif re.fullmatch(r'/api/commands/[0-9a-f]{32}', route):
                path = self.server.commands / (route.rsplit('/', 1)[1] + '.result.json')
                self.respond(200, json.loads(path.read_text()) if path.exists() else {'pending': True})
            else:
                self.not_found()
        except Exception:
            self.respond(503, {'error': 'Данные временно недоступны. Проверьте журнал guard.'})

    def do_POST(self):
        route = self.route()
        if route is None:
            self.not_found()
            return
        if not self.origin_ok():
            self.respond(403, {'error': 'Запрос должен исходить из этой панели'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError()
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError()
        except Exception:
            self.respond(400, {'error': 'Некорректный запрос'})
            return
        if route == '/api/login':
            ip = self.client_address[0]  # Ignore attacker-controlled forwarded headers.
            if not self.server.allow_login(ip):
                self.respond(429, {'error': 'Слишком много попыток. Повторите через 15 минут.'})
                return
            if not password_matches(request.get('password'), self.server.auth):
                self.respond(401, {'error': 'Неверный пароль'})
                return
            token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
            with self.server.lock:
                if len(self.server.sessions) >= 128:
                    self.server.sessions.clear()
                self.server.sessions[token] = dict(csrf=csrf, created=now, last=now, command_at=0)
                self.server.attempts.pop(ip, None)
            cookie = f'guard_session={token}; Path={self.server.prefix}; HttpOnly; SameSite=Strict; Max-Age=43200'
            self.respond(200, {'ok': True}, headers={'Set-Cookie': cookie})
            return
        token = self.token()
        session = self.server.session(token)
        if not session:
            self.respond(401, {'error': 'Войдите в панель'})
            return
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            self.respond(403, {'error': 'Обновите страницу и повторите действие'})
            return
        if route == '/api/logout':
            with self.server.lock:
                self.server.sessions.pop(token, None)
            self.respond(200, {'ok': True}, headers={'Set-Cookie': f'guard_session=; Path={self.server.prefix}; HttpOnly; SameSite=Strict; Max-Age=0'})
            return
        if route != '/api/action' or request.get('action') not in ('pause', 'resume', 'rearm'):
            self.not_found()
            return
        if request['action'] == 'rearm' and request.get('confirm') != 'rearm':
            self.respond(400, {'error': 'Подтвердите новый период ожидания'})
            return
        try:
            with self.server.lock:
                now = time.time()
                if now - self.server.sessions[token]['command_at'] < .5:
                    self.respond(429, {'error': 'Подождите завершения предыдущего действия'})
                    return
                self.server.sessions[token]['command_at'] = now
            health = json.loads((self.server.data / 'health.json').read_text())
            if not 0 <= time.time() - health['at'] < 30:
                raise RuntimeError('Controller is not running')
            if len(list(self.server.commands.glob('*.request.json'))) >= 20:
                raise RuntimeError('Command queue is full')
            request_id = write_request(self.server.commands, request['action'])
            self.respond(202, {'id': request_id})
        except Exception:
            self.respond(503, {'error': 'Контроллер недоступен. Проверьте Docker и журнал guard.'})

    def do_OPTIONS(self):
        self.not_found()

    def do_PUT(self):
        self.not_found()

    def do_DELETE(self):
        self.not_found()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'hash-password':
        password = sys.stdin.readline().rstrip('\n')
        print(json.dumps(password_record(password)))
        return
    os.umask(0o027)
    auth = json.loads(Path('/run/secrets/web-auth.json').read_text())
    server = Panel(('0.0.0.0', 8080), os.environ['WEB_PATH'], auth,
                   Path('/data'), Path('/commands'), Path('/app/static'))
    print('Control panel listening on port 8080; credentials and path omitted from logs', flush=True)
    server.serve_forever(poll_interval=.5)


if __name__ == '__main__':
    main()
