import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import guard
from resources import Sampler, Store, read_samples, start_collector, RETENTION
from test_guard import CFG, FakeDocker, state, next_sample


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'boot_id').write_text('boot-one')
        self.write_cpu('100 0 50 800 50 0 0 0 20 0')
        (self.root/'meminfo').write_text('MemTotal: 2048 kB\nMemAvailable: 1024 kB\nCached: 800 kB\n')

    def write_cpu(self, values):
        (self.root/'stat').write_text('cpu '+values+'\ncpu0 999999\n')

    def test_cpu_uses_deltas_and_does_not_count_guest_twice(self):
        reader=Sampler(self.root)
        first=reader.read()
        self.assertIsNone(first[1])
        self.write_cpu('125 0 75 850 50 0 0 0 1000 0')
        self.assertEqual(reader.read()[1],50)

    def test_iowait_is_not_busy_cpu(self):
        reader=Sampler(self.root);reader.read()
        self.write_cpu('100 0 50 850 100 0 0 0')
        self.assertEqual(reader.read()[1],0)

    def test_reset_or_reboot_does_not_create_fake_cpu_spike(self):
        reader=Sampler(self.root);reader.read()
        self.write_cpu('1 0 1 1 0 0 0 0')
        self.assertIsNone(reader.read()[1])
        (self.root/'boot_id').write_text('boot-two')
        self.write_cpu('100 0 50 800 50 0 0 0')
        self.assertIsNone(reader.read()[1])

    def test_memory_uses_available_instead_of_free(self):
        sample=Sampler(self.root).read()
        self.assertEqual(sample[2:], [1024*1024,2048*1024])

    def test_missing_memory_is_not_reported_as_zero_usage(self):
        (self.root/'meminfo').write_text('MemTotal: 2048 kB\n')
        with self.assertRaises(ValueError):Sampler(self.root).read()

    def test_retention_incremental_reads_and_survival_after_reopen(self):
        path=self.root/'resources.sqlite3';store=Store(path)
        now=time.time()
        for age in (RETENTION+1,90000,100,30,0):store.record([now-age,50,100,200])
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0],4)
        store.db.close()
        samples=read_samples(path,now)
        self.assertEqual(len(samples),3)
        self.assertEqual(read_samples(path,now,samples[-2][0]),[samples[-1]])
        self.assertEqual(read_samples(path,now,now+1),[])
        self.assertEqual(read_samples(self.root/'absent.sqlite3',now),[])

    def test_slow_or_failed_metrics_do_not_block_or_fault_quota(self):
        entered=threading.Event();release=threading.Event();stop=threading.Event()
        class BrokenSampler:
            def read(self):
                entered.set();release.wait(3);raise OSError('metrics unavailable')
        c=guard.Controller(CFG,self.root,FakeDocker(),lambda _:next_sample(c.load()))
        guard.atomic_json(c.state,state())
        with patch('builtins.print'):
            thread=start_collector(self.root,stop,BrokenSampler())
            try:
                self.assertTrue(entered.wait(1))
                c.tick()
                self.assertEqual(c.load()['used'],110)
                self.assertFalse(c.latch.exists())
                self.assertTrue(json.loads(c.health.read_text())['ok'])
            finally:
                stop.set();release.set();thread.join(timeout=2)
                if c.history:c.history.db.close()
        self.assertFalse(thread.is_alive())


if __name__=='__main__':
    unittest.main()
