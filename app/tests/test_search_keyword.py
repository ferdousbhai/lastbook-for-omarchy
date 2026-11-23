#!/usr/bin/env python3
"""Keyword search: what search says when the library is damaged, and
the order of what server.hybrid() gives. Against a small made-up library; no Recall, no window, no port.

  PYTHONPATH=<deps> python3 -B -m unittest app.tests.test_search_keyword

The engine reads its data folder when it is first imported, so each check that needs server.py runs it in a process of
its own (as the command line does), over a copy of the library made here."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
from app.tests.fixture import search_db  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix='lastbook-search-'))
ENV = {**os.environ, 'HOME': str(TMP / 'home'), 'XDG_CONFIG_HOME': str(TMP / 'home/config'), 'XDG_DATA_HOME': str(TMP / 'home/data'),
       'XDG_CACHE_HOME': str(TMP / 'home/cache'), 'LASTBOOK_RECALL_SOCKET': str(TMP / 'no-recall.sock'),
       'PYTHONPATH': os.pathsep.join([str(ROOT), str(ROOT / 'app'), os.environ.get('PYTHONPATH', '')])}
for k in ('LASTBOOK_DATA', 'LASTBOOK_DEV', 'LASTBOOK_OPEN'):
    ENV.pop(k, None)
ROLLING = ('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\ngather dry tinder first\n\n'          # as auto captions are written:
           '00:00:03.000 --> 00:00:03.010\ngather dry tinder first\n\n'                     # each cue repeats the line before
           '00:00:03.010 --> 00:00:06.000\ngather dry tinder first\nthen strike the rod\n\n'
           '00:00:06.000 --> 00:00:06.010\nthen strike the rod\n\n'
           '00:00:06.010 --> 00:00:09.000\nthen strike the rod\nand shield the flame\n\n'
           '00:00:09.000 --> 00:00:09.010\nand shield the flame\n')


def library(name):
    """A first-aid PDF of two pages, a saved page, a scanned PDF with no text (so no row in the index), a video with
    rolling captions, and four pages that only fill a list."""
    data = TMP / name
    lib = data / 'library-data'
    for d in ('text', 'videos', 'documents', 'pages'):
        (lib / d).mkdir(parents=True)
    (lib / 'videos' / 'a.en.vtt').write_text(ROLLING, encoding='utf-8')
    for f in ('documents/aid.pdf', 'documents/scan.pdf', 'pages/w.html', 'videos/a.mp4'):
        (lib / f).write_bytes(b'x')
    recs = [{'id': 'aid', 'kind': 'pdf', 'path': 'documents/aid.pdf', 'title': 'First Aid Manual', 'publisher': 'Test', 'pages': 2},
            {'id': 'scan', 'kind': 'pdf', 'path': 'documents/scan.pdf', 'title': 'Vegetable Planting Schedule', 'publisher': 'Test', 'pages': 1},
            {'id': 'web', 'kind': 'html', 'path': 'pages/w.html', 'title': 'Safe water', 'publisher': 'Test'},
            {'id': 'video-a', 'kind': 'video', 'path': 'videos/a.mp4', 'title': 'Fire', 'publisher': 'Test', 'duration': 90, 'captions': ['videos/a.en.vtt']}]
    rows = [('aid', 'First Aid Manual', 'Test', 'test', 'documents/aid.pdf', 1, 'Stop the bleeding by pressing hard on the wound.'),
            ('aid', 'First Aid Manual', 'Test', 'test', 'documents/aid.pdf', 2, 'Cool burned skin under running water.'),
            ('web', 'Safe water', 'Test', 'test', 'pages/w.html', 1, 'Boil river water for one minute before drinking it.'),
            ('video-a', 'Fire', 'Test', 'test', 'videos/a.mp4', 1, 'gather dry tinder first then strike the rod and shield the flame')]
    for i in range(4):
        (lib / 'documents' / f'f{i}.pdf').write_bytes(b'x')
        recs.append({'id': f'fill{i}', 'kind': 'pdf', 'path': f'documents/f{i}.pdf', 'title': f'Filler {i}', 'publisher': 'Test', 'pages': 1})
        rows.append((f'fill{i}', f'Filler {i}', 'Test', 'test', f'documents/f{i}.pdf', 1, f'Keep river water in clean jars, note {i}.'))
    (lib / 'catalog.json').write_text(json.dumps(recs))
    (lib / 'summary.json').write_text('{"resources": 8}')
    search_db(lib / 'search.sqlite', rows)
    return data


def cli(data, *args, env=None):
    """(exit code, the JSON printed or None, standard error) of the command line over this library."""
    r = subprocess.run([sys.executable, '-B', str(ROOT / 'app' / 'cli.py'), '--data', str(data), *args],
                       capture_output=True, text=True, env={**ENV, **(env or {})}, stdin=subprocess.DEVNULL)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr


def engine(data, code, env=None):
    """What `code` leaves in `out`, run in a process with server.py started over this library."""
    head = 'import json, sys, types\nimport server\nserver.init(no_index=True)\nout = None\n'
    r = subprocess.run([sys.executable, '-B', '-c', head + code + '\nprint(json.dumps(out))'], capture_output=True, text=True,
                       env={**ENV, 'LASTBOOK_DATA': str(data), **(env or {})}, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise AssertionError(r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def tree(folder):
    return sorted((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns) for p in Path(folder).rglob('*') if p.is_file())




class ClinicalTexts(unittest.TestCase):
    def test_lay_health_guides_are_not_put_after_general_guidance(self):
        """The down-weight for surgeons' and clinicians' texts goes by title and collection name. The collection that
        holds Where There Is No Doctor is called "Low-resource clinical care": those are the lay guides themselves."""
        code = ("out = [server.for_clinicians(r) for r in ("
                "{'title': 'Where There Is No Doctor (2025) — 10 First aid', 'collection': 'Low-resource clinical care'},"
                "{'title': 'MSF Clinical Guidelines', 'collection': 'MSF Clinical Guidelines'},"
                "{'title': '3.4 Hygiene', 'collection': 'MSF Management of a Cholera Epidemic'},"
                "{'title': 'War Surgery', 'collection': 'Low-resource clinical care'},"
                "{'title': 'First aid manual'})]")
        self.assertEqual(engine(library('clinical'), code), [False, True, True, True, False])


class DamagedLibrary(unittest.TestCase):
    """A library that cannot be read is an error (exit 1), never "no results" (exit 0)."""

    def test_unreadable_catalog(self):
        for name, text in (('cat-cut', '[{"id": "aid", "kind": "pdf", "pa'), ('cat-shape', '{"records": []}')):
            data = library(name)
            (data / 'library-data' / 'catalog.json').write_text(text)
            code, out, err = cli(data, 'search', 'water')
            self.assertEqual((code, out), (1, None), name)
            self.assertIn('catalog', err)
            self.assertNotIn('Traceback', err)

    def test_keyword_index_with_nothing_in_it(self):
        data = library('index-empty')
        (data / 'library-data' / 'search.sqlite').write_bytes(b'')
        code, out, err = cli(data, 'search', 'water')
        self.assertEqual((code, out), (1, None))
        self.assertIn('keyword index', err)
        self.assertNotIn('Traceback', err)

    def test_a_question_of_signs_only_is_no_results_not_an_error(self):
        code, out, err = cli(library('signs'), 'search', '"" ** ((')
        self.assertEqual((code, out['results']), (0, []), err)


FAKES = '''
class Enc:
    """Three articles, best first, as encyclopedia.Encyclopedias.search gives them."""
    asked = None
    def search(self, words, groups, qv=None, k=2):
        Enc.asked = k
        return [{'doc_id': f'zim:t.zim/{n}', 'heading': '', 'text': n, 'sem': None,
                 'record': {'id': f'zim:t.zim/{n}', 'kind': 'article', 'title': n, 'path': f'zim/t.zim/{n}', 'publisher': 'Wiki'}} for n in ('Best', 'Second', 'Third')][:k]
server.ENC = Enc()
'''


class Order(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = library('order')

    def test_encyclopedias_alone_are_best_first_and_as_many_as_asked(self):
        got = engine(self.data, FAKES + "out = [[r['title'] for r in server.hybrid('river water', k, 'encyclopedia')] for k in (8, 2)] + [Enc.asked]")
        self.assertEqual(got, [['Best', 'Second', 'Third'], ['Best', 'Second'], 2])

    def test_mixed_list_pages_first_then_articles_in_their_own_order(self):
        got = engine(self.data, FAKES + "out = [[(r['kind'], r['title']) for r in server.hybrid('river water', k)] for k in (8, 6, 4)]")
        shape = lambda rows: [t if k == 'article' else 'page' for k, t in rows]
        self.assertEqual(shape(got[0]), ['page'] * 6 + ['Best', 'Second'])   # six pages hold a word; then the articles, the best before the next
        self.assertEqual(shape(got[1]), ['page'] * 5 + ['Best'])             # a full list of six keeps its last place for the best article
        self.assertEqual((len(got[2]), [t for k, t in got[2] if k == 'article']), (4, []))   # a short list: the pages fill it

    def test_a_video_found_by_words_and_by_meaning_is_one_result(self):
        code = '''
class Index:
    source, mat, status = 'model', [1], {}
    def query_vector(self, q): return 'qv'
    def semantic(self, q, k=60, allowed=None, floor=0, doc=None):
        hit = {'doc_id': 'video-a', 'page': None, 't': 50.0, 'text': 'then strike the rod and shield the flame', 'sem': 0.8}
        return [hit] if doc in (None, 'video-a') else []
server.INDEX = Index()
server.ENC = None
out = [(r['doc_id'], r['t'], sorted(r['found_by'])) for r in server.hybrid('strike the rod', 8, 'video')]'''
        self.assertEqual(engine(self.data, code), [['video-a', 50.0, ['keyword', 'meaning']]])


class Small(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = library('small')

    def test_rolling_captions_say_each_line_once(self):
        got = engine(self.data, "out = ' '.join(c['text'] for c in server.transcript('video-a')['cues'])")
        self.assertEqual(got, 'gather dry tinder first then strike the rod and shield the flame')

    def test_encyclopedia_entry_names_may_start_with_dots(self):
        code = '''ok = lambda p: server.Handler.path_ok(types.SimpleNamespace(SERVED_PREFIXES=server.Handler.SERVED_PREFIXES), p)
out = [ok('/zim/a.zim/The Future We Deserve/...middle... - Nick Taylor'), ok('/zim/a.zim/_assets_/0c/..Tamil_Nadu_Flag.png'),
       ok('/zim/a.zim/../../catalog.json'), ok('/zim/..'), ok('/documents/../catalog.json'), ok('/documents/..hidden/x.pdf')]'''
        self.assertEqual(engine(self.data, code), [True, True, False, False, False, False])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
