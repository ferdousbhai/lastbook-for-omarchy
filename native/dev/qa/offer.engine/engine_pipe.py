"""A stand-in engine for native/dev/qa/offer.jsonl: the real engine over the made-up library, on a computer whose
weather location in Omarchy is Testville (dev/seed.py), which has no street map, and which is taken to be online
(engine/setup/here.py). The download it starts is made up: nothing reaches the network.

Never packaged."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

weather = Path.home() / '.local/state/omarchy/settings/weather.json'
weather.parent.mkdir(parents=True, exist_ok=True)
weather.write_text(json.dumps({'name': 'Testville', 'latitude': 12.5, 'longitude': 34.25}))
os.environ['LASTBOOK_MAP_OFFER'] = '1'
standin.use_real_engine()
from engine.setup import api  # noqa: E402

job = None


def start(s, body):
    """The job the real engine would start for the region asked for, downloading."""
    global job
    rid = body['regions'][0]
    item = {'id': rid, 'kind': 'osm', 'title': f'{rid.title()} streets', 'state': 'downloading', 'source': 'download',
            'bytes_done': 40_000_000, 'bytes_total': 120_000_000, 'rate_bps': 2_000_000, 'eta_s': 40, 'error': None}
    job = {'id': 'made-up', 'state': 'downloading', 'error': None, 'folder': os.environ['LASTBOOK_DATA'], 'items': [item]}
    if s.on_job:
        s.on_job('downloading')
    return job


def pause(s, body):
    job['state'] = job['items'][0]['state'] = 'paused'
    if s.on_job:
        s.on_job('paused')
    return job


api.ROUTES.update({('POST', 'start'): start, ('GET', 'job'): lambda s, b: job, ('POST', 'pause'): pause})
standin.run_real_engine()
