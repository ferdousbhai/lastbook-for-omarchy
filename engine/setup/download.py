"""The one way out: downloads for setup. This is the only module in Lastbook that opens a network connection.

  - HTTPS only, to the hosts in ALLOWED_HOSTS, port 443.
  - No proxy: http.client never reads the system's or the environment's proxy settings (urllib would), so a request goes
    straight to the host named, or nowhere.
  - A redirect is followed only to another allowed address, at most MAX_REDIRECTS times.
  - Resuming uses HTTP Range on the ETag saved beside the partial file. R2 ignores If-Range (it answers 206 whatever the ETag),
    so the check is made here: another ETag on the 206, a 200 instead, or another total size starts that file over.
    (Not If-Match: on a redirect chain the first host's ETag is not the file's, so it would refuse every resume.)
  - The SHA-256 is computed while the bytes arrive. After a resume the bytes already on disk are read once to rebuild it.
  - Nothing here runs unless the person started or resumed a download (or opened setup: one GET of the manifest).
  - A connection that goes silent (TIMEOUT) or only trickles (STALL_*) is dropped and asked again from the byte reached;
    a line that brings almost nothing for a long time (GIVE_UP_*) ends the download with a plain failure, and every
    retry is printed to the log. Pause and cancel hang up the connection being read, so they do not wait for it.

Tests pass their own `allowed` and `context` to reach a local test server. The product never does.
"""
import collections
import contextlib
import errno
import hashlib
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from pathlib import Path

ALLOWED_HOSTS = frozenset({
    'downloads.lastbook.org',
    'build-metadata.protomaps.dev',     # the list of Protomaps' daily OpenStreetMap builds
    'build.protomaps.com',              # the builds: the map is cut from one with range requests (engine/setup/maps.py)
    'download.geonames.org',            # the place names for searching the map (GeoNames, CC BY 4.0)
})
MAX_REDIRECTS = 5
TIMEOUT = 30                        # seconds without a byte before the connection counts as broken
CHUNK = 1024 * 1024
SAVE_EVERY = 2.0                    # seconds between saves of the partial file's .json
NETWORK_TRIES = 6                   # broken connections in a row, with no progress between them, before giving up
BACKOFF = (2, 4, 8, 15, 30, 30)
# A connection that still sends a byte now and then never trips TIMEOUT, yet gets nowhere. Lastbook is made for bad
# networks, so both floors below sit under the slowest line that is still working, not under a comfortable one:
#   - STALL: one connection whose answer arrives at under 1 KiB a second (8 kbit/s), averaged over two whole minutes of
#     reading, is dropped and the rest asked for on a new connection. Working slow lines are above this (a 56k modem
#     gives about 5 KiB/s, GPRS a few). Under it is what a connection stuck retransmitting looks like (a packet every
#     several seconds), and a new connection is what cures that. The bytes that came are kept, so being wrong about a
#     line costs one new handshake (a few KB) every two minutes, never the download.
#   - GIVE UP: fewer than 512 KiB (about 580 bytes a second) in 15 minutes of reading, failed tries and waits, counted
#     across every connection and retry of one download or one map, ends it as a 'network' failure the person can try
#     again. At that speed the smallest thing setup fetches (the 0.6 GB world map) would take two weeks. This is the
#     only one of the two that fails anything, and it is the lower floor, so a line steady at 600-1000 bytes a second
#     is reconnected now and then but never failed.
# Only time spent waiting on the network counts (reading an answer, a try that failed, the wait before the next): a slow
# disk, a paused job or many small quick requests cannot trip either.
STALL_WINDOW, STALL_BYTES = 120, 120 * 1024
GIVE_UP_WINDOW, GIVE_UP_BYTES = 15 * 60, 512 * 1024
USER_AGENT = 'Lastbook-setup/1'


class Stopped(Exception):
    """The person paused or cancelled. .why is 'paused' or 'cancelled'."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


class Failed(Exception):
    """A plain sentence for the person, and a code from docs/SETUP-API.md."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class BadChecksum(Failed):
    def __init__(self, message):
        super().__init__('checksum', message)


