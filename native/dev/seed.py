#!/usr/bin/env python3
"""A small made-up library for dev/preview.sh: one manual (a real two-page PDF), one saved page and one encyclopedia
article. Nothing here comes from a real library.

  PYTHONPATH=$(bin/test-deps) python3 -B native/dev/seed.py <data folder>"""
import json, sqlite3, sys
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

PAGES = ['Boil the water for one minute to kill the germs. If the water is cloudy, let it settle and filter it through a '
         'clean cloth first, then boil it.',
         'Store the treated water in a clean container with a lid. Keep it in a cool place, away from the sun.']
ARTICLE = '''<!doctype html><html><head><meta charset="utf-8"><title>Water purification</title></head><body>
<h1>Water purification</h1>
<p><b>Water purification</b> removes germs and dirt from water so that it is safe to drink. Boiling is the most
reliable way: <a href="Boiling">a rolling boil</a> for one minute kills the germs that cause disease.</p>
<h2>Methods</h2>
<table class="wikitable"><tr><th>Method</th><th>Kills germs</th><th>Removes dirt</th></tr>
<tr><td>Boiling</td><td>Yes</td><td>No</td></tr><tr><td>Cloth filter</td><td>No</td><td>Some</td></tr></table>
<h2>Storage</h2><p>Keep treated water in a clean container with a lid.</p></body></html>'''


def pdf(pages):
    """A PDF with one line of text a page, written by hand: enough for Poppler to draw a page and a thumbnail."""
    objects = ['<< /Type /Catalog /Pages 2 0 R >>',
               '<< /Type /Pages /Kids [%s] /Count %d >>' % (' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages))), len(pages)),
               '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for i, text in enumerate(pages):
        stream = f'BT /F1 18 Tf 60 720 Td (Safe water, page {i + 1}) Tj /F1 11 Tf 0 -30 Td ({text[:80]}) Tj ET'
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R '
                       '/Resources << /Font << /F1 3 0 R >> >> >>')
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
    out, offsets = b'%PDF-1.4\n', []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{n} 0 obj\n{body}\nendobj\n'.encode()
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    return out + f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


class Page(Item):
    def __init__(self, path, title, html):
        super().__init__()
        self.path, self.title, self.html = path, title, html

    def get_path(self):
        return self.path

    def get_title(self):
        return self.title

    def get_mimetype(self):
        return 'text/html'

    def get_contentprovider(self):
        return StringProvider(self.html)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: True}


def main(data):
    lib = data / 'library-data'
    for d in ('documents', 'pages', 'collections', 'text'):
        (lib / d).mkdir(parents=True)
    (lib / 'documents' / 'safe-water.pdf').write_bytes(pdf(PAGES))
    (lib / 'text' / 'safe-water.txt').write_text('\n\f\n'.join(PAGES), encoding='utf-8')
    (lib / 'pages' / 'rope.html').write_text('<!doctype html><title>Rope</title><h1>Rope</h1><p>A saved page about rope.</p>')
    records = [{'id': 'safe-water', 'kind': 'pdf', 'path': 'documents/safe-water.pdf', 'title': 'Safe water at home',
                'publisher': 'Preview Press', 'year': '2020', 'pages': len(PAGES), 'category': 'water', 'collection': 'Manuals'},
               {'id': 'rope', 'kind': 'html', 'path': 'pages/rope.html', 'title': 'Rope', 'publisher': 'Preview Press',
                'category': 'skills', 'collection': 'Manuals'}]
    (lib / 'catalog.json').write_text(json.dumps(records))
    (lib / 'summary.json').write_text('{"resources": 2, "pdfs": 1, "pdf_pages": 2, "web_pages": 1}')
    con = sqlite3.connect(lib / 'search.sqlite')
    con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,'
                'page UNINDEXED,body,tokenize="unicode61")')
    for n, body in enumerate(PAGES, 1):
        con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)',
                    ('safe-water', 'Safe water at home', 'Manuals', 'water', 'documents/safe-water.pdf', n, body))
    con.commit()
    con.close()
    with Creator(lib / 'collections' / 'mdwiki_preview.zim').config_indexing(True, 'eng') as zim:
        zim.set_mainpath('Water_purification')
        zim.add_item(Page('Water_purification', 'Water purification', ARTICLE))
        for name, value in (('Title', 'Preview encyclopedia'), ('Language', 'eng'), ('Name', 'mdwiki_preview')):
            zim.add_metadata(name, value)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
