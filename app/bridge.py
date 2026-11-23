"""No-port transport between the app's window and the engine. Nothing in here opens a socket.

A request from the window runs the engine's unchanged request Handler on a worker thread, over in-memory streams.
engine_pipe.py (the native window's engine process) calls start(...), which feeds a Sink piece by piece. The Sink's write to the
window's pipe blocks while the window is behind, and that is all that holds an answer back.

What holds:
  - Lanes. The app's own small requests, page renders and large files each have their own workers, so a flood of one
    never takes a worker from another. A lane starts a worker whenever a request finds every one busy, up to its most,
    and keeps it (the engine keeps database connections per thread): the small lane's most is high, because a slow
    answer mostly sleeps and must not hold up a quick one behind it.
  - The flood gate. An answer over BIG_BODY is refused before any of it moves: the window holds a whole answer in memory.
  - No PDF ever reaches the web view. A PDF can carry scripts and the system's viewers run them, so what arrives is a page of images PDFium has drawn (_answer_pdf).
"""
import hashlib
import html
import io
import os
import re
import threading
import traceback
import urllib.parse
from collections import deque

MiB = 1024 * 1024
CHUNK = MiB                         # largest piece handed to a stream Sink
VIDEO_PIECE = 8 * MiB               # all of a video an answer holds: its start. A memory guard for a page that names one; the window plays the library's videos from disk.
BIG_BODY = 64 * MiB                 # the largest answer handed over whole. The largest real saved page is 44 MB.
MAX_POST = 256 * 1024               # only setup is posted to, and its own limit is smaller (engine/setup/api.py MAX_BODY)

LANES = {           # name: most workers
    'small': 48,    # /api/
    'render': 2,    # PDF page images: PDFium runs one at a time anyway, so more would only sit waiting
    'large': 6,     # video, PDF, saved pages, encyclopedia entries
}

FILE_PREFIXES = ('/documents/', '/pages/', '/videos/', '/videos-external/', '/transcripts/', '/metadata/videos/', '/courses/', '/collections/', '/assets/')
VIDEO_SUFFIXES = ('.mp4', '.webm', '.mkv')
PDF_PAGE = re.compile(r'[?&]pdf-page=(\d+)(?:&|$)')
PDF_WIDTH = 1400                    # the longer side of a drawn page
PDF_MAX_PAGES = 20000               # the most one viewer page lists
PDF_SIZES_KEPT = 16
PDF_NOTICE = b'This PDF could not be shown here.'
BAD_TARGET = re.compile(r'[\x00-\x20\x7f]')
TOO_LARGE = b'This file is too large to show here.'
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

class Sink:
    """What the caller implements (engine_pipe.PipeSink). All three run on a worker thread, in order, for one request."""

    def head(self, status, reason, headers):
        """headers: a list of (name, value)."""

    def data(self, chunk):
        """chunk: bytes, at most CHUNK. Taking as long as the window needs is how a fast answer is slowed to its pace."""

    def finish(self, ok):
        """The last call. ok False: the answer stopped short."""


class _Lane:
    def __init__(self, most):
        self.most, self.threads, self.active = most, 0, 0
        self.waiting = deque()
        self.cond = threading.Condition()

    def submit(self, ex):
        with self.cond:
            self.waiting.append(ex)
            if self.threads < self.most and self.threads < self.active + len(self.waiting):
                self.threads += 1
                threading.Thread(target=self._work, daemon=True, name='lane').start()
            self.cond.notify()

    def _work(self):
        while True:
            with self.cond:
                while not self.waiting:
                    self.cond.wait()
                ex = self.waiting.popleft()
                self.active += 1
            try:
                ex.run()
            except Exception:
                traceback.print_exc()
            finally:
                with self.cond:
                    self.active -= 1


class _Sock:
    """All of a socket the Handler touches."""

    def __init__(self, raw, ex):
        self.raw, self.ex = raw, ex

    def makefile(self, mode, buffering=-1):
        return io.BufferedReader(io.BytesIO(self.raw))

    def sendall(self, b):
        self.ex.write(bytes(b))


def _make_handler(base):
    class BridgeHandler(base):
        def copyfile(self, source, outputfile):
            self.connection.ex.file_body(source)

        def send_head(self):
            self.connection.ex.local = self.translate_path(self.path)
            return super().send_head()

    return BridgeHandler


