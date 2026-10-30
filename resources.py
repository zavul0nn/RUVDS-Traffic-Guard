"""Low-frequency host metrics, independent of quota accounting and its database."""
import json
from pathlib import Path
import sqlite3
import threading
import time

INTERVAL = 30
RETENTION = 7 * 86400


class Sampler:
    def __init__(self, root=Path('/host')):
        self.root = Path(root)
        self.previous = None

    def read(self):
        with (self.root / 'stat').open() as file:
            fields = file.readline().split()
        if fields[0] != 'cpu' or len(fields) < 9:
            raise ValueError('Invalid aggregate CPU counters')
        counters = tuple(int(value) for value in fields[1:9])  # guest already included in user/nice
        boot = (self.root / 'boot_id').read_text().strip()
        if min(counters) < 0 or not boot:
            raise ValueError('Invalid CPU sample')
        cpu = None
        if self.previous and self.previous[0] == boot:
            old = self.previous[1]
            total = sum(counters) - sum(old)
            idle = counters[3] + counters[4] - old[3] - old[4]
            if total > 0 and all(c >= p for c, p in zip(counters, old)) and 0 <= idle <= total:
                cpu = round(100 * (total - idle) / total, 2)
        self.previous = (boot, counters)
        memory = {}
        for line in (self.root / 'meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.split()[0]) * 1024
        total = memory['MemTotal']
        if total <= 0 or 'MemAvailable' not in memory:
            raise ValueError('Host MemAvailable is required')
        available = max(0, min(total, memory['MemAvailable']))
        return [time.time(), cpu, total - available, total]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=.2)
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS resources (at REAL PRIMARY KEY, cpu REAL, mem_used INTEGER NOT NULL, mem_total INTEGER NOT NULL)')
        self.db.commit()

    def record(self, sample):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO resources VALUES (?,?,?,?)', sample)
            self.db.execute('DELETE FROM resources WHERE at < ?', (sample[0] - RETENTION,))


def read_samples(path, now, since=0):
    if not Path(path).exists():
        return []
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
    try:
        return [list(row) for row in db.execute(
            'SELECT at,cpu,mem_used,mem_total FROM resources WHERE at>? AND at<=? ORDER BY at LIMIT 3000',
            (max(now - 86400, since), now))]
    finally:
        db.close()


def start_collector(directory, stopping, sampler=None):
    def collect():
        store = None
        reader = sampler or Sampler()
        try:
            while not stopping.is_set():
                try:
                    sample = reader.read()
                    if store is None:
                        store = Store(directory / 'resources.sqlite3')
                    store.record(sample)
                except Exception as error:
                    # A broken /proc mount or full telemetry disk must never stop a service.
                    print(json.dumps({'event':'resource_metrics_error', 'error':str(error)}), flush=True)
                    if store is not None:
                        store.db.close()
                        store = None
                stopping.wait(INTERVAL)
        finally:
            if store is not None:
                store.db.close()
    thread = threading.Thread(target=collect, name='host-metrics', daemon=True)
    thread.start()
    return thread
