"""A small made-up library for the bridge tests. Built once under derived/app-build/testdata. And a made-up street map
(region_file) for the tests of the help points read from one."""
import gzip, hashlib, json, math, os, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'derived' / 'app-build' / 'testdata'
LIB = DATA / 'library-data'
CANARY = b'CANARY-MUST-NEVER-BE-SERVED'
MiB = 1024 * 1024


def pattern(offset, n):
    """Bytes that say where they are, so a wrong slice cannot pass."""
    first = offset // 4
    words = b''.join((i & 0xffffffff).to_bytes(4, 'big') for i in range(first, first + n // 4 + 2))
    return words[offset - first * 4:offset - first * 4 + n]


def write_pattern(path, size):
    with open(path, 'wb') as f:
        for off in range(0, size, MiB):
            f.write(pattern(off, min(MiB, size - off)))


def link_out():
    """The link the escape checks need: a served folder pointing at a file outside the library. Made again on every run, so one that was removed or points somewhere else is put back."""
    link, target = LIB / 'documents' / 'out.txt', DATA / 'outside-canary.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_bytes(CANARY)      # the 'ready' branch below does not rebuild the fixture: a deleted canary would leave a link that resolves to nothing
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def ensure():
    if (DATA / 'ready').exists():
        link_out()
        return DATA
    for d in ('videos', 'documents', 'pages', 'tools', 'logs', 'collections', 'transcripts', 'assets'):
        (LIB / d).mkdir(parents=True, exist_ok=True)
    (DATA / 'derived' / 'geo').mkdir(parents=True, exist_ok=True)
    (DATA / 'derived' / 'cache').mkdir(parents=True, exist_ok=True)
    (DATA / 'tmp').mkdir(exist_ok=True)
    write_pattern(LIB / 'videos' / 'a.mp4', 20 * MiB)
    write_pattern(LIB / 'videos' / 'b.webm', 3 * MiB)
    with open(LIB / 'videos' / 'huge.mkv', 'wb') as f:
        f.truncate(200 * MiB)
    with open(LIB / 'documents' / 's.pdf', 'wb') as f:
        f.write(pattern(0, MiB))
    write_pattern(LIB / 'documents' / 'big.pdf', 70 * MiB)
    write_pattern(LIB / 'documents' / 'big.bin', 40 * MiB)
    write_pattern(LIB / 'documents' / 'over.bin', 33 * MiB)
    write_pattern(LIB / 'documents' / 'mid.bin', 5 * MiB)
    (LIB / 'documents' / 'plain.txt').write_text('plain text file\n')
    (LIB / 'documents' / 'x.info.json').write_bytes(CANARY)
    (LIB / 'documents' / '.hidden').write_bytes(CANARY)
    (LIB / 'documents' / 'video-auth-copy.json').write_bytes(CANARY)
    (LIB / 'video-auth.json').write_bytes(CANARY)
    (LIB / 'canary-root.txt').write_bytes(CANARY)
    (LIB / 'tools' / 'secret.txt').write_bytes(CANARY)
    (LIB / 'logs' / 'x.log').write_bytes(CANARY)
    (DATA / 'outside-canary.txt').write_bytes(CANARY)
    link_out()
    (LIB / 'documents' / 'hl.txt').write_bytes(CANARY)
    os.link(LIB / 'documents' / 'hl.txt', LIB / 'pages' / 'hl2.txt')
    (LIB / 'pages' / 's.html').write_text('<!doctype html><title>Saved</title><p>saved page</p>')
    (LIB / 'pages' / 'a.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    (LIB / 'reader.css').write_text('body{}')
    recs = []
    for rid, kind, path in [('pdfsmall', 'pdf', 'documents/s.pdf'), ('pdfbig', 'pdf', 'documents/big.pdf'), ('saved', 'html', 'pages/s.html'), ('vida', 'video', 'videos/a.mp4'), ('txt', 'text', 'documents/plain.txt')]:
        recs.append({'id': rid, 'kind': kind, 'path': path, 'title': rid, 'category': 'test', 'collection': 'Test', 'publisher': 'Test', 'bytes': (LIB / path).stat().st_size})
    (LIB / 'catalog.json').write_text(json.dumps(recs))
    (LIB / 'summary.json').write_text('{"resources": 5}')
    con = sqlite3.connect(LIB / 'search.sqlite')
    con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
    con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('pdfsmall', 'pdfsmall', 'Test', 'test', 'documents/s.pdf', 1, 'boil water for one minute'))
    con.commit()
    con.close()
    (DATA / 'ready').write_text('1')
    return DATA


# ------------------------------------------------------------------------------------------- a made-up street map --
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number, payload):
    """A length-delimited field (bytes) or a varint one (int)."""
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def map_tile(layers, extent=4096):
    """A vector tile as setup stores it (gzip): {layer name: [(properties, (x, y))]}, each a point in tile units. A text
    property is a string value, a whole number an unsigned one."""
    def zigzag(n):
        return (n << 1) ^ (n >> 63)
    tile = b''
    for name, features in layers.items():
        keys, values, body = [], [], b''
        for props, (x, y) in features:
            tags = b''
            for k, v in props.items():
                value = _field(1, v.encode()) if isinstance(v, str) else _field(5, v)
                for seen, item in ((keys, k.encode()), (values, value)):
                    if item not in seen:
                        seen.append(item)
                    tags += _varint(seen.index(item))
            body += _field(2, _field(2, tags) + _field(3, 1) + _field(4, _varint(9) + _varint(zigzag(x)) + _varint(zigzag(y))))
        tile += _field(3, _field(1, name.encode()) + body + b''.join(_field(3, k) for k in keys) + b''.join(_field(4, v) for v in values)
                       + _field(5, extent) + _field(15, 2))
    return gzip.compress(tile)


def region_file(path, points, tiles=None, minzoom=9, maxzoom=15):
    """A street map as setup makes one (MBTiles), holding these help points: {zoom: [(kind, name or None, lat, lon)]}.
    Each tile also has a building, which is no help point. `tiles` adds ready-made ones: {(zoom, x, y): bytes}."""
    by_tile = {}
    for zoom, found in points.items():
        for kind, name, lat, lon in found:
            n = 1 << zoom
            x, y = (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
            props = {'kind': kind, 'min_zoom': zoom, **({'name': name, 'name:en': name + ' (en)'} if name else {})}
            by_tile.setdefault((zoom, int(x), int(y)), []).append((props, (round((x - int(x)) * 4096), round((y - int(y)) * 4096))))
    blobs = {key: map_tile({'buildings': [({'kind': 'hospital', 'name': 'A building'}, (9, 9))], 'pois': features})
             for key, features in by_tile.items()}
    blobs.update(tiles or {})
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);''')
    db.executemany('INSERT INTO metadata VALUES (?,?)', {'name': 'Test', 'format': 'pbf', 'minzoom': str(minzoom), 'maxzoom': str(maxzoom)}.items())
    db.executemany('INSERT INTO tiles VALUES (?,?,?,?)', [(z, x, (1 << z) - 1 - y, blob) for (z, x, y), blob in blobs.items()])   # rows from the south
    db.commit()
    db.close()
    return path


if __name__ == '__main__':
    print(ensure())
