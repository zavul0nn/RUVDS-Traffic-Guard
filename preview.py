"""Local UI preview with synthetic traffic and a fake node. Never uses Docker."""
import argparse
import datetime as dt
from pathlib import Path
import math
import tempfile
import threading
import time

import guard
from history import History
from test_guard import FakeDocker
import web


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8789)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix='traffic-guard-preview-')
    directory = Path(temporary.name)
    (directory / 'commands').mkdir()
    cfg = dict(target='remnanode', interface='eth0', limit=145_000_000_000)
    began = time.time()
    def reader(_):
        now = time.time()
        return dict(wall=now, day=guard.utc_day(now), uptime=1000 + now - began,
                    counters=[1000 + int((now - began) * 8e6), 1000 + int((now - began) * 2e6)],
                    boot='synthetic-preview', ifindex=2)
    s = guard.initial(cfg, reader(cfg))
    s.update(mode='active', reason=None, used=37_160_000_000, passive_wait=False,
             resume_pending=False, policy_initialized=True)
    guard.atomic_json(directory / 'state.json', s)
    history = History(directory / 'history.sqlite3')
    for back, used in ((0,37.16),(1,118.44),(2,94.83),(3,132.11),(4,107.55)):
        day=(dt.datetime.now(dt.timezone.utc).date()-dt.timedelta(days=back)).isoformat()
        history.db.execute('INSERT INTO days VALUES (?,?,?,?,?,?)',
                           (day,int(used*1e9),int(used*.32*1e9),int(used*.68*1e9),1,began))
    for step in range(1440):
        stamp=began-86400+step*60
        wave=abs(math.sin(step*.008)+.4*math.cos(step*.031))
        history.db.execute('INSERT INTO samples VALUES (?,?,?)',(int(stamp),wave*6e6+5e5,wave*3e6+3e5))
    history.db.commit()
    history.db.close()
    stopped=threading.Event()
    def controller_loop():
        controller=guard.Controller(cfg,directory,FakeDocker(),reader)
        try:
            while not stopped.is_set():
                try:
                    controller.tick()
                    controller.process_commands()
                except Exception as error:
                    controller.emergency(error)
                stopped.wait(.5)
        finally:
            if controller.history:
                controller.history.db.close()
    thread=threading.Thread(target=controller_loop,daemon=True)
    thread.start()
    path='preview-hidden-path-0123456789'
    panel=web.Panel(('127.0.0.1',args.port),path,web.password_record('preview-password-123'),
                    directory,directory/'commands',Path(__file__).parent/'static')
    print(f'Preview: http://127.0.0.1:{args.port}/{path}',flush=True)
    print('Demo password: preview-password-123 (synthetic data, loopback only)',flush=True)
    try:
        panel.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        panel.server_close()
        stopped.set()
        thread.join(timeout=3)
        temporary.cleanup()


if __name__=='__main__':
    main()
