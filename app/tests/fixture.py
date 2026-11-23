"""A small made-up library for the bridge tests. Built once under derived/app-build/testdata. And a made-up street map
(region_file) for the tests of the help points read from one, the keyword index's table (search_db), the local HTTPS
server the setup and download tests run against (start_server), and a stand-in Recall (FakeRecall)."""
import gzip, hashlib, http.server, json, math, os, re, socket, socketserver, sqlite3, ssl, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'derived' / 'app-build' / 'testdata'
LIB = DATA / 'library-data'
CANARY = b'CANARY-MUST-NEVER-BE-SERVED'
MiB = 1024 * 1024
PAGES = 'CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")'


def search_db(path, rows):
    """A keyword index (search.sqlite) as the library keeps it, holding these pages: (doc_id, title, collection, category, path, page, body)."""
    con = sqlite3.connect(path)
    con.execute(PAGES)
    con.executemany('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', rows)
    con.commit()
    con.close()
    return path


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
    search_db(LIB / 'search.sqlite', [('pdfsmall', 'pdfsmall', 'Test', 'test', 'documents/s.pdf', 1, 'boil water for one minute')])
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


# ------------------------------------------------------------------------------------- a local HTTPS test server --
class TLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A client that pauses, cancels, hangs up or is killed drops its connection, which is what these tests ask for.
        The server's next read can then raise, and the default handler would print a traceback into a passing run.
        Anything else is still printed."""
        if not isinstance(sys.exception(), OSError):
            super().handle_error(request, client_address)


def start_server(folder, files, handler):
    """(server, the client's TLS context, base address) on 127.0.0.1 with a certificate made for the run (folder/cert.pem),
    serving `files` (the handler's self.server.files) with this request handler."""
    key, crt = folder / 'key.pem', folder / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1', '-keyout', str(key), '-out', str(crt)],
                   check=True, capture_output=True)
    srv = TLSServer(('127.0.0.1', 0), handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(crt, key)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    srv.files = files
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, ssl.create_default_context(cafile=str(crt)), f'https://127.0.0.1:{srv.server_address[1]}'


# ------------------------------------------------------------------------------------------- a stand-in Recall --
def vector(text, dim):
    """Embeds by hashing words, so similar wording gives similar vectors."""
    import numpy as np
    v = np.zeros(dim, dtype=np.float32)
    for w in re.findall(r'[a-z0-9]+', text.lower().split('|', 1)[-1]):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


class FakeRecall(threading.Thread):
    """Recall's api 1 (capabilities, embed) on a Unix socket, answering from the moment it is made. calls: requests of
    any kind. requests: requests for passages ('title:...' inputs) answered. embeds: passages embedded so far.
    fail: an error to answer everything with. stop_after: requests for passages answered before it refuses to embed
    anything (None: never). reject: some words; a request holding them is refused, the others answered. biggest: the
    most inputs one request held. delay: seconds to wait before answering for passages."""

    def __init__(self, path, dim=64, operations=('embed',)):
        super().__init__(daemon=True)
        self.path, self.dim, self.operations, self.source = path, dim, list(operations), 'fake-model@1'
        self.fail, self.calls, self.embeds, self.requests, self.stop_after, self.reject, self.biggest, self.delay = None, 0, [], 0, None, None, 0, 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(16)
        self.start()

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                buf = b''
                while not buf.endswith(b'\n'):
                    piece = c.recv(65536)
                    if not piece:
                        break
                    buf += piece
                if not buf:
                    continue
                req = json.loads(buf)
                self.calls += 1
                if self.fail:
                    reply = {'api': 1, 'error': self.fail}
                elif req.get('op') == 'capabilities':
                    reply = {'api': 1, 'operations': self.operations, 'source': self.source}
                elif req.get('op') == 'embed':
                    assert req['kind'] == 'text' and 0 < len(req['inputs']) <= 32
                    passages = [t for t in req['inputs'] if t.startswith('title:')]   # not the probes, not questions
                    self.biggest = max(self.biggest, len(req['inputs']))
                    if self.stop_after is not None and self.requests >= self.stop_after:
                        reply = {'api': 1, 'error': 'Recall is busy'}
                    elif self.reject and any(self.reject in t for t in req['inputs']):
                        reply = {'api': 1, 'error': 'cannot embed this'}
                    else:
                        if passages:
                            time.sleep(self.delay)
                            self.requests += 1
                            self.embeds += passages
                        reply = {'api': 1, 'source': self.source, 'embeddings': [{'vector': vector(t, self.dim)} for t in req['inputs']]}
                else:
                    reply = {'api': 1, 'error': 'unknown op'}
                c.sendall(json.dumps(reply).encode() + b'\n')

    def close(self):
        self.sock.close()
        Path(self.path).unlink(missing_ok=True)


if __name__ == '__main__':
    print(ensure())
