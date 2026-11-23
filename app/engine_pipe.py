"""The engine for the native app: JSON lines on standard input and output, no port, no socket.

The native window (native/, Rust and GTK 4) starts this as a child process and sends one request a line:

  {"id": 7, "method": "GET", "path": "/api/find?q=water"}
  {"id": 8, "method": "POST", "path": "/api/setup/start", "body": {...}}

Each answer comes back as lines carrying the same id: one head, any number of pieces, one end:

  {"id": 7, "head": 200, "type": "application/json"}
  {"id": 7, "chunk": "{\\"results\\": ..."}
  {"id": 7, "end": true}

A JSON answer's pieces are "chunk", text; anything else (pages, images, video) comes as "b64", base64, byte for byte.
Pieces arrive as the engine writes them. "end" is false when the answer stopped short. A line that is not a request is
passed over. Lines with no id are sent unasked: {"restart": folder} when setup has finished, and {"job": state} when a
setup job's state changes. The first line out is {"ready": {...}} with the data and library folders, which the window reads
files from directly (thumbnails, PDFs, videos): only answers that need the engine come through here.

Requests run through app/bridge.py's Bridge (lanes, limits and the engine's own Handler):
this process and the window are one program, split so the engine can stay in Python. It leaves when its standard input closes.
"""
import base64
import codecs
import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]
if (HERE.parent / 'lib').is_dir():   # the installed package: its lib/ before the system (engine/paths.py INSTALLED)
    sys.path.insert(2, str(HERE.parent / 'lib'))

OUT = sys.stdout
sys.stdout = sys.stderr          # anything the engine prints goes to the log, never into the protocol
_out_lock = threading.Lock()
_open = threading.Condition()
_running = 0     # requests still being answered. Under _open.


def send(obj):
    line = json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n'
    with _out_lock:
        OUT.write(line)
        OUT.flush()


class PipeSink:
    """A bridge Sink that forwards an answer to the window as it is written."""

    def __init__(self, rid):
        global _running
        self.rid = rid
        self.text = None     # for a JSON answer: its decoder, which keeps a character cut between two chunks whole
        with _open:
            _running += 1

    def head(self, status, reason, headers):
        ctype = next((v for k, v in headers if k.lower() == 'content-type'), '')
        if ctype.split(';')[0].strip() == 'application/json':
            self.text = codecs.getincrementaldecoder('utf-8')('replace')
        send({'id': self.rid, 'head': status, 'type': ctype})

    def data(self, chunk):
        if self.text:
            chunk = self.text.decode(chunk)
            if chunk:
                send({'id': self.rid, 'chunk': chunk})
        elif chunk:
            send({'id': self.rid, 'b64': base64.b64encode(chunk).decode('ascii')})

    def finish(self, ok):
        global _running
        try:
            if self.text and (rest := self.text.decode(b'', final=True)):     # a last character cut short
                send({'id': self.rid, 'chunk': rest})
            send({'id': self.rid, 'end': bool(ok)})
        finally:     # also when the window has gone and the pipe is closed: the request is over, and main() is waiting to leave
            with _open:
                _running -= 1
                _open.notify_all()


def answer(bridge, line):
    """One line from the window: a request, handed to the bridge. A line that is not one (not JSON, not an object, no
    id that can be answered) carries no id an answer could name, so it is passed over."""
    try:
        req = json.loads(line)
    except ValueError:      # UnicodeDecodeError too: the line's bytes are not UTF-8
        return
    if not isinstance(req, dict) or not isinstance(req.get('id'), (int, str)):
        return
    body = req.get('body')
    bridge.start(req.get('method', 'GET'), req.get('path', '/'), json.dumps(body).encode() if body is not None else None, PipeSink(req['id']))


