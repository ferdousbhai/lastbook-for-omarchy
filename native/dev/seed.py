#!/usr/bin/env python3
"""A small made-up library for dev/preview.sh: two manuals (one real two-page PDF), one saved page and one encyclopedia
article. Nothing here comes from a real library.

  PYTHONPATH=$(bin/test-deps) python3 -B native/dev/seed.py <data folder>"""
import gzip, json, math, sqlite3, sys
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

PAGES = ['Boil the water for one minute to kill the germs. If the water is cloudy, let it settle and filter it through a '
         'clean cloth first, then boil it.',
         'Store the treated water in a clean container with a lid. Keep it in a cool place, away from the sun.']
ARTICLE = '''<!doctype html><html><head><meta charset="utf-8"><title>Water purification</title></head><body>
<h1>Water purification</h1><div id="mw-content-text">
<p><b>Water purification</b> removes germs and dirt from water so that it is safe to drink. Boiling is the most
reliable way: <a href="Boiling">a rolling boil</a> for one minute kills the germs that cause disease.</p>
<h2>Methods</h2>
<table class="wikitable"><tr><th>Method</th><th>Kills germs</th><th>Removes dirt</th></tr>
<tr><td>Boiling</td><td>Yes</td><td>No</td></tr><tr><td>Cloth filter</td><td>No</td><td>Some</td></tr></table>
<h2>Storage</h2><p>Keep treated water in a clean container with a lid.</p></div></body></html>'''


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


# A made-up town with a street map around it, and one with none: the Map screen's help points, and its plain answer
# where no street map is installed. A tile is land with a road through it and the help points that fall in it.
TOWN = ('Mapton', 'Somewhere', -8.5, 115.25)
HELP = [('hospital', 'RS Mapton', 0.004, 0.0), ('clinic', 'Klinik Sehat', -0.002, -0.003), ('doctors', '', 0.001, 0.004),
        ('pharmacy', 'Apotek Satu', 0.0, 0.002), ('police', 'Polsek Mapton', -0.004, 0.001),
        ('fire_station', 'Pemadam Kebakaran', 0.006, -0.005), ('drinking_water', 'Water refill', -0.001, 0.0),
        ('shelter', '', 0.002, -0.002), ('toilets', '', -0.003, 0.004), ('supermarket', 'Pasar Mapton', 0.003, 0.003)]


def _varint(n):
    out = bytearray()
    while n > 0x7f:
        out.append(n & 0x7f | 0x80)
        n >>= 7
    return bytes(out) + bytes([n])


def _field(number, payload):
    return _varint(number << 3) + _varint(payload) if isinstance(payload, int) else _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _layer(name, features):
    """A layer of (properties, geometry type, commands): a text property is a string value, a number an unsigned one."""
    keys, values, body = [], [], b''
    for props, shape, cmds in features:
        tags = []
        for k, v in props.items():
            v = _field(1, v.encode()) if isinstance(v, str) else _field(5, v)
            for seen, item in ((keys, k.encode()), (values, v)):
                seen.append(item) if item not in seen else None
                tags.append(seen.index(item))
        body += _field(2, _field(2, b''.join(map(_varint, tags))) + _field(3, shape) + _field(4, b''.join(map(_varint, cmds))))
    return _field(3, _field(1, name.encode()) + body + b''.join(_field(3, k) for k in keys) + b''.join(_field(4, v) for v in values) + _field(5, 4096) + _field(15, 2))


def _zz(n):
    return n << 1 if n >= 0 else (-n << 1) - 1


