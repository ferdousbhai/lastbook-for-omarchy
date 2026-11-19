#!/usr/bin/env python3
"""The command line (app/cli.py): what each command prints, and its exit codes.

  PYTHONPATH=<deps> python3 -B app/tests/test_cli.py [-v]

Runs the commands as the native program does (a Python process each) against the made-up library the bridge tests use,
and against a second small one made here with a text, a transcript and a gazetteer. No model, no Recall. Never packaged."""
import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE)]
import fixture  # noqa: E402
DATA = fixture.ensure()
TMP = Path(tempfile.mkdtemp(prefix='lastbook-cli-'))
ENV = {**os.environ, 'HOME': str(TMP / 'home'), 'XDG_CONFIG_HOME': str(TMP / 'home/config'), 'XDG_DATA_HOME': str(TMP / 'home/data'),
       'XDG_CACHE_HOME': str(TMP / 'home/cache'), 'LASTBOOK_RECALL_SOCKET': str(TMP / 'no-recall.sock')}
for k in ('LASTBOOK_DATA', 'LASTBOOK_DEV', 'LASTBOOK_LOCAL_AI', 'LASTBOOK_OPEN'):
    ENV.pop(k, None)


def run(*args, data=DATA, env=ENV):
    """(exit code, the JSON printed or None, standard error)."""
    r = subprocess.run([sys.executable, '-B', str(ROOT / 'app' / 'cli.py'), *(['--data', str(data)] if data else []), *args],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr


def small_library():
    """A PDF's text (two pages), a saved page, a video with captions and one town."""
    data = TMP / 'LastbookData'
    lib = data / 'library-data'
    for d in ('text', 'videos', 'documents', 'pages'):
        (lib / d).mkdir(parents=True)
    (data / 'derived' / 'geo').mkdir(parents=True)
    (lib / 'text' / 'manual.txt').write_text('Page one:   clean  water.\n\f\nPage two: boil it for one minute.', encoding='utf-8')
    (lib / 'text' / 'web.txt').write_text('A saved page about rope.', encoding='utf-8')
    (lib / 'videos' / 'a.en.vtt').write_text('WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nGather dry tinder first\n\n'
                                              '00:01:10.000 --> 00:01:14.000\nThen strike the ferro rod\n', encoding='utf-8')
    for f in ('documents/m.pdf', 'pages/w.html', 'videos/a.mp4'):
        (lib / f).write_bytes(b'x')
    recs = [{'id': 'manual', 'kind': 'pdf', 'path': 'documents/m.pdf', 'title': 'Water Manual 2019', 'publisher': 'CDC', 'pages': 2},
            {'id': 'web', 'kind': 'html', 'path': 'pages/w.html', 'title': 'Rope', 'publisher': 'Test'},
            {'id': 'video-a', 'kind': 'video', 'path': 'videos/a.mp4', 'title': 'Fire', 'publisher': 'Test', 'duration': 90, 'captions': ['videos/a.en.vtt']}]
    (lib / 'catalog.json').write_text(json.dumps(recs))
    (lib / 'summary.json').write_text('{"resources": 3, "pdfs": 1, "pdf_pages": 2, "web_pages": 1, "videos": 1}')
    con = sqlite3.connect(lib / 'search.sqlite')
    con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
    con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('manual', 'Water Manual 2019', 'Test', 'test', 'documents/m.pdf', 2, 'boil it for one minute'))
    con.commit()
    con.close()
    con = sqlite3.connect(data / 'derived' / 'geo' / 'world-places.sqlite')
    con.executescript('''CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
        INSERT INTO place VALUES (1, 'Testville', 'town', 'Nowhere', 12.5, 34.25, 1, 'made up');
        INSERT INTO place_fts(rowid, name, detail) VALUES (1, 'Testville', 'Nowhere');''')
    con.commit()
    con.close()
    return data


SMALL = small_library()


