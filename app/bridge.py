"""No-port transport between the app's window and the engine. Nothing in here opens a socket.

A request from the window runs the engine's unchanged request Handler on a worker thread, over in-memory streams.
engine_pipe.py (the native window's engine process) calls start(...), which feeds a Sink piece by piece. The Sink's write to the
window's pipe blocks while the window is behind, and that is all that holds an answer back.

What holds:
  - Lanes. The app's own small requests, page renders, chat and large files each have their own workers,
    so a flood of one never takes a worker from another. The small lane also adds workers
    when a request has waited, because a slow answer mostly sleeps: it must not hold up a quick one behind it.
  - The flood gate. An answer over BIG_BODY is refused before any of it moves: the window holds a whole answer in memory.
  - No PDF ever reaches the web view. A PDF can carry scripts and the system's viewers run them, so what arrives is a page of images PDFium has drawn (_answer_pdf).
"""
import hashlib
import html
import io
import os
import re
import threading
import time
import traceback
import urllib.parse
from collections import deque

ORIGIN = 'lastbook://app'

MiB = 1024 * 1024
CHUNK = MiB                         # largest piece handed to a stream Sink
VIDEO_PIECE = 8 * MiB               # most bytes one video Range answer holds. The player asks for the rest.
BIG_BODY = 64 * MiB                 # the largest answer handed over whole. The largest real saved page is 44 MB.
WHOLE_MAX = 256 * MiB               # above this the refusal says 413, not 403. PDFs and videos come in pieces.
MAX_POST = 256 * 1024               # the engine's own limit on a chat request is 200,000 bytes

# name: (workers, most waiting)
LANES = {
    'small': (8, 5000),     # the app's own files and /api/
    'render': (2, 500),     # PDF page images: PDFium runs one at a time anyway, so more workers would only sit waiting
    'chat': (8, 32),        # long-lived: must not hold a small worker for the length of an answer
    'large': (6, 1000),     # video, PDF, saved pages, encyclopedia entries
}
GROW_TO = {'small': 48}                 # most workers once requests are kept waiting
GROW_AFTER = 0.05                       # seconds a request waits before a lane adds workers for what is waiting
SHRINK_AFTER = 10                       # seconds an added worker sits idle before it goes

PASS_HEADERS = {'range', 'content-type', 'accept'}  # the only request headers the engine sees
FILE_PREFIXES = ('/documents/', '/pages/', '/videos/', '/videos-external/', '/transcripts/', '/metadata/videos/', '/courses/', '/collections/', '/assets/')
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'te', 'upgrade', 'proxy-connection', 'trailer'}
DROP = HOP | {'server', 'date'}
VIDEO_SUFFIXES = ('.mp4', '.webm', '.mkv')
ENGINE_RANGE = re.compile(r'bytes=(\d+)-(\d*)')   # read as the engine reads it: match at the start, so a second range is ignored there too
PDF_PAGE = re.compile(r'[?&]pdf-page=(\d+)(?:&|$)')
PDF_WIDTH = 1400                    # the longer side of a drawn page
PDF_MAX_PAGES = 20000               # the most one viewer page lists
PDF_SIZES_KEPT = 16
PDF_NOTICE = b'This PDF could not be shown here.'
BAD_TARGET = re.compile(r'[\x00-\x20\x7f]')
RENDER = re.compile(r'/api/page/[^/]+/\d+\.png$')
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
    def __init__(self, name, workers, max_waiting, grow_to=None):
        self.name, self.workers, self.max_waiting = name, workers, max_waiting
        self.waiting = deque()
        self.cond = threading.Condition()
        self.grow_to = max(workers, grow_to or 0)
        self.threads = self.active = self.peak_active = self.peak_waiting = self.peak_threads = self.done = 0

    def _add_thread(self):
        self.threads += 1
        self.peak_threads = max(self.peak_threads, self.threads)
        threading.Thread(target=self._work, daemon=True, name=f'lane-{self.name}').start()

    def submit(self, ex):
        with self.cond:
            if len(self.waiting) >= self.max_waiting:
                return False
            self.waiting.append(ex)
            self.peak_waiting = max(self.peak_waiting, len(self.waiting))
            if self.threads < self.workers and self.threads < self.active + len(self.waiting):
                self._add_thread()
            self.cond.notify()
        return True

    def grow(self):
        """Called now and then while anything is waiting. True if this lane still has requests waiting."""
        with self.cond:
            if self.waiting and time.time() - self.waiting[0].t0 >= GROW_AFTER:
                for _ in range(min(len(self.waiting), self.grow_to - self.threads)):
                    self._add_thread()
            return bool(self.waiting)

    def _work(self):
        while True:
            with self.cond:
                while not self.waiting:
                    if not self.cond.wait(SHRINK_AFTER if self.threads > self.workers else None) and not self.waiting and self.threads > self.workers:
                        self.threads -= 1
                        return
                ex = self.waiting.popleft()
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
            try:
                ex.run()
            except Exception:
                traceback.print_exc()
            finally:
                with self.cond:
                    self.active -= 1
                    self.done += 1


