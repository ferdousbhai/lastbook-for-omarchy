#!/usr/bin/env python3
"""Encyclopedia search without Recall, and reading one section of an article (encyclopedia.py), against a ZIM file of a
few made-up articles written here. Skipped where there is no libzim to write one.

  PYTHONPATH=<deps> python3 -B -m unittest app.tests.test_search_encyclopedia
"""
import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
import encyclopedia  # noqa: E402

ZIM = 'tiny_en_search.zim'
LONG = ' This sentence is here so that the passage is long enough to be kept as one that people can read and search.'
ARTICLES = {  # path: (title, body of the article inside #mw-content-text)
    'Burn': ('Burn', '<p>A burn is an injury to the skin. To treat a burn, cool it under running water.' + LONG + '</p>'
                     '<h2>Signs</h2><p>Red skin and blisters are signs of a burn and of how deep it is.' + LONG + '</p>'
                     '<h2>Management</h2><h3>First aid</h3><p>Cool the burn for twenty minutes and cover it loosely.' + LONG + '</p>'
                     '<h3>Medicines</h3><p>Pain relief may be given by mouth while the burn is dressed.' + LONG + '</p>'
                     '<h2>History</h2><p>Burns have been described since the earliest medical writing that is known.' + LONG + '</p>'
                     '<h2>References</h2><p>Not part of the article.</p>'),
    'Treated_straw_as_feed': ('Treated straw as feed', '<p>Farmers treat straw with urea, and some burn what cattle leave.' + LONG + '</p>'),
    'Wood_Harvesting_with_Hand_Tools/Cross-Cutting': ('Cross-Cutting', '<p>Cutting a log by hand: treat the saw well and do not burn the handle.' + LONG + '</p>'),
    'Thermal_burn': ('Thermal burn', '<p>A thermal burn comes from heat. Doctors treat a deep burn in hospital.' + LONG + '</p>'),
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
        def get_contentprovider(self): return StringProvider(f'<html><head><title>{self.title}</title></head><body><div id="mw-content-text">{self.body}</div></body></html>')
        def get_hints(self): return {Hint.FRONT_ARTICLE: True}

    with Creator(path).config_indexing(True, 'eng') as c:
        for p, (title, body) in ARTICLES.items():
            c.add_item(Page(p, title, body))
        c.add_metadata('Title', 'Tiny')
        c.add_metadata('Language', 'eng')


class Articles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='lastbook-enc-'))
        (cls.tmp / 'collections').mkdir()
        zim = cls.tmp / 'collections' / ZIM
        r = subprocess.run([sys.executable, '-B', __file__, '--zim', str(zim)], capture_output=True)   # libzim prints from C++
        if r.returncode or not zim.is_file():
            raise unittest.SkipTest('no libzim here to write the small ZIM file')
        from libzim.reader import Archive
        arc = Archive(zim)
        cat = {'by_id': {'zim-tiny': {'id': 'zim-tiny', 'kind': 'zim', 'path': f'collections/{ZIM}', 'title': 'Tiny', 'publisher': 'Test'}}}
        cls.enc = encyclopedia.Encyclopedias(cls.tmp, lambda: cat, lambda name: arc, None)
        cls.burn = encyclopedia.Encyclopedias.doc_id(ZIM, 'Burn')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def found(self, words, groups, k=8):
        return [h['record']['title'] for h in self.enc.search(words, groups, None, k)]

    def test_without_recall_a_title_must_be_the_questions_words(self):
        groups = [['treat'], ['burn', 'burns', 'scald']]
        # "Treated straw as feed" and "Cross-Cutting" hold both words in their text, and letters of them in their titles.
        self.assertEqual(self.found(['treat', 'burn'], groups), ['Burn', 'Thermal burn'])
        self.assertEqual(self.found(['treat', 'burn'], groups, 1), ['Burn'])
        self.assertEqual(self.found(['cut', 'hand'], [['cut'], ['hand']]), [])       # no article is about that
        self.assertEqual(self.found(['burns'], [['burns']])[:1], ['Burn'])           # whole words, in any of their forms

    def test_a_section_brings_the_sections_under_it(self):
        _, text = self.enc.read(self.burn, 'management')
        self.assertEqual([l for l in text.splitlines() if l.startswith('## ')], ['## First aid', '## Medicines'])
        self.assertNotIn('earliest medical writing', text)
        _, text = self.enc.read(self.burn, 'first aid')
        self.assertIn('twenty minutes', text)
        self.assertNotIn('Pain relief', text)

    def test_a_section_that_is_not_there_matches_nothing(self):
        self.assertEqual(self.enc.matched(self.burn, 'treatment'), [])
        self.assertEqual(self.enc.read(self.burn, 'treatment')[1], '')
        self.assertEqual(self.enc.headings(self.burn), ['Signs', 'Management', 'First aid', 'Medicines', 'History'])
        _, whole = self.enc.read(self.burn)
        self.assertIn('A burn is an injury', whole)
        self.assertIn('earliest medical writing', whole)
        self.assertIsNone(self.enc.read(encyclopedia.Encyclopedias.doc_id(ZIM, 'No_such_article'), 'x'))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--zim']:
        write_zim(sys.argv[2])
    else:
        unittest.main()
