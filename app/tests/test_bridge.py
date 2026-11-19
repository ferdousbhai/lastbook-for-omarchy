#!/usr/bin/env python3
"""The bridge with no window: streaming, cancel, lanes, every refusal.

  python3 -B app/tests/test_bridge.py [-v]

Uses the made-up library in derived/app-build/testdata (no model) and a stub for chat. Never packaged."""
import faulthandler, hashlib, json, os, shutil, socket, statistics, subprocess, sys, threading, time, types, unittest
from pathlib import Path

sys.dont_write_bytecode = True
faulthandler.dump_traceback_later(600, exit=True)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
DATA = fixture.ensure()
os.environ['LASTBOOK_DATA'] = str(DATA)
os.environ['LASTBOOK_LOCAL_AI'] = '0'   # no model: the local AI is never looked for, checked or started here
os.environ['LASTBOOK_RECALL_SOCKET'] ='/nonexistent/recall.sock'   # no embedding model: meaning-based search stays off

import server, assistant  # noqa: E402
import bridge as B  # noqa: E402

LISTENS = []
_real_listen = socket.socket.listen


def _no_listen(self, *a):
    LISTENS.append(self)
    return _real_listen(self, *a)


socket.socket.listen = _no_listen
server.init(no_index=True)
assert server.INDEX.query_vector('water') is None, 'meaning-based search is on; refusing to run here'

MiB = B.MiB
A = '/videos/a.mp4'
pat = fixture.pattern
OBSERVED = {}   # numbers worth printing at the end


def fixture_pdf():
    """Three real pages and a script that would run on opening, were anything to open it with a script engine. Written by hand: a PDF is plain enough."""
    objs = [b'<</Type/Catalog/Pages 2 0 R/OpenAction<</S/JavaScript/JS(app.alert("ran"))>>>>', b'<</Type/Pages/Count 3/Kids[3 0 R 4 0 R 5 0 R]>>'] + [b'<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 400]>>'] * 3
    out, at = bytearray(b'%PDF-1.4\n'), []
    for i, o in enumerate(objs, 1):
        at.append(len(out))
        out += b'%d 0 obj\n' % i + o + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + b''.join(b'%010d 00000 n \n' % a for a in at)
    out += b'trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)
    return bytes(out)


class TestHandler(server.Handler):
    """The real Handler plus slow and odd routes, in the lanes they would land in. Lives only here."""
    seen = []
    slow_started = []

    def do_GET(self):
        p, _, q = self.path.partition('?')
        q = dict(x.split('=') for x in q.split('&') if '=' in x)
        if p in ('/api/t/slow', '/documents/t/slow'):
            TestHandler.slow_started.append(p)
            time.sleep(int(q.get('ms', 500)) / 1000)
            return self.send_json({'slept': True})
        if p == '/zim/t/pdf':
            return self.send_data(b'%PDF-1.4 inside an encyclopedia', 'application/pdf')
        if p == '/documents/t/sized':
            n = int(q.get('bytes', B.BIG_BODY + 1))
            self.send_response(200)
            self.send_header('Content-Length', str(n))
            self.end_headers()
            for off in range(0, n, MiB):
                self.wfile.write(b'x' * min(MiB, n - off))
            return
        if p == '/documents/t/nolen':
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            try:
                for _ in range(int(q.get('mb', 40))):
                    self.wfile.write(b'x' * MiB)
                    time.sleep(int(q.get('ms', 0)) / 1000)
            except server.library.CLIENT_GONE:
                pass
            return
        if p == '/documents/t/manywrites':
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(3 * MiB))
            self.end_headers()
            self.wfile.write(b'y' * (3 * MiB))   # one write bigger than a chunk
            return
        if p == '/documents/t/short':
            self.send_response(200)
            self.send_header('Content-Length', '1000')
            self.end_headers()
            self.wfile.write(b'z' * 10)
            return
        if p == '/documents/t/redirect':
            self.send_response(302)
            self.send_header('Location', '/documents/plain.txt?a="<b>')
            self.end_headers()
            return
        if p == '/api/t/echo':
            return self.send_json({'headers': {k.lower(): v for k, v in self.headers.items()}, 'path': self.path})
        if p == '/api/t/uncaught':
            raise RuntimeError('deliberate')
        if p == '/api/t/boom':
            try:
                raise RuntimeError('deliberate ' + str(ROOT))
            except RuntimeError:
                return self.internal_error()
        return super().do_GET()


MOD = types.SimpleNamespace(Handler=TestHandler, catalog=server.catalog, render_page=server.render_page, _pdf_lock=server._pdf_lock)