def disk_full(e):
    return getattr(e, 'errno', None) in (errno.ENOSPC, errno.EDQUOT)


def host_allowed(host, allowed=None):
    host = (host or '').lower().rstrip('.')
    if allowed is not None:
        return host in allowed
    return host in ALLOWED_HOSTS


def url_allowed(url, allowed=None):
    """An https address on an allowed host, with no user name or password in it, on port 443 (a test's own hosts: any port)."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        return False
    if allowed is None and port not in (None, 443):
        return False
    return host_allowed(u.hostname, allowed)


def roots_path():
    """engine/setup/roots.pem, beside this file (the package installs it there too)."""
    return Path(__file__).with_name('roots.pem')


def tls_context():
    """TLS 1.2 or later, hostname checked, and trust in the roots of roots.pem only: never the system's store (see
    packaging/setup_roots.py for why and which). With the file missing, nothing is downloaded."""
    roots = roots_path()
    if not roots.is_file():
        raise Failed('network', 'This copy of Lastbook is missing its list of trusted download servers, so it cannot download. '
                                'Download Lastbook again.')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # check_hostname on and CERT_REQUIRED; no default certificates loaded
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(roots))
    return ctx


class Stalled(OSError):
    """A connection that sends too little to be worth keeping (STALL_*): handled as a broken one."""


class _Local(Exception):
    """A file on this computer could not be read or written. .error is the OSError."""

    def __init__(self, error):
        super().__init__(str(error))
        self.error = error


@contextlib.contextmanager
def _local():
    """Around work on this computer's own files, so that its errors are never taken for a broken connection."""
    try:
        yield
    except OSError as e:
        raise _Local(e) from e


def file_failure(e, folder):
    """The Failed for an error from this computer's own disk while downloading into `folder`."""
    if disk_full(e):
        return Failed('no_space', 'The disk filled up while downloading. Free some space, then try again.')
    return Failed('disk', f'Lastbook could not write to {folder} ({type(e).__name__}: {getattr(e, "strerror", None) or e}). '
                          'Check that the disk is still connected and can be written to, then try again.')


class _Floor:
    """Whether bytes arrive at a least rate: add(seconds, bytes) is True once the last `window` seconds (or more) of
    waiting brought fewer than `least` bytes. The seconds are the caller's count of time spent waiting on the network."""

    def __init__(self, window, least):
        self.window, self.least = window, least
        self.t, self.n = 0.0, 0
        self.marks = collections.deque([(0.0, 0)])      # (seconds, bytes) so far, a few a window

    def add(self, seconds, n):
        self.t += max(seconds, 0.0)
        self.n += n
        while len(self.marks) > 1 and self.marks[1][0] <= self.t - self.window:
            self.marks.popleft()        # the first mark left is the newest one a whole window old, if any is
        if self.t - self.marks[-1][0] >= self.window / 20:
            self.marks.append((self.t, self.n))
        t0, n0 = self.marks[0]
        return self.t - t0 >= self.window and self.n - n0 < self.least


class Watch:
    """Whether one download (a file, or all the parts of one map) is getting anywhere, across its connections and
    retries: see GIVE_UP_* above."""

    def __init__(self):
        self.floor, self.hopeless = _Floor(GIVE_UP_WINDOW, GIVE_UP_BYTES), False
        self._at = (time.monotonic(), 0.0)

    def add(self, seconds, n):
        self.hopeless = self.floor.add(seconds, n)

    def start(self):
        """Call before a try; failed(wait) after one that broke counts all the time it took, and the wait before the next."""
        self._at = (time.monotonic(), self.floor.t)

    def failed(self, wait):
        clock, counted = self._at
        self.add(max(time.monotonic() - clock - (self.floor.t - counted), 0.0) + wait, 0)

    def check(self):
        if self.hopeless:
            raise Failed('network', f'The connection is too slow or keeps breaking: less than {GIVE_UP_BYTES // 1024} KB arrived in '
                                    f'{GIVE_UP_WINDOW // 60} minutes. Check the internet connection, then try again.')