class Exchange:
    """One request."""

    def __init__(self, bridge, method, target, raw, sink):
        self.b, self.method, self.target, self.raw, self.sink = bridge, method, target, raw, sink
        self.path = urllib.parse.unquote(target.split('?', 1)[0])
        self.hbuf = bytearray()
        self.head_done = False
        self.status = None
        self.reason = ''
        self.headers = []
        self.announced = None    # Content-Length, when the engine gave one
        self.sent = 0
        self.local = None        # the file the engine is answering from, once it has found one
        self.swallow = False     # the engine's body is not wanted: a refusal or a redirect replaced it
        self.cut = False         # stopped here: a body that never said how long it was grew past the limit

    def run(self):
        try:
            self.b.Handler(_Sock(self.raw, self), ('127.0.0.1', 0), None)  # the constructor runs the whole request
        except GONE:
            pass
        except Exception:
            traceback.print_exc()
        if not self.head_done:
            self.status, self.reason = 502, 'No Answer'
            self.headers = [('Content-Length', '0')]
            self.head_done = True
            self.announced = 0
            self._send_head()
        short = self.announced is not None and not self.swallow and self.sent < self.announced and self.status not in (204, 304)
        self.sink.finish(not (short or self.cut))

    def write(self, b):
        if self.head_done:
            return self._body(b)
        self.hbuf += b
        i = self.hbuf.find(b'\r\n\r\n')
        if i < 0:
            return
        head, rest = bytes(self.hbuf[:i]), bytes(self.hbuf[i + 4:])
        self.hbuf = bytearray()
        self.head_done = True
        self._parse_head(head)
        self._on_head()
        if rest:
            self._body(rest)

    def _parse_head(self, head):
        lines = head.decode('latin-1').split('\r\n')
        parts = lines[0].split(None, 2)
        self.status, self.reason = int(parts[1]), parts[2] if len(parts) > 2 else ''
        for ln in lines[1:]:
            k, _, v = ln.partition(':')
            self.headers.append((k.strip(), v.strip()))

    def _header(self, name):
        return next((v for k, v in self.headers if k.lower() == name), None)

    def _replace(self, status, reason, ctype, body):
        """This answer instead of the engine's, whose body is no longer wanted."""
        self.status, self.reason, self.swallow, self.announced = status, reason, True, len(body)
        self.headers = [('Content-Type', ctype), ('Content-Length', str(len(body)))]
        self._send_head()
        self._send(body)

    def _answer_pdf(self):
        """The file itself is never handed over, whoever asks: not to a frame, an object or a fetch. A library address with ?pdf-page=N is one page as a picture.
        Without it, it is a page holding them all. PDFium as packaged has no script engine, and the engine draws pages with it already.
        The engine has already judged the address, so everything it refuses stays refused."""
        path = self.path
        if self.local and path.startswith(FILE_PREFIXES) and os.path.isfile(self.local):
            try:
                m = PDF_PAGE.search(self.target)
                if m:
                    return self._replace(200, 'OK', 'image/png', self.b.pdf_page(path, self.local, int(m.group(1))))
                return self._replace(200, 'OK', 'text/html; charset=utf-8', self.b.pdf_viewer(path, self.local))
            except GONE:
                raise
            except Exception as e:     # a damaged or locked PDF
                print('PDF not shown:', type(e).__name__, flush=True)
        self._replace(404, 'Not Found', 'text/html; charset=utf-8', PDF_NOTICE)

    def _on_head(self):
        """The Handler's headers are in and no body has moved. Everything that can change the answer happens here."""
        if self.status in (200, 206) and 'pdf' in (self._header('content-type') or '').lower():
            self._answer_pdf()
            raise BrokenPipeError   # stops the Handler before it sends any of the file
        length = self._header('content-length')
        self.announced = int(length) if length and length.isdigit() else None
        refusal = self._flood_gate()
        if refusal:
            self._replace(*refusal, 'text/html; charset=utf-8', TOO_LARGE)   # as plain text, a frame showed it blank
            raise BrokenPipeError   # stops the Handler before it reads the file
        if 300 <= self.status < 400 and self._header('location'):
            # No public URI scheme call follows a redirect, so hand the frame a page that moves itself. It holds only the engine's address, escaped.
            body = f'<!doctype html><meta http-equiv="refresh" content="0;url={html.escape(self._header("location"), quote=True)}">'.encode()
            return self._replace(200, 'OK', 'text/html; charset=utf-8', body)
        self._send_head()

    def _flood_gate(self):
        if self.method == 'GET' and self.status in (200, 206) and self.announced is not None and self.announced > BIG_BODY:
            return 403, 'Not For This Page'
        return None

    def file_body(self, source):
        """The Handler offers a whole open file."""
        if self.swallow:
            return
        while b := source.read(CHUNK):
            self._body(b)

    def _body(self, b):
        if self.swallow or not b:
            return
        self.sent += len(b)
        if self.announced is None and self.sent > BIG_BODY:   # a body that never said how long it was
            self.cut = True
            raise BrokenPipeError
        self._send(b)

    def _send_head(self):
        self.sink.head(self.status, self.reason, list(self.headers))

    def _send(self, b):
        for i in range(0, len(b), CHUNK):
            self.sink.data(b[i:i + CHUNK])


