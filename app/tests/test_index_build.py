#!/usr/bin/env python3
"""The meaning-search indexer (retrieval.Index.update/build) when things go wrong, and `lastbook index` / `lastbook status`.

  PYTHONPATH=<deps> python3 -B app/tests/test_index_build.py [-v]

A stand-in Recall on a Unix socket (it can stop answering, reject one document, change its vectors' width) and a made-up
library in a temporary folder per test. The one invariant checked after every scenario: row i of vectors.f16 is the
vector of passage id i+1, for every passage search may return. No model, no network. Never packaged."""
import contextlib, fcntl, io, json, os, sqlite3, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
import numpy as np  # noqa: E402
import engine.paths as paths  # noqa: E402
import retrieval  # noqa: E402
from app.tests.fixture import FakeRecall, search_db, vector  # noqa: E402

SENTENCES = ['Boil the water for one minute to kill the germs before you drink it or cook with it.',
             'Press firmly on the wound with a clean cloth and keep pressing until the bleeding stops.',
             'Keep the fire small and clear the ground around it of dry grass and of fallen leaves.',
             'Store the grain in a dry place and keep it away from the rats and from the damp of the floor.',
             'Wash your hands with soap after the latrine and before you touch any food for the children.',
             'Dig the trench on the high side of the shelter so that the rain runs around it and not through.']


def page(doc, n):
    """A page long enough to be one passage, different for every (doc, n)."""
    return ' '.join(f'{s} This is page {n} of the {doc} manual.' for s in SENTENCES[n % 3:n % 3 + 3])


class Case(unittest.TestCase):
    """A library of three manuals: big (20 passages), small (2), other (3). Trust order as listed, so `big` is first."""
    DOCS = {'big': 20, 'small': 2, 'other': 3}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-index-'))
        self.saved = (retrieval.IDX, retrieval.LIB, retrieval.BUSY_PAUSE)
        retrieval.BUSY_PAUSE = 0.01
        quiet = contextlib.redirect_stderr(io.StringIO())   # the indexer says on standard error what it could not embed
        self.said = quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        retrieval.IDX, retrieval.LIB = self.tmp / 'derived' / 'index', self.tmp / 'library-data'
        retrieval.LIB.mkdir(parents=True)
        self.fts = retrieval.LIB / 'search.sqlite'
        search_db(self.fts, [(d, d, 'Manuals', 'health', f'documents/{d}.pdf', i, page(d, i)) for d, n in self.DOCS.items() for i in range(1, n + 1)])
        self.cat = {'by_id': {d: {'id': d, 'kind': 'pdf', 'title': f'{d} manual', 'sha256': d * 4, 'pages': n, 'path': f'documents/{d}.pdf'}
                              for d, n in self.DOCS.items()}, 'by_path': {}, 'video_tx': {}, 'thumbs': {}}
        self.order = {d: i for i, d in enumerate(self.DOCS)}
        self.sock = self.tmp / 'recall.sock'
        self.fake = FakeRecall(self.sock, dim=48)
        self.addCleanup(self.fake.close)

    def tearDown(self):
        retrieval.IDX, retrieval.LIB, retrieval.BUSY_PAUSE = self.saved

    def index(self):
        ix = retrieval.Index(lambda: self.cat, lambda r: ('Reference', 10 - self.order.get(r['id'], 9)), self.fts, retrieval.Recall(self.sock))
        self.addCleanup(ix.close)
        real = ix.build
        ix.build = lambda wait=0, pause=0.01, report=None: real(wait, pause, report)   # no quarter minute between two tries here
        return ix

    def rows(self, sql, *args):
        con = sqlite3.connect(retrieval.IDX / 'semantic.sqlite')
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def aligned(self, expect_docs=None):
        """Every passage search may return has its own vector at its own row; the file holds no more rows than the table."""
        dim = self.fake.dim
        raw = np.fromfile(retrieval.IDX / 'vectors.f16', dtype=np.float16)
        self.assertEqual(len(raw) % dim, 0)
        mat = raw.reshape(-1, dim)
        live = self.rows('SELECT id, doc_id, text FROM chunk WHERE active=1')
        top = self.rows('SELECT max(id) FROM chunk')[0][0] or 0
        self.assertEqual(len(mat), top, 'vector rows and passage rows')
        titles = {d: r['title'] for d, r in self.cat['by_id'].items()}
        for i, doc, text in live:
            want = np.asarray(vector(retrieval.DOCUMENT.format(titles[doc], text), dim), dtype=np.float16)
            self.assertTrue(np.array_equal(mat[i - 1], want), f'passage {i} of {doc} has another passage\'s vector')
        if expect_docs is not None:
            self.assertEqual({d: n for d, n in self.rows('SELECT doc_id, count(*) FROM chunk WHERE active=1 GROUP BY doc_id')}, expect_docs)