class Control:
    """Pause and cancel, from any thread. The download notices within one chunk or one second of waiting, and a read
    that is waiting on a silent connection is woken by hanging up that connection. (A connection still being made is
    not: that waits for it to open or time out.)"""

    def __init__(self):
        self.ev = threading.Event()
        self.why = None
        self._lock = threading.Lock()
        self._conn = None

    def stop(self, why):
        if self.why != 'cancelled':
            self.why = why
        self.ev.set()
        with self._lock:
            sock = getattr(self._conn, 'sock', None)
            if isinstance(sock, socket.socket):
                try:
                    # The plain socket's own shutdown: the read in the other thread ends at once, as on a closed
                    # connection. (SSLSocket.shutdown would first take the TLS state away from under that read.)
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass

    def hold(self, conn):
        """The connection now being read (None: none), for stop() to hang up."""
        with self._lock:
            self._conn = conn

    def check(self):
        if self.ev.is_set():
            raise Stopped(self.why or 'paused')

    def sleep(self, s):
        if self.ev.wait(s):
            raise Stopped(self.why or 'paused')


def _hold(control, conn):
    hold = getattr(control, 'hold', None)
    if hold:
        hold(conn)


def _close(control, conn):
    _hold(control, None)
    try:
        conn.close()
    except OSError:
        pass


def _open(url, start, etag, allowed, context, timeout, end=None, control=None):
    """(connection, response, final url) for a GET from byte `start`, after following allowed redirects. With `end`, exactly
    bytes start..end are asked for (always as a Range, from byte 0 too) and no If-Range is sent. The connection is the
    `control`'s to hang up from when it is open until the caller closes it (_close)."""
    for _ in range(MAX_REDIRECTS + 1):
        if not url_allowed(url, allowed):
            raise Failed('network', 'Lastbook refused to download from an address it does not know.')
        u = urllib.parse.urlsplit(url)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=context)
        headers = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'}
        if end is not None:
            headers['Range'] = f'bytes={start}-{end}'
        elif start:
            headers['Range'] = f'bytes={start}-'
            if etag:
                headers['If-Range'] = etag
        try:
            conn.connect()
            _hold(control, conn)
            if control is not None:
                control.check()         # stopped while it was opening: stop() had no connection to hang up yet
            conn.request('GET', (u.path or '/') + ('?' + u.query if u.query else ''), headers=headers)
            r = conn.getresponse()
        except BaseException:
            _close(control, conn)
            if control is not None:
                control.check()         # hung up by a pause or cancel, not broken
            raise
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader('Location')
            r.close()
            _close(control, conn)
            if not loc:
                raise Failed('network', 'The download server answered with a move to nowhere.')
            url = urllib.parse.urljoin(url, loc)
            continue
        return conn, r, url
    raise Failed('network', 'The download server sent Lastbook round in circles.')


def _body(r, n, control=None, watch=None, to_end=False):
    """The next n bytes of an answer, in pieces of at most CHUNK as they arrive (so progress, pause and the checks here
    stay quick on a slow line). The connection closing before n bytes is a ConnectionError, or with to_end the end of the
    answer. Raises Stalled for a connection too slow to keep (STALL_*), and what `watch` raises, both only after the
    bytes that did come were handed over."""
    floor, slow = _Floor(STALL_WINDOW, STALL_BYTES), False
    while n > 0:
        if control is not None:
            control.check()
        if watch is not None:
            watch.check()
        if slow:
            raise Stalled(f'under {STALL_BYTES // STALL_WINDOW} bytes a second for {STALL_WINDOW // 60} minutes')
        t = time.monotonic()
        try:
            b = r.read1(min(CHUNK, n))
        except Exception:
            if control is not None:
                control.check()         # hung up by a pause or cancel, not broken
            raise
        waited = time.monotonic() - t
        if not b:
            if control is not None:
                control.check()
            if to_end:
                return
            raise ConnectionError('the connection closed early')
        n -= len(b)
        if watch is not None:
            watch.add(waited, len(b))
        slow = floor.add(waited, len(b))
        yield b


