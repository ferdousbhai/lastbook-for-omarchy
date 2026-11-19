#!/usr/bin/env python3
"""The bridge against a real data folder with no model. Prints what it saw. No window.

  LASTBOOK_TEST_DATA=<folder> python3 -B app/tests/real_data_check.py"""
import faulthandler, hashlib, json, os, resource, shutil, subprocess, sys, threading, time, urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
faulthandler.dump_traceback_later(420, exit=True)
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'app')]
DATA = Path(os.environ['LASTBOOK_TEST_DATA'])
os.environ['LASTBOOK_DATA'] = str(DATA)
os.environ['SURVIVAL_MODEL_BASE'] = 'http://127.0.0.1:9'
os.environ['LASTBOOK_RECALL_SOCKET'] = '/nonexistent/recall.sock'
import server  # noqa: E402
import bridge as B  # noqa: E402
server.init(no_index=True)
assert server.INDEX.query_vector('water') is None, 'meaning-based search is on; refusing to run here'
MiB = B.MiB
LIB = DATA / 'library-data'
results = []


def rss():
    return int(subprocess.run(['ps', '-o', 'rss=', '-p', str(os.getpid())], capture_output=True, text=True).stdout) * 1024


def note(name, ok, detail=''):
    ok = bool(ok)
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


class Rec(B.Sink):
    def __init__(self, keep=True):
        self.keep = keep
        self.h = hashlib.sha256()
        self.n = 0
        self.parts = []
        self.done = threading.Event()
        self.ok = None
        self.status, self.hs = None, {}

    def head(self, status, reason, headers):
        self.status, self.hs = status, {k.lower(): v for k, v in headers}

    def data(self, chunk):
        self.n += len(chunk)
        self.h.update(chunk)
        if self.keep:
            self.parts.append(chunk)

    def finish(self, ok):
        self.ok = ok
        self.done.set()


def call(b, method, target, headers=None, body=None, timeout=120):
    r = Rec()
    b.start(method, b.origin + target, headers or {}, body, r)
    assert r.done.wait(timeout), target
    return r.status, r.hs, b''.join(r.parts)


def q(p):
    return '/' + urllib.parse.quote(p, safe='/')


cat = server.catalog()['by_path']
pdf = 'documents/science-education/anatomy-and-physiology-2e.pdf'
video = next(p for p in cat if 'Laceface' in p)
vsize = (LIB / video).stat().st_size
psize = (LIB / pdf).stat().st_size
small_pdf = next(p for p, r in cat.items() if r['kind'] == 'pdf' and r['bytes'] < 8 * MiB)
saved = next(p for p, r in cat.items() if r['kind'] == 'html' and p.startswith('pages/') and r['bytes'] > 100)
zims = sorted(x.name for x in (LIB / 'collections').glob('*.zim'))
print('data', DATA, '| pdf', psize, '| video', vsize, '| zims', zims, flush=True)

b = B.Bridge(server)
piece = B.VIDEO_PIECE
for t, want in [('/', 404), ('/ui/app.js', 404), ('/api/topics', 200), ('/api/library', 200), ('/api/shelves', 200), ('/api/videos', 200), ('/api/places?q=springfield', 200), ('/api/health', 200), ('/api/nope', 404)]:
    s, h, d = call(b, 'GET', t)
    note(f'GET {t}', s == want, f'{s} {len(d)} bytes')
counts = []
for word in ('water', 'canning', 'generator'):
    s, h, d = call(b, 'GET', f'/api/find?q={word}')
    counts.append(len(json.loads(d).get('results', [])) if s == 200 else s)
