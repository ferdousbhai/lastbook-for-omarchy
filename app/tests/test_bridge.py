#!/usr/bin/env python3
"""The bridge and the engine's pipe with no window: streaming, lanes, every refusal, lines that are not requests.

  python3 -B app/tests/test_bridge.py [-v]

Uses the made-up library in derived/app-build/testdata. Never packaged."""
import base64, faulthandler, hashlib, json, os, shutil, socket, statistics, subprocess, sys, tempfile, threading, time, types, unittest
from pathlib import Path

sys.dont_write_bytecode = True
faulthandler.dump_traceback_later(600, exit=True)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
DATA = fixture.ensure()
os.environ['LASTBOOK_DATA'] = str(DATA)
os.environ['LASTBOOK_RECALL_SOCKET'] ='/nonexistent/recall.sock'   # no embedding model: meaning-based search stays off

import server  # noqa: E402
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
        if p == '/api/t/wide':     # JSON with a two-byte character cut in two by the first chunk's end
            data = b'["' + b'a' * (MiB - 3) + 'é'.encode() + b'"]'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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


def call(b, method, path, body=None, timeout=60):
    r = Rec()
    b.start(method, path, body, r)
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
        self.piece = B.VIDEO_PIECE

    def get(self, path, **k):
        return call(self.b, 'GET', path, **k)

    # ---- a video through the bridge: its first piece and no more (the window plays the library's videos from disk)
    def test_video_gets_first_piece(self):
        r = self.get(A)
        self.assertEqual(r.status, 206)
        self.assertEqual(len(r.body), self.piece)
        self.assertEqual(hashlib.sha256(r.body).digest(), hashlib.sha256(pat(0, self.piece)).digest())

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
        self.assertEqual(B._lane_for('/documents/real.pdf', '/documents/real.pdf?pdf-page=2'), 'render')
        self.assertEqual(B._lane_for('/documents/real.pdf', '/documents/real.pdf'), 'large')
        self.assertEqual(B._lane_for('/api/find', '/api/find?q=x'), 'small')

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
    def test_refused_target(self):
        for t in ('//evil/', '/a b', '/a\nb', '/a\x00b', '/a\tb', '/\x7f', 'https://evil.example/', 'file:///etc/hosts', 'about:blank', ''):
            self.assertEqual(self.get(t).status, 400, repr(t))

    def test_refused_method(self):
        for m in ('HEAD', 'PUT', 'DELETE', 'OPTIONS', 'TRACE', 'PATCH', 'CONNECT', 'get'):
            self.assertEqual(call(self.b, m, '/').status, 405, m)

    def test_post_too_big(self):
        self.assertEqual(call(self.b, 'POST', '/api/setup/start', b'x' * (B.MAX_POST + 1)).status, 413)

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

    def test_the_engine_sees_the_path_and_no_header_of_the_caller(self):
        got = json.loads(self.get('/api/t/echo?a=1').body)
        self.assertEqual(got['path'], '/api/t/echo?a=1')
        self.assertEqual(got['headers'], {'host': 'localhost', 'connection': 'close'})

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
        self.assertEqual(self.get('/documents/t/sized?bytes=%d' % (4 * B.BIG_BODY)).status, 403)

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

    def test_a_link_to_another_saved_page_leads_to_the_saved_copy(self):
        """A saved page's link to the address another saved page came from (its source_url or final_url in the catalog)
        opens that page in the library, an index.html page's as well. Other links to the web are left as they are."""
        cat = fixture.LIB / 'catalog.json'
        before = cat.read_bytes()
        self.addCleanup(cat.write_bytes, before)
        cat.write_text(json.dumps(json.loads(before) + [{'id': 'saved-b', 'kind': 'html', 'path': 'pages/kinds/b.html', 'title': 'B', 'category': 'test',
                                                         'collection': 'Test', 'publisher': 'Test', 'source_url': 'https://e.example/b?x=1&y=2', 'final_url': 'https://e.example/b/'}]))
        d = fixture.LIB / 'pages' / 'kinds' / 'cross'
        d.mkdir(parents=True, exist_ok=True)
        page = (b'<!doctype html><html><head><title>a</title></head><body><a target="_blank" href="https://e.example/b?x=1&amp;y=2#part">one</a>'
                b"<a href='https://e.example/b/'>two</a><a href=https://e.example/c>three</a></body></html>")
        (d / 'a.html').write_bytes(page)
        (d / 'index.html').write_bytes(page)
        for t in ('/pages/kinds/cross/a.html', '/pages/kinds/cross/'):
            r = self.get(t)
            self.assertEqual(r.status, 200, t)
            self.assertRegex(r.body, rb'<a\s+href="/pages/kinds/b.html#part">one</a>', t)    # and opens where it is
            self.assertIn(b'<a href="/pages/kinds/b.html">two</a>', r.body, t)
            self.assertIn(b'<a href=https://e.example/c>three</a>', r.body, t)

    def test_saved_page_transcript_stays_in_the_library(self):
        self.assertIsNone(self.part('/pages/kinds/ocw.html', '../../../canary-root.txt'))
        self.assertIsNone(self.part('/pages/kinds/ocw.html', 'https://evil.example/a.vtt'))
        self.assertIsNone(self.part('/pages/kinds/ocw.html', '//evil.example/a.vtt'))
        self.assertIsNone(self.part('/pages/kinds/ocw.html', '/api/topics'))

    def part(self, page, src):
        h = server.Handler.__new__(server.Handler)
        h.path, h.directory = page, str(fixture.LIB)
        return h.saved_page_part(src)

    def test_engine_error_is_an_answer(self):
        r = self.get('/api/t/boom')
        self.assertEqual(r.status, 500)
        self.assertNotIn(str(ROOT).encode(), r.body)
        self.assertEqual(self.get('/api/t/uncaught').status, 502)   # an error the Handler let out still becomes an answer

    def test_ordinary_api(self):
        self.assertEqual(self.get('/reader.css').status, 200)
        self.assertEqual(self.get('/api/nope').status, 404)
        self.assertEqual(call(self.b, 'POST', '/api/nope', b'{}').status, 404)
        r = json.loads(self.get('/api/find?q=boil').body)
        self.assertTrue(r)
        self.assertEqual(json.loads(self.get('/api/doc/pdfsmall').body)['id'], 'pdfsmall')

    def test_short_answer(self):
        r = self.get('/documents/t/short')
        self.assertEqual(r.status, 200)
        self.assertFalse(r.ok)


