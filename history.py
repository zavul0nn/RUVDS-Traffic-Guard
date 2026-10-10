"""Best-effort telemetry. Quota decisions always use the durable JSON state."""
from pathlib import Path
import sqlite3


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS days (
                day TEXT PRIMARY KEY, used INTEGER NOT NULL, rx INTEGER NOT NULL,
                tx INTEGER NOT NULL, complete INTEGER NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (
                at INTEGER PRIMARY KEY, rx REAL NOT NULL, tx REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, mode TEXT NOT NULL,
                reason TEXT, used INTEGER NOT NULL);
        ''')
        self.last_bucket = None

    def record(self, old, new):
        o, n = old['sample'], new['sample']
        elapsed = n['uptime'] - o['uptime']
        continuous = (n['boot'] == o['boot'] and n['ifindex'] == o['ifindex']
                      and all(c >= p for c, p in zip(n['counters'], o['counters'])))
        rebooted = n['boot'] != o['boot'] and (new['mode'] != 'waiting' or
                                              not new.get('passive_wait', False))
        if rebooted:
            drx, dtx = n['counters']
        elif continuous:
            drx, dtx = [c - p for c, p in zip(n['counters'], o['counters'])]
        else:
            drx = dtx = 0
        if new['mode'] != 'waiting' and old['sample']['day'] == n['day']:
            # Reconcile samples after a crash between JSON and telemetry commit.
            row = self.db.execute('SELECT rx,tx FROM days WHERE day=?', (n['day'],)).fetchone()
            if row and sum(row) + drx + dtx > new['used']:
                drx = dtx = 0
        complete = int(new['mode'] != 'waiting' and new.get('accounting_complete', True))
        bucket = int(n['wall']) // 30
        with self.db:
            self.db.execute('''INSERT INTO days VALUES (?,?,?,?,?,?)
                ON CONFLICT(day) DO UPDATE SET used=MAX(days.used,excluded.used,days.rx+days.tx+excluded.rx+excluded.tx),
                rx=days.rx+excluded.rx, tx=days.tx+excluded.tx,
                complete=MIN(days.complete, excluded.complete), updated=excluded.updated''',
                (n['day'], new['used'], drx, dtx, complete, n['wall']))
            if bucket != self.last_bucket:
                self.db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?)',
                                (int(n['wall']), drx / elapsed if continuous and elapsed > 0 else 0,
                                 dtx / elapsed if continuous and elapsed > 0 else 0))
                self.db.execute('DELETE FROM samples WHERE at < ?', (n['wall'] - 30 * 86400,))
                self.last_bucket = bucket
            if rebooted or (old['mode'], old['reason'], o['day']) != (new['mode'], new['reason'], n['day']):
                self.db.execute('INSERT INTO events(at,mode,reason,used) VALUES (?,?,?,?)',
                                (n['wall'], new['mode'], 'host_reboot' if rebooted else new['reason'], new['used']))
                self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 10000)')

    def event(self, state, reason):
        with self.db:
            self.db.execute('INSERT INTO events(at,mode,reason,used) VALUES (?,?,?,?)',
                            (state['sample']['wall'], state['mode'], reason, state['used']))


def read_history(path, now):
    if not Path(path).exists():
        return dict(days=[], samples=[], events=[])
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        return dict(days=[dict(r) for r in db.execute('SELECT * FROM days ORDER BY day DESC')],
                    samples=[dict(r) for r in db.execute('SELECT * FROM samples WHERE at>=? ORDER BY at', (now - 86400,))],
                    events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')])
    finally:
        db.close()
