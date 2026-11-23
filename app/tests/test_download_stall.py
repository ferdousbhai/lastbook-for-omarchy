#!/usr/bin/env python3
"""The downloader on a bad line (engine/setup/download.py): a connection that trickles or goes silent, a line that gets
nowhere, a pause while nothing arrives, a part request that breaks half way, and this computer's own disk failing.

Against a local HTTPS test server, as app/tests/test_setup.py does (a certificate made for the run, the tests' own
`allowed` and TLS context; the product's allowlist is not touched). No real internet. Never packaged.
The floors are made small for the run (STALL_*, GIVE_UP_*); FloorTests checks the real numbers with made-up seconds.
"""
import contextlib, errno, hashlib, http.server, io, os, shutil, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[1])]
from engine.setup import download  # noqa: E402
from app.tests import fixture  # noqa: E402

ALLOWED = {'127.0.0.1'}
KiB = 1024


class Files:
    """What the server serves and how it misbehaves. Each list holds one entry for each of the next answers for that name."""

    def __init__(self):
        self.lock = threading.Lock()
        self.release = threading.Event()        # set: a silent connection is let go
        self.reset()

    def reset(self):
        self.data, self.log = {}, []
        self.status = {}        # name -> statuses sent instead of the file
        self.cut = {}           # name -> bytes of the answer after which the connection is closed
        self.silent = {}        # name -> bytes of the answer after which nothing more comes, the connection left open
        self.trickle = {}       # name -> (bytes at full speed, then this many bytes, every this many seconds)
        self.short = {}         # name -> bytes of the answer sent before closing, the headers promising all of it
        self.redirect = {}
        self.pace = 0           # seconds to sleep per 64 KiB, every answer
        self.release.clear()

    def next(self, table, name):
        with self.lock:
            return table[name].pop(0) if table.get(name) else None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        F = self.server.files
        name = self.path.split('?')[0].lstrip('/')
        rng = self.headers.get('Range')
        with F.lock:
            F.log.append((name, rng))
        status = F.next(F.status, name)
        if status or name in F.redirect or name not in F.data:
            self.send_response(status or (302 if name in F.redirect else 404))
            if not status and name in F.redirect:
                self.send_header('Location', F.redirect[name])
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = F.data[name]
        start, end = 0, len(body) - 1
        if rng:
            a, _, b = rng.split('=')[1].partition('-')
            start, end = int(a), min(int(b), end) if b else end
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(body)}')
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(end + 1 - start))
        self.send_header('ETag', '"e1"')
        self.end_headers()
        body = body[start:end + 1]
        cut, silent, trickle, short = (F.next(t, name) for t in (F.cut, F.silent, F.trickle, F.short))
        self.close_connection = True
        try:
            if trickle:
                fast, n, every = trickle
                self.wfile.write(body[:fast])
                self.wfile.flush()
                for i in range(fast, len(body), n):
                    if F.release.wait(every):
                        return
                    self.wfile.write(body[i:i + n])
                    self.wfile.flush()
                return
            stop = next((x for x in (cut, silent, short) if x is not None), None)
            sent = 0
            while sent < len(body):
                n = min(64 * KiB, len(body) - sent, (stop - sent) if stop is not None else len(body))
                self.wfile.write(body[sent:sent + n])
                self.wfile.flush()
                sent += n
                if stop is not None and sent >= stop:
                    if silent is not None:
                        F.release.wait(30)
                    self.connection.shutdown(2)
                    return
                if F.pace:
                    time.sleep(F.pace)
        except OSError:
            return


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_class = Path(tempfile.mkdtemp(prefix='lastbook-dl-srv-')).resolve()
        cls.files = Files()
        cls.srv, cls.ctx, cls.base = fixture.start_server(cls.tmp_class, cls.files, Handler)

    @classmethod
    def tearDownClass(cls):
        cls.files.release.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp_class, ignore_errors=True)

    def setUp(self):
        self.files.release.set()        # let go of whatever the test before left waiting
        time.sleep(0.01)
        self.files.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-dl-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.files.release.set)
        self.blob = os.urandom(2 * KiB * KiB + 321)
        self.files.data['blob.bin'] = self.blob
        self.out = io.StringIO()        # what the downloader prints: the setup log

    def patch(self, **values):
        for name, value in values.items():
            p = mock.patch.object(download, name, value)
            p.start()
            self.addCleanup(p.stop)

    def dl(self, control=None, timeout=download.TIMEOUT, **kw):
        return download.Download(f'{self.base}/blob.bin', self.tmp / 'dl' / 'blob.bin', len(self.blob), hashlib.sha256(self.blob).hexdigest(),
                                 control or download.Control(), allowed=ALLOWED, context=self.ctx, timeout=timeout, **kw)

    def ranges(self):
        return [r for _, r in self.files.log]

    def stopped_in_time(self, work, control, when, why='paused', within=3.0):
        """Runs work() in a thread, stops it once when() is true, and returns (what it raised, seconds the stop took)."""
        box = {}

        def run():
            try:
                work()
            except BaseException as e:
                box['error'] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 20
        while not when() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(when(), 'the download never got as far as the test waits for')
        time.sleep(0.3)             # the read is now waiting on a connection that sends nothing
        t0 = time.time()
        control.stop(why)
        t.join(within + 20)
        took = time.time() - t0
        self.assertFalse(t.is_alive(), 'the download did not stop')
        self.assertLess(took, within, f'stopping took {took:.1f} s')
        return box.get('error'), took