NOT_THE_SERVER = ('Lastbook could not confirm it is talking to the download server, so it stopped. '
                  'A network that intercepts secure connections (some offices, hotels) can cause this.')


def fetch_range(url, start, length, control, allowed=None, context=None, timeout=TIMEOUT, watch=None, got=None):
    """Exactly `length` bytes of `url` from byte `start` (HTTP Range requests), retried with backoff on a broken or stalled
    connection or a 429 or 5xx answer. A retry asks only for the bytes still missing. NETWORK_TRIES tries in a row that
    bring nothing end it; `watch` (a Watch shared by all the parts of one map) ends it when the whole is getting nowhere.
    got(n) is called as bytes arrive (n below zero: that many are being fetched again).
    The map is cut from a large archive this way (engine/setup/maps.py). Raises Failed, or Stopped when paused or cancelled."""
    context = context or tls_context()
    watch = watch or Watch()
    out, etag, tries = bytearray(), None, 0
    while True:
        control.check()
        had = len(out)
        watch.start()
        try:
            conn, r, _ = _open(url, start + had, None, allowed, context, timeout, end=start + length - 1, control=control)
            try:
                if r.status == 429 or 500 <= r.status < 600:
                    raise OSError(f'HTTP {r.status}')     # the server is busy or briefly broken: tried again below, as a broken connection is
                if r.status != 206:
                    raise Failed('network', f'The map server answered {r.status} to a part request.')
                cr = _content_range(r.getheader('Content-Range'))
                if cr and cr[0] != start + had:
                    raise Failed('network', 'The map server answered with another part of the map than the one asked for.')
                tag = r.getheader('ETag')
                if had and etag and tag and tag != etag:      # another file than the one the first bytes came from: this part over
                    if got:
                        got(-had)
                    out.clear()
                    etag = tag
                    continue
                etag = tag or etag
                for piece in _body(r, length - had, control, watch):
                    out += piece
                    if got:
                        got(len(piece))
                return bytes(out)
            finally:
                _close(control, conn)
        except ssl.SSLCertVerificationError:
            raise Failed('network', NOT_THE_SERVER) from None
        except (OSError, http.client.HTTPException) as e:
            control.check()
            tries = 0 if len(out) > had else tries + 1
            if tries >= NETWORK_TRIES:
                raise Failed('network', f'The connection to the map server keeps breaking ({type(e).__name__}). Try again later.')
            print(f'setup: a map part broke at {len(out)} of {length} (from byte {start}): {type(e).__name__}: {e}; trying again', flush=True)
            wait = BACKOFF[min(max(tries - 1, 0), len(BACKOFF) - 1)]
            watch.failed(wait)
            watch.check()
            control.sleep(wait)


def fetch_small(url, limit=1 << 20, allowed=None, context=None, timeout=10, control=None):
    """One small GET (the manifest, the list of map builds, the place names). Bytes, or None on any failure: an answer
    that is not 200, is cut short, is longer than `limit` or stalls. With a `control`, raises Stopped when paused or cancelled."""
    try:
        conn, r, _ = _open(url, 0, None, allowed, context or tls_context(), timeout, control=control)
        try:
            if r.status != 200:
                return None
            data = b''.join(_body(r, limit + 1, control, to_end=True))
            if r.length:        # the connection closed before the length the server gave
                return None
            return data if len(data) <= limit else None
        finally:
            _close(control, conn)
    except (OSError, http.client.HTTPException, Failed, ValueError):
        return None