class Build(Case):
    def test_builds_until_nothing_is_pending(self):
        ix = self.index()
        self.assertFalse(ix._loaded, 'opening the index reads no vectors')
        self.assertEqual(ix.coverage(), {'total': 3, 'indexed': 0, 'no_text': 0, 'pending': 3, 'failed': 0, 'passages': 0})
        out = ix.build()
        self.assertEqual((out['outcome'], out['documents_indexed_now'], out['passages_indexed_now']), ('complete', 3, 25))
        self.assertEqual(ix.coverage(), {'total': 3, 'indexed': 3, 'no_text': 0, 'pending': 0, 'failed': 0, 'passages': 25})
        self.assertEqual((ix.status['state'], ix.status['chunks'], ix.status['docs'], ix.status['pending'], ix.status['total']), ('up to date', 25, 3, 0, 3))
        self.assertLessEqual(self.fake.biggest, retrieval.INDEX_BATCH)
        self.aligned(self.DOCS)
        self.assertIsInstance(ix.mat, np.memmap, 'the vectors are mapped, not read into memory')
        self.assertEqual(ix.semantic('boil the water page 7 of the big manual', k=1, floor=0.1, margin=-1)[0]['doc_id'], 'big')
        n = len(self.fake.embeds)
        self.assertEqual(ix.build()['documents_indexed_now'], 0)
        self.assertEqual(len(self.fake.embeds), n, 'nothing is embedded twice')

    def test_a_pass_cut_short_carries_on_where_it_was(self):
        """Recall stops answering in the middle of the first document. What was embedded is kept, and the next pass
        embeds the rest: every passage exactly once."""
        ix = self.index()
        self.fake.stop_after = 2                      # two batches of `big`, then nothing
        self.assertEqual(ix.update(), 0)
        self.assertEqual(ix.status['state'], 'waiting for Recall')
        self.assertEqual((ix.status['pending'], ix.status['indexed']), (3, 0))
        saved = self.rows('SELECT count(*) FROM chunk WHERE active=2')[0][0]
        self.assertEqual(saved, 2 * retrieval.INDEX_BATCH)
        self.assertEqual(self.rows('SELECT doc_id FROM docstate'), [], 'a document is not indexed until all of it is')
        self.assertEqual(ix.semantic('boil the water', floor=-1, margin=-1), [], 'and none of it is offered before')
        self.assertEqual(self.rows('SELECT count(*) FROM retry')[0][0], 0, 'Recall being away is not held against the document')
        self.aligned({})
        # another process (a new Index, as after a restart) takes it up
        self.fake.stop_after = None
        ix2 = self.index()
        self.assertEqual(ix2.build()['outcome'], 'complete')
        self.assertEqual(len(self.fake.embeds), 25)
        self.assertEqual(len(set(self.fake.embeds)), 25, 'no passage was embedded twice')
        self.aligned(self.DOCS)

    def test_a_changed_document_does_not_reuse_what_was_saved_of_the_old_one(self):
        ix = self.index()
        self.fake.stop_after = 1
        ix.update()
        self.cat['by_id']['big']['sha256'] = 'changed'
        self.fake.stop_after = None
        ix.indexer_recall.down_until = 0
        self.assertEqual(ix.build()['outcome'], 'complete')
        self.assertEqual(self.rows('SELECT count(*) FROM chunk WHERE active=2')[0][0], 0)
        self.aligned(self.DOCS)

    def test_one_document_recall_rejects_does_not_hold_up_the_rest(self):
        self.fake.reject = 'page 13 of the big manual'
        ix = self.index()
        out = ix.build()
        self.assertEqual(out['outcome'], 'incomplete')
        self.assertEqual(ix.coverage(), {'total': 3, 'indexed': 2, 'no_text': 0, 'pending': 0, 'failed': 1, 'passages': 5})
        self.assertEqual(ix.status['state'], 'incomplete')
        (doc, tries, reason), = self.rows('SELECT doc_id, tries, reason FROM retry')
        self.assertEqual((doc, tries), ('big', retrieval.MAX_TRIES))
        self.assertIn('cannot embed this', reason)
        self.aligned({'small': 2, 'other': 3})
        # left alone for a while, then tried again: this time Recall takes it, and only what was not saved is embedded
        calls = len(self.fake.embeds)
        self.assertEqual(ix.build()['documents_indexed_now'], 0)
        self.assertEqual(len(self.fake.embeds), calls)
        self.fake.reject = None
        con = sqlite3.connect(retrieval.IDX / 'semantic.sqlite')
        con.execute('UPDATE retry SET at=at-?', (retrieval.PARK_S + 1,))
        con.commit()
        con.close()
        self.assertEqual(ix.build()['outcome'], 'complete')
        self.assertEqual(len(set(self.fake.embeds)), 25)
        self.assertEqual(len(self.fake.embeds), 25)
        self.aligned(self.DOCS)

    def test_text_that_cannot_be_read_now_is_not_marked_indexed(self):
        """The keyword index is away for one pass (a drive that hiccups). Nothing is recorded as done; the next pass indexes."""
        ix = self.index()
        away = self.fts.with_name('away.sqlite')
        self.fts.rename(away)
        self.assertEqual(ix.update(), 0)
        self.assertEqual(self.rows('SELECT count(*) FROM docstate')[0][0], 0)
        self.assertEqual((ix.status['state'], ix.status['pending']), ('indexing', 3))
        self.assertEqual(ix.coverage()['pending'], 3)
        away.rename(self.fts)
        self.assertEqual(ix.update(), 3)
        self.assertEqual(ix.status['state'], 'up to date')
        self.assertEqual(self.rows('SELECT count(*) FROM retry')[0][0], 0)
        self.aligned(self.DOCS)

    def test_text_with_nothing_to_embed_is_finished(self):
        self.cat['by_id']['blank'] = {'id': 'blank', 'kind': 'html', 'title': 'Blank', 'sha256': 'b', 'pages': 1, 'path': 'pages/blank.html'}
        (retrieval.LIB / 'text').mkdir()
        (retrieval.LIB / 'text' / 'blank.txt').write_text('12 34 56\n', encoding='utf-8')
        self.cat['by_id']['lost'] = {'id': 'lost', 'kind': 'html', 'title': 'Lost', 'sha256': 'l', 'pages': 1, 'path': 'pages/lost.html'}   # no text anywhere
        ix = self.index()
        self.assertEqual(ix.build()['outcome'], 'incomplete')
        self.assertEqual(ix.coverage(), {'total': 5, 'indexed': 3, 'no_text': 1, 'pending': 0, 'failed': 1, 'passages': 25})
        self.assertEqual(self.rows('SELECT doc_id, reason FROM gaps'), [('blank', 'no extractable text')])
        self.assertEqual(self.rows('SELECT doc_id FROM retry'), [('lost',)])

    def test_a_second_engine_starting_cuts_nothing(self):
        """An indexer has appended a batch's vectors and not yet committed their rows. A second engine opening the index
        (and searching) leaves the file as it is; only the process holding the indexer lock realigns it."""
        ix = self.index()
        ix.build()
        vec = retrieval.IDX / 'vectors.f16'
        with open(vec, 'ab') as f:
            f.write(np.ones(self.fake.dim * 3, dtype=np.float16).tobytes())
        size = vec.stat().st_size
        other = retrieval.Index(lambda: self.cat, lambda r: ('Reference', 1.0), self.fts, retrieval.Recall(self.sock))
        self.addCleanup(other.close)
        self.assertEqual(len(other.mat), 25)
        self.assertTrue(other.semantic('boil the water', floor=-1, margin=-1))
        self.assertEqual(vec.stat().st_size, size)
        with open(retrieval.IDX / 'indexer.lock', 'a') as held:      # while someone indexes, update() stands down, and cuts nothing
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(other.update(), -1)
            self.assertEqual(other.status['state'], 'indexing (other process)')
            self.assertTrue(other.indexing_now())
            self.assertEqual(other.build()['outcome'], 'indexing elsewhere')
            self.assertEqual(vec.stat().st_size, size)
        self.assertFalse(other.indexing_now())
        self.assertEqual(other.update(), 0)                          # the indexer itself puts it right
        self.assertEqual(vec.stat().st_size, size - self.fake.dim * 3 * 2)
        self.aligned(self.DOCS)

    def test_engines_starting_over_and_over_while_one_indexes(self):
        """What used to cut vectors from under the indexer: other processes opening the index (as an engine does at its
        start) and searching it, again and again, while documents are being embedded."""
        self.cat['by_id'].update({f'more{i}': {'id': f'more{i}', 'kind': 'pdf', 'title': f'more{i} manual', 'sha256': 'm', 'pages': 2, 'path': 'x'} for i in range(40)})
        con = sqlite3.connect(self.fts)
        for i in range(40):
            for n in (1, 2):
                con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', (f'more{i}', 'more', 'Manuals', 'health', 'x', n, page(f'more{i}', n)))
        con.commit()
        con.close()
        code = f'''
import sys, time
sys.path[:0] = [{str(ROOT)!r}]
import retrieval
from pathlib import Path
retrieval.IDX, retrieval.LIB = Path({str(retrieval.IDX)!r}), Path({str(retrieval.LIB)!r})
cat = {{'by_id': {{}}, 'by_path': {{}}, 'video_tx': {{}}, 'thumbs': {{}}}}
end, n = time.time() + 30, 0
Path({str(self.tmp)!r}, f'ready-{{sys.argv[1]}}').touch()
while time.time() < end and not Path({str(self.tmp / 'done')!r}).exists():
    ix = retrieval.Index(lambda: cat, lambda r: ('Reference', 1.0), {str(self.fts)!r}, retrieval.Recall({str(self.sock)!r}))
    ix.semantic('boil the water', floor=-1, margin=-1)
    ix.close()
    n += 1
print(n)
'''
        others = [subprocess.Popen([sys.executable, '-B', '-c', code, str(i)], stdout=subprocess.PIPE, text=True) for i in range(3)]
        try:
            end = time.time() + 30
            while len(list(self.tmp.glob('ready-*'))) < 3 and time.time() < end:
                time.sleep(0.02)
            self.fake.delay = 0.03
            ix = self.index()
            self.assertEqual(ix.build()['outcome'], 'complete')
        finally:
            (self.tmp / 'done').touch()
            opened = [int(p.communicate(timeout=60)[0] or 0) for p in others]
        self.assertTrue(all(n > 5 for n in opened), opened)
        self.aligned({**self.DOCS, **{f'more{i}': 2 for i in range(40)}})

    def test_a_reader_follows_another_process_indexing(self):
        reader = self.index()
        self.assertEqual(reader.semantic('boil the water', floor=-1, margin=-1), [])
        writer = self.index()
        self.cat['by_id'].pop('other')
        writer.build()
        self.assertEqual(reader.semantic('rats and the damp page 2 of the small manual', k=1, floor=-1, margin=-1)[0]['doc_id'], 'small')
        self.assertEqual((reader.status['chunks'], reader.status['docs']), (22, 2))
        self.cat['by_id']['other'] = {'id': 'other', 'kind': 'pdf', 'title': 'other manual', 'sha256': 'o', 'pages': 3, 'path': 'documents/other.pdf'}
        writer.build()
        self.assertEqual(len(reader.semantic('manual', k=60, floor=-1, margin=-1)), 25)
        self.cat['by_id'].pop('small')                               # removed from the library: hidden, with no vector written
        writer.build()
        self.assertNotIn('small', {h['doc_id'] for h in reader.semantic('manual', k=60, floor=-1, margin=-1)})

    def test_a_question_that_times_out_does_not_stop_the_indexer(self):
        ix = self.index()
        self.assertIsNot(ix.recall, ix.indexer_recall)
        ix.recall.error, ix.recall.down_until = 'timeout', 9e12     # a search just failed: its client backs off
        self.assertEqual(ix.build()['outcome'], 'complete')
        self.assertIsNone(ix.query_vector('boil water'))
        ix.recall.down_until = 0
        self.assertIsNotNone(ix.query_vector('boil water'))
        r = retrieval.Recall(self.sock)                              # an answer ends the backing off for everyone using the client
        r.down_until = 0
        r.capabilities()
        self.assertEqual((r.down_until, r.error), (0.0, None))

    def test_questions_of_spaces_are_not_questions(self):
        ix = self.index()
        ix.build()
        n = self.fake.requests
        for q in ('', '   ', '\n\t'):
            self.assertIsNone(ix.query_vector(q))
            self.assertEqual(ix.semantic(q, floor=-1, margin=-1), [])
        self.assertEqual(self.fake.requests, n)

    def test_vectors_of_another_width_under_the_same_name_start_the_index_over(self):
        ix = self.index()
        ix.build()
        self.fake.dim = 32
        ix._qcache.clear()
        self.assertEqual(ix.semantic('boil the water', floor=-1, margin=-1), [], 'vectors of two widths are never compared')
        self.assertEqual(ix.build()['outcome'], 'complete')
        self.assertEqual((ix.dim, ix.status['reset']['why'], ix.status['reset']['passages_dropped']), (32, 'Recall vector width changed', 25))
        self.aligned(self.DOCS)
        self.assertTrue(ix.semantic('boil the water', floor=-1, margin=-1))

    def test_a_new_model_while_a_reader_has_the_old_vectors_mapped(self):
        reader = self.index()
        writer = self.index()
        writer.build()
        self.assertEqual(len(reader.mat), 25)
        self.fake.source = 'fake-model@2'
        self.cat['by_id'].pop('big')
        writer.build()
        self.assertEqual(writer.status['reset']['why'], 'Recall model changed')
        reader._qcache.clear()
        hits = reader.semantic('manual', k=60, floor=-1, margin=-1)
        self.assertEqual((len(hits), reader.source), (5, 'fake-model@2'))
        self.aligned({'small': 2, 'other': 3})

    def test_a_damaged_index_file_stops_nothing(self):
        ix = self.index()
        ix.build()
        db = retrieval.IDX / 'semantic.sqlite'
        for f in retrieval.IDX.glob('semantic.sqlite-*'):
            f.unlink()
        db.write_bytes(b'this is not a database ' * 400)
        reader = self.index()                                        # every command opens the index: it must open
        self.assertEqual((reader.source, reader.status['chunks'], bool(reader.status['damaged'])), (None, 0, True))
        self.assertEqual(reader.semantic('boil the water', floor=-1, margin=-1), [])
        self.assertIsNone(reader.query_vector('boil the water'))
        self.assertEqual(reader.coverage()['pending'], 3)
        self.assertEqual(reader.build()['outcome'], 'complete')      # the indexer sets it aside and builds a new one
        self.assertTrue((retrieval.IDX / 'semantic.sqlite.damaged').is_file())
        self.assertEqual(reader.status['reset']['why'], 'the index file was damaged')
        self.assertNotIn('damaged', reader.status)
        self.aligned(self.DOCS)
        self.assertTrue(reader.semantic('boil the water', floor=-1, margin=-1))

    def test_rows_without_vectors_are_embedded_again(self):
        ix = self.index()
        ix.build()
        vec = retrieval.IDX / 'vectors.f16'
        with open(vec, 'r+b') as f:                                   # the end of the file was lost (a crash before it reached the disk)
            f.truncate(21 * self.fake.dim * 2)
        ix2 = self.index()
        self.assertEqual(ix2.build()['outcome'], 'complete')
        self.assertTrue(ix2.status['repaired']['rows_without_vectors'])
        self.aligned(self.DOCS)

    def test_without_recall_it_says_so_and_ends(self):
        self.fake.close()
        ix = self.index()
        out = ix.build(wait=600)       # no socket: nobody to wait for
        self.assertEqual((out['outcome'], out['documents_indexed_now']), ('waiting for Recall', 0))
        self.assertLess(out['seconds'], 5)
        self.assertEqual((ix.status['state'], ix.status['recall'], ix.status['pending'], ix.status['total']), ('waiting for Recall', 'not running', 3, 3))

    def test_a_busy_recall_is_waited_for_only_as_long_as_asked(self):
        self.fake.stop_after = 0
        ix = self.index()
        out = ix.build(wait=0)
        self.assertEqual(out['outcome'], 'waiting for Recall')
        threading.Timer(0.5, lambda: setattr(self.fake, 'stop_after', None)).start()
        out = ix.build(wait=30, pause=0.3)
        self.assertEqual(out['outcome'], 'complete')
        self.aligned(self.DOCS)

    def test_asked_to_stop_it_keeps_what_it_has(self):
        ix = self.index()
        real = ix.indexer_recall.embed

        def embed(texts, **kw):
            if len(self.fake.embeds) >= retrieval.INDEX_BATCH:
                ix.stop.set()                                         # SIGTERM, as `lastbook index` hears it
            return real(texts, **kw)
        ix.indexer_recall.embed = embed
        out = ix.build()
        self.assertEqual(out['outcome'], 'stopped')
        self.assertGreater(self.rows('SELECT count(*) FROM chunk WHERE active=2')[0][0], 0)
        self.aligned({})
        self.assertEqual(self.index().build()['outcome'], 'complete')
        self.assertEqual(len(set(self.fake.embeds)), len(self.fake.embeds))
        self.aligned(self.DOCS)