def open_library(setup_api, settings_dir, note):
    """The engine, on the library LASTBOOK_DATA names. If it does not open and a switch has just put it there (note), the
    old library is put back, once, and opened instead; note['error'] then says so. Any other failure is raised as it is."""
    import server
    from engine.setup import install
    try:     # upstream's US-only map data, which nothing reads: gone from a library an earlier version unpacked
        install.drop_upstream_maps(os.environ['LASTBOOK_DATA'])
    except Exception as e:
        print('setup: upstream map data not removed:', e, flush=True)
    try:
        server.init()
    except Exception:
        said = setup_api.rolled_back(settings_dir) if note else None
        if not said:
            raise
        import traceback
        traceback.print_exc()
        print('setup:', said, flush=True)
        install.rolled_back_note(note, said)
        server.init()
    return server


def failed_start(note, e):
    """What the window says when the library does not open: with what setup did at this start (a switch undone), which
    would otherwise never be told, since the notice rides on the ready line that is then never sent."""
    said = f'The engine could not open the library ({type(e).__name__}: {str(e)[:300]}).'
    return f'{note["error"]} {said}' if note and note.get('error') else said


def options(app_main):
    """Setup's hooks for this window: a finished setup asks the window to restart on the new library (it starts this
    engine again), and each change of a job's state is told, so the window reads the job then and not on a timer (it
    still reads progress on one, while someone is looking: bytes arriving are not events)."""
    def finished(folder):
        app_main.remember(Path(folder))
        send({'restart': str(folder)})

    def job_changed(state):
        try:
            send({'job': state})
        except (OSError, ValueError):     # the window has gone and the pipe with it: the job carries on
            pass
    return dict(app_main.setup_options(), on_finish=finished, on_job=job_changed)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', help='the data folder holding library-data/ (else LASTBOOK_DATA, else the first library found)')
    a = ap.parse_args()
    import main as app_main
    import bridge as B
    from engine.setup import api as setup_api
    # A switch setup staged (a new library, an upgrade, a map) is made now, before anything is open. A folder named outright
    # (--data, LASTBOOK_DATA) is opened as it is: only its own staged switch is made.
    given = a.data or os.environ.get('LASTBOOK_DATA')
    lib, note = setup_api.startup(app_main.app_folders()[0], only=Path(given) if given else None)
    if lib and not given:
        app_main.remember(lib)
    data = app_main.data_folder(a.data)
    found = app_main.check(data)[0] if data is not None else None
    if found is None:
        # No library yet: first-run setup. Nothing here reaches the network until the person starts a download.
        work = app_main.app_folders()[1] / 'Setup'
        app_main.open_log(work)
        setup_api.configure(mode='first-run', **options(app_main))
        bridge = B.Bridge(setup_api.SetupServer)
        send({'ready': {'setup': True, 'data': None, 'library': '', 'version': app_main.version()}})
    else:
        data = found
        os.environ['LASTBOOK_DATA'] = str(data)
        app_main.open_log(data)     # the installed app's output goes to the log from here; the protocol stays on OUT
        try:
            server = open_library(setup_api, app_main.app_folders()[0], note)
        except Exception as e:
            send({'error': failed_start(note, e)})      # the window shows it in place of the library
            raise
        if not given or note:   # the library opened: delete the old library an upgrade moved aside
            setup_api.opened(app_main.app_folders()[0], data, note)
        if not given:           # the person's own library (not one named for this run): its short, stable path
            app_main.link_library(data)
        import retrieval
        retrieval.kick(default_library=not given)   # the background indexer, if documents wait and Recall is there; never raises
        setup_api.configure(mode='manage', current=data, **options(app_main))
        bridge = B.Bridge(server)
        ready = {'setup': False, 'data': str(data), 'library': str(server.LIB), 'version': app_main.version()}
        if note and note.get('error'):
            ready['notice'] = note['error']     # a switch that failed or was undone at this start, in a sentence
        send({'ready': ready})
    for line in sys.stdin.buffer:
        answer(bridge, line)
    with _open:   # the window has gone: finish what it asked for, then leave
        _open.wait_for(lambda: not _running, timeout=30)
    return 0


if __name__ == '__main__':
    sys.exit(main())