class Shapes(unittest.TestCase):
    def ok(self, *args, data=DATA):
        code, out, err = run(*args, data=data)
        self.assertEqual(code, 0, err)
        return out

    def test_search(self):
        out = self.ok('search', 'boil water', '--limit', '3')
        self.assertEqual(out['query'], 'boil water')
        hit = out['results'][0]
        self.assertEqual((hit['doc_id'], hit['kind'], hit['page']), ('pdfsmall', 'pdf', 1))
        for k in ('title', 'publisher', 'year', 't', 'excerpt', 'trust'):
            self.assertIn(k, hit)
        self.assertIn('boil', hit['excerpt'])

    def test_no_matches_is_success(self):
        self.assertEqual(self.ok('search', 'zzzqqqxxx')['results'], [])
        self.assertEqual(self.ok('search', 'boil water', '--kind', 'video')['results'], [])
        self.assertEqual(self.ok('place', 'zzzqqqxxx')['places'], [])

    def test_data_may_follow_the_command(self):
        code, out, err = run('search', 'boil water', '--data', str(DATA), data=None)
        self.assertEqual((code, out['results'][0]['doc_id']), (0, 'pdfsmall'), err)

    def test_read_pages(self):
        out = self.ok('read', 'manual', '--pages', '1-2', data=SMALL)
        self.assertEqual((out['doc_id'], out['title'], out['publisher'], out['year'], out['kind'], out['pages']), ('manual', 'Water Manual 2019', 'CDC', '2019', 'pdf', 2))
        self.assertEqual(out['trust'], 'Government guidance')
        self.assertEqual(out['texts'], [{'page': 1, 'text': 'Page one: clean water.'}, {'page': 2, 'text': 'Page two: boil it for one minute.'}])
        self.assertEqual(self.ok('read', 'manual', data=SMALL)['texts'][0]['page'], 1)                 # page 1 unless asked
        self.assertEqual(self.ok('read', 'web', data=SMALL)['texts'], [{'page': 1, 'text': 'A saved page about rope.'}])

    def test_transcript(self):
        out = self.ok('transcript', 'video-a', data=SMALL)
        self.assertEqual((out['doc_id'], out['kind'], out['duration'], out['query']), ('video-a', 'video', 90, None))
        self.assertEqual(out['lines'], [{'t': 1, 'text': 'Gather dry tinder first'}, {'t': 70, 'text': 'Then strike the ferro rod'}])
        out = self.ok('transcript', 'video-a', '--query', 'where is the ferro rod', data=SMALL)   # no Recall here: by words
        self.assertEqual((out['matched_by'], [c['t'] for c in out['lines']]), ('words', [70]))

    def test_place(self):
        out = self.ok('place', 'testville', '--nearby', 'hospital,pharmacy', data=SMALL)
        p = out['places'][0]
        self.assertEqual((p['name'], p['kind'], p['detail'], p['lat'], p['lon']), ('Testville', 'town', 'Nowhere', 12.5, 34.25))
        self.assertEqual((out['nearby_of'], out['nearby']), (None, []))           # no street map of it in this library
        self.assertNotIn('nearby', self.ok('place', 'testville', data=SMALL))

    def test_guides(self):
        listed = self.ok('guides')['guides']
        self.assertEqual(sorted(g['id'] for g in listed), sorted(p.stem for p in (ROOT / 'content' / 'topics').glob('*.json')))
        self.assertTrue(all(g['title'] and g['short'] for g in listed))
        g = self.ok('guide', 'hurt')
        self.assertEqual((g['id'], g['title']), ('hurt', 'Someone is hurt or sick'))
        for k in ('choices', 'limits', 'prepare', 'learn', 'related'):
            self.assertIsInstance(g[k], list)
        self.assertTrue(all(lim['text'] for lim in g['limits']))                    # a warning stands without its source
        self.assertFalse([i for c in g['choices'] for i in c['read'] if 'doc_id' not in i])

    def test_status(self):
        out = self.ok('status', data=SMALL)
        self.assertEqual(out['library'], str((SMALL / 'library-data').resolve()))
        self.assertEqual(out['counts'], {'documents': 3, 'pdfs': 1, 'pages': 2, 'web_pages': 1, 'videos': 1, 'encyclopedias': 0})
        self.assertEqual(out['version'], (ROOT / 'VERSION').read_text().strip())
        self.assertEqual(out['meaning_search']['ready'], False)
        self.assertEqual(out['meaning_search']['recall']['running'], False)
        self.assertIn(out['ai']['state'], ('absent', 'no runtime'))
        self.assertEqual(out['maps'], {'world': False, 'places': True, 'regions': []})

    def test_open_only_says_where_with_dry_run(self):
        for args, route in ((['manual', '2'], 'read:manual:2'), (['manual'], 'read:manual:1'), (['video-a', '70'], 'watch:video-a:70'),
                            (['web'], 'read:web:1'), (['guide:hurt'], 'guide:hurt'), (['search:boil water'], 'search:boil water'),
                            (['map:12.5,34.25,Testville'], 'map:12.5,34.25,Testville'), (['read:manual', '2'], 'read:manual:2')):
            out = self.ok('open', *args, '--dry-run', data=SMALL)
            self.assertEqual((out['route'], out['opened']), (route, False), args)
            self.assertEqual(out['command'][1:], ['--data', str(SMALL.resolve())])