class Rec(B.Sink):
    """hold: an Event. Once `after` pieces are in, data() waits for it, as a write to a window that has stopped reading would."""

    def __init__(self, hold=None, after=1):
        self.hold, self.after, self.status, self.reason, self.hs = hold, after, None, None, {}
        self.parts, self.times = [], []
        self.ok = None
        self.done = threading.Event()
        self.first = threading.Event()

    def head(self, status, reason, headers):
        self.status, self.reason, self.hs = status, reason, {k.lower(): v for k, v in headers}
        self.raw_hs = headers

    def data(self, chunk):
        self.parts.append(chunk)
        self.times.append(time.time())
        self.first.set()
        if self.hold and len(self.parts) >= self.after:
            self.hold.wait(30)

    def finish(self, ok):
        self.ok = ok
        self.done.set()

    @property
    def body(self):
        return b''.join(self.parts)


class Res:
    def __init__(self, status, hs, body, ok=True):
        self.status, self.hs, self.body, self.ok = status, hs, body, ok


def call(b, method, path, headers=None, body=None, timeout=60, url=None):
    url = url if url is not None else b.origin + path
    r = Rec()
    b.start(method, url, headers or {}, body, r)
    assert r.done.wait(timeout), f'no answer: {method} {path}'
    return Res(r.status, r.hs, r.body, r.ok)


def wait_until(fn, timeout=10):
    t = time.time() + timeout
    while time.time() < t:
        if fn():
            return True
        time.sleep(0.01)
    return False