note('keyword search water, canning, generator', all(isinstance(c, int) and c > 0 for c in counts), str(counts))
s, h, d = call(b, 'GET', q(small_pdf) + '?pdf-page=1')
note('PDF page image', s == 200 and d[:4] == b'\x89PNG', f'{s} {len(d)} bytes')
s, h, d = call(b, 'GET', q(saved))
csp = h.get('content-security-policy', '')
note('saved page runs no code', s == 200 and csp.startswith('sandbox;') and "script-src 'none'" in csp and 'allow-' not in csp, csp[:60])
tools = 'courses/18-05-introduction-to-probability-and-statistics-spring-2022/resources/tools/index.html'
if (LIB / tools).is_file():
    s, h, d = call(b, 'GET', q(tools))
    note('a course page in a folder named tools opens, with no code', s == 200 and h.get('content-security-policy', '').startswith('sandbox;'), f'{s} {len(d)} bytes')
with open(LIB / video, 'rb') as f:
    for off in (0, 5 * MiB, vsize - 1000):
        s, h, d = call(b, 'GET', q(video), {'Range': f'bytes={off}-'})
        f.seek(off)
        want = f.read(min(piece, vsize - off))
        note(f'1.75 GB video, open Range at {off}', s == 206 and d == want, f"{h.get('content-range')}")
s, h, d = call(b, 'GET', q(video))
note('1.75 GB video, no Range', s == 206 and len(d) == piece, h.get('content-range'))
s, h, d = call(b, 'GET', q(pdf))
note('476 MB PDF comes as a page of pictures', s == 200 and 'html' in h.get('content-type', '') and b'%PDF' not in d[:1024] and d.count(b'pdf-page=') > 900, f"{s} {len(d)} bytes, {d.count(b'pdf-page=')} pages")
s, h, d = call(b, 'GET', q(pdf), {'Range': 'bytes=0-65535'})
note('476 MB PDF: a Range gets no bytes of the file', s == 200 and b'%PDF' not in d[:1024], f'{s} {len(d)} bytes')
s, h, d = call(b, 'GET', q(pdf) + '?pdf-page=900')
note('476 MB PDF: page 900 is a picture', s == 200 and h.get('content-type') == 'image/png' and d[:4] == b'\x89PNG', f'{s} {len(d)} bytes')
if zims:
    z = urllib.parse.quote(zims[0])
    s, h, d = call(b, 'GET', f'/zim/{z}/')
    loc = h.get('location')
    note('ZIM root', s == 200 and b'refresh' in d, f'{s} {loc or d[:70]}')

# Video asked for as fast as it can be, with a window that takes about 200 MB/s per worker.
b = B.Bridge(server)
base = rss()
N, count = 400, [0]
done = threading.Event()
lock = threading.Lock()
peak = [0]

def one_done():
    with lock:
        count[0] += 1
        if count[0] >= N:
            done.set()

class Drain(Rec):
    def finish(self, ok):
        one_done()

    def data(self, chunk):
        time.sleep(0.005)

t = time.time()
for i in range(N):
    rng = {'Range': f'bytes={(i * 37 * MiB) % (vsize - 40 * MiB)}-'}
    b.start('GET', b.origin + q(video), rng, None, Drain())
worst_small = 0
while not done.wait(0.05):
    peak[0] = max(peak[0], rss())
    t1 = time.perf_counter()
    call(b, 'GET', '/api/health')
    worst_small = max(worst_small, time.perf_counter() - t1)
time.sleep(0.2)
lane = b.lanes['large']
note(f'flood of {N} video pieces', lane.active == 0 and not lane.waiting,
     f'{time.time() - t:.1f} s; process RSS {base / MiB:.0f} -> peak {peak[0] / MiB:.0f} MiB; worst /api/health during it {worst_small * 1000:.1f} ms')

LSOF = shutil.which('lsof') or 'lsof'
files = subprocess.run([LSOF, '-nP', '-p', str(os.getpid())], capture_output=True, text=True).stdout.lower()
net = subprocess.run([LSOF, '-nP', '-a', '-p', str(os.getpid()), '-i'], capture_output=True, text=True).stdout
note('no socket and no model file open', not net.strip() and 'onnx' not in files and 'bge-small' not in files, f'lsof -i rows: {len(net.strip().splitlines())}')
print(f'{sum(results)} of {len(results)} passed; peak RSS of the whole run {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / MiB:.0f} MiB', flush=True)
sys.stdout.flush()
os._exit(0 if all(results) else 1)