class Videos(Case):
    DOCS = {'small': 2}

    def test_a_transcript_rewritten_in_place_is_embedded_again(self):
        (retrieval.LIB / 'videos').mkdir()
        vtt = retrieval.LIB / 'videos' / 'a.en.vtt'

        def captions(word):
            vtt.write_text('WEBVTT\n\n' + ''.join(f'00:0{i}:00.000 --> 00:0{i}:50.000\nGather the dry {word} first and keep it out of the rain {i}\n\n' for i in range(4)), encoding='utf-8')
        captions('tinder')
        self.cat['by_id']['video-a'] = {'id': 'video-a', 'kind': 'video', 'path': 'videos/a.mp4', 'title': 'Fire', 'captions': ['videos/a.en.vtt']}
        ix = self.index()
        self.assertEqual(ix.build()['outcome'], 'complete')
        self.assertIn('tinder', ix.semantic('dry tinder', k=1, floor=-1, margin=-1, doc='video-a')[0]['text'])
        self.assertEqual(ix.build()['documents_indexed_now'], 0)
        captions('kindling')
        self.assertEqual(ix.coverage()['pending'], 1)
        self.assertEqual(ix.build()['documents_indexed_now'], 1)
        texts = [h['text'] for h in ix.semantic('dry kindling', k=9, floor=-1, margin=-1, doc='video-a')]
        self.assertTrue(texts and all('kindling' in t for t in texts))