class Both:
    """The tests StreamTests runs on a fresh bridge each."""

    def setUp(self):
        self.b = B.Bridge(MOD)
        self.json = {'Content-Type': 'application/json'}
        self.piece = B.VIDEO_PIECE

    def get(self, path, **k):
        return call(self.b, 'GET', path, {}, **k)

    # ---- a video through the bridge: its first piece and no more (the window plays the library's videos from disk)
    def test_video_gets_first_piece(self):
        r = self.get(A)
        self.assertEqual(r.status, 206)
        self.assertEqual(len(r.body), self.piece)
        self.assertEqual(hashlib.sha256(r.body).digest(), hashlib.sha256(pat(0, self.piece)).digest())

    def test_video_range_from_a_caller_is_not_passed_on(self):
        r = call(self.b, 'GET', A, {'Range': 'bytes=1000-1999'})
        self.assertEqual(len(r.body), self.piece)
        self.assertEqual(r.body[:1000], pat(0, 1000))

    def test_video_other_types_and_case(self):
        self.assertEqual(self.get('/videos/b.webm').status, 206)
        self.assertEqual(self.get('/videos/huge.mkv').status, 206)
        self.assertEqual(len(self.get('/videos/huge.mkv').body), self.piece)

    # ---- a PDF never reaches the web view: it becomes pictures
    def real_pdf(self):
        path = DATA / 'library-data' / 'documents' / 'real.pdf'
        if not path.exists():
            path.write_bytes(fixture_pdf())
        return '/documents/real.pdf'

    def test_pdf_file_is_never_handed_over(self):
        r = self.get(self.real_pdf())
        self.assertEqual(r.status, 200)
        self.assertTrue(r.hs['content-type'].startswith('text/html'))
        self.assertNotIn(b'%PDF', r.body)

    def test_pdf_viewer_lists_pages_and_runs_nothing(self):
        b = self.get(self.real_pdf()).body
        self.assertEqual(b.count(b'class="p"'), 3)
        self.assertIn(b'id="page=3"', b)
        self.assertIn(b'src="?pdf-page=2"', b)
        self.assertNotIn(b'<script', b.lower())
        self.assertNotRegex(b.split(b'</style>')[1], rb'\son\w+=')

    def test_pdf_page_is_a_picture(self):
        r = self.get(self.real_pdf() + '?pdf-page=2')
        self.assertEqual(r.status, 200)
        self.assertEqual(r.hs['content-type'], 'image/png')
        self.assertEqual(r.body[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(self.get(self.real_pdf() + '?pdf-page=9').status, 404)
        self.assertEqual(self.get(self.real_pdf() + '?pdf-page=0').status, 404)

    def test_pdf_pictures_have_their_own_lane(self):
        self.assertEqual(B._lane_for('GET', '/documents/real.pdf', '/documents/real.pdf?pdf-page=2'), 'render')
        self.assertEqual(B._lane_for('GET', '/documents/real.pdf', '/documents/real.pdf'), 'large')

    def test_damaged_pdf_shows_a_notice_and_not_its_bytes(self):
        for p in ('/documents/s.pdf', '/documents/big.pdf'):
            r = self.get(p)
            self.assertEqual(r.status, 404)
            self.assertEqual(r.body, B.PDF_NOTICE)

    def test_pdf_elsewhere_is_refused_by_its_type(self):
        r = self.get('/zim/t/pdf')
        self.assertEqual(r.status, 404)
        self.assertEqual(r.body, B.PDF_NOTICE)

    # ---- every refusal
    def test_refused_host(self):
        for url in ('https://evil.example/', 'lastbook://other/', self.b.origin + '.evil/', self.b.origin + '@evil/', 'file:///etc/hosts', 'about:blank', ''):
            r = call(self.b, 'GET', '', url=url)
            self.assertEqual(r.status, 400, url)

    def test_refused_target(self):
        for t in ('//evil/', '/a b', '/a\nb', '/a\x00b', '/a\tb', '/\x7f'):
            self.assertEqual(self.get(t).status, 400, repr(t))

    def test_refused_method(self):
        for m in ('HEAD', 'PUT', 'DELETE', 'OPTIONS', 'TRACE', 'PATCH', 'CONNECT', 'get'):
            self.assertEqual(call(self.b, m, '/').status, 405, m)

    def test_post_too_big(self):
        self.assertEqual(call(self.b, 'POST', '/api/chat', self.json, b'x' * (B.MAX_POST + 1)).status, 413)

    def test_path_attacks(self):
        attacks = ['/../canary-root.txt', '/documents/../canary-root.txt', '/%2e%2e/canary-root.txt', '/documents/%2e%2e/%2e%2e/outside-canary.txt', '/canary-root.txt',
                   '/video-auth.json', '/documents/video-auth-copy.json', '/documents/x.info.json', '/documents/.hidden', '/tools/secret.txt', '/logs/x.log', '/catalog.json',
                   '/search.sqlite', '/documents/hl.txt', '/pages/hl2.txt', '/zim/../canary-root.txt', '/zim/%2e%2e%2fcanary-root.txt/x', '/ui/../server.py',
                   '/ui/%2e%2e/server.py', '/documents%2f..%2f..%2fcanary-root.txt', '/documents/..\\canary-root.txt', '/documents/plain.txt/../../canary-root.txt', '/assets/../tools/secret.txt']
        for t in attacks:
            r = self.get(t)
            self.assertGreaterEqual(r.status, 400, t)
            self.assertNotIn(fixture.CANARY, r.body, t)
        r = self.get('/?x=../canary-root.txt')
        self.assertEqual(r.status, 404)
        self.assertNotIn(fixture.CANARY, r.body)

    def test_symlink_out_of_the_library_is_refused(self):
        """A link from a served folder to a file outside the library."""
        self.assertTrue(os.path.isfile(fixture.LIB / 'documents' / 'out.txt'))
        r = self.get('/documents/out.txt')
        self.assertGreaterEqual(r.status, 400)
        self.assertNotIn(fixture.CANARY, r.body)

    def test_hard_link_refused_but_plain_files_served(self):
        self.assertEqual(os.stat(fixture.LIB / 'documents' / 'hl.txt').st_nlink, 2)
        self.assertEqual(self.get('/documents/hl.txt').status, 404)
        self.assertEqual(self.get('/pages/hl2.txt').status, 404)
        self.assertEqual(self.get('/documents/plain.txt').status, 200)

    def test_only_two_headers_reach_the_engine(self):
        r = call(self.b, 'GET', '/api/t/echo', {'X-Lastbook-Key': 'secret', 'Origin': 'null', 'Cookie': 'sg_key=1', 'Host': 'evil.example', 'X-Forwarded-For': '1.2.3.4', 'Accept': 'x/y', 'Range': 'bytes=0-1', 'Bad': 'a\r\nInjected: 1', 'Accept\r\nInjected': '2'})
        seen = json.loads(r.body)['headers']
        self.assertEqual(seen['host'], 'localhost')
        self.assertEqual(seen['accept'], 'x/y')
        for h in ('x-lastbook-key', 'origin', 'cookie', 'x-forwarded-for', 'range', 'bad', 'injected'):
            self.assertNotIn(h, seen)

    def test_non_ascii_target(self):
        r = self.get('/documents/پ.txt')
        self.assertEqual(r.status, 404)   # no such file, and no crash on the way

    # ---- the flood gate
    def test_gate_refuses_an_answer_over_the_limit(self):
        r = self.get('/documents/t/sized')
        self.assertEqual(r.status, 403)
        self.assertEqual(r.body, B.TOO_LARGE)

    def test_gate_size_edge(self):
        self.assertEqual(self.get('/documents/t/sized?bytes=%d' % (B.BIG_BODY + 1)).status, 403)
        r = self.get('/documents/t/sized?bytes=%d' % B.BIG_BODY)
        self.assertEqual(r.status, 200)
        self.assertEqual(len(r.body), B.BIG_BODY)
        self.assertEqual(self.get('/documents/t/sized?bytes=%d' % (B.WHOLE_MAX + 1)).status, 413)

    def test_a_page_the_size_of_the_largest_real_one_opens(self):
        self.assertGreater(B.BIG_BODY, 44 * 1000 * 1000)
        r = self.get('/documents/big.bin')     # 40 MiB
        self.assertEqual(r.status, 200)
        self.assertEqual(len(r.body), 40 * MiB)
        self.assertEqual(r.body[:1000], pat(0, 1000))
        self.assertEqual(self.get('/documents/over.bin').status, 200)
        self.assertEqual(self.get('/documents/mid.bin').status, 200)

    def test_gate_catches_a_video_the_engine_does_not_recognise(self):
        r = self.get('/videos/huge.mk%76')   # the engine's own Range path looks at the raw address, so it answers whole
        self.assertEqual(r.status, 403)

    def test_rewrite_takes_linear_time_on_tags_that_never_close(self):
        """A whole page against 1/64 of one. In step with its size the whole page takes 64 times as long; quadratic, 4096 times. A limit in seconds
        says more about the computer, and what else it is doing, than about rewrite: at 5 s this failed two runs in five on a busy laptop, on
        '<a x>', which takes 2.2 s there when the laptop is idle. The eleven other pages take 0.1 to 0.5 s."""
        from engine import saved_pages as P

        def took(data):
            t0 = time.perf_counter()
            P.rewrite(data, lambda src: b'WEBVTT\n' + b'<' * P.MAX_TRANSCRIPT)
            return time.perf_counter() - t0

        for unit in (b'<button x>', b'<a x>', b'<a ', b'<head ', b'<track src="a.vtt ', b'<track ', b'<div class="image-gallery" data-base-url="x">', b'<div class="image-gallery ',
                     b'<!doctype ', b'<a href=x target=y download ', b'<div class="image-gallery" data-base-url="x"><a ', b'<tr><td><small>A1</small> </td>'):
            small = min(took(unit * (P.MAX_PAGE // 64 // len(unit))) for _ in range(5))     # the quietest of five: other work on the computer only ever adds time
            whole = took(unit * (P.MAX_PAGE // len(unit)))
            self.assertLess(whole, 16 * 64 * small, (unit, small, whole))     # measured 2 to 3 times in step when idle, 6 with every core busy; quadratic is 64

    def test_rewrite_blank_rows_only_when_too_large(self):
        from engine import saved_pages as P
        blank = b'<tr><td><small>A1</small> </td><td><small>B1</small> </td></tr>'
        real = b'<tr><td><small>A2</small> water</td><td><small>B2</small> </td></tr>'
        small = b'<table>' + blank + real + b'</table>'
        self.assertIn(blank, P.rewrite(small))
        big = P.rewrite(b'<html><head></head><table>' + blank * (P.MAX_PAGE // len(blank) + 1) + real + b'</table>')
        self.assertNotIn(blank, big)
        self.assertIn(real, big)
        self.assertIn(P.CSS, big)
        full = b'<html><head></head>' + b'x' * P.MAX_PAGE
        self.assertEqual(P.rewrite(full), full)

    def test_unknown_length_over_the_limit(self):
        r = self.get('/documents/t/nolen?mb=%d' % (B.BIG_BODY // MiB + 8))
        self.assertEqual(r.status, 200)
        self.assertFalse(r.ok)
        self.assertLessEqual(len(r.body), B.BIG_BODY + MiB)

    # ---- what stays the engine's
    def test_library_pages_of_every_kind_are_answered(self):
        d = fixture.LIB / 'pages' / 'kinds'
        (d / 'folder').mkdir(parents=True, exist_ok=True)
        (d / 'folder' / 'index.html').write_bytes(b'<p>in a folder')
        (d / 'nosuffix').write_bytes(b'<script>1</script>')
        (d / 'upper.HTML').write_bytes(b'<script>1</script>')
        (d / 'x.xhtml').write_bytes(b'<html xmlns="http://www.w3.org/1999/xhtml"><script>1</script></html>')
        for t in ('/pages/s.html', '/pages/a.svg', '/pages/kinds/nosuffix', '/pages/kinds/upper.HTML', '/pages/kinds/x.xhtml', '/pages/kinds/folder/', '/pages/kinds/folder/index.html', '/%70ages/kinds/nosuffix'):
            self.assertEqual(self.get(t).status, 200, t)
        for t in ('/pages/kinds/none.html', '/pages/kinds/none', '/zim/none.zim/A/x', '/zim/none.zim/', '/zim/'):
            self.assertEqual(self.get(t).status, 404, t)
        r = self.get('/pages/kinds/folder')     # a folder with no slash: a redirect, answered as a page that moves itself
        self.assertEqual(r.status, 200)
        self.assertIn(b'http-equiv="refresh"', r.body)
        self.assertNotIn(b'<script', r.body)

    def test_a_library_page_may_be_called_tools(self):
        for rel in ('courses/c1/resources/tools/index.html', 'pages/work/index.html', 'documents/logs/index.html', 'assets/runtime/index.html', 'metadata/videos/tools/index.html', 'collections/tools/index.html'):
            p = fixture.LIB / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'<!doctype html><title>t</title><script>1</script><p>a real page')
            for t in ('/' + rel, '/' + rel[:-len('index.html')]):
                r = self.get(t)
                self.assertEqual(r.status, 200, t)
                self.assertIn(b'a real page', r.body, t)
        hidden = fixture.LIB / 'courses' / 'c1' / 'tools' / '.Data'
        hidden.mkdir(parents=True, exist_ok=True)
        (hidden / 'x.txt').write_bytes(fixture.CANARY)
        (fixture.LIB / 'courses' / 'c1' / '.git').mkdir(exist_ok=True)
        (fixture.LIB / 'courses' / 'c1' / '.git' / 'config').write_bytes(fixture.CANARY)
        for t in ('/courses/c1/tools/.Data/x.txt', '/courses/c1/.git/config', '/tools/secret.txt', '/TOOLS/secret.txt', '/logs/x.log', '/courses/../tools/secret.txt', '/courses/c1/%2e%2e/%2e%2e/tools/secret.txt'):
            r = self.get(t)
            self.assertGreaterEqual(r.status, 400, t)
            self.assertNotIn(fixture.CANARY, r.body, t)

    def test_a_link_inside_a_served_folder_is_refused(self):
        """A link from a served folder to the library's own tools folder, in its own test: it needs the right to make a link, so where that right is missing this says so instead of checking an address that is simply absent."""
        link, target = fixture.LIB / 'courses' / 'c1' / 'tools-link', fixture.LIB / 'tools'
        link.parent.mkdir(parents=True, exist_ok=True)
        usable = lambda: link.is_symlink() and os.path.isfile(link / 'secret.txt')
        why = 'the link does not resolve to the tools folder'
        if not usable():
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                os.symlink(target, link)
            except OSError as e:
                why = f'{type(e).__name__}: {e}'     # e.g. no right to make links on this file system
        if not usable():
            self.skipTest(f'could not make a usable link to the tools folder ({why})')
        r = self.get('/courses/c1/tools-link/secret.txt')
        self.assertGreaterEqual(r.status, 400)
        self.assertNotIn(fixture.CANARY, r.body)

    def test_page_with_a_connection_hint_loses_it(self):
        d = fixture.LIB / 'pages' / 'kinds'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'hints.html').write_bytes(b'''<!doctype html><html><head><link rel=preconnect href="http://x.invalid"><link href="//y.invalid" rel="dns-prefetch"><LINK REL='Prefetch' href=/z>
<link rel="prerender" href=/q><link rel="stylesheet" href="keep.css"><link rel="canonical" href="/keep"></head><body><p>rel=preconnect stays as text</p></body></html>''')
        r = self.get('/pages/kinds/hints.html')
        self.assertEqual(r.status, 200)
        low = r.body.lower()
        for gone in (b'x.invalid', b'y.invalid', b'prerender', b"'prefetch'"):
            self.assertNotIn(gone, low)
        for kept in (b'keep.css', b'href="/keep"', b'rel=preconnect stays as text'):
            self.assertIn(kept, low)

    def test_saved_page_comes_with_what_its_scripts_unfolded(self):
        d = fixture.LIB / 'pages' / 'kinds'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'talk.vtt').write_bytes(b'WEBVTT\n\n00:01.000 --> 00:02.000\nHello <b>there</b> & welcome\n\n00:02.000 --> 00:03.000\nHello <b>there</b> & welcome\n')
        page = (b'<!doctype html><html><head><title>t</title></head><body><a href="x.pdf" target="_blank" download>f</a><a href="https://e.example/" target="_blank">w</a>'
                b'<button data-toggle="offcanvas" data-target="#menu">m</button><video><track src="talk.vtt"></video>'
                b'<video><track src="../../../secret.vtt"></video></body></html>')
        (d / 'ocw.html').write_bytes(page)
        (d / 'index.html').write_bytes(page)
        for t in ('/pages/kinds/ocw.html', '/pages/kinds/', '/pages/kinds/index.html'):
            r = self.get(t)
            self.assertEqual(r.status, 200, t)
            self.assertEqual(int(r.hs['content-length']), len(r.body), t)
            b = r.body
            self.assertLess(b.lower().index(b'<head>'), b.index(b'<style>'))
            self.assertIn(b'.collapse', b)
            self.assertRegex(b, rb'<a href="x.pdf"\s*>')
            self.assertIn(b'https://e.example/" target="_blank"', b)    # still does nothing, but the page is not changed for it
            self.assertIn(b'<a href="#menu"', b)
            self.assertEqual(b.count(b'Hello there &amp; welcome'), 1)
            self.assertNotIn(b'<script', b)
        self.assertEqual(self.get('/pages/s.html').status, 200)

    def test_saved_page_transcript_stays_in_the_library(self):
        self.assertIsNone(self.part('/pages/kinds/ocw.html', '../../../canary-root.txt'))
        self.assertIsNone(self.part('/pages/kinds/ocw.html', 'https://evil.example/a.vtt'))
        self.assertIsNone(self.part('/pages/kinds/ocw.html', '//evil.example/a.vtt'))
        self.assertIsNone(self.part('/pages/kinds/ocw.html', '/api/topics'))

    def part(self, page, src):
        h = server.Handler.__new__(server.Handler)
        h.path, h.directory = page, str(fixture.LIB)
        return h.saved_page_part(src)

    def test_no_page_of_the_app_itself(self):
        for t in ('/', '/index.html', '/sw.js', '/ui/app.js', '/ui/setup.html', '/api/map/info', '/api/tools'):
            self.assertEqual(self.get(t).status, 404, t)
        self.assertEqual(call(self.b, 'POST', '/api/tool/search', self.json, b'{}').status, 404)

    def test_no_server_or_date_header(self):
        r = self.get('/api/doc/pdfsmall')
        self.assertEqual(r.status, 200)
        self.assertNotIn('server', r.hs)
        self.assertNotIn('date', r.hs)

    def test_engine_error_is_an_answer(self):
        r = self.get('/api/t/boom')
        self.assertEqual(r.status, 500)
        self.assertNotIn(str(ROOT).encode(), r.body)
        self.assertEqual(self.get('/api/t/uncaught').status, 502)   # an error the Handler let out still becomes an answer

    def test_ordinary_api(self):
        self.assertEqual(self.get('/api/health').status, 404)   # gone: nothing asks whether the engine is up, the window reads its "ready" line
        self.assertEqual(self.get('/reader.css').status, 200)
        self.assertEqual(self.get('/api/nope').status, 404)
        r = json.loads(self.get('/api/find?q=boil').body)
        self.assertTrue(r)
        self.assertEqual(json.loads(self.get('/api/doc/pdfsmall').body)['id'], 'pdfsmall')

    def test_short_answer(self):
        r = self.get('/documents/t/short')
        self.assertEqual(r.status, 200)
        self.assertFalse(r.ok)


class AssistantStaysHere(unittest.TestCase):
    def test_the_assistant_has_no_way_to_a_model_server(self):
        """The model is the local AI over pipes, or none: neither file can open a connection."""
        for name in ('assistant.py', 'llm_local.py'):
            src = (ROOT / name).read_text(encoding='utf-8')
            for word in ('urllib', 'http.client', 'import socket', 'socket.socket', 'create_connection', 'http://', 'https://'):
                self.assertNotIn(word, src, name)

    def test_no_model_means_not_online(self):
        st = REAL_STATUS()
        self.assertFalse(st['online'])
        self.assertIsNone(st['local'])


class NothingGoesOut(unittest.TestCase):
    """What must be gone from the window's code is checked in its source."""

    def test_no_way_to_hand_anything_to_the_system(self):
        for name in ('engine_pipe.py', 'bridge.py', 'main.py'):
            src = (ROOT / 'app' / name).read_text(encoding='utf-8')
            for word in ('open_in_browser', 'open_external', 'startfile', 'NSWorkspace', 'openURL', "'browser'", 'webbrowser', 'Process.Start', 'ShellExecute'):
                self.assertNotIn(word, src, name)


class StreamTests(Both, unittest.TestCase):

    def test_cancel_mid_body_stops_the_handler(self):
        b = B.Bridge(MOD)
        hold = threading.Event()
        r = Rec(hold, after=4)
        ex = b.start('GET', b.origin + '/videos/huge.mkv', {}, None, r)   # one piece of video: 8 chunks
        self.assertTrue(wait_until(lambda: len(r.parts) == 4))
        lane = b.lanes['large']
        self.assertEqual(lane.active, 1)
        ex.cancel()
        self.assertTrue(ex.stopped)
        hold.set()
        self.assertTrue(wait_until(lambda: lane.active == 0))
        self.assertFalse(r.done.is_set())
        self.assertEqual(len(r.parts), 4)     # the write stopped midway: the other four chunks never came

    def test_chunks_are_bounded(self):
        r = Rec()
        self.b.start('GET', self.b.origin + A, {}, None, r)
        self.assertTrue(r.done.wait(20))
        self.assertTrue(all(len(p) <= B.CHUNK for p in r.parts))
        self.assertTrue(r.ok)
        r = Rec()
        self.b.start('GET', self.b.origin + '/documents/t/manywrites', {}, None, r)
        self.assertTrue(r.done.wait(20))
        self.assertEqual(max(len(p) for p in r.parts), B.CHUNK)
        self.assertEqual(sum(len(p) for p in r.parts), 3 * MiB)

    def test_head_comes_before_body_and_order_is_kept(self):
        events = []

        class S(Rec):
            def head(self, *a):
                events.append('head')
                super().head(*a)

            def data(self, c):
                events.append('data')
                super().data(c)

            def finish(self, ok):
                events.append('finish')
                super().finish(ok)
        r = S()
        self.b.start('GET', self.b.origin + '/documents/s.pdf', {}, None, r)
        self.assertTrue(r.done.wait(10))
        self.assertEqual(events[0], 'head')
        self.assertEqual(events[-1], 'finish')
        self.assertEqual(set(events[1:-1]), {'data'})

    def test_chat_arrives_piece_by_piece(self):
        r = Rec()
        STUB.update(pieces=6, delay=0.1)
        t0 = time.time()
        self.b.start('POST', self.b.origin + '/api/chat', self.json, b'{"messages":[{"role":"user","content":"hi"}]}', r)
        self.assertTrue(r.first.wait(5))
        first_at = time.time() - t0
        self.assertTrue(r.done.wait(10))
        spread = r.times[-1] - r.times[0]
        OBSERVED['stream chat'] = f'{len(r.parts)} pieces over {spread:.2f} s, first after {first_at * 1000:.0f} ms'
        self.assertEqual(r.status, 200)
        self.assertEqual(r.hs['content-type'], 'text/event-stream')
        self.assertGreaterEqual(len(r.parts), 6)
        self.assertGreater(spread, 0.4)
        self.assertLess(first_at, 0.3)
        self.assertIn(b'"done"', r.body)
        self.assertTrue(r.ok)

    def test_chat_stop_reaches_the_model_side(self):
        r = Rec()
        STUB.update(pieces=200, delay=0.05)
        before = len(STUB['stopped'])
        ex = self.b.start('POST', self.b.origin + '/api/chat', self.json, b'{}', r)
        self.assertTrue(wait_until(lambda: len(r.parts) >= 3))
        ex.cancel()
        self.assertTrue(wait_until(lambda: len(STUB['stopped']) > before, 5))
        self.assertLess(STUB['stopped'][-1], 50)
        self.assertFalse(r.done.is_set())
        self.assertTrue(wait_until(lambda: self.b.lanes['chat'].active == 0))

    def test_cancel_while_queued_never_runs(self):
        b = B.Bridge(MOD)
        lane = b.lanes['large'] = B._Lane('large', 1, 10)
        hold = threading.Event()
        r1, r2 = Rec(hold), Rec()
        e1 = b.start('GET', b.origin + '/videos/huge.mkv', {}, None, r1)
        self.assertTrue(r1.first.wait(5))
        e2 = b.start('GET', b.origin + '/videos/a.mp4', {}, None, r2)
        e2.cancel()
        e1.cancel()
        hold.set()
        self.assertTrue(wait_until(lambda: not lane.waiting and lane.active == 0))
        self.assertIsNone(r2.status)
        self.assertFalse(r2.done.is_set())

    def test_small_stays_quick_under_a_flood_of_large(self):
        b = B.Bridge(MOD)
        hold = threading.Event()
        recs = [Rec(hold) for _ in range(300)]
        exs = [b.start('GET', b.origin + '/videos/huge.mkv', {}, None, r) for r in recs]
        lane = b.lanes['large']
        self.assertTrue(wait_until(lambda: lane.active == 6))
        time.sleep(0.2)
        self.assertEqual(lane.active, 6)
        self.assertEqual(sum(len(r.parts) for r in recs), 6)     # each worker is held at its first chunk; nothing else has been read
        self.assertGreater(len(lane.waiting), 280)
        times = []
        for _ in range(200):
            t = time.perf_counter()
            self.assertEqual(call(b, 'GET', '/api/t/echo').status, 200)
            times.append(time.perf_counter() - t)
        OBSERVED['stream, small during large flood (300 videos, the window not reading)'] = f'/api/t/echo median {statistics.median(times) * 1000:.2f} ms, max {max(times) * 1000:.2f} ms over 200'
        self.assertLess(max(times), 1.0)
        for e in exs:
            e.cancel()
        hold.set()
        self.assertTrue(wait_until(lambda: lane.active == 0 and not lane.waiting))

    def test_queue_full(self):
        b = B.Bridge(MOD)
        b.lanes['large'] = B._Lane('large', 1, 2)
        hold = threading.Event()
        recs = [Rec(hold) for _ in range(3)]
        exs = [b.start('GET', b.origin + '/videos/huge.mkv', {}, None, recs[0])]
        self.assertTrue(recs[0].first.wait(5))     # one running, held
        exs += [b.start('GET', b.origin + '/videos/huge.mkv', {}, None, r) for r in recs[1:]]     # two waiting
        self.assertNotIn(None, exs)
        extra = [Rec() for _ in range(3)]
        got = [b.start('GET', b.origin + '/videos/a.mp4', {}, None, r) for r in extra]
        self.assertEqual(got, [None] * 3)
        self.assertEqual([r.status for r in extra], [503] * 3)
        for e in exs:
            e.cancel()
        hold.set()

    def test_redirect_becomes_a_page_that_moves_itself(self):
        r = self.get('/documents/t/redirect')
        self.assertEqual(r.status, 200)
        self.assertIn(b'http-equiv="refresh"', r.body)
        self.assertIn(b'&quot;&lt;b&gt;', r.body)
        self.assertNotIn(b'"<b>', r.body)

    def test_real_chat_with_no_model_is_plain(self):
        assistant.model_status, assistant.chat = REAL_STATUS, REAL_CHAT
        try:
            r = call(self.b, 'POST', '/api/chat', self.json, b'{}', timeout=15)
        finally:
            assistant.model_status, assistant.chat = stub_status, stub_chat
        self.assertEqual(r.status, 200)
        self.assertIn(b'model_unavailable', r.body)
        OBSERVED['chat with no model'] = r.body.decode().strip()


class WholeProcess(unittest.TestCase):
    def test_no_listening_socket_and_no_model(self):
        self.assertEqual(LISTENS, [])
        pid = str(os.getpid())
        lsof = shutil.which('lsof')
        if not lsof:
            self.skipTest('no lsof on this system: the process-level socket and file check cannot run here')
        out = subprocess.run([lsof, '-nP', '-a', '-p', pid, '-i'], capture_output=True, text=True).stdout
        files = subprocess.run([lsof, '-nP', '-p', pid], capture_output=True, text=True).stdout.lower()
        how = 'lsof -i'
        self.assertIn('python', files, 'the process file/module listing came back empty, so this check proved nothing')
        self.assertEqual(out.strip(), '', out)
        self.assertNotIn('derived/models', files)
        OBSERVED[f'{how} for this process'] = 'no rows' if not out.strip() else out

    def test_nothing_written_to_the_real_library(self):
        self.assertTrue(str(server.LIB).startswith(str(DATA)))

    def test_video_piece_size(self):
        self.assertGreaterEqual(B.VIDEO_PIECE, 4 * MiB)
        self.assertLessEqual(B.VIDEO_PIECE, 8 * MiB)


# ---- the stub: stands in for the model. handle_post, emit and wfile are the real ones.
STUB = {'pieces': 4, 'delay': 0.05, 'runs': [], 'sent': [], 'stopped': []}
REAL_STATUS, REAL_CHAT = assistant.model_status, assistant.chat


def stub_status():
    return {'online': True, 'model': 'TEST-STUB-NOT-A-MODEL', 'local': None}


def stub_chat(messages, about, emit, cancelled):
    STUB['runs'].append(time.time())
    emit({'type': 'state', 'state': 'thinking'})
    for i in range(STUB['pieces']):
        if cancelled():
            STUB['stopped'].append(i)
            return
        time.sleep(STUB['delay'])
        emit({'type': 'delta', 'text': f'p{i} '})
        STUB['sent'].append(i)
    emit({'type': 'done', 'answer': 'x', 'sources': [], 'consulted': [], 'invalid': [], 'latency': {}})


assistant.model_status, assistant.chat = stub_status, stub_chat

if __name__ == '__main__':
    prog = unittest.main(exit=False, verbosity=2 if '-v' in sys.argv else 1)
    print('\nObserved:')
    for k, v in OBSERVED.items():
        print(f'  {k}: {v}')
    sys.stdout.flush()
    os._exit(0 if prog.result.wasSuccessful() else 1)
