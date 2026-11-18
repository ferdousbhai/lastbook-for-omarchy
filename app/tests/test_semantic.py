#!/usr/bin/env python3
"""Meaning-based search through Recall's socket, against a stand-in Recall, and the encyclopedia article reader.

  PYTHONPATH=<deps> python3 -B app/tests/test_semantic.py [-v]

The stand-in speaks Recall's api 1 (capabilities, embed) on a Unix socket in a temporary folder and embeds by hashing words,
so similar wording gives similar vectors. No model, no network. Never packaged."""
import hashlib, json, re, socket, sqlite3, sys, tempfile, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = Path(tempfile.mkdtemp(prefix='lastbook-semantic-'))
sys.path[:0] = [str(ROOT)]
import numpy as np  # noqa: E402
import retrieval, encyclopedia  # noqa: E402
retrieval.IDX, retrieval.LIB = TMP / 'derived' / 'index', TMP / 'library-data'   # this test's own folders; the data folder is left alone
retrieval.IDX.mkdir(parents=True, exist_ok=True)

DIM = 64


def vector(text):
    v = np.zeros(DIM, dtype=np.float32)
    for w in re.findall(r'[a-z]+', text.lower().split('|', 1)[-1]):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


class FakeRecall(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.source, self.fail, self.calls = path, 'fake-model@1', None, 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                buf = b''
                while not buf.endswith(b'\n'):
                    piece = c.recv(65536)
                    if not piece:
                        break
                    buf += piece
                req = json.loads(buf)
                self.calls += 1
                if self.fail:
                    reply = {'api': 1, 'error': self.fail}
                elif req.get('op') == 'capabilities':
                    reply = {'api': 1, 'operations': ['embed', 'records.search', 'records.forget'], 'source': self.source}
                elif req.get('op') == 'embed':
                    assert req['kind'] == 'text' and 0 < len(req['inputs']) <= 32
                    reply = {'api': 1, 'source': self.source, 'embeddings': [{'vector': vector(t)} for t in req['inputs']]}
                else:
                    reply = {'api': 1, 'error': 'unknown op'}
                c.sendall(json.dumps(reply).encode() + b'\n')

    def close(self):
        self.sock.close()


PAGES = {
    'water': ['Boil the water for one minute to kill the germs. If the water is cloudy, let it settle and filter it through a clean cloth first, then boil it.',
              'Store the treated water in a clean container with a lid and keep it in a cool place away from the sun and from animals.'],
    'fire': ['Keep the fire small and clear the ground around it of dry grass and leaves. Never leave a fire alone and put it out with water and soil.'],
    'bleeding': ['Press firmly on the wound with a clean cloth and keep pressing. If the bleeding does not stop, press harder and get help from a trained person.'],
}


def make_library():
    lib = TMP / 'library-data'
    lib.mkdir(parents=True, exist_ok=True)
    db = lib / 'search.sqlite'
    con = sqlite3.connect(db)
    con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
    for d, pages in PAGES.items():
        for i, body in enumerate(pages, 1):
            con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', (d, d.title(), 'Manuals', 'health', f'documents/{d}.pdf', i, body))
    con.commit()
    con.close()
    by_id = {d: {'id': d, 'kind': 'pdf', 'title': d.title() + ' manual', 'sha256': d * 4, 'pages': len(p), 'path': f'documents/{d}.pdf'} for d, p in PAGES.items()}
    return {'by_id': by_id, 'by_path': {}, 'video_tx': {}, 'thumbs': {}}, db


class Semantic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat, cls.db = make_library()
        cls.sock = TMP / 'recall.sock'

    def index(self, path=None):
        for f in ('semantic.sqlite', 'semantic.sqlite-wal', 'semantic.sqlite-shm', 'vectors.f16'):
            (retrieval.IDX / f).unlink(missing_ok=True)
        return retrieval.Index(lambda: self.cat, lambda r: ('Reference', 1.0), self.db, retrieval.Recall(path or self.sock))

    def test_without_recall_keyword_search_carries_on(self):
        ix = self.index(TMP / 'no-such.sock')
        self.assertEqual(ix.update(), 0)
        self.assertEqual(ix.status['state'], 'waiting for Recall')
        self.assertEqual(ix.status['recall'], 'not running')
        self.assertEqual(ix.semantic('boil water'), [])
        self.assertIsNone(ix.query_vector('boil water'))

    def test_index_search_and_one_document(self):
        fake = FakeRecall(self.sock)
        fake.start()
        try:
            ix = self.index()
            self.assertEqual(ix.update(), 3)
            self.assertEqual(ix.status['state'], 'up to date')
            self.assertEqual((ix.dim, ix.source), (DIM, 'fake-model@1'))
            self.assertEqual(len(ix.mat), 4)
            hits = ix.semantic('how long do I boil cloudy water', k=3, floor=0.1)
            self.assertEqual(hits[0]['doc_id'], 'water')
            self.assertEqual(hits[0]['page'], 1)
            # One document's passages, even when the question matches other documents far better (searching inside a
            # document used to pick the best passages of the whole library first, and then keep only that document's).
            inside = ix.semantic('how long do I boil cloudy water', k=2, doc='fire')
            self.assertEqual([h['doc_id'] for h in inside], ['fire'])
            # allowed filters without losing passages further down the ranking
            only = ix.semantic('how long do I boil cloudy water', k=5, floor=-1, allowed=lambda d: d == 'bleeding')
            self.assertEqual([h['doc_id'] for h in only], ['bleeding'])
            # unchanged documents are not embedded again
            calls = fake.calls
            self.assertEqual(ix.update(), 0)
            self.assertEqual(fake.calls - calls, 1)   # the capabilities check only
            # the question is embedded once, then cached
            calls = fake.calls
            ix.semantic('store the water', floor=-1)
            ix.semantic('store the water', floor=-1)
            self.assertEqual(fake.calls - calls, 1)
        finally:
            fake.close()
            self.sock.unlink(missing_ok=True)

    def test_a_new_model_empties_the_index(self):
        fake = FakeRecall(self.sock)
        fake.start()
        try:
            ix = self.index()
            ix.update()
            fake.source = 'fake-model@2'
            ix._qcache.clear()
            self.assertIsNone(ix.query_vector('boil water'), 'vectors of two models are never compared')
            self.assertEqual(ix.update(), 3)
            self.assertEqual(ix.source, 'fake-model@2')
            self.assertEqual(ix.status['reset']['passages_dropped'], 4)
            self.assertEqual(len(ix.mat), 4)
            self.assertEqual(ix.vec_path.stat().st_size, 4 * DIM * 2)
            self.assertTrue(ix.semantic('boil water', floor=0.1))
        finally:
            fake.close()
            self.sock.unlink(missing_ok=True)

    def test_errors_back_off(self):
        fake = FakeRecall(self.sock)
        fake.start()
        try:
            r = retrieval.Recall(self.sock)
            fake.fail = 'Recall models are not ready'
            with self.assertRaises(retrieval.RecallUnavailable):
                r.embed(['x'])
            fake.fail = None
            calls = fake.calls
            with self.assertRaises(retrieval.RecallUnavailable):   # still backing off: Recall is not asked again yet
                r.embed(['x'])
            self.assertEqual(fake.calls, calls)
            r.down_until = 0
            self.assertEqual(r.embed(['boil water'] * 40).shape, (40, DIM))   # two requests: Recall takes 32 at a time
        finally:
            fake.close()
            self.sock.unlink(missing_ok=True)


ARTICLE = '''<html><body><div id="bodyContent"><h1>Choking</h1><div id="mw-content-text"><div class="mw-parser-output">
<style>.x{color:red}</style><table class="infobox"><tr><td>Specialty: Emergency medicine</td></tr></table>
<p>Choking is when the airway gets blocked<sup class="reference">[1]</sup> so that a person cannot breathe.</p>
<div class="hatnote navigation-not-searchable">Not to be confused with strangling.</div>
<h2><span class="mw-headline">Treatment</span></h2><ul><li>Give up to five back blows.</li><li>Then five abdominal thrusts.</li></ul>
<figure><figcaption>A diagram</figcaption></figure>
<h2>References</h2><ol class="references"><li>Some journal</li></ol><p>After the references.</p>
</div></div></div><div class="navbox">Health topics</div></body></html>'''


class Articles(unittest.TestCase):
    def test_sections(self):
        self.assertEqual(encyclopedia.article_sections(ARTICLE), [
            ('', 'Choking is when the airway gets blocked so that a person cannot breathe.'),
            ('Treatment', 'Give up to five back blows.\nThen five abdominal thrusts.')])

    def test_lead_only(self):
        self.assertEqual(encyclopedia.article_sections(ARTICLE, lead_only=True),
                         [('', 'Choking is when the airway gets blocked so that a person cannot breathe.')])

    def test_ids(self):
        d = encyclopedia.Encyclopedias.doc_id('mdwiki_en_all_maxi_2025-11.zim', 'Snake/bite_(a)')
        self.assertEqual(encyclopedia.Encyclopedias.split_id(d), ('mdwiki_en_all_maxi_2025-11.zim', 'Snake/bite_(a)'))
        self.assertIsNone(encyclopedia.Encyclopedias.split_id('med-123'))


class Warm(unittest.TestCase):
    """llm_local.Local.warm: starts the model ahead of a question and reads the shared prefix, at most once at a time."""

    def local(self, state='stopped', fits=True):
        import llm_local
        L = llm_local.Local.__new__(llm_local.Local)
        L.lock, L.gen_lock, L.warming = threading.RLock(), threading.Lock(), False
        L.state, L.path, L.fit = state, Path('/m.gguf'), {'fits': fits}
        L.started, L.read = threading.Event(), []
        L.ensure_running = lambda *a, **k: (L.started.wait(5), None)[1]
        L.generate = lambda segs, n, *a: L.read.append((segs, n))
        return L

    def test_not_set_up(self):
        for state, fits in (('absent', True), ('too_big', False), ('loading', True), ('stopped', False)):
            self.assertFalse(self.local(state, fits).warm([['x', False]]))

    def test_once_then_reads_prefix(self):
        L = self.local()
        self.assertTrue(L.warm([['<|turn>system\n', True], ['rules', False]]))
        self.assertFalse(L.warm([['x', False]]), 'a second warm while the first runs does nothing')
        L.started.set()
        for _ in range(100):
            if not L.warming:
                break
            threading.Event().wait(0.02)
        self.assertEqual(L.read, [([['<|turn>system\n', True], ['rules', False]], 1)])
        self.assertFalse(L.warming)

    def test_ubatch_by_memory(self):
        import llm_local
        self.assertIn(llm_local.ubatch(), (512, 1024, 2048))


if __name__ == '__main__':
    unittest.main()