class _Sock:
    """All of a socket the Handler touches."""

    def __init__(self, raw, ex):
        self.raw, self.ex = raw, ex

    def makefile(self, mode, buffering=-1):
        return io.BufferedReader(io.BytesIO(self.raw))

    def settimeout(self, t):
        pass

    def sendall(self, b):
        self.ex.write(bytes(b))

    def getsockname(self):
        return ('127.0.0.1', 0)

    def shutdown(self, how):
        pass

    def close(self):
        pass


def _make_handler(base):
    class BridgeHandler(base):
        def copyfile(self, source, outputfile):
            self.connection.ex.file_body(source)

        def send_head(self):
            self.connection.ex.local = self.translate_path(self.path)
            return super().send_head()

    return BridgeHandler


class Exchange:
    """One request. `stopped` is safe to read from any thread; cancel() may be called from any thread."""

    def __init__(self, bridge, method, target, raw, lane):
        self.b, self.method, self.target, self.raw, self.lane = bridge, method, target, raw, lane
        self.path = urllib.parse.unquote(target.split('?', 1)[0])
        self.stopped = False
        self.hbuf = bytearray()
        self.head_done = False
        self.status = None
        self.reason = ''
        self.headers = []
        self.announced = None    # Content-Length, when the engine gave one
        self.sent = 0
        self.local = None        # the file the engine is answering from, once it has found one
        self.swallow = False     # the engine's body is not wanted: a refusal or a redirect replaced it
        self.note = None
        self.t0 = time.time()

    def cancel(self):
        """The window gave up on this request. The Handler stops at its next write."""
        if self.stopped:
            return
        self.stopped = True
        self.b._log_end(self, 'cancelled')

    # ---- worker side
    def run(self):
        if self.stopped:
            return
        try:
            self.b.Handler(_Sock(self.raw, self), ('127.0.0.1', 0), None)  # the constructor runs the whole request
        except GONE:
            pass
        except Exception:
            traceback.print_exc()
            self.note = 'exception'
        self._end()

    def write(self, b):
        if self.stopped:
            raise BrokenPipeError
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
            k, v = k.strip(), v.strip()
            if k.lower() not in DROP:
                self.headers.append((k, v))

    def _header(self, name):
        return next((v for k, v in self.headers if k.lower() == name), None)

    def _set_header(self, name, value):
        self.headers = [(k, v) for k, v in self.headers if k.lower() != name.lower()] + [(name, value)]

    def _replace(self, status, reason, ctype, body, *extra):
        """This answer instead of the engine's. Stops the Handler before it sends any of its own."""
        self.status, self.reason, self.swallow, self.announced = status, reason, True, len(body)
        self.headers = [('Content-Type', ctype), ('Content-Length', str(len(body))), ('X-Content-Type-Options', 'nosniff'), *extra]
        self._send_head()
        if self.method != 'HEAD':
            self._send(body)
        raise BrokenPipeError

    def _answer_pdf(self):
        """The file itself is never handed over, whoever asks: not to a frame, an object, a fetch or a Range. A library address with ?pdf-page=N is one page as a picture.
        Without it, it is a page holding them all. PDFium as packaged has no script engine, and the engine draws pages with it already.
        The engine has already judged the address, so everything it refuses stays refused."""
        csp = ('Content-Security-Policy', self.b.server.Handler.SAVED_PAGE_CSP)
        path = self.path
        if self.local and path.startswith(FILE_PREFIXES) and os.path.isfile(self.local):
            try:
                m = PDF_PAGE.search(self.target)
                if m:
                    body = self.b.pdf_page(path, self.local, int(m.group(1)))
                    self._replace(200, 'OK', 'image/png', body, csp, ('Cache-Control', 'max-age=86400'))
                body = self.b.pdf_viewer(path, self.local)
                self._replace(200, 'OK', 'text/html; charset=utf-8', body, ('Content-Security-Policy', self.b.viewer_csp()))
            except GONE:
                raise
            except Exception as e:     # a damaged or locked PDF
                print('PDF not shown:', type(e).__name__, flush=True)
        self.note = 'pdf-refused'
        self._replace(404, 'Not Found', 'text/html; charset=utf-8', PDF_NOTICE, csp)

    def _on_head(self):
        """The Handler's headers are in and no body has moved. Everything that can change the answer happens here."""
        if self.status in (200, 206) and 'pdf' in (self._header('content-type') or '').lower():
            self._answer_pdf()
        length = self._header('content-length')
        self.announced = int(length) if length and length.isdigit() else None
        refusal = self._flood_gate()
        if refusal:
            self.note = 'flood-gate'
            self.b.count('flood-gate')
            self.status, self.reason = refusal
            self.headers = [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(TOO_LARGE))), ('X-Content-Type-Options', 'nosniff'),   # as plain text, a frame showed it blank
                            ('Content-Security-Policy', self.b.server.Handler.SAVED_PAGE_CSP)]
            self.announced = len(TOO_LARGE)
            self.swallow = True
            self._send_head()
            self._send(TOO_LARGE)
            raise BrokenPipeError   # stops the Handler before it reads the file
        if 300 <= self.status < 400 and self._header('location'):
            # No public URI scheme call follows a redirect, so hand the frame a page that moves itself.
            # A sandbox would stop the refresh, so this one page says instead that it runs nothing and loads nothing. It holds only the engine's address, escaped.
            body = f'<!doctype html><meta http-equiv="refresh" content="0;url={html.escape(self._header("location"), quote=True)}">'.encode()
            self.status, self.reason = 200, 'OK'
            self.headers = [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(body))), ('X-Content-Type-Options', 'nosniff'),
                            ('Content-Security-Policy', "default-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")]
            self.announced = len(body)
            self.swallow = True
            self._send_head()
            self._send(body)
            return
        ctype = (self._header('content-type') or '').lower()
        if 'html' in ctype and not self._header('content-security-policy'):   # the app's own page may not sit inside a frame
            self.headers.append(('Content-Security-Policy', "frame-ancestors 'none'"))
        self._send_head()

    def _flood_gate(self):
        if self.method != 'GET' or self.status not in (200, 206) or self.announced is None or self.announced <= BIG_BODY:
            return None
        if self.announced > WHOLE_MAX:
            return 413, 'Too Large'
        return 403, 'Not For This Page'

    def file_body(self, source):
        """The Handler offers a whole open file."""
        if self.swallow:
            return
        while True:
            if self.stopped:
                raise BrokenPipeError
            b = source.read(CHUNK)
            if not b:
                break
            self._body(b)

    def _body(self, b):
        if self.swallow or not b:
            return
        self.sent += len(b)
        unknown_limit = None if self.lane is self.b.lanes['chat'] else BIG_BODY
        if self.announced is None and unknown_limit and self.sent > unknown_limit:   # a body that never said how long it was
            self.note = 'unknown-length-over-limit'
            self.b.count('unknown-length')
            raise BrokenPipeError
        self._send(b)

    def _end(self):
        raise NotImplementedError

    def _send_head(self):
        raise NotImplementedError

    def _send(self, b):
        raise NotImplementedError