class FloorTests(unittest.TestCase):
    """The floors as shipped, fed made-up seconds: what they leave alone matters as much as what they catch."""

    def run_line(self, floor, rate, seconds, step=1.0):
        """True if a line bringing `rate` bytes a second, read every `step` seconds, trips `floor` within `seconds`."""
        t = 0.0
        while t < seconds:
            if floor.add(step, int(rate * step)):
                return t + step
            t += step
        return None

    def test_the_numbers(self):
        self.assertLessEqual(download.STALL_BYTES / download.STALL_WINDOW, 1024)        # at most 1 KiB/s: under GPRS and a 56k modem
        self.assertGreaterEqual(download.STALL_WINDOW, 120)                             # and judged over minutes, not seconds
        self.assertLess(download.GIVE_UP_BYTES / download.GIVE_UP_WINDOW, download.STALL_BYTES / download.STALL_WINDOW)
        self.assertGreaterEqual(download.GIVE_UP_WINDOW, 10 * 60)
        self.assertGreater(download.STALL_WINDOW, 2 * download.TIMEOUT)

    def test_a_slow_line_that_works_is_left_alone(self):
        day = 24 * 3600
        for rate in (1100, 2 * KiB, 5 * KiB, 30 * KiB):         # just above the floor, GPRS, a modem, EDGE
            self.assertIsNone(self.run_line(download._Floor(download.STALL_WINDOW, download.STALL_BYTES), rate, day), rate)
        for rate in (600, 1100, 5 * KiB):                       # 600 B/s is reconnected now and then, never failed
            self.assertIsNone(self.run_line(download._Floor(download.GIVE_UP_WINDOW, download.GIVE_UP_BYTES), rate, day), rate)
        # In bursts: 25 s of nothing (under TIMEOUT), then 100 KiB at once, over and over. 4 KiB/s on average.
        f, tripped = download._Floor(download.STALL_WINDOW, download.STALL_BYTES), False
        for _ in range(2000):
            tripped = tripped or f.add(25, 0) or f.add(0.2, 100 * KiB)
        self.assertFalse(tripped)

    def test_a_trickle_is_caught_after_the_window_and_not_before(self):
        for rate in (0, 50, 500, 900):
            at = self.run_line(download._Floor(download.STALL_WINDOW, download.STALL_BYTES), rate, 3600)
            self.assertIsNotNone(at, rate)
            self.assertGreaterEqual(at, download.STALL_WINDOW)
            self.assertLess(at, download.STALL_WINDOW * 1.2)
        # A good start does not excuse what follows: 10 MiB in ten seconds, then 200 B/s.
        f = download._Floor(download.STALL_WINDOW, download.STALL_BYTES)
        self.assertIsNone(self.run_line(f, KiB * KiB, 10))
        at = self.run_line(f, 200, 3600)
        self.assertTrue(at and at <= download.STALL_WINDOW * 1.2, at)
        # And a trickle that picks up again before the window is out is forgiven.
        f = download._Floor(download.STALL_WINDOW, download.STALL_BYTES)
        self.assertIsNone(self.run_line(f, 200, download.STALL_WINDOW - 20))
        self.assertIsNone(self.run_line(f, 50 * KiB, 3600))

    def test_a_line_that_gets_nowhere_is_given_up_on(self):
        w = download.Watch()
        t = 0
        while t < 3 * download.GIVE_UP_WINDOW and not w.hopeless:
            w.add(10, 1000)             # 100 B/s
            t += 10
        self.assertTrue(w.hopeless)
        self.assertGreaterEqual(t, download.GIVE_UP_WINDOW)
        with self.assertRaises(download.Failed) as e:
            w.check()
        self.assertEqual(e.exception.code, 'network')
        self.assertIn('too slow', e.exception.message)
        # Only waiting on the network counts: a failed try counts its own time and the wait after it, once.
        w = download.Watch()
        w.start()
        w.add(1.0, 10)                  # a second of reading inside the try
        with mock.patch.object(download.time, 'monotonic', lambda at=w._at[0]: at + 5.0):
            w.failed(7)                 # the try took 5 s in all; then a 7 s wait
        self.assertAlmostEqual(w.floor.t, 12.0, places=3)


