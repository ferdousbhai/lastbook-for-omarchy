"""A stand-in engine for native/dev/qa/download.jsonl: first-run setup with a download already running. Nothing is
downloaded: the job is made up, and it changes only as the window asks (pause, resume, cancel, start), each change told
with a {"job": state} line as the real engine tells it. A job started anew finishes by itself three seconds later, which
is how the script sees a window put away for a download come back.

Never packaged."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402
from standin import answer, send  # noqa: E402

job = {'id': 'made-up', 'state': 'downloading', 'error': None, 'folder': '/made-up/LastbookData',
       'items': [{'id': 'small', 'kind': 'pack', 'title': 'Small', 'state': 'downloading', 'source': 'download',
                  'bytes_done': 2_000_000_000, 'bytes_total': 8_000_000_000, 'rate_bps': 5_000_000, 'eta_s': 1200, 'error': None}]}


def become(state, error=None):
    job['state'], job['error'] = state, error
    job['items'][0].update(state=state, rate_bps=0, eta_s=None, error=None)
    if state == 'done':
        job['items'][0]['bytes_done'] = job['items'][0]['bytes_total']
    send({'job': state})


def state():
    return {'mode': 'first-run', 'hardware': {'disks': []}, 'current': None, 'found': [], 'job': job, 'map_regions': [],
            'world_bytes': 650_000_000,
            'manifest': {'source': 'bundled', 'packs': [{'id': 'small', 'title': 'Small', 'summary': 'A made-up library.',
                                                         'installed_bytes': 8_000_000_000, 'available': True}]},
            'recommend': {'pack': 'small', 'why': '', 'folder': '/made-up/LastbookData', 'need_bytes': {'small': 17_000_000_000}}}


send({'ready': {'setup': True, 'data': None, 'library': '', 'version': 'stand-in'}})
for req in standin.requests():
    name = req['path'].rsplit('/', 1)[-1]
    if name == 'pause':
        become('paused')
    elif name == 'resume':
        become('downloading')
    elif name == 'start':
        become('downloading')
        threading.Timer(3, become, ['done']).start()
    elif name == 'cancel':
        become('failed', {'code': 'cancelled', 'message': 'Cancelled. Nothing was changed.'})
    answer(req['id'], state() if name == 'state' else job)
