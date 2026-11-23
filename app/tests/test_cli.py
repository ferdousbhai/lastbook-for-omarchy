#!/usr/bin/env python3
"""The command line (app/cli.py): what each command prints, and its exit codes.

  PYTHONPATH=<deps> python3 -B app/tests/test_cli.py [-v]

Runs the commands as the native program does (a Python process each) against the made-up library the bridge tests use,
and against a second small one made here with a text, a transcript, a gazetteer and a street map of one of its two towns.
No Recall. Never packaged."""
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
for k in ('LASTBOOK_DATA', 'LASTBOOK_DEV', 'LASTBOOK_OPEN'):
    ENV.pop(k, None)


def run(*args, data=DATA, env=ENV):
    """(exit code, the JSON printed or None, standard error)."""
    r = subprocess.run([sys.executable, '-B', str(ROOT / 'app' / 'cli.py'), *(['--data', str(data)] if data else []), *args],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr


def small_library():
    """A PDF's text (two pages), a saved page, a video with captions, and towns: Testville, which has no street map, and
    Mapton, which has one (its help points); upstream's US map files beside them, which are not read."""
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
    con.executescript('''PRAGMA user_version = 2;
        CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
        INSERT INTO place VALUES (1, 'Testville', 'town', 'Nowhere', 12.5, 34.25, 1, 'made up');
        INSERT INTO place_fts(rowid, name, detail) VALUES (1, 'Testville', 'Nowhere');
        INSERT INTO place VALUES (2, 'Mapton', 'town', 'Somewhere', -8.5, 115.25, 1, 'made up');
        INSERT INTO place_fts(rowid, name, detail) VALUES (2, 'Mapton', 'Somewhere');
        INSERT INTO place VALUES (3, 'Newbury', 'town', 'Elsewhere', 40.0, 10.0, 3, 'made up');
        INSERT INTO place_fts(rowid, name, detail) VALUES (3, 'Newbury', 'Elsewhere · Newbury Old Borough');
        INSERT INTO place VALUES (4, '29 Palms', 'town', 'Desert', 34.1, -116.0, 3, 'made up');
        INSERT INTO place_fts(rowid, name, detail) VALUES (4, '29 Palms', 'Desert');''')
    con.commit()
    con.close()
    lat, lon = -8.5, 115.25
    fixture.region_file(data / 'derived' / 'geo' / 'regions-testland.mbtiles', {
        15: [('hospital', 'RS Mapton', lat + 0.004, lon), ('pharmacy', 'Apotek Satu', lat, lon + 0.002), ('pharmacy', None, lat - 0.01, lon),
             ('hospital', 'Klinik Utara', lat + 0.05, lon), ('restaurant', 'Warung', lat, lon), ('pharmacy', 'Apotek Jauh', lat, lon + 0.6),
             ('hospital', 'RS Provinsi', lat, lon - 0.6)]
            # At 60 N, 25 E, where a degree of longitude is 55.6 km: one pharmacy 1.3 km east, five 1.67 to 2.12 km north.
            + [('restaurant', 'Kahvila', 60.0, 25.0), ('pharmacy', 'East', 60.0, 25.0 + 1.3 / 55.6)] + [('pharmacy', f'North {i}', 60.0 + (1.67 + 0.1 * i) / 111.2, 25.0) for i in range(5)],
        14: [('hospital', 'RS Provinsi', lat, lon - 0.6)]})
    # Upstream's US map files, as a library that has not been cleaned still holds them: a hospital in a `poi` table and a
    # town in a US gazetteer, neither of which is ever given.
    con = sqlite3.connect(data / 'derived' / 'geo' / 'map.sqlite')
    con.executescript('CREATE TABLE poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL);')
    con.execute('INSERT INTO poi(cls,name,lon,lat) VALUES (?,?,?,?)', ('hospital', 'County Hospital', lon + 0.02, lat))
    con.commit()
    con.close()
    con = sqlite3.connect(data / 'derived' / 'geo' / 'places.sqlite')
    con.executescript('''CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id');
        INSERT INTO place VALUES (1, 'Oldtown', 'town', 'Some County, Ohio', -8.5, 115.25, 3, 'GNIS');
        INSERT INTO place_fts(rowid, name, detail) VALUES (1, 'Oldtown', 'Some County, Ohio');''')
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
        self.assertEqual((p['name'], p['kind'], p['detail'], p['lat'], p['lon'], p['match'], p['onMap']),
                         ('Testville', 'town', 'Nowhere', 12.5, 34.25, 'exact', False))
        # No street map of it in this library: nothing is looked up, and the answer says so and what there is.
        self.assertEqual((out['nearby_of'], out['covered'], out['nearby'], out['regions_installed']), ('Testville', False, [], ['testland']))
        self.assertIn('No street-level map covers Testville', out['nearby_note'])
        self.assertNotIn('nearby', self.ok('place', 'testville', data=SMALL))
        self.assertEqual(self.ok('place', 'oldtown', data=SMALL)['places'], [])     # upstream's US gazetteer is not read

    def test_nothing_found_says_which_gazetteer_this_library_has(self):
        self.assertIn('places of 500 people or more', self.ok('place', 'zzzqqqxxx', '--nearby', 'hospital', data=SMALL)['nearby_note'])
        db = sqlite3.connect(SMALL / 'derived' / 'geo' / 'world-places.sqlite')
        try:
            db.execute('PRAGMA user_version = 0')                                    # as an earlier version made it
            db.commit()
            self.assertIn('the older one, of towns of 15,000+ only', self.ok('place', 'zzzqqqxxx', '--nearby', 'hospital', data=SMALL)['nearby_note'])
        finally:
            db.execute('PRAGMA user_version = 2')
            db.commit()
            db.close()

    def test_how_a_place_was_matched(self):
        self.assertEqual(self.ok('place', 'Testville, Nowhere', data=SMALL)['places'][0]['match'], 'exact')   # with its region
        self.assertEqual(self.ok('place', 'old borough', data=SMALL)['places'][0]['match'], 'alternate name')
        self.assertEqual(self.ok('place', 'Mapt', data=SMALL)['places'][0]['match'], 'partial')               # the start of a name
        out = self.ok('place', 'mapton harbour', '--nearby', 'hospital', data=SMALL)   # one word left out to find anything
        self.assertEqual((out['places'][0]['name'], out['places'][0]['match']), ('Mapton', 'partial'))
        self.assertEqual((out['nearby_of'], out['nearby']), (None, []))
        self.assertIn('does not match every word', out['nearby_note'])
        p = self.ok('place', '29 Palms', data=SMALL)['places'][0]                    # a name that starts with a number
        self.assertEqual((p['name'], p['match']), ('29 Palms', 'exact'))

    def test_a_point_given_by_its_coordinates(self):
        out = self.ok('place', '-8.5,115.25', '--nearby', 'hospital', data=SMALL)
        p = out['places'][0]
        self.assertEqual((p['kind'], p['lat'], p['lon'], p['match'], p['source'], p['onMap']), ('point', -8.5, 115.25, 'coordinates', 'coordinates', True))
        self.assertEqual(p['detail'], '0 km from Mapton, Somewhere')
        self.assertEqual((out['nearby_of'], out['covered'], out['nearby'][0]['name']), ('-8.5,115.25', True, 'RS Mapton'))
        self.assertEqual(self.ok('place', ' 12.5 , 34.25 ', data=SMALL)['places'][0]['detail'], '0 km from Testville, Nowhere')

    def test_the_nearest_is_the_nearest_on_the_ground(self):
        near = self.ok('place', '60,25', '--nearby', 'pharmacy', data=SMALL)['nearby']
        self.assertEqual([(p['name'], p['dir']) for p in near][:2], [('East', 'east'), ('North 0', 'north')])
        self.assertEqual(len(near), 5)

    def test_place_nearby_reads_the_street_map(self):
        out = self.ok('place', 'mapton', '--nearby', 'hospital,pharmacy', data=SMALL)
        self.assertEqual((out['places'][0]['name'], out['places'][0]['onMap'], out['nearby_of'], out['covered']), ('Mapton', True, 'Mapton', True))
        self.assertNotIn('nearby_note', out)
        near = out['nearby']
        # Nearest first, from the street map alone (not the US file's `poi` table). RS Provinsi is 66 km away, where the
        # map is read at a zoom that shows hospitals and not pharmacies (Apotek Jauh, as far, is not found).
        self.assertEqual([(p['cls'], p['name']) for p in near],
                         [('pharmacy', 'Apotek Satu'), ('hospital', 'RS Mapton'), ('pharmacy', ''), ('hospital', 'Klinik Utara'),
                          ('hospital', 'RS Provinsi')])
        self.assertEqual([p['km'] for p in near], sorted(p['km'] for p in near))
        first = near[0]
        self.assertEqual(sorted(first), ['cls', 'dir', 'km', 'label', 'lat', 'lon', 'name'])
        self.assertEqual((first['label'], first['km'], first['dir']), ('Pharmacy', 0.22, 'east'))
        self.assertAlmostEqual(first['lat'], -8.5, places=4)
        self.assertAlmostEqual(first['lon'], 115.252, places=4)
        self.assertEqual((near[-1]['km'], near[-1]['dir']), (65.98, 'west'))
        self.assertEqual(self.ok('place', 'mapton', '--nearby', 'police', data=SMALL)['nearby'], [])   # none on the map near it

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
        size = (SMALL / 'derived/geo/regions-testland.mbtiles').stat().st_size
        self.assertEqual(out['maps'], {'world': False, 'places': True, 'regions': [{'id': 'testland', 'name': 'testland', 'bytes': size}]})

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

    def test_bad_arguments_are_1(self):
        # Omarchy's commands exit 1 on any failure: a script tells a bad argument from a missing page by the line on
        # standard error, not by the code.
        for args in (['search'], ['search', 'x', '--limit', '0'], ['search', 'x', '--limit', '21'], ['search', 'x', '--kind', 'book'],
                     ['read'], ['read', 'manual', '--pages', 'two'], ['read', 'manual', '--pages', '1-11'], ['read', 'manual', '--pages', '0'],
                     ['page', 'manual'], ['page', 'manual', 'x'], ['page', 'manual', '1', '--width', '9000'], ['transcript'],
                     ['place', 'testville', '--nearby', 'castle'], ['place', 'mapton', '--nearby', 'dam'], ['place', 'mapton', '--nearby', 'embassy'],
                     ['place', '91,10'], ['place', '-8.5,181', '--nearby', 'hospital'], ['place', '-8.5,115.25,3'], ['place', '12'], ['guide'], ['frobnicate'], [], ['status', 'extra'], ['open', 'manual', 'two']):
            self.fails(1, *args, data=SMALL)

    def test_runtime_errors_are_1(self):
        for args in (['read', 'nope'], ['read', 'manual', '--pages', '3'], ['read', 'video-a'], ['read', 'zim:none.zim/A'],
                     ['page', 'web', '1'], ['page', 'manual', '3'], ['transcript', 'manual'], ['guide', 'nope'], ['open', 'nope', '--dry-run']):
            self.fails(1, *args, data=SMALL)
        self.assertIn('3 does not exist: the document has 2 pages', self.fails(1, 'read', 'manual', '--pages', '3', data=SMALL))

    def test_no_library_is_1_and_says_what_to_do(self):
        self.assertIn('run `lastbook`', self.fails(1, 'status', data=None))
        self.assertIn('not a Lastbook library', self.fails(1, 'search', 'x', data=TMP / 'home'))
        self.assertFalse((TMP / 'home' / 'data').exists())         # nothing was made on the way


class Reading(unittest.TestCase):
    """read() with a stand-in engine: an article's section that is not there, and PDF pages without text."""

    def setUp(self):
        sys.path[:0] = [str(ROOT / 'app')]
        import cli
        self.cli = cli

        class Enc:
            def matched(self, doc_id, section):
                return [1] if 'treat' in section.lower() else []

            def headings(self, doc_id):
                return ['Signs', 'Treatment', 'History']

            def read(self, doc_id, section, limit):
                return None, f'text of {section or "the article"}'

        recs = {'zim:t.zim/Burn': {'id': 'zim:t.zim/Burn', 'kind': 'article'}, 'scan': {'id': 'scan', 'kind': 'pdf'}}

        class S:
            ENC = Enc()
            record = staticmethod(recs.get)
            public = staticmethod(lambda r: {'title': r['id'], 'publisher': 'Test', 'year': None, 'trust': None})
            page_texts = staticmethod(lambda r: ['Page one.', '  ', ''])
        self.S = S

    def read(self, doc_id, pages=None, section=None):
        import argparse
        return self.cli.read(self.S, argparse.Namespace(doc_id=doc_id, pages=pages, section=section))

    def test_a_section_that_is_not_there_is_an_error_naming_the_sections(self):
        self.assertEqual(self.read('zim:t.zim/Burn', section='treat')['texts'], [{'text': 'text of treat'}])
        with self.assertRaises(self.cli.Failed) as e:
            self.read('zim:t.zim/Burn', section='prevention')
        self.assertEqual(str(e.exception), "No section of zim:t.zim/Burn has a heading containing 'prevention'. Its sections: Signs; Treatment; History.")
        self.assertEqual(self.read('zim:t.zim/Burn')['texts'], [{'text': 'text of the article'}])

    def test_a_page_without_text_says_so(self):
        out = self.read('scan', pages=[1, 2])
        self.assertEqual(out['texts'][0], {'page': 1, 'text': 'Page one.'})
        self.assertEqual(out['texts'][1], {'page': 2, 'text': '', 'note': 'Page 2 has no text the engine can read: lastbook page scan 2 shows it as a picture.'})
        for pages in ([3], [2, 3]):
            with self.assertRaises(self.cli.Failed) as e:
                self.read('scan', pages=pages)
            self.assertIn('lastbook page scan', str(e.exception))


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