class CommandLine(Case):
    """`lastbook index` and `lastbook status` as the native program runs them: a Python process each."""
    DOCS = {'manual': 5, 'guide': 2}

    def setUp(self):
        super().setUp()
        lib = retrieval.LIB
        (lib / 'catalog.json').write_text(json.dumps([{**r, 'publisher': 'Test'} for r in self.cat['by_id'].values()]), encoding='utf-8')
        (lib / 'summary.json').write_text('{"resources": 2, "pdfs": 2, "pdf_pages": 7, "web_pages": 0, "videos": 0}', encoding='utf-8')
        self.env = {**os.environ, 'HOME': str(self.tmp / 'home'), 'XDG_CONFIG_HOME': str(self.tmp / 'home/config'), 'XDG_DATA_HOME': str(self.tmp / 'home/data'),
                    'XDG_CACHE_HOME': str(self.tmp / 'home/cache'), 'LASTBOOK_RECALL_SOCKET': str(self.sock), 'LASTBOOK_INDEX_KICK': '0'}
        for k in ('LASTBOOK_DATA', 'LASTBOOK_DEV', 'LASTBOOK_OPEN'):
            self.env.pop(k, None)

    def run_cli(self, *args, data=True, env=None):
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'app' / 'cli.py'), *(['--data', str(self.tmp)] if data else []), *args],
                           capture_output=True, text=True, env=env or self.env, stdin=subprocess.DEVNULL, timeout=120)
        return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr

    def test_status_says_how_much_is_indexed_and_index_finishes_it(self):
        code, out, err = self.run_cli('status')
        self.assertEqual(code, 0, err)
        m = out['meaning_search']
        self.assertEqual({k: m[k] for k in ('ready', 'complete', 'coverage', 'state', 'passages', 'documents', 'documents_total', 'pending', 'failed', 'no_text')},
                         {'ready': False, 'complete': False, 'coverage': 0.0, 'state': 'idle', 'passages': 0, 'documents': 0, 'documents_total': 2,
                          'pending': 2, 'failed': 0, 'no_text': 0})
        self.assertEqual(m['recall'], {'installed': True, 'running': True, 'model': 'fake-model@1'})
        self.assertFalse((retrieval.IDX / 'vectors.f16').exists(), 'status builds nothing')

        self.fake.stop_after = 1                                      # a first run that Recall leaves after one document
        code, out, err = self.run_cli('index')
        self.assertEqual((code, out['outcome']), (0, 'waiting for Recall'), err)
        m = self.run_cli('status')[1]['meaning_search']
        self.assertEqual((m['ready'], m['complete'], m['documents'], m['pending'], m['coverage']), (False, False, 1, 1, 0.5))   # `manual` went in whole

        self.fake.stop_after = None
        code, out, err = self.run_cli('index')
        self.assertEqual(code, 0, err)
        self.assertEqual((out['outcome'], out['documents_indexed_now']), ('complete', 1))
        self.assertEqual(out['meaning_search']['complete'], True)
        self.assertEqual(len(set(self.fake.embeds)), 7)
        self.assertEqual(len(self.fake.embeds), 7)
        m = self.run_cli('status')[1]['meaning_search']
        self.assertEqual({k: m[k] for k in ('ready', 'complete', 'coverage', 'state', 'passages', 'documents', 'documents_total', 'pending', 'failed')},
                         {'ready': True, 'complete': True, 'coverage': 1.0, 'state': 'idle', 'passages': 7, 'documents': 2, 'documents_total': 2,
                          'pending': 0, 'failed': 0})
        self.assertEqual(m['recall']['width'], self.fake.dim)
        self.aligned(self.DOCS)
        code, out, err = self.run_cli('index')                        # again: nothing to do
        self.assertEqual((code, out['outcome'], out['documents_indexed_now'], out['passages_indexed_now']), (0, 'complete', 0, 0), err)

        self.fake.dim = 16                                            # the same model name, other vectors: not ready, and it says why
        m = self.run_cli('status')[1]['meaning_search']
        self.assertEqual((m['ready'], m['recall']['width']), (False, 16))

    def test_a_partial_index_is_ready_and_says_how_partial(self):
        self.fake.reject = 'page 2 of the guide manual'
        code, out, err = self.run_cli('index', '--pause', '0')
        self.assertEqual((code, out['outcome']), (0, 'incomplete'), err)
        self.assertIn('guide', err)                                   # which document, and why, on standard error
        m = out['meaning_search']
        self.assertEqual((m['ready'], m['complete'], m['coverage'], m['documents'], m['documents_total'], m['pending'], m['failed']),
                         (True, False, 0.5, 1, 2, 0, 1))

    def test_index_stands_down_while_another_process_indexes(self):
        retrieval.IDX.mkdir(parents=True)
        with open(retrieval.IDX / 'indexer.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = self.run_cli('index')
            self.assertEqual((code, out['outcome'], err), (0, 'indexing elsewhere', ''))
            self.assertEqual(out['meaning_search']['state'], 'indexing')
            self.assertEqual(self.run_cli('status')[1]['meaning_search']['state'], 'indexing')
        self.assertEqual(self.fake.embeds, [])

    def test_no_recall_and_no_library_are_not_errors_for_index(self):
        self.fake.close()
        code, out, err = self.run_cli('index', '--wait', '60')
        self.assertEqual(code, 0, err)
        self.assertIn(out['outcome'], ('waiting for Recall', 'Recall not installed'))    # by whether this computer has Recall
        m = out['meaning_search']
        self.assertEqual((m['ready'], m['complete'], m['pending'], m['recall']['running']), (False, False, 2, False))
        self.assertIn(m['state'], ('waiting for Recall', 'Recall not installed'))
        self.assertEqual(m['state'] == 'Recall not installed', not m['recall']['installed'])
        code, out, err = self.run_cli('index', data=False)            # the service at a login before any library
        self.assertEqual((code, out, err), (0, {'outcome': 'no library'}, ''))
        code, out, err = self.run_cli('index', '--data', str(self.tmp / 'home'), data=False)   # a folder named and wrong is an error, as ever
        self.assertEqual((code, out), (1, None))
        self.assertEqual(self.run_cli('index', '--wait', 'x')[0], 1)   # a bad argument is a failure like any other

    def test_a_damaged_index_file_breaks_no_command(self):
        self.run_cli('index')
        for f in retrieval.IDX.glob('semantic.sqlite-*'):
            f.unlink()
        (retrieval.IDX / 'semantic.sqlite').write_bytes(b'garbage' * 999)
        code, out, err = self.run_cli('search', 'boil the water')
        self.assertEqual(code, 0, err)
        self.assertEqual(out['results'][0]['doc_id'], 'manual')       # by keyword
        code, out, err = self.run_cli('status')
        self.assertEqual(code, 0, err)
        m = out['meaning_search']
        self.assertEqual((m['ready'], m['damaged'], m['pending']), (False, True, 2))
        self.assertEqual(self.run_cli('index')[1]['outcome'], 'complete')
        self.assertEqual(self.run_cli('status')[1]['meaning_search']['ready'], True)


class Units(unittest.TestCase):
    """The user units that run `lastbook index` with no window, and that the package installs and switches on."""

    def test_units(self):
        service = (ROOT / 'packaging' / 'systemd' / retrieval.UNIT).read_text(encoding='utf-8')
        path = (ROOT / 'packaging' / 'systemd' / retrieval.PATH_UNIT).read_text(encoding='utf-8')
        wants = (ROOT / 'packaging' / 'systemd' / 'default.target.d' / 'lastbook-index.conf').read_text(encoding='utf-8')
        self.assertIn('ExecStart=/usr/bin/lastbook index', service)
        for line in ('Type=exec', 'Restart=no', 'Nice=19', 'IOSchedulingClass=idle', 'CPUSchedulingPolicy=idle'):
            self.assertIn(line, service)
        self.assertNotIn('Restart=', service.replace('Restart=no', ''))                     # it ends when it is done; nothing loops
        # the socket retrieval.recall_socket() names, under the user's runtime folder
        os.environ.pop('LASTBOOK_RECALL_SOCKET', None)
        self.assertIn(f'PathChanged=%t/{retrieval.recall_socket().name}', path)
        self.assertIn(f'Unit={retrieval.UNIT}', path)
        self.assertIn(f'Wants={retrieval.UNIT} {retrieval.PATH_UNIT}', wants)
        pkgbuild = (ROOT / 'packaging' / 'arch' / 'PKGBUILD').read_text(encoding='utf-8')
        for f in (retrieval.UNIT, retrieval.PATH_UNIT, 'default.target.d/lastbook-index.conf'):
            self.assertIn(f'packaging/systemd/{f}', pkgbuild)

    def test_kick_asks_systemd_and_never_fails(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {'LASTBOOK_INDEX_KICK': '1'}), mock.patch('subprocess.Popen') as popen:
            self.assertTrue(retrieval.kick())
            self.assertEqual(popen.call_args[0][0], ['systemctl', '--user', 'start', '--no-block', retrieval.PATH_UNIT, retrieval.UNIT])
            self.assertFalse(retrieval.kick(default_library=False))    # the service indexes the library it finds, not this one
            popen.side_effect = FileNotFoundError('systemctl')
            self.assertFalse(retrieval.kick())
        with mock.patch.dict(os.environ, {'LASTBOOK_INDEX_KICK': '0'}), mock.patch('subprocess.Popen') as popen:
            self.assertFalse(retrieval.kick())
            popen.assert_not_called()
        with mock.patch('subprocess.Popen') as popen:                  # a source checkout has no unit to start
            os.environ.pop('LASTBOOK_INDEX_KICK', None)
            self.assertEqual(retrieval.kick(), paths.INSTALLED)


if __name__ == '__main__':
    unittest.main()
