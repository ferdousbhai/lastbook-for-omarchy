"""A stand-in engine for native/dev/qa/map-download.jsonl: the real engine over the made-up library, with setup's
download made up, so the Map screen's own download of a region's streets runs without the network. The first start
finds the disk too full; the next one runs until paused; resumed, it loses the network; tried again, it is done two
seconds later, and the streets around Testville (dev/seed.py) are then in the library, help points and all. Each change
of state is told as the real engine tells it.

Never packaged."""
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

standin.use_real_engine(standin.REPO / 'native' / 'dev')
import seed  # noqa: E402
from engine.setup import api  # noqa: E402

GEO = Path(os.environ['LASTBOOK_DATA']) / 'derived' / 'geo'
TESTVILLE = (12.5, 34.25)
starts = 0
job = None


def item(state, done, error=None):
    return {'id': 'sudan', 'kind': 'osm', 'title': 'Sudan streets', 'state': state, 'source': 'download', 'bytes_done': done,
            'bytes_total': 120_000_000, 'rate_bps': 2_000_000 if state == 'downloading' else 0,
            'eta_s': 40 if state == 'downloading' else None, 'error': error}


def become(s, state, done=40_000_000, error=None):
    global job
    job = {'id': 'made-up', 'state': state, 'error': error, 'folder': os.environ['LASTBOOK_DATA'], 'items': [item(state, done, error)]}
    if s.on_job:
        s.on_job(state)
    return job


def start(s, body):
    global starts
    starts += 1
    if starts == 1:
        return {'error': 'This needs 3.1 GB free on that disk, and it has 1.2 GB. Free some space or choose another disk.',
                'code': 'no_space', 'need_bytes': 3_100_000_000, 'free_bytes': 1_200_000_000}
    return become(s, 'downloading')


def resume(s, body):
    lost = {'code': 'network', 'message': 'Lastbook could not reach the map server to find the newest map. Try again later.'}
    threading.Timer(1.5, become, [s, 'failed', 40_000_000, lost]).start()
    return become(s, 'downloading')


def retry(s, body):
    def done():
        lat, lon = TESTVILLE
        part = GEO / 'regions-sudan.mbtiles.part'
        seed.street_map(part, 'Sudan', lat, lon, [('hospital', 'RS Testville', lat + 0.003, lon), ('pharmacy', 'Apotek Testville', lat, lon + 0.002)])
        part.rename(GEO / 'regions-sudan.mbtiles')
        become(s, 'done', 120_000_000)
    threading.Timer(2, done).start()
    return become(s, 'downloading')


api.ROUTES.update({('POST', 'start'): start, ('GET', 'job'): lambda s, b: job, ('POST', 'pause'): lambda s, b: become(s, 'paused'),
                   ('POST', 'resume'): resume, ('POST', 'retry'): retry})
standin.run_real_engine()