def _tile(z, x, y, places=()):
    land = ({'kind': 'earth'}, 3, [9, _zz(-64), _zz(-64), 26, _zz(4224), 0, 0, _zz(4224), _zz(-4224), 0, 15])
    road = ({'kind': 'major_road', 'min_zoom': 9}, 2, [9, 0, _zz(2100), 10, _zz(4096), 0])
    pois = []
    for kind, name, lat, lon in places:
        px, py = _world(lat, lon, z)
        if int(px) == x and int(py) == y:
            pois.append(({'kind': kind, 'min_zoom': 14, **({'name': name} if name else {})}, 1, [9, _zz(round((px - x) * 4096)), _zz(round((py - y) * 4096))]))
    return gzip.compress(_layer('earth', [land]) + _layer('roads', [road]) + _layer('pois', pois))


def _world(lat, lon, z):
    return (lon + 180) / 360 * (1 << z), (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * (1 << z)


def _mbtiles(path, name, minzoom, maxzoom, tiles):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE metadata (name TEXT, value TEXT); CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);')
    db.executemany('INSERT INTO metadata VALUES (?,?)', [('name', name), ('format', 'pbf'), ('minzoom', str(minzoom)), ('maxzoom', str(maxzoom))])
    db.executemany('INSERT INTO tiles VALUES (?,?,?,?)', [(z, x, (1 << z) - 1 - y, b) for (z, x, y), b in tiles.items()])   # rows from the south
    db.commit()
    db.close()


def maps(data):
    geo = data / 'derived' / 'geo'
    geo.mkdir(parents=True)
    name, detail, lat, lon = TOWN
    # The world to zoom 8, as setup makes it, where the street map is: a tile drawn thousands of times its size is not.
    cx, cy = map(int, _world(lat, lon, 8))
    _mbtiles(geo / 'world.mbtiles', 'world', 0, 8, {(0, 0, 0): _tile(0, 0, 0), **{(8, x, y): _tile(8, x, y) for x in range(cx - 1, cx + 2) for y in range(cy - 1, cy + 2)}})
    places = [(kind, label, lat + dy, lon + dx) for kind, label, dy, dx in HELP]
    tiles = {}
    for z in range(12, 16):
        cx, cy = map(int, _world(lat, lon, z))
        for x in range(cx - 2, cx + 3):
            for y in range(cy - 2, cy + 3):
                tiles[(z, x, y)] = _tile(z, x, y, places)
    _mbtiles(geo / 'regions-testland.mbtiles', 'testland', 12, 15, tiles)
    con = sqlite3.connect(geo / 'world-places.sqlite')
    # user_version 2 as engine/setup/maps.py marks the place search it makes now (PLACES_VERSION): setup then shows it as here.
    con.executescript('''PRAGMA user_version = 2;
        CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');''')
    for i, (n, d, y, x) in enumerate([TOWN, ('Testville', 'Nowhere', 12.5, 34.25)], 1):
        con.execute('INSERT INTO place VALUES (?,?,?,?,?,?,?,?)', (i, n, 'town', d, y, x, 1, 'made up'))
        con.execute('INSERT INTO place_fts(rowid, name, detail) VALUES (?,?,?)', (i, n, d))
    con.commit()
    con.close()


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
               # The id the repair guide's choices read from, so `guide:repair` has choices to show.
               {'id': 'eng-cea09c7220cd4bd0', 'kind': 'pdf', 'path': 'documents/safe-water.pdf', 'title': 'Hand tools',
                'publisher': 'Preview Press', 'year': '2020', 'pages': len(PAGES), 'category': 'repair', 'collection': 'Manuals'},
               {'id': 'rope', 'kind': 'html', 'path': 'pages/rope.html', 'title': 'Rope', 'publisher': 'Preview Press',
                'category': 'skills', 'collection': 'Manuals'}]
    (lib / 'catalog.json').write_text(json.dumps(records))
    (lib / 'summary.json').write_text('{"pdfs": 2, "pdf_pages": 4, "web_pages": 1, "videos": 0, "collections": 1, "document_bytes": 0}')
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
        for name, value in (('Title', 'Preview encyclopedia'), ('Language', 'eng'), ('Name', 'mdwiki_preview'),
                            ('Description', 'Made-up health articles for the preview')):
            zim.add_metadata(name, value)
    maps(data)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