class StallTests(ServerCase):
    def test_a_trickling_connection_is_dropped_and_the_rest_asked_for(self):
        """300 KB at full speed, then 200 bytes every 50 ms (4 KB/s against a floor of 100 KB/s): dropped after the
        window, and the next connection carries on from the byte reached."""
        self.patch(STALL_WINDOW=0.5, STALL_BYTES=50_000, BACKOFF=(0.05,) * 6)
        self.files.trickle['blob.bin'] = [(300_000, 200, 0.05)]
        t0 = time.time()
        with contextlib.redirect_stdout(self.out):
            p = self.dl().run()
        self.assertEqual(Path(p).read_bytes(), self.blob)
        asked = self.ranges()
        self.assertEqual(len(asked), 2, asked)
        self.assertIsNone(asked[0])
        reached = int(asked[1].split('=')[1].rstrip('-'))
        self.assertGreater(reached, 300_000)            # the trickled bytes were kept too
        self.assertLess(reached, 330_000)
        self.assertLess(time.time() - t0, 10)
        self.assertIn('Stalled', self.out.getvalue())   # and the log says why
        self.assertIn('trying again', self.out.getvalue())

    def test_a_slow_steady_line_above_the_floor_is_not_dropped(self):
        self.patch(STALL_WINDOW=0.3, STALL_BYTES=30_000)        # floor 100 KB/s
        self.files.pace = 0.05                                  # 64 KiB every 50 ms: about 1.2 MB/s, for two seconds
        with contextlib.redirect_stdout(self.out):
            p = self.dl().run()
        self.assertEqual(Path(p).read_bytes(), self.blob)
        self.assertEqual(self.ranges(), [None])
        self.assertEqual(self.out.getvalue(), '')

    def test_a_line_that_only_ever_trickles_fails_plainly_and_keeps_what_came(self):
        """Every connection trickles. Each is dropped and asked again (progress, so never 'kept breaking'), until the
        whole download has brought too little for too long: a 'network' failure, the bytes kept for the next try."""
        self.patch(STALL_WINDOW=0.2, STALL_BYTES=50_000, GIVE_UP_WINDOW=1.2, GIVE_UP_BYTES=10 ** 9, BACKOFF=(0.05,) * 6)
        self.files.trickle['blob.bin'] = [(20_000, 200, 0.02)] * 200
        t0 = time.time()
        d = self.dl()
        with contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual(e.exception.code, 'network')
        self.assertIn('too slow or keeps breaking', e.exception.message)
        self.assertLess(time.time() - t0, 15)
        self.assertGreater(len(self.ranges()), 2)               # it did try again before giving up
        kept = os.path.getsize(d.part)
        self.assertGreater(kept, 20_000)
        self.files.trickle.clear()
        self.files.log.clear()
        with contextlib.redirect_stdout(self.out):
            p = self.dl().run()                                 # Try again: from the bytes on disk
        self.assertEqual(self.ranges(), [f'bytes={kept}-'])
        self.assertEqual(Path(p).read_bytes(), self.blob)

    def test_a_silent_connection_still_times_out_and_is_asked_again(self):
        self.patch(BACKOFF=(0.05,) * 6)
        self.files.silent['blob.bin'] = [500_000]
        with contextlib.redirect_stdout(self.out):
            p = self.dl(timeout=0.5).run()
        self.assertEqual(Path(p).read_bytes(), self.blob)
        self.assertEqual(self.ranges(), [None, 'bytes=500000-'])

    def test_a_dead_line_still_fails_after_its_tries(self):
        self.patch(BACKOFF=(0.02,) * 6)
        self.files.status['blob.bin'] = [503] * 50
        with contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
            self.dl().run()
        self.assertEqual(e.exception.code, 'network')
        self.assertIn('kept breaking', e.exception.message)
        self.assertEqual(len(self.ranges()), download.NETWORK_TRIES)


