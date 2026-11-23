#!/usr/bin/env python3
"""What the reading screens and the Search screen ask of the engine: a document's text a page at a time (find in the
document, its page copied), the documents related to one, and where meaning search stands. Against a small made-up
library and a stand-in Recall; no window, no port, no network.

  PYTHONPATH=<deps> python3 -B -m unittest app.tests.test_reading

The engine reads its data folder when it is first imported, so the routes are asked in a process of their own, over a
library made here, as test_search_keyword does."""
import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(HERE)]

TMP = Path(tempfile.mkdtemp(prefix='lastbook-reading-'))
ENV = {**os.environ, 'HOME': str(TMP / 'home'), 'XDG_CONFIG_HOME': str(TMP / 'home/config'), 'XDG_DATA_HOME': str(TMP / 'home/data'),
       'XDG_CACHE_HOME': str(TMP / 'home/cache'), 'LASTBOOK_RECALL_SOCKET': str(TMP / 'no-recall.sock'),
       'PYTHONPATH': os.pathsep.join([str(ROOT), str(ROOT / 'app'), os.environ.get('PYTHONPATH', '')])}
for k in ('LASTBOOK_DATA', 'LASTBOOK_DEV', 'LASTBOOK_OPEN'):
    ENV.pop(k, None)
TABLE = 'CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")'
PAGES = ['Boil the water for one minute.', 'Store the treated water in a clean container.']


def library():
    """A water manual of two pages (its text as the library keeps it, a form feed between pages), another about
    water, one about fire, and a saved page."""
    data = TMP / 'data'
    lib = data / 'library-data'
    for d in ('text', 'documents', 'pages'):
        (lib / d).mkdir(parents=True)
    (lib / 'text' / 'water.txt').write_text('\n\f\n'.join(PAGES), encoding='utf-8')
    recs = [{'id': 'water', 'kind': 'pdf', 'path': 'documents/water.pdf', 'title': 'Safe water', 'publisher': 'Test', 'pages': 2},
            {'id': 'wells', 'kind': 'pdf', 'path': 'documents/wells.pdf', 'title': 'Wells', 'publisher': 'Test', 'pages': 1},
            {'id': 'fire', 'kind': 'pdf', 'path': 'documents/fire.pdf', 'title': 'Fire', 'publisher': 'Test', 'pages': 1},
            {'id': 'rope', 'kind': 'html', 'path': 'pages/rope.html', 'title': 'Rope', 'publisher': 'Test'}]
    for r in recs:
        (lib / r['path']).write_bytes(b'x')
    (lib / 'catalog.json').write_text(json.dumps(recs))
    (lib / 'summary.json').write_text('{"resources": 4}')
    con = sqlite3.connect(lib / 'search.sqlite')
    con.execute(TABLE)
    con.executemany('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', [
        ('water', 'Safe water', 'Test', 'test', 'documents/water.pdf', 1, PAGES[0]),
        ('water', 'Safe water', 'Test', 'test', 'documents/water.pdf', 2, PAGES[1]),
        ('wells', 'Wells', 'Test', 'test', 'documents/wells.pdf', 1, 'Dig the well uphill from latrines to keep the water safe.'),
        ('fire', 'Fire', 'Test', 'test', 'documents/fire.pdf', 1, 'Gather dry tinder first.'),
        ('rope', 'Rope', 'Test', 'test', 'pages/rope.html', 1, 'A bowline makes a fixed loop.')])
    con.commit()
    con.close()
    return data


def routes(data, *paths):
    """The engine's answer to each path, through the bridge as the window asks: [(status, JSON)]."""
    code = f'''
import json, threading
import server, bridge as B
server.init(no_index=True)
class Rec(B.Sink):
    def __init__(self):
        self.status, self.parts, self.done = None, [], threading.Event()
    def head(self, status, reason, headers):
        self.status = status
    def data(self, chunk):
        self.parts.append(chunk)
    def finish(self, ok):
        self.done.set()
b, out = B.Bridge(server), []
for p in {list(paths)!r}:
    r = Rec()
    b.start('GET', p, None, r)
    r.done.wait(30)
    out.append([r.status, json.loads(b''.join(r.parts))])
print(json.dumps(out))'''
    r = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True,
                       env={**ENV, 'LASTBOOK_DATA': str(data)}, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise AssertionError(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = library()

    def test_a_documents_text_page_by_page(self):
        (status, text), (missing, _) = routes(self.data, '/api/text/water', '/api/text/nothing')
        self.assertEqual((status, text), (200, {'pages': PAGES}))
        self.assertEqual(missing, 404)

    def test_related_without_a_meaning_index_is_the_search_for_the_title(self):
        """No passage of the library is embedded (no Recall): what is related is what the title finds, the document
        itself left out, each once and opening at its start."""
        [(_, by_id), (_, by_path), (_, unknown)] = routes(self.data, '/api/related?doc=water', '/api/related?path=/pages/rope.html',
                                                          '/api/related?doc=nothing')
        self.assertEqual([r['doc_id'] for r in by_id['results']], ['wells'])
        self.assertEqual((by_id['results'][0]['page'], by_id['results'][0]['title']), (None, 'Wells'))
        self.assertEqual(by_path['results'], [])   # nothing else mentions rope
        self.assertEqual(unknown['results'], [])

    def test_search_status_is_what_lastbook_status_says(self):
        [(status, m)] = routes(self.data, '/api/search-status')
        self.assertEqual(status, 200)
        self.assertFalse(m['ready'])
        self.assertEqual(m['recall']['running'], False)
        self.assertEqual((m['documents_total'], m['pending']), (4, 4))


class Centroid(unittest.TestCase):
    """The average of a document's passages, by meaning: found with no question asked, and with Recall gone. retrieval
    is imported only here, as the tests run: importing it fixes the data folder (engine/paths.py), which test_bridge,
    loaded beside this, sets first."""

    def setUp(self):
        global retrieval
        import retrieval
        self.was = retrieval.IDX
        retrieval.IDX = TMP / 'index'   # this test's own: test_semantic's are left as they are
        retrieval.IDX.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        retrieval.IDX = self.was

    def test_the_nearest_documents_to_one(self):
        from test_semantic import FakeRecall, make_library
        cat, db = make_library()
        sock = TMP / 'recall.sock'
        fake = FakeRecall(sock)
        fake.start()
        ix = retrieval.Index(lambda: cat, lambda r: ('Reference', 1.0), db, retrieval.Recall(sock))
        try:
            self.assertEqual(ix.update(), 3)
        finally:
            fake.close()
        v = ix.centroid('water')
        self.assertAlmostEqual(float((v * v).sum()), 1.0, places=5)
        near = ix.semantic(v, 10, lambda d: d != 'water', floor=-1, margin=-2)
        self.assertTrue(near)
        self.assertNotIn('water', {h['doc_id'] for h in near})
        self.assertIsNone(ix.centroid('nothing'))


if __name__ == '__main__':
    unittest.main()
