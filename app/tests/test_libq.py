#!/usr/bin/env python3
"""tools/libq.py against a small made-up library: no real library, no server, no port.

  python3 -B -m unittest app.tests.test_libq

The tool is run the way a curator runs it, as its own process. The library is a catalog of two records, the text of a
three-page PDF, a search index with the app's own table (engine/library.py) and, where libzim can write one, a ZIM file
of two pages with its own full-text index.
"""
import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'tools' / 'libq.py'
PAGES = ['Safe water\nBring water to a rolling boil.', 'Storing water\nUse clean containers: “food grade”.', 'When to get help']
ZIM = 'tiny_en_test.zim'


def make_zim(path):
    """A ZIM file with a full-text index. False where there is no libzim to write one. libzim prints its progress from
    C++, past Python's own output, so the writing is done in a child process whose output is not shown."""
    r = subprocess.run([sys.executable, '-B', __file__, '--zim', str(path)], capture_output=True)
    return r.returncode == 0 and Path(path).is_file()


def write_zim(path):
    from libzim.writer import Creator, Hint, Item, StringProvider

    class Page(Item):
        def __init__(self, path, title, text):
            super().__init__()
            self.path, self.title, self.text = path, title, text
        def get_path(self): return self.path
        def get_title(self): return self.title
        def get_mimetype(self): return 'text/html'
        def get_contentprovider(self): return StringProvider(f'<html><head><title>{self.title}</title></head><body><p>{self.text}</p></body></html>')
        def get_hints(self): return {Hint.FRONT_ARTICLE: True}

    with Creator(path).config_indexing(True, 'eng') as c:
        c.add_item(Page('Rainwater_harvesting', 'Rainwater harvesting', 'Collect rainwater in a clean barrel.'))
        c.add_item(Page('Seed_saving', 'Seed saving', 'Dry seeds fully before storing them.'))
        c.add_metadata('Title', 'Tiny')
        c.add_metadata('Language', 'eng')


class Libq(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.data = Path(cls.dir.name) / 'Lastbook Data #1'          # a space and a '#': the search index is opened by address
        lib = cls.data / 'library-data'
        (lib / 'text').mkdir(parents=True)
        cat = [{'id': 'doc-water', 'kind': 'pdf', 'pages': 3, 'title': 'Agua segura: “guía”', 'collection': 'Water', 'text_path': 'text/doc-water.txt'},
               {'id': 'metadata-video-1', 'kind': 'metadata', 'title': 'Water video details', 'collection': 'Water'}]
        (lib / 'catalog.json').write_text(json.dumps(cat, ensure_ascii=False), encoding='utf-8')
        (lib / 'text' / 'doc-water.txt').write_text('\n\f\n'.join(PAGES), encoding='utf-8')
        con = sqlite3.connect(lib / 'search.sqlite')
        con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
        for n, body in enumerate(PAGES, 1):
            con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('doc-water', 'Agua segura', 'Water', 'water', 'documents/w.pdf', n, body))
        con.commit()
        con.close()
        (lib / 'collections').mkdir()
        cls.zim = make_zim(lib / 'collections' / ZIM)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def run_tool(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if k != 'LASTBOOK_DATA'}
        e.update(env or {})
        r = subprocess.run([sys.executable, '-B', str(TOOL), *args], capture_output=True, text=True, encoding='utf-8', errors='replace', env=e, cwd=self.dir.name)
        return r.returncode, r.stdout + r.stderr

    def test_search_finds_the_page(self):
        code, out = self.run_tool('--data', str(self.data), 'search', 'rolling boil')
        self.assertEqual(code, 0, out)
        self.assertIn('doc-water | Agua segura | Water | pages [1]', out)
        code, out = self.run_tool('search', 'rolling boil', '--data', str(self.data))      # the option may come after the command
        self.assertEqual(code, 0, out)
        self.assertIn('doc-water | Agua segura | Water | pages [1]', out)
        code, out = self.run_tool('search', 'rolling boil', '--data')                       # the option with no folder
        self.assertNotEqual(code, 0)
        self.assertIn('--data needs a folder', out)
        self.assertNotIn('Traceback', out)

    def test_page_and_outline(self):
        code, out = self.run_tool('page', 'doc-water', '2', env={'LASTBOOK_DATA': str(self.data)})
        self.assertEqual(code, 0, out)
        self.assertIn('[3 pages]', out)
        self.assertIn('“food grade”', out)                      # outside ASCII, whatever the console's own encoding is
        code, out = self.run_tool('--data', str(self.data), 'toc', 'doc-water')
        self.assertEqual(code, 0, out)
        self.assertEqual([l for l in out.splitlines() if l][-1], '3 When to get help')

    def test_titles_leave_out_metadata(self):
        code, out = self.run_tool('--data', str(self.data), 'titles', 'water|agua')
        self.assertEqual(code, 0, out)
        self.assertIn('doc-water | pdf | Agua segura: “guía”', out)
        self.assertNotIn('metadata-video-1', out)

    def test_doc(self):
        code, out = self.run_tool('--data', str(self.data), 'doc', 'doc-water')
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['pages'], 3)

    def test_encyclopedia_search_without_a_server(self):
        if not self.zim:
            # In CI a missing tool fails the run: a check that was skipped proved nothing, and a green run would hide that.
            (self.fail if os.environ.get('CI') else self.skipTest)('no libzim here to write the small ZIM file')
        code, out = self.run_tool('--data', str(self.data), 'zim', 'rainwater barrel')
        self.assertEqual(code, 0, out)
        self.assertIn(f'zim/{ZIM}/Rainwater_harvesting | Rainwater harvesting', out)    # the form a guide's "zim" field takes
        self.assertNotIn('Seed_saving', out)
        self.assertNotIn(str(self.data), out)                                           # no full path of the library in what is printed

    def test_no_library_or_no_command_says_so(self):
        empty = Path(self.dir.name) / 'empty'
        empty.mkdir(exist_ok=True)
        code, out = self.run_tool('--data', str(empty), 'search', 'water')
        self.assertNotEqual(code, 0)
        self.assertIn('No library at', out)
        self.assertNotIn('Traceback', out)
        code, out = self.run_tool('--data', str(self.data))
        self.assertNotEqual(code, 0)
        self.assertIn('libq.py search "words"', out)             # the usage text
        self.assertNotIn('Traceback', out)

    def test_mistakes_get_plain_words(self):
        for args, says in ((('search',), 'libq.py search "words"'),                       # a command without its words
                           (('page', 'doc-water'), 'libq.py search "words"'),             # a page without its number
                           (('doc', 'doc-missing'), 'No record with the id doc-missing'),
                           (('page', 'doc-missing', '1'), 'No record with the id doc-missing'),
                           (('page', 'doc-water', '9'), 'doc-water has 3 pages; there is no page 9.')):
            code, out = self.run_tool('--data', str(self.data), *args)
            self.assertNotEqual(code, 0, args)
            self.assertIn(says, out, args)
            self.assertNotIn('Traceback', out, args)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--zim']:
        write_zim(sys.argv[2])
    else:
        unittest.main()