def _load_meta(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_meta(path, d):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _sha256(path, control, progress):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while b := f.read(8 * CHUNK):
            control.check()
            h.update(b)
            progress(f.tell())
    return h.hexdigest()


def _content_range(v):
    """(start, total) from 'bytes 100-199/1000', or None."""
    try:
        unit, _, rest = (v or '').partition(' ')
        span, _, total = rest.partition('/')
        return int(span.split('-')[0]), int(total)
    except ValueError:
        return None


def remove(path):
    """Deletes a finished download."""
    os.unlink(path)


# Files this process has matched against their published SHA-256: (device, inode, size, mtime, ctime) -> sha256.
_VERIFIED = {}


class Download:
    """One file: <dest>.part grows, with <dest>.part.json beside it (url, size, sha256, bytes done, ETag).
    run() returns once the whole file is there and its SHA-256 matches, renamed to <dest>. Raises Stopped, Failed or BadChecksum.
    A file this process has matched is remembered (in memory: a record on disk could be brought along with a file that is
    not the published one), so a resume while it is being unpacked need not read it through again; any change to the
    file and it is hashed again.
    progress(done, total, phase) is called as bytes arrive; phase is 'downloading' or 'verifying'."""

    def __init__(self, url, dest, size, sha256, control, progress=lambda d, t, p: None, allowed=None, context=None, timeout=TIMEOUT):
        self.url, self.dest, self.size, self.sha256 = url, str(dest), int(size), sha256.lower()
        self.part, self.meta = self.dest + '.part', self.dest + '.part.json'
        self.control, self.progress = control, progress
        self.allowed, self.context, self.timeout = allowed, context, timeout

    def done_bytes(self):
        try:
            return min(os.path.getsize(self.part), self.size)
        except OSError:
            return 0

    def discard(self):
        for p in (self.part, self.meta, self.meta + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _rehash(self, n):
        """The SHA-256 of the first n bytes already on disk."""
        h = hashlib.sha256()
        done = 0
        with open(self.part, 'rb') as f:
            while done < n:
                self.control.check()
                b = f.read(min(8 * CHUNK, n - done))
                if not b:
                    break
                h.update(b)
                done += len(b)
                self.progress(done, n, 'verifying')
        return h, done

    def _identity(self):
        st = os.stat(self.dest)
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def _write_stamp(self):
        try:
            _VERIFIED[self._identity()] = self.sha256
        except OSError:
            pass

    def run(self):
        try:
            return self._run()
        except _Local as e:      # this computer's disk, not the connection: said so at once, never retried as a broken download
            print(f'setup: {os.path.basename(self.dest)}: {type(e.error).__name__}: {e.error}', flush=True)
            raise file_failure(e.error, os.path.dirname(self.dest) or '.') from None

    def _run(self):
        with _local():
            if os.path.isfile(self.dest) and os.path.getsize(self.dest) == self.size and not os.path.exists(self.part):
                try:
                    if _VERIFIED.get(self._identity()) == self.sha256:
                        return self.dest     # matched before a pause or restart and not changed since: not read through again
                except OSError:
                    pass
                if _sha256(self.dest, self.control, lambda done: self.progress(done, self.size, 'verifying')) == self.sha256:
                    self._write_stamp()
                    return self.dest     # finished before a restart, and still what it was
                print(f'setup: {os.path.basename(self.dest)} is here at the right size but is not the published file; downloading it again', flush=True)
                os.unlink(self.dest)
            os.makedirs(os.path.dirname(self.dest) or '.', exist_ok=True)
            meta = _load_meta(self.meta)
            if meta and (meta.get('sha256') != self.sha256 or meta.get('size') != self.size):
                self.discard()       # left by another version of this file
                meta = {}
        ctx = self.context or tls_context()
        with _local():
            start = self.done_bytes()
            if start and not meta.get('etag'):
                start = 0            # nothing to tell the server which file those bytes came from
            with open(self.part, 'ab') as f:
                f.truncate(start)
            h, start = self._rehash(start) if start else (hashlib.sha256(), 0)
        etag = meta.get('etag')
        tries = 0
        ignored_once = False
        self.watch = Watch()
        while start < self.size:
            self.control.check()
            self.watch.start()
            try:
                start, etag = self._fetch(start, etag, h, ctx)
                tries = 0
            except _Restart as r:
                if r.whole and not ignored_once:      # one 200 to a Range can be a passing hiccup at the edge: ask once more first
                    ignored_once = True
                    print(f'setup: the server sent the whole of {os.path.basename(self.dest)} to a resume; asking again', flush=True)
                    self.control.sleep(1)
                    continue
                print(f'setup: the file on the server changed or ignores Range; starting {os.path.basename(self.dest)} over', flush=True)
                with _local(), open(self.part, 'r+b') as f:
                    f.truncate(0)
                h, start, etag = hashlib.sha256(), 0, None
                continue
            except ssl.SSLCertVerificationError:
                raise Failed('network', NOT_THE_SERVER)
            except (OSError, http.client.HTTPException, ValueError) as e:
                self.control.check()
                now = self.done_bytes()     # h already holds every byte written: it is updated just after each write
                tries = 0 if now > start else tries + 1
                start = now
                etag = _load_meta(self.meta).get('etag') or etag
                if tries >= NETWORK_TRIES:
                    raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
                print(f'setup: download broke at {start} of {self.size}: {type(e).__name__}: {e}; trying again', flush=True)
                wait = BACKOFF[min(tries, len(BACKOFF) - 1)]
                self.watch.failed(wait)
                self.watch.check()      # what is on disk stays: trying again later carries on from it
                self.control.sleep(wait)
        if h.hexdigest() != self.sha256:
            self.discard()
            raise BadChecksum('The downloaded file did not match its published checksum.')
        with _local():
            os.replace(self.part, self.dest)
        self._write_stamp()
        try:
            os.unlink(self.meta)
        except OSError:
            pass
        return self.dest

    def _fetch(self, start, etag, h, ctx):
        """Reads until the end or a break. Returns (bytes done, etag). Raises _Restart when the server's file is not the one
        begun, and _Local when it is this computer's file that cannot be written."""
        watch = getattr(self, 'watch', None)
        conn, r, final = _open(self.url, start, etag, self.allowed, ctx, self.timeout, control=self.control)
        try:
            if r.status >= 400:
                if 500 <= r.status < 600 or r.status == 429:     # busy, not refused: tried again after a wait
                    raise OSError(f'HTTP {r.status}')
                raise Failed('network', f'The download server refused the file (HTTP {r.status}).')
            new_etag = r.getheader('ETag')
            if r.status == 206:
                cr = _content_range(r.getheader('Content-Range'))
                if not cr or cr[0] != start or cr[1] != self.size or (etag and new_etag and new_etag != etag):
                    raise _Restart(new_etag)
            elif r.status == 200:
                if start:
                    raise _Restart(new_etag, whole=True)    # the server sent the whole file again: its copy changed, or it ignores Range
                length = r.getheader('Content-Length')
                if length is not None and length.isdigit() and int(length) != self.size:
                    raise BadChecksum(f'The file on the server is {int(length):,} bytes, not the {self.size:,} Lastbook expects.')
            else:
                raise OSError(f'HTTP {r.status}')
            etag = new_etag or etag
            meta = {'url': self.url, 'size': self.size, 'sha256': self.sha256, 'etag': etag, 'bytes_done': start}
            with _local():
                _save_meta(self.meta, meta)
                f = open(self.part, 'r+b')
            saved = time.time()
            try:
                with _local():
                    f.seek(start)
                for b in _body(r, self.size - start, self.control, watch):     # the network: its errors are a broken connection
                    with _local():
                        f.write(b)
                    h.update(b)
                    start += len(b)
                    self.progress(start, self.size, 'downloading')
                    if time.time() - saved > SAVE_EVERY:
                        with _local():
                            f.flush()
                            meta['bytes_done'] = start
                            _save_meta(self.meta, meta)
                        saved = time.time()
                with _local():
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                try:
                    f.close()       # writes what is still in memory, so that the size on disk is what `h` holds
                except OSError as e:
                    raise _Local(e) from e
            with _local():
                meta['bytes_done'] = start
                _save_meta(self.meta, meta)
            return start, etag
        finally:
            _close(self.control, conn)


class _Restart(Exception):
    def __init__(self, etag, whole=False):
        super().__init__('restart')
        self.etag, self.whole = etag, whole