class _Streamed(Exchange):
    def __init__(self, bridge, method, target, raw, lane, sink):
        super().__init__(bridge, method, target, raw, lane)
        self.sink = sink

    def _send_head(self):
        self.sink.head(self.status, self.reason, list(self.headers))

    def _send(self, b):
        for i in range(0, len(b), CHUNK):
            if self.stopped:     # a cancel stops a write midway, not only at the Handler's next one
                raise BrokenPipeError
            self.sink.data(b[i:i + CHUNK])

    def _end(self):
        if self.stopped:
            return
        if not self.head_done:
            self.status, self.reason, self.note = 502, 'No Answer', self.note or 'no-answer'
            self.headers = [('Content-Length', '0')]
            self.head_done = True
            self.announced = 0
            self._send_head()
        short = self.method != 'HEAD' and self.announced is not None and not self.swallow and self.sent < self.announced and self.status not in (204, 304)
        cut = self.note == 'unknown-length-over-limit'
        self.sink.finish(not (short or cut))
        self.b._log_end(self, 'short' if short or cut else 'finished')


class Bridge:
    def __init__(self, server_module, log=None, lanes=None):
        """server_module: the imported engine, after server.init(). log: a function taking one dict per request."""
        self.server, self.origin = server_module, ORIGIN
        self.logger = log
        self.Handler = _make_handler(server_module.Handler)
        self.lanes = {n: _Lane(n, w, q, GROW_TO.get(n)) for n, (w, q) in (lanes or LANES).items()}
        self.waiting_now = threading.Event()
        threading.Thread(target=self._grow_lanes, daemon=True, name='lane-grow').start()
        self.counts = {}
        self.count_lock = threading.Lock()
        self.pdf_sizes = {}
        self.pdf_lock = threading.Lock()

    # ---- requests
    def start(self, method, url, headers, body, sink):
        """Runs sink's calls on a worker. Returns the Exchange, whose cancel() the caller uses to stop the request,
        or None if the bridge answered at once (a refusal), in which case sink already has the whole answer.
        headers: (name, value) pairs or a dict. body: bytes or None."""
        refused, prepared = self._prepare(method, url, headers, body)
        if refused:
            status, reason, hs, text = refused
            sink.head(status, reason, hs)
            sink.data(text)
            sink.finish(True)
            return None
        target, raw, lane = prepared
        ex = _Streamed(self, method, target, raw, lane, sink)
        if not lane.submit(ex):
            self.count('queue-full')
            status, reason, hs, text = _plain(503, 'Busy', b'too many requests')
            sink.head(status, reason, hs)
            sink.data(text)
            sink.finish(True)
            return None
        self.waiting_now.set()
        return ex

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
        """A page this program writes, with no script in it and a policy allowing none. Each picture loads when it comes near the view; the ids are what #page=N looks for."""
        rec = self.server.catalog()['by_path'].get(path.lstrip('/')) or {}
        title = html.escape(str(rec.get('title') or os.path.basename(real)))
        rows = [f'<div class="p" id="page={i}"><img loading="lazy" decoding="async" alt="Page {i}" width="{max(round(w), 1)}" height="{max(round(h), 1)}" src="?pdf-page={i}"></div>'
                for i, (w, h) in enumerate(self._pdf_page_sizes(real), 1)]
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + title + '</title><style>'
                'html{background:#525659}body{margin:0;padding:8px}'
                '.p{max-width:1000px;margin:0 auto 8px}'
                '.p img{display:block;width:100%;height:auto;background:#fff}'
                '</style></head><body>' + ''.join(rows) + '</body></html>').encode()

    def viewer_csp(self):
        return 'sandbox allow-scripts; ' + self.server.Handler.NO_CODE_CSP     # no script is allowed from anywhere, and the page keeps no origin of its own

    def target_of(self, url):
        if url == self.origin:
            return '/'
        if not url.startswith(self.origin + '/'):
            return None
        target = url[len(self.origin):].split('#', 1)[0]
        return re.sub(r'[\x80-\U0010ffff]', lambda m: urllib.parse.quote(m.group()), target)

    def count(self, what):
        with self.count_lock:
            self.counts[what] = self.counts.get(what, 0) + 1

    # ---- inside
    def _refusal(self, why, status, reason, url):
        self.count(why)
        self._log(kind='refused', why=why, status=status, url=url[:300])
        return _plain(status, reason, b'refused')

    def _prepare(self, method, url, headers, body):
        target = self.target_of(url)
        if target is None:
            return self._refusal('host', 400, 'Bad Request', url), None
        if not target.startswith('/') or target.startswith('//') or BAD_TARGET.search(target):
            return self._refusal('target', 400, 'Bad Request', url), None
        if method not in ('GET', 'HEAD', 'POST'):
            return self._refusal('method', 405, 'Method Not Allowed', url), None
        low = _lower(headers)
        path = target.split('?', 1)[0]
        if method == 'POST' and body is not None and len(body) > MAX_POST:
            return self._refusal('post-size', 413, 'Too Large', url), None
        lines = [f'{method} {target} HTTP/1.1', 'Host: localhost', 'Connection: close']
        for k, v in low.items():
            if k in PASS_HEADERS and k != 'range' and not re.search(r'[\r\n]', v):
                lines.append(f'{k}: {v}')
        rng = self._engine_range(method, path, low.get('range'))
        if rng:
            lines.append(f'Range: {rng}')
        if method == 'POST':
            lines.append(f'Content-Length: {len(body or b"")}')
        raw = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1', 'replace') + (body or b'' if method == 'POST' else b'')
        return None, (target, raw, self.lanes[_lane_for(method, path, target)])

    def _engine_range(self, method, path, asked):
        """What the Handler gets as Range. A video answer holds at most one piece, and a video asked for with no Range gets the first."""
        real = urllib.parse.unquote(path).lower()
        if not real.endswith(VIDEO_SUFFIXES) or not real.startswith(FILE_PREFIXES):
            return asked
        if method != 'GET':
            return asked
        if not asked:
            return f'bytes=0-{VIDEO_PIECE - 1}'
        m = ENGINE_RANGE.match(asked)
        if not m:
            return asked   # the engine answers 416
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else None
        if end is None or end - start + 1 > VIDEO_PIECE:
            end = start + VIDEO_PIECE - 1
        return f'bytes={start}-{end}'

    def _grow_lanes(self):
        while True:
            self.waiting_now.wait()
            time.sleep(GROW_AFTER)
            self.waiting_now.clear()
            if any([lane.grow() for lane in self.lanes.values()]):
                self.waiting_now.set()

    def _log(self, **kw):
        if self.logger:
            try:
                self.logger({'ts': round(time.time(), 3), **kw})
            except Exception:
                traceback.print_exc()

    def _log_end(self, ex, end):
        self._log(kind='req', m=ex.method, t=ex.target[:240], lane=ex.lane.name if ex.lane else None, status=ex.status, sent=ex.sent,
                  ms=round((time.time() - ex.t0) * 1000, 1), end=end, note=ex.note)


def _plain(status, reason, text):
    return status, reason, [('Content-Type', 'text/plain'), ('Content-Length', str(len(text))), ('X-Content-Type-Options', 'nosniff')], text


def _lower(headers):
    items = headers.items() if hasattr(headers, 'items') else headers
    return {str(k).lower(): str(v) for k, v in (items or ())}


def _lane_for(method, path, target=''):
    if method == 'POST' and path == '/api/chat':
        return 'chat'
    if RENDER.match(path) or PDF_PAGE.search(target):
        return 'render'
    if path.startswith(('/api/', '/ui/')) or path in ('/', '/index.html', '/sw.js', '/reader.css'):
        return 'small'
    return 'large'
