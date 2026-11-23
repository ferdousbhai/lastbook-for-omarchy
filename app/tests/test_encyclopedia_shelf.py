#!/usr/bin/env python3
"""The window's Encyclopedias screen (encyclopedia.py: installed, random, opening): each encyclopedia by its front page,
and a random article that has text to show, against a ZIM file of a few made-up articles written here. Skipped where
there is no libzim to write one.

  PYTHONPATH=<deps> python3 -B -m unittest app.tests.test_encyclopedia_shelf
"""
import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
import encyclopedia  # noqa: E402

ZIM = 'tiny_en_shelf.zim'
ARTICLES = {  # path: (title, body); an article with no #mw-content-text has no text of its own, as Appropedia's
    'Home': ('Home', '<div id="mw-content-text"><p>Welcome. Read about burns.</p></div>'),    # translations
    'Burn_(injury)': ('Burn (injury)', '<div id="mw-content-text"><p>A burn is an injury to the skin. Cool it under '
                                       'running water.</p><h2>Signs</h2><p>Blisters.</p></div>'),
    'Empty_shell': ('Empty shell', '<p>Nothing inside the article text.</p>'),
}


def write_zim(path):
    from libzim.writer import Creator, Hint, Item, StringProvider

    class Page(Item):
        def __init__(self, path, title, body):
            super().__init__()
            self.path, self.title, self.body = path, title, body
        def get_path(self): return self.path
        def get_title(self): return self.title
        def get_mimetype(self): return 'text/html'
        def get_contentprovider(self): return StringProvider(f'<html><body>{self.body}</body></html>')
        def get_hints(self): return {Hint.FRONT_ARTICLE: True}

    with Creator(path).config_indexing(False, 'eng') as c:
        c.set_mainpath('Home')
        for p, (title, body) in ARTICLES.items():
            c.add_item(Page(p, title, body))
        c.add_redirection('Burns', 'Burns', 'Burn_(injury)', {Hint.FRONT_ARTICLE: True})
        c.add_metadata('Title', 'Tiny')
        c.add_metadata('Description', 'A few made-up articles')
        c.add_metadata('Language', 'eng')


class Shelf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='lastbook-shelf-'))
        (cls.tmp / 'collections').mkdir()
        zim = cls.tmp / 'collections' / ZIM
        r = subprocess.run([sys.executable, '-B', __file__, '--zim', str(zim)], capture_output=True)   # libzim prints from C++
        if r.returncode or not zim.is_file():
            raise unittest.SkipTest('no libzim here to write the small ZIM file')
        from libzim.reader import Archive
        arc = Archive(zim)
        # Not in the catalog: the folder decides what is installed, as it does for the /zim/ addresses.
        cls.enc = encyclopedia.Encyclopedias(cls.tmp, lambda: {'by_id': {}}, lambda name: arc, None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_each_encyclopedia_by_its_front_page(self):
        [e] = self.enc.installed()
        self.assertEqual(e, {'file': ZIM, 'title': 'Tiny', 'description': 'A few made-up articles',
                             'articles': e['articles'], 'main': f'{ZIM}/Home'})
        self.assertGreaterEqual(e['articles'], 3)

    def test_a_random_article_has_text_and_an_address_the_wiki_screen_opens(self):
        for name in (None, ZIM):
            for _ in range(20):
                a = self.enc.random(name)
                self.assertIn(a['title'], ('Home', 'Burn (injury)'), 'never a redirect or a page with no text')
                self.assertEqual(a['file'], ZIM)
                if a['title'] == 'Burn (injury)':
                    self.assertEqual(a['wiki'], f'{ZIM}/Burn_%28injury%29')
                    self.assertEqual(a['snippet'], 'A burn is an injury to the skin. Cool it under running water.')

    def test_only_an_encyclopedia_of_this_library(self):
        for name in ('nope.zim', '../collections/' + ZIM, 'catalog.json'):
            self.assertIsNone(self.enc.random(name), name)

    def test_no_encyclopedia(self):
        empty = Path(tempfile.mkdtemp(prefix='lastbook-shelf-'))
        try:
            enc = encyclopedia.Encyclopedias(empty, lambda: {'by_id': {}}, None, None)
            self.assertEqual(enc.installed(), [])
            self.assertIsNone(enc.random())
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_the_opening_is_whole_sentences(self):
        text = 'First sentence here. ' * 10 + 'And more.'
        self.assertEqual(encyclopedia.opening('Short. Text', 320), 'Short. Text')
        self.assertEqual(encyclopedia.opening(text, 50), 'First sentence here. First sentence here.')
        self.assertEqual(encyclopedia.opening('一句话。' * 100, 10), '一句话。一句话。')
        self.assertEqual(encyclopedia.opening('word ' * 100, 22), 'word word word word…')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--zim']:
        write_zim(sys.argv[2])
    else:
        unittest.main()