class Bridge:
    def __init__(self, server_module):
        """server_module: the imported engine, after server.init()."""
        self.server = server_module
        self.Handler = _make_handler(server_module.Handler)
        self.lanes = {n: _Lane(most) for n, most in LANES.items()}
        self.pdf_sizes = {}
        self.pdf_lock = threading.Lock()

    # ---- requests
    def start(self, method, path, body, sink):
        """Runs sink's calls on a worker, or here and now for a refusal.
        path: as the window sent it, query included. body: bytes or None."""
        refused, prepared = self._prepare(method, path, body)
        if refused:
            status, reason, hs, text = refused
            sink.head(status, reason, hs)
            sink.data(text)
            sink.finish(True)
            return
        target, raw, lane = prepared
        lane.submit(Exchange(self, method, target, raw, sink))

    # ---- PDFs, as pictures
    def pdf_page(self, path, real, n):
        rec = self.server.catalog()['by_path'].get(path.lstrip('/'))
        if not rec or rec.get('kind') != 'pdf':     # a file a course page links to, with no entry of its own
            rec = {'id': 'file-' + hashlib.sha256(path.encode()).hexdigest()[:32], 'path': real}
        return self.server.render_page(rec, n, PDF_WIDTH)

    def _pdf_page_sizes(self, real):
        st = os.stat(real)
        key = (real, st.st_mtime_ns, st.st_size)
        with self.pdf_lock:
            sizes = self.pdf_sizes.get(key)
        if sizes is None:
            import pypdfium2
            with self.server._pdf_lock:
                pdf = pypdfium2.PdfDocument(real)
                try:
                    sizes = [pdf.get_page_size(i) for i in range(min(len(pdf), PDF_MAX_PAGES))]
                finally:
                    pdf.close()
            with self.pdf_lock:
                while len(self.pdf_sizes) >= PDF_SIZES_KEPT:
                    self.pdf_sizes.pop(next(iter(self.pdf_sizes)))
                self.pdf_sizes[key] = sizes
        return sizes

    def pdf_viewer(self, path, real):
        """A page this program writes, with no script in it. Each picture loads when it comes near the view; the ids are what #page=N looks for."""
        rec = self.server.catalog()['by_path'].get(path.lstrip('/')) or {}
        title = html.escape(str(rec.get('title') or os.path.basename(real)))
        rows = [f'<div class="p" id="page={i}"><img loading="lazy" decoding="async" alt="Page {i}" width="{max(round(w), 1)}" height="{max(round(h), 1)}" src="?pdf-page={i}"></div>'
                for i, (w, h) in enumerate(self._pdf_page_sizes(real), 1)]
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + title + '</title><style>'
                'html{background:#525659}body{margin:0;padding:8px}'
                '.p{max-width:1000px;margin:0 auto 8px}'
                '.p img{display:block;width:100%;height:auto;background:#fff}'
                '</style></head><body>' + ''.join(rows) + '</body></html>').encode()

    # ---- inside
    def _prepare(self, method, path, body):
        """The request the Handler reads: the path as its target, and no header of the window's (the pipe carries none)."""
        target = re.sub(r'[\x80-\U0010ffff]', lambda m: urllib.parse.quote(m.group()), str(path).split('#', 1)[0])
        if not target.startswith('/') or target.startswith('//') or BAD_TARGET.search(target):
            return _plain(400, 'Bad Request', b'refused'), None
        if method not in ('GET', 'POST'):
            return _plain(405, 'Method Not Allowed', b'refused'), None
        path = target.split('?', 1)[0]
        if method == 'POST' and body is not None and len(body) > MAX_POST:
            return _plain(413, 'Too Large', b'refused'), None
        lines = [f'{method} {target} HTTP/1.1', 'Host: localhost', 'Connection: close']
        real = urllib.parse.unquote(path).lower()
        if method == 'GET' and real.endswith(VIDEO_SUFFIXES) and real.startswith(FILE_PREFIXES):
            lines.append(f'Range: bytes=0-{VIDEO_PIECE - 1}')   # the pipe carries no Range, so the engine is asked for a video's first piece and no more
        if method == 'POST':
            lines.append(f'Content-Length: {len(body or b"")}')
        raw = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1', 'replace') + (body or b'' if method == 'POST' else b'')
        return None, (target, raw, self.lanes[_lane_for(path, target)])


def _plain(status, reason, text):
    return status, reason, [('Content-Type', 'text/plain'), ('Content-Length', str(len(text)))], text


def _lane_for(path, target=''):
    if PDF_PAGE.search(target):
        return 'render'
    if path.startswith('/api/') or path == '/reader.css':
        return 'small'
    return 'large'