class StopTests(ServerCase):
    """Pause and cancel while the connection sends nothing: the socket timeout is 30 s and must not be waited for."""

    def test_pause_during_a_silent_download(self):
        self.files.silent['blob.bin'] = [400_000]
        control, seen = download.Control(), [0]
        d = self.dl(control, progress=lambda done, total, phase: seen.append(done))
        error, took = self.stopped_in_time(d.run, control, lambda: seen[-1] >= 400_000)
        self.assertIsInstance(error, download.Stopped)
        self.assertEqual(error.why, 'paused')
        self.assertEqual(d.done_bytes(), 400_000)               # what came is on disk for the resume
        self.files.release.set()
        self.files.log.clear()
        p = self.dl().run()
        self.assertEqual(self.ranges(), ['bytes=400000-'])
        self.assertEqual(Path(p).read_bytes(), self.blob)

    def test_cancel_during_a_silent_part_request(self):
        self.files.silent['blob.bin'] = [1000]
        control, got = download.Control(), []
        work = lambda: download.fetch_range(f'{self.base}/blob.bin', 0, 50_000, control, allowed=ALLOWED, context=self.ctx, got=got.append)
        error, took = self.stopped_in_time(work, control, lambda: sum(got) >= 1000, why='cancelled')
        self.assertIsInstance(error, download.Stopped)
        self.assertEqual(error.why, 'cancelled')
        self.assertEqual(len(self.ranges()), 1)                 # not taken for a broken connection and asked again

    def test_pause_before_the_answer_begins(self):
        """The server has the request and says nothing at all: stop() hangs up while the headers are waited for."""
        self.files.data['quiet'] = b''
        seen = threading.Event()
        real = Handler.do_GET

        def quiet(handler):
            if handler.path == '/quiet':
                seen.set()
                self.files.release.wait(30)
                handler.close_connection = True
                return
            real(handler)
        with mock.patch.object(Handler, 'do_GET', quiet):
            control = download.Control()
            work = lambda: download.fetch_small(f'{self.base}/quiet', allowed=ALLOWED, context=self.ctx, timeout=30, control=control)
            error, took = self.stopped_in_time(work, control, seen.is_set)
        self.assertIsInstance(error, download.Stopped)

    def test_a_stop_before_it_starts_connects_to_nothing(self):
        control = download.Control()
        control.stop('paused')
        with self.assertRaises(download.Stopped):
            download.fetch_range(f'{self.base}/blob.bin', 0, 10, control, allowed=ALLOWED, context=self.ctx)
        self.assertEqual(self.files.log, [])
        control = download.Control()
        self.assertEqual(download.fetch_range(f'{self.base}/blob.bin', 5, 10, control, allowed=ALLOWED, context=self.ctx), self.blob[5:15])
        self.assertIsNone(control._conn)                        # nothing held once the request is over


