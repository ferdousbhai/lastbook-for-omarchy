#!/usr/bin/env python3
"""What a pack's app shows and ships: encyclopedia articles with their scripts taken out, guide items that open nothing hidden,
and the guides as the only part of content/ the package scan lets through.

  python3 -B app/tests/test_what_ships.py [-v]

Uses the made-up library in derived/app-build/testdata. Never packaged."""
import json, os, sys, unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
DATA = fixture.ensure()
os.environ['LASTBOOK_DATA'] = str(DATA)
from engine import saved_pages  # noqa: E402
import server  # noqa: E402


class Article(unittest.TestCase):
    A = staticmethod(saved_pages.article)

    def test_scripts_go_and_the_rest_stays(self):
        page = (b'<!doctype html><html><head><title>x <script> y</title><script src="/-/a.js"></script><script>var a="</p>"</script></head>'
                b'<body><p onclick="go()" class="k" data-on="1">Text <a href="javascript:alert(1)">one</a> <a href="java&#115;cript:x">two</a> '
                b'<a href="Other_page">three</a></p><!-- <script>kept</script> --><table><tr><td>cell</td></tr></table><img src="I/p.png" alt="">'
                b'<svg><script/><circle onload="x" r="1"/></svg><textarea><script>as text</script></textarea></body></html>')
        out = self.A(page)
        self.assertNotIn(b'/-/a.js', out)
        self.assertNotIn(b'var a', out)
        self.assertNotIn(b'onclick', out)
        self.assertNotIn(b'onload', out)
        self.assertNotIn(b'javascript', out)
        self.assertNotIn(b'cript:x', out)
        for kept in (b'<title>x <script> y</title>', b'class="k" data-on="1"', b'<a href="Other_page">three</a>', b'<!-- <script>kept</script> -->',
                     b'<td>cell</td>', b'<img src="I/p.png" alt="">', b'<textarea><script>as text</script></textarea>', b'Text one two '):
            self.assertIn(kept, out)

    def test_web_links_become_plain_text(self):
        page = (b'<p>See <a rel="nofollow" class="external text" href="https://www.who.int/x">the WHO page</a>, '
                b'<a href=\'HTTP://a.b\'>b</a> <a href="//c.d/e">c</a> <a href="mailto:x@y.z">mail</a> <a href="&#104;ttps://f.g">h</a> '
                b'<a href="Local_page">local</a> <a href="../A/Other#s">other</a> <a href="#cite_note-1">[1]</a> '
                b'<map><area shape="rect" href="https://m.n" alt="m"></map><svg><a xlink:href="http://s.t"><text>s</text></a></svg>'
                b'<a href="https://u.v">never closed</p><p>next</p></body>')
        out = self.A(page)
        self.assertNotIn(b'who.int', out)
        for gone in (b'a.b', b'c.d', b'mailto', b'f.g', b'm.n', b's.t', b'u.v'):
            self.assertNotIn(gone, out)
        for kept in (b'<p>See the WHO page, b c mail h ', b'<a href="Local_page">local</a>', b'<a href="../A/Other#s">other</a>',
                     b'<a href="#cite_note-1">[1]</a>', b'<map></map>', b'<svg><text>s</text></svg>', b'never closed</p><p>next</p>'):
            self.assertIn(kept, out)

    def test_a_script_that_never_closes_takes_the_rest_as_a_browser_would(self):
        self.assertEqual(self.A(b'<p>before</p><script>never closed <p>hidden'), b'<p>before</p>')
        self.assertEqual(self.A(b'<p>x</p><script>a</script'), b'<p>x</p>')   # an end tag cut off before its '>' once sent this round and round

    def test_a_page_with_nothing_to_take_is_unchanged(self):
        page = b'<!doctype html><p class=a>plain &amp; simple <a href="x">x</a></p><p title=">">'
        self.assertEqual(self.A(page), page)
        utf16 = '<script>x</script>'.encode('utf-16')
        self.assertEqual(self.A(utf16), utf16)

    def test_no_quadratic_time_on_a_page_that_never_closes_a_tag(self):
        import time
        t = time.time()
        self.A(b'<a ' * 200000)
        self.A(b'<a "' * 100000 + b'>')
        self.assertLess(time.time() - t, 5)

    def test_served_encyclopedia_pages_go_through_it(self):
        src = (ROOT / 'server.py').read_text(encoding='utf-8')
        self.assertIn("data = saved_pages.article(data)", src)


class Guides(unittest.TestCase):
    """The made-up library holds none of the guides' documents and no encyclopedia, so nothing the guides name may show."""

    def test_nothing_shown_opens_nothing(self):
        self.assertEqual(server.zim_names(), [])
        for t in server.topics_resolved():
            items = t['prepare'] + t['learn'] + [i for c in t['choices'] for i in c['read']] + [i for s in t['route'] for i in s['read']]
            self.assertFalse([i for i in items if i.get('zim') or (i.get('doc') and not i.get('src'))], t['id'])
            for lim in t['limits']:
                self.assertTrue(lim.get('text'))
                self.assertFalse(lim.get('zim') or (lim.get('doc') and not lim.get('src')))

    def test_a_present_item_stays(self):
        self.assertTrue(server.item_here({'doc': 'saved'}, []))
        self.assertTrue(server.item_here({'zim': 'zim/m.zim/A'}, ['m.zim']))
        self.assertFalse(server.item_here({'zim': 'zim/w.zim/A'}, ['m.zim']))
        self.assertFalse(server.item_here({'doc': 'not-here'}, []))

    def test_not_a_pack_offers_everything(self):
        self.assertIsNone(server.pack_info())


class WhatShips(unittest.TestCase):
    def test_the_package_scan_lets_the_guides_through_and_nothing_else_of_content(self):
        sys.path.insert(0, str(ROOT / 'packaging' / 'arch'))
        import scan_package
        at = scan_package.P + 'content/'
        guides = ['gaps.json'] + [f'topics/{p.name}' for p in (ROOT / 'content' / 'topics').glob('*.json')]
        self.assertGreater(len(guides), 1)
        for rel in guides:
            json.loads((ROOT / 'content' / rel).read_text(encoding='utf-8'))
            self.assertTrue(scan_package.GUIDES.search(at + rel), rel)
        for rel in ('SCHEMA.md', 'home-area.json', 'topics/notes.md', 'topics/deeper/x.json'):
            self.assertIsNone(scan_package.GUIDES.search(at + rel), rel)


if __name__ == '__main__':
    unittest.main()
