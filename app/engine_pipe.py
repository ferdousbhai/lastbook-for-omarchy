"""The engine for the native app: JSON lines on standard input and output, no port, no socket.

The native window (native/, Rust and GTK 4) starts this as a child process and sends one request a line:

  {"id": 7, "method": "GET", "path": "/api/search?q=water"}
  {"id": 8, "method": "POST", "path": "/api/chat", "body": {...}}

Each answer comes back as lines carrying the same id: one head, any number of text chunks, one end:

  {"id": 7, "head": 200, "type": "application/json"}
  {"id": 7, "chunk": "{\\"results\\": ..."}
  {"id": 7, "end": true}

Chunks arrive as the engine writes them, so a streamed answer (Chat's server-sent events) reaches the window as it is written.
{"cancel": 8} stops request 8. The first line out is {"ready": {...}} with the data and library folders, which the window reads
files from directly (thumbnails, PDFs, videos): only answers that need the engine come through here.

Requests run through app/bridge.py's Bridge, the same lanes, limits and engine Handler the web window used, holding its key:
this process and the window are one program, split so the engine can stay in Python. It leaves when its standard input closes.
"""
import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]
if (HERE.parent / 'lib').is_dir():   # the installed package: run as packaged (packaging/linux/lastbook.py has the rules)
    sys.frozen = True
    sys._MEIPASS = str(HERE.parent)
    sys.path.insert(2, str(HERE.parent / 'lib'))

OUT = sys.stdout
sys.stdout = sys.stderr          # anything the engine prints goes to the log, never into the protocol
_out_lock = threading.Lock()
_open = threading.Condition()
_open_ids = set()


def send(obj):
    line = json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n'
    with _out_lock:
        OUT.write(line)
        OUT.flush()


class PipeSink:
    """A bridge Sink that forwards an answer to the window as it is written."""

    def __init__(self, rid):
        self.rid = rid
        with _open:
            _open_ids.add(rid)

    def head(self, status, reason, headers):
        hs = dict((k.lower(), v) for k, v in (headers.items() if hasattr(headers, 'items') else headers))
        send({'id': self.rid, 'head': status, 'type': hs.get('content-type', '')})

    def data(self, chunk, ack):
        if chunk:
            send({'id': self.rid, 'chunk': chunk.decode('utf-8', 'replace')})
        ack()

    def finish(self, ok):
        send({'id': self.rid, 'end': bool(ok)})
        with _open:
            _open_ids.discard(self.rid)
            _open.notify_all()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', help='the data folder holding library-data/ (else LASTBOOK_DATA, else the first library found)')
    a = ap.parse_args()
    import main as app_main
    data = app_main.data_folder(a.data)
    if data is None or app_main.check(data)[0] is None:
        send({'ready': None, 'error': 'no-library'})
        return 2
    data = app_main.check(data)[0]
    os.environ['LASTBOOK_DATA'] = str(data)
    os.environ.setdefault('LASTBOOK_LOCAL_AI', '1')
    app_main.open_log(data)
    sys.stdout = sys.stderr      # open_log points the standard streams at the log; keep the protocol on OUT
    import server
    import bridge as B
    from engine.setup import api as setup_api
    server.init()
    setup_api.configure(mode='manage', current=data, **app_main.setup_options())
    bridge = B.Bridge(server)
    send({'ready': {'data': str(data), 'library': str(server.LIB), 'version': (HERE.parent / 'VERSION').read_text().strip()
                    if (HERE.parent / 'VERSION').exists() else None}})
    running = {}
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if 'cancel' in req:
            ex = running.pop(req['cancel'], None)
            if ex:
                ex.cancel()
            continue
        rid, method, path = req.get('id'), req.get('method', 'GET'), req.get('path', '/')
        body = req.get('body')
        raw = json.dumps(body).encode() if body is not None else None
        headers = {B.KEY_HEADER: bridge.key, 'accept': 'application/json'}
        if raw is not None:
            headers['content-type'] = 'application/json'
        ex = bridge.start(method, B.ORIGIN + path, headers, raw, PipeSink(rid), is_document=False)
        if ex:
            running[rid] = ex
            if len(running) > 256:   # finished ones are dropped lazily; cancelling a finished exchange does nothing
                for k in list(running)[:128]:
                    running.pop(k, None)
    with _open:   # the window has gone: finish what it asked for, then leave
        _open.wait_for(lambda: not _open_ids, timeout=30)
    return 0


if __name__ == '__main__':
    sys.exit(main())