class PartRequestTests(ServerCase):
    """fetch_range, which the map is cut with."""

    def get(self, start, length, **kw):
        return download.fetch_range(f'{self.base}/blob.bin', start, length, kw.pop('control', None) or download.Control(),
                                    allowed=ALLOWED, context=self.ctx, **kw)

    def test_a_part_that_breaks_half_way_carries_on_from_there_and_says_so(self):
        self.patch(BACKOFF=(0.02,) * 6)
        self.files.cut['blob.bin'] = [300_000, 200_000]
        got = []
        with contextlib.redirect_stdout(self.out):
            data = self.get(1000, 900_000, got=got.append)
        self.assertEqual(data, self.blob[1000:901_000])
        self.assertEqual(self.ranges(), ['bytes=1000-900999', 'bytes=301000-900999', 'bytes=501000-900999'])
        self.assertEqual(sum(got), 900_000)
        self.assertTrue(all(n > 0 for n in got))
        log = self.out.getvalue()
        self.assertEqual(log.count('a map part broke'), 2)      # a retry is in the log, as a download's is
        self.assertIn('at 300000 of 900000 (from byte 1000)', log)

    def test_progress_between_breaks_is_not_counted_as_failing(self):
        """More breaks than NETWORK_TRIES, each after some bytes: the part still arrives."""
        self.patch(BACKOFF=(0.01,) * 6)
        self.files.cut['blob.bin'] = [50_000] * (download.NETWORK_TRIES + 3)
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(self.get(0, 600_000), self.blob[:600_000])
        self.assertEqual(len(self.ranges()), download.NETWORK_TRIES + 4)

    def test_tries_that_bring_nothing_end_it(self):
        self.patch(BACKOFF=(0.01,) * 6)
        self.files.cut['blob.bin'] = [0] * 50
        with contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
            self.get(0, 1000)
        self.assertEqual(e.exception.code, 'network')
        self.assertEqual(len(self.ranges()), download.NETWORK_TRIES)
        self.assertEqual(self.out.getvalue().count('a map part broke'), download.NETWORK_TRIES - 1)

    def test_parts_that_each_arrive_on_the_second_try_do_not_run_for_ever(self):
        """The retry count starts again with every part; the map's one Watch does not."""
        self.patch(BACKOFF=(0.05,) * 6, GIVE_UP_WINDOW=0.6, GIVE_UP_BYTES=10 ** 9)
        watch, parts = download.Watch(), 0
        with contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
            for parts in range(200):
                self.files.status['blob.bin'] = [503]
                self.get(parts * 100, 100, watch=watch)
        self.assertEqual(e.exception.code, 'network')
        self.assertIn('too slow or keeps breaking', e.exception.message)
        self.assertGreater(parts, 3)
        self.assertLess(parts, 60)

    def test_a_trickling_part_is_dropped_and_finished_on_a_new_connection(self):
        self.patch(STALL_WINDOW=0.4, STALL_BYTES=50_000, BACKOFF=(0.02,) * 6)
        self.files.trickle['blob.bin'] = [(100_000, 100, 0.05)]
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(self.get(0, 400_000), self.blob[:400_000])
        asked = self.ranges()
        self.assertEqual(len(asked), 2)
        self.assertGreater(int(asked[1].split('=')[1].split('-')[0]), 100_000)
        self.assertIn('Stalled', self.out.getvalue())

    def test_the_rules_of_the_way_out_still_hold(self):
        """A redirect off the allowed hosts is refused without a second connection, and a server that is not the one
        the roots vouch for is refused at once, not asked six times."""
        self.files.redirect['moved'] = 'https://example.com/blob.bin'
        with self.assertRaises(download.Failed) as e:
            download.fetch_range(f'{self.base}/moved', 0, 10, download.Control(), allowed=ALLOWED, context=self.ctx)
        self.assertIn('does not know', e.exception.message)
        self.assertEqual(len(self.files.log), 1)
        self.assertIsNone(download.fetch_small(f'{self.base}/moved', allowed=ALLOWED, context=self.ctx))
        self.files.log.clear()
        with self.assertRaises(download.Failed) as e:
            download.fetch_range(f'{self.base}/blob.bin', 0, 10, download.Control(), allowed=ALLOWED, context=download.tls_context())
        self.assertIn('could not confirm', e.exception.message)
        self.assertEqual(self.files.log, [])
        with self.assertRaises(download.Failed):
            download.fetch_range('http://127.0.0.1/blob.bin', 0, 10, download.Control(), allowed=ALLOWED, context=self.ctx)

    def test_another_part_than_the_one_asked_for_is_refused(self):
        real = download._content_range
        with mock.patch.object(download, '_content_range', lambda v: (real(v)[0] + 1, real(v)[1])), self.assertRaises(download.Failed):
            self.get(100, 10)