class StreamTests(Both, unittest.TestCase):

    def test_chunks_are_bounded(self):
        r = Rec()
        self.b.start('GET', A, None, r)
        self.assertTrue(r.done.wait(20))
        self.assertTrue(all(len(p) <= B.CHUNK for p in r.parts))
        self.assertTrue(r.ok)
        r = Rec()
        self.b.start('GET', '/documents/t/manywrites', None, r)
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
        self.b.start('GET', '/documents/s.pdf', None, r)
        self.assertTrue(r.done.wait(10))
        self.assertEqual(events[0], 'head')
        self.assertEqual(events[-1], 'finish')
        self.assertEqual(set(events[1:-1]), {'data'})

    def test_an_answer_arrives_piece_by_piece(self):
        r = Rec()
        t0 = time.time()
        self.b.start('GET', '/documents/t/nolen?mb=6&ms=100', None, r)
        self.assertTrue(r.first.wait(5))
        first_at = time.time() - t0
        self.assertTrue(r.done.wait(10))
        spread = r.times[-1] - r.times[0]
        OBSERVED['stream'] = f'{len(r.parts)} pieces over {spread:.2f} s, first after {first_at * 1000:.0f} ms'
        self.assertEqual(r.status, 200)
        self.assertEqual(len(r.parts), 6)
        self.assertGreater(spread, 0.4)
        self.assertLess(first_at, 0.3)
        self.assertTrue(r.ok)

    def test_small_stays_quick_under_a_flood_of_large(self):
        b = B.Bridge(MOD)
        hold = threading.Event()

        class Held(Rec):     # keeps how much came, not the bytes: thirty videos' first pieces are 240 MiB
            def data(self, chunk):
                super().data(len(chunk))
        recs = [Held(hold) for _ in range(30)]
        for r in recs:
            b.start('GET', '/videos/huge.mkv', None, r)
        self.assertTrue(wait_until(lambda: sum(len(r.parts) for r in recs) == B.LANES['large']))
        time.sleep(0.2)
        self.assertEqual(sum(len(r.parts) for r in recs), B.LANES['large'])     # each that runs is held at its first chunk; the rest wait
        times = []
        for _ in range(200):
            t = time.perf_counter()
            self.assertEqual(call(b, 'GET', '/api/t/echo').status, 200)
            times.append(time.perf_counter() - t)
        OBSERVED['stream, small during large flood (30 videos, the window not reading)'] = f'/api/t/echo median {statistics.median(times) * 1000:.2f} ms, max {max(times) * 1000:.2f} ms over 200'
        self.assertLess(max(times), 1.0)
        hold.set()
        self.assertTrue(all(r.done.wait(30) for r in recs))
        self.assertTrue(all(sum(r.parts) == B.VIDEO_PIECE for r in recs))

    def test_slow_requests_do_not_hold_up_a_quick_one(self):
        """A search that waits (on the embedding model, say) mostly sleeps: however many there are, a quick request starts at once."""
        recs = [Rec() for _ in range(20)]
        for r in recs:
            self.b.start('GET', '/api/t/slow?ms=1500', None, r)
        t = time.perf_counter()
        self.assertEqual(call(self.b, 'GET', '/api/t/echo').status, 200)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertTrue(all(r.done.wait(10) for r in recs))

    def test_redirect_becomes_a_page_that_moves_itself(self):
        r = self.get('/documents/t/redirect')
        self.assertEqual(r.status, 200)
        self.assertIn(b'http-equiv="refresh"', r.body)
        self.assertIn(b'&quot;&lt;b&gt;', r.body)
        self.assertNotIn(b'"<b>', r.body)