class ExitCodes(unittest.TestCase):
    def fails(self, code, *args, data=DATA, env=ENV):
        got, out, err = run(*args, data=data, env=env)
        self.assertEqual((got, out), (code, None), err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)      # one line, no traceback
        self.assertTrue(err.startswith('lastbook: '), err)
        return err

    def test_bad_arguments_are_2(self):
        for args in (['search'], ['search', 'x', '--limit', '0'], ['search', 'x', '--limit', '21'], ['search', 'x', '--kind', 'book'],
                     ['read'], ['read', 'manual', '--pages', 'two'], ['read', 'manual', '--pages', '1-11'], ['read', 'manual', '--pages', '0'],
                     ['page', 'manual'], ['page', 'manual', 'x'], ['page', 'manual', '1', '--width', '9000'], ['transcript'],
                     ['place', 'testville', '--nearby', 'castle'], ['guide'], ['frobnicate'], [], ['status', 'extra'], ['open', 'manual', 'two']):
            self.fails(2, *args, data=SMALL)

    def test_runtime_errors_are_1(self):
        for args in (['read', 'nope'], ['read', 'manual', '--pages', '3'], ['read', 'video-a'], ['read', 'zim:none.zim/A'],
                     ['page', 'web', '1'], ['page', 'manual', '3'], ['transcript', 'manual'], ['guide', 'nope'], ['open', 'nope', '--dry-run']):
            self.fails(1, *args, data=SMALL)
        self.assertIn('3 does not exist: the document has 2 pages', self.fails(1, 'read', 'manual', '--pages', '3', data=SMALL))

    def test_no_library_is_1_and_says_what_to_do(self):
        self.assertIn('run `lastbook`', self.fails(1, 'status', data=None))
        self.assertIn('not a Lastbook library', self.fails(1, 'search', 'x', data=TMP / 'home'))
        self.assertFalse((TMP / 'home' / 'data').exists())         # nothing was made on the way

    def test_ask_without_a_local_ai_is_1(self):
        self.assertIn('No local AI', self.fails(1, 'ask', 'how do I boil water?', data=SMALL, env={**ENV, 'LASTBOOK_LOCAL_AI': '0'}))


class Shipped(unittest.TestCase):
    def test_help_skill_and_package_name_every_command(self):
        sys.path[:0] = [str(ROOT / 'app')]
        import cli
        commands = set(cli.COMMANDS)
        rust = (ROOT / 'native' / 'src' / 'cli.rs').read_text(encoding='utf-8')
        listed = rust.split('const ENGINE: &[&str] = &[', 1)[1].split(']', 1)[0]
        self.assertEqual({c.strip(' "') for c in listed.split(',')}, commands)
        skill = (ROOT / 'skills' / 'lastbook' / 'SKILL.md').read_text(encoding='utf-8')
        for c in commands:
            self.assertIn(f'`{c}', skill.replace('`lastbook ', '`'), c)
        pkgbuild = (ROOT / 'packaging' / 'arch' / 'PKGBUILD').read_text(encoding='utf-8')
        self.assertIn('app/cli.py', pkgbuild)
        self.assertIn('skills/lastbook/SKILL.md', pkgbuild)


if __name__ == '__main__':
    unittest.main()