class SmallTests(ServerCase):
    def small(self, name='blob.bin', **kw):
        return download.fetch_small(f'{self.base}/{name}', allowed=ALLOWED, context=self.ctx, **kw)

    def test_whole_or_nothing(self):
        self.files.data['s'] = b'x' * 5000
        self.assertEqual(self.small('s'), b'x' * 5000)
        self.assertEqual(self.small('s', limit=5000), b'x' * 5000)
        self.assertIsNone(self.small('s', limit=4999))
        self.assertIsNone(self.small('missing'))
        self.files.short['s'] = [1200]              # cut short: the headers promised 5000
        self.assertIsNone(self.small('s'))
        self.assertEqual(self.small('s'), b'x' * 5000)

    def test_a_trickling_or_silent_answer_is_given_up_on(self):
        self.patch(STALL_WINDOW=0.4, STALL_BYTES=50_000)
        self.files.trickle['blob.bin'] = [(1000, 50, 0.05)]
        t0 = time.time()
        self.assertIsNone(self.small(limit=10 ** 7))
        self.assertLess(time.time() - t0, 5)
        self.files.silent['blob.bin'] = [1000]
        self.assertIsNone(self.small(limit=10 ** 7, timeout=0.4))


class DiskTests(ServerCase):
    """This computer's own disk failing is said to be that, at once: not retried and reported as the internet."""

    def test_a_write_that_fails_is_not_a_broken_connection(self):
        self.patch(BACKOFF=(0.05,) * 6, SAVE_EVERY=0.0)
        self.files.pace = 0.01
        real, calls = download.write_json, []

        def save(path, d):
            calls.append(path)
            if len(calls) > 2:
                raise PermissionError(errno.EACCES, 'Permission denied', path)
            return real(path, d)
        d = self.dl()
        with mock.patch.object(download, 'write_json', save), contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual(e.exception.code, 'disk')
        self.assertIn(str(self.tmp / 'dl'), e.exception.message)
        self.assertIn('PermissionError', e.exception.message)
        self.assertNotIn('internet', e.exception.message)
        self.assertEqual(len(self.ranges()), 1)             # one request: no tries after it
        self.assertIn('PermissionError', self.out.getvalue())
        self.assertGreater(d.done_bytes(), 0)               # and what was written stays for the next try
        self.files.log.clear()
        self.files.pace = 0
        self.assertEqual(Path(self.dl().run()).read_bytes(), self.blob)
        self.assertTrue(self.ranges()[0].startswith('bytes='))

    def test_a_file_that_cannot_be_written_to(self):
        real = os.fsync
        for error, code in ((OSError(errno.EIO, 'Input/output error'), 'disk'), (OSError(errno.EROFS, 'Read-only file system'), 'disk'),
                            (OSError(errno.ENOSPC, 'No space left on device'), 'no_space')):
            self.files.log.clear()
            shutil.rmtree(self.tmp / 'dl', ignore_errors=True)

            def fsync(fd, error=error):
                raise error
            with mock.patch.object(download.os, 'fsync', fsync), contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
                self.dl().run()
            self.assertEqual(e.exception.code, code, error)
            self.assertEqual(len(self.ranges()), 1, error)
        self.assertIs(os.fsync, real)

    def test_a_folder_that_cannot_be_made(self):
        (self.tmp / 'dl').write_bytes(b'a file where the downloads folder should be')
        with contextlib.redirect_stdout(self.out), self.assertRaises(download.Failed) as e:
            self.dl().run()
        self.assertEqual(e.exception.code, 'disk')
        self.assertEqual(self.files.log, [])                # nothing was asked of the server

    def test_a_broken_connection_is_still_one(self):
        self.patch(BACKOFF=(0.02,) * 6)
        self.files.cut['blob.bin'] = [700_000]
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(Path(self.dl().run()).read_bytes(), self.blob)
        self.assertEqual(self.ranges(), [None, 'bytes=700000-'])
        self.assertIn('download broke at 700000', self.out.getvalue())


if __name__ == '__main__':
    unittest.main()