class Pipe(unittest.TestCase):
    """app/engine_pipe.py: what goes out on the window's pipe, and lines that are not requests."""

    @classmethod
    def setUpClass(cls):
        out = sys.stdout
        import engine_pipe     # importing it points sys.stdout at the log, as the engine process wants
        sys.stdout = out
        cls.P = engine_pipe
        cls.b = B.Bridge(MOD)

    def setUp(self):
        self.sent, self.real_send = [], self.P.send
        self.P.send = self.sent.append

    def tearDown(self):
        self.P.send = self.real_send

    def answered(self, rid, timeout=20):
        self.assertTrue(wait_until(lambda: any(m.get('id') == rid and 'end' in m for m in self.sent), timeout), rid)
        return [m for m in self.sent if m.get('id') == rid]

    def test_a_character_cut_between_chunks_arrives_whole(self):
        self.P.answer(self.b, b'{"id": 1, "path": "/api/t/wide"}\n')
        got = self.answered(1)
        chunks = [m['chunk'] for m in got if 'chunk' in m]
        self.assertGreater(len(chunks), 1)     # it did come in two
        self.assertEqual(json.loads(''.join(chunks)), ['a' * (MiB - 3) + 'é'])
        self.assertIs(got[-1]['end'], True)

    def test_anything_but_json_comes_byte_for_byte(self):
        page = fixture.LIB / 'pages' / 'kinds' / 'cut.html'
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(b'<p>' + b'a' * (MiB - 4) + 'é'.encode() + b'</p>')
        slides = fixture.LIB / 'documents' / 'slides.pptx'
        slides.write_bytes(b'PK\x03\x04\xff\xfe' * 100)
        for rid, path in ((2, '/pages/kinds/cut.html'), (3, '/documents/slides.pptx')):
            self.P.answer(self.b, json.dumps({'id': rid, 'path': path}))
            got = self.answered(rid)
            self.assertFalse(any('chunk' in m for m in got), path)
            body = b''.join(base64.b64decode(m['b64']) for m in got if 'b64' in m)
            if path.endswith('.pptx'):
                self.assertEqual(body, slides.read_bytes())
            else:
                self.assertIn(b'a' + 'é'.encode() + b'</p>', body)     # the page is rewritten, and comes as the engine wrote it

    def test_a_line_that_is_not_a_request_is_passed_over(self):
        bad = [b'5\n', b'null\n', b'[1]\n', b'"cancel"\n', b'{"cancel": 8}\n', b'{"id": [1], "path": "/api/t/echo"}\n', b'{"id": {"a": 1}}\n',
               b'{"id": null}\n', b'\xff\xfe{"id": 9}\n', b'{"id": 9, "path": "/api/\xff"}\n', b'\n', b'{not json\n']
        for line in bad:
            self.P.answer(self.b, line)
        self.P.answer(self.b, b'{"id": 10, "path": "/api/t/echo"}\n')
        self.assertEqual(self.answered(10)[0]['head'], 200)
        self.assertEqual({m.get('id') for m in self.sent}, {10})

    def test_the_engine_waits_for_what_it_was_asked_and_no_longer(self):
        """main() leaves once nothing is being answered: an answered request, a refused one and one whose window has gone all end."""
        self.P.answer(self.b, b'{"id": "slow", "path": "/documents/t/slow?ms=300"}')
        self.assertTrue(wait_until(lambda: self.P._running == 1))
        for i in range(50):
            self.P.answer(self.b, json.dumps({'id': i, 'path': '/api/t/echo'}))
        self.P.answer(self.b, b'{"id": "bad", "method": "PUT", "path": "/"}')     # refused at once
        self.answered('slow')
        self.assertTrue(wait_until(lambda: self.P._running == 0))

        def closed(obj):     # the window has gone: every write to its pipe fails
            raise BrokenPipeError
        self.P.send = closed
        sink = self.P.PipeSink('gone')
        with self.assertRaises(BrokenPipeError):
            sink.finish(True)
        self.assertEqual(self.P._running, 0)     # over all the same, so the engine does not wait 30 s to leave
        self.P.answer(self.b, b'{"id": "gone too", "path": "/api/t/echo"}')
        self.assertTrue(wait_until(lambda: self.P._running == 0))

    def test_the_real_loop_lives_through_bad_lines(self):
        """engine_pipe.py itself, on the made-up library: malformed lines, then a request that is answered."""
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, 'XDG_CONFIG_HOME': home, 'XDG_DATA_HOME': home, 'PYTHONDONTWRITEBYTECODE': '1'}
            with subprocess.Popen([sys.executable, '-B', str(ROOT / 'app' / 'engine_pipe.py'), '--data', str(DATA)], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                self.assertIn('ready', json.loads(proc.stdout.readline()))
                proc.stdin.write(b'null\n[1]\n{"id": [1]}\n\xff\xfe\n{"id": 1, "path": "/api/doc/pdfsmall"}\n')
                proc.stdin.close()
                got = [json.loads(proc.stdout.readline()) for _ in range(3)]
                self.assertEqual([next(k for k in m if k != 'id') for m in got], ['head', 'chunk', 'end'])
                self.assertEqual(json.loads(got[1]['chunk'])['id'], 'pdfsmall')
                self.assertEqual(proc.wait(30), 0)


class WholeProcess(unittest.TestCase):
    def test_no_listening_socket(self):
        self.assertEqual(LISTENS, [])
        pid = str(os.getpid())
        lsof = shutil.which('lsof')
        if not lsof:
            # In CI a missing tool fails the run: a check that was skipped proved nothing, and a green run would hide that.
            (self.fail if os.environ.get('CI') else self.skipTest)('no lsof on this system: the process-level socket and file check cannot run here')
        out = subprocess.run([lsof, '-nP', '-a', '-p', pid, '-i'], capture_output=True, text=True).stdout
        files = subprocess.run([lsof, '-nP', '-p', pid], capture_output=True, text=True).stdout.lower()
        how = 'lsof -i'
        self.assertIn('python', files, 'the process file/module listing came back empty, so this check proved nothing')
        self.assertEqual(out.strip(), '', out)
        OBSERVED[f'{how} for this process'] = 'no rows' if not out.strip() else out


if __name__ == '__main__':
    prog = unittest.main(exit=False, verbosity=2 if '-v' in sys.argv else 1)
    print('\nObserved:')
    for k, v in OBSERVED.items():
        print(f'  {k}: {v}')
    sys.stdout.flush()
    os._exit(0 if prog.result.wasSuccessful() else 1)
