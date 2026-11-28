"""What the QA scripts' stand-in engines (<script>.engine/engine_pipe.py, see native/dev/qa.sh) share: the engine's
side of the pipe (docs/ENGINE.md), and the real engine run with some of its answers made up.

Never packaged."""
import json
import os
import runpy
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_lock = threading.Lock()


def send(obj):
    """One line to the window; a timer's thread may send one too."""
    with _lock:
        sys.stdout.write(json.dumps(obj) + '\n')
        sys.stdout.flush()


def answer(rid, body, status=200):
    send({'id': rid, 'head': status, 'type': 'application/json'})
    send({'id': rid, 'chunk': json.dumps(body)})
    send({'id': rid, 'end': True})


def data():
    """The library folder the window opens (--data)."""
    return sys.argv[sys.argv.index('--data') + 1]


def requests():
    """Each request the window sends, but the cancels of one."""
    for line in sys.stdin:
        req = json.loads(line)
        if 'cancel' not in req:
            yield req


def fail_but(path, body):
    """Starts over the library, then answers every request with an error, except `path`, answered with `body` from the
    second time it is asked (so Try again is seen to ask again)."""
    send({'ready': {'setup': False, 'data': data(), 'library': data() + '/library-data', 'version': 'stand-in'}})
    asked = set()
    for req in requests():
        if req['path'] == path and path in asked:
            answer(req['id'], body)
        else:
            answer(req['id'], {'error': 'The stand-in engine could not answer.'}, 500)
        asked.add(req['path'])


def made_up_download(api):
    """Setup's start, for the real engine's routes: a job for the region asked for that is at once downloading, and
    paused when asked, with nothing fetched."""
    job = {}

    def become(s, state):
        job['state'] = job['items'][0]['state'] = state
        if s.on_job:
            s.on_job(state)
        return job

    def start(s, body):
        rid = body['regions'][0]
        job.update(id='made-up', error=None, folder=os.environ['LASTBOOK_DATA'], items=[{
            'id': rid, 'kind': 'osm', 'title': f'{rid.title()} streets', 'source': 'download', 'bytes_done': 40_000_000,
            'bytes_total': 120_000_000, 'rate_bps': 2_000_000, 'eta_s': 40, 'error': None}])
        return become(s, 'downloading')
    api.ROUTES.update({('POST', 'start'): start, ('GET', 'job'): lambda s, b: job or None,
                       ('POST', 'pause'): lambda s, b: become(s, 'paused')})


def use_real_engine(*more):
    """Makes the real engine's modules importable over the library, before any of them is imported: they are then
    changed, and the engine run (`run_real_engine`). `more`: other folders to import from."""
    sys.path[:0] = [str(REPO / 'app'), str(REPO), *map(str, more)]
    os.environ['LASTBOOK_DATA'] = data()   # engine/paths.py reads it when first imported


def run_real_engine():
    sys.argv[0] = str(REPO / 'app' / 'engine_pipe.py')
    runpy.run_path(sys.argv[0], run_name='__main__')
