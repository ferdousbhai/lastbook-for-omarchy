#!/usr/bin/env python3
"""Cuts the offline map out of a Protomaps world build: OpenStreetMap vector tiles (MVT) into an MBTiles file.

  python3 tools/map_extract.py OUT.mbtiles --world [--maxzoom 8]
  python3 tools/map_extract.py OUT.mbtiles --bbox W,S,E,N [--minzoom 9] [--maxzoom 15] [--name "Bali"]
  options: --build 20261117.pmtiles (default: the newest listed at build-metadata.protomaps.dev)

The source is a PMTiles v3 archive (https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md) read over HTTPS
range requests, so only the wanted tiles are downloaded: the world to zoom 8 is about 0.6 GB, a region at street level
(zoom 9-15) about 1.45 times its Geofabrik .osm.pbf. Tiles stay as the archive stores them (gzip-compressed MVT) and go
into a standard MBTiles table (TMS rows), which the app reads and to which regions can be added (INSERT OR REPLACE).
Data © OpenStreetMap contributors (ODbL); the build is Protomaps' (https://docs.protomaps.com/basemaps/downloads).
"""
import argparse
import gzip
import json
import math
import sqlite3
import sys
import time
import urllib.request

BUILDS = 'https://build-metadata.protomaps.dev/builds.json'
BASE = 'https://build.protomaps.com/'
UA = {'User-Agent': 'lastbook-map-extract/1'}


def fetch(url, start=None, length=None, tries=5):
    for i in range(tries):
        req = urllib.request.Request(url, headers=dict(UA, **({'Range': f'bytes={start}-{start + length - 1}'} if start is not None else {})))
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if start is not None and len(data) != length:
                raise IOError(f'short read {len(data)} of {length}')
            return data
        except Exception as e:
            if i == tries - 1:
                raise
            print(f'  retry {i + 1}: {e}', file=sys.stderr, flush=True)
            time.sleep(2 * (i + 1))


# ------------------------------------------------------------------------------------------------- PMTiles v3 reading --
def varint(buf, pos):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def decompress(data, kind):
    return gzip.decompress(data) if kind == 2 else data   # 1 none, 2 gzip (the Protomaps builds use gzip)


def directory(raw):
    n, pos = varint(raw, 0)
    ids, runs, lengths, offsets = [0] * n, [0] * n, [0] * n, [0] * n
    last = 0
    for i in range(n):
        d, pos = varint(raw, pos)
        last += d
        ids[i] = last
    for i in range(n):
        runs[i], pos = varint(raw, pos)
    for i in range(n):
        lengths[i], pos = varint(raw, pos)
    for i in range(n):
        v, pos = varint(raw, pos)
        offsets[i] = offsets[i - 1] + lengths[i - 1] if v == 0 and i > 0 else v - 1
    return list(zip(ids, runs, lengths, offsets))


def header(buf):
    if buf[:7] != b'PMTiles' or buf[7] != 3:
        raise SystemExit('not a PMTiles v3 archive')
    u64 = lambda o: int.from_bytes(buf[o:o + 8], 'little')
    return {'root_offset': u64(8), 'root_length': u64(16), 'metadata_offset': u64(24), 'metadata_length': u64(32),
            'leaf_offset': u64(40), 'leaf_length': u64(48), 'data_offset': u64(56), 'internal_compression': buf[97],
            'tile_compression': buf[98], 'tile_type': buf[99], 'min_zoom': buf[100], 'max_zoom': buf[101]}


def zxy_to_id(z, x, y):
    """The Hilbert-ordered tile id PMTiles uses."""
    acc = sum(4 ** i for i in range(z))
    n = 1 << z
    d, s = 0, n >> 1
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def id_to_zxy(i):
    acc, z = 0, 0
    while True:
        n = 4 ** z
        if acc + n > i:
            break
        acc += n
        z += 1
    d, x, y, s = i - acc, 0, 0, 1
    while s < (1 << z):
        rx = 1 & (d // 2)
        ry = 1 & (d ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        d //= 4
        s *= 2
    return z, x, y


def tile_range(lon_w, lat_s, lon_e, lat_n, z):
    def tx(lon):
        return min((1 << z) - 1, max(0, int((lon + 180) / 360 * (1 << z))))

    def ty(lat):
        lat = max(-85.0511, min(85.0511, lat))
        r = math.radians(lat)
        return min((1 << z) - 1, max(0, int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z))))
    return tx(lon_w), ty(lat_n), tx(lon_e), ty(lat_s)


class Archive:
    def __init__(self, url):
        self.url = url
        head = fetch(url, 0, 16384)
        self.h = header(head)
        self.root = directory(decompress(head[self.h['root_offset']:self.h['root_offset'] + self.h['root_length']], self.h['internal_compression']))
        self._leaves = {}

    def metadata(self):
        raw = fetch(self.url, self.h['metadata_offset'], self.h['metadata_length'])
        return json.loads(decompress(raw, self.h['internal_compression']))

    def leaf(self, offset, length):
        key = (offset, length)
        if key not in self._leaves:
            raw = fetch(self.url, self.h['leaf_offset'] + offset, length)
            self._leaves[key] = directory(decompress(raw, self.h['internal_compression']))
        return self._leaves[key]

    def entries(self, lo, hi, d=None, end=None):
        """Every tile entry (id, run, length, offset) with lo <= id < hi. A leaf directory covers the ids from its entry
        up to the next entry's, so only the leaves that overlap [lo, hi) are fetched."""
        d = self.root if d is None else d
        for i, (tid, run, length, offset) in enumerate(d):
            nxt = d[i + 1][0] if i + 1 < len(d) else (end if end is not None else 1 << 62)
            if run == 0:
                if tid < hi and nxt > lo:
                    yield from self.entries(lo, hi, self.leaf(offset, length), nxt)
            elif tid + run > lo and tid < hi:
                yield tid, run, length, offset


def run(args):
    build = args.build
    if not build:
        build = json.loads(fetch(BUILDS))[-1]['key']
    url = BASE + build
    print(f'source {url}', flush=True)
    a = Archive(url)
    if a.h['tile_type'] != 1:
        raise SystemExit('expected vector (MVT) tiles')
    db = sqlite3.connect(args.out)
    db.executescript('''CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row);''')
    meta = a.metadata()
    wanted = []   # (z, x0, y0, x1, y1)
    if args.world:
        lo, hi = 0, sum(4 ** z for z in range(args.maxzoom + 1))
        ranges = [(lo, hi)]
    else:
        w, s, e, n = map(float, args.bbox.split(','))
        ranges = []
        for z in range(args.minzoom, args.maxzoom + 1):
            x0, y0, x1, y1 = tile_range(w, s, e, n, z)
            wanted.append((z, x0, y0, x1, y1))
            ids = [zxy_to_id(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
            ranges.append((min(ids), max(ids) + 1))
    inside = (lambda z, x, y: True) if args.world else (lambda z, x, y: any(z == wz and x0 <= x <= x1 and y0 <= y <= y1 for wz, x0, y0, x1, y1 in wanted))
    # Collect entries, then fetch their bytes in merged spans (tiles of one region sit near each other in the archive).
    todo = []
    for lo, hi in ranges:
        for tid, run_len, length, offset in a.entries(lo, hi):
            for k in range(run_len):   # a run is the same bytes for consecutive ids (ocean, empty land)
                t = tid + k
                if lo <= t < hi:
                    z, x, y = id_to_zxy(t)
                    if inside(z, x, y):
                        todo.append((offset, length, z, x, y))
    todo.sort()
    total = sum(length for offset, length, *_ in {(o, l, 0, 0, 0) for o, l, *_ in todo})
    print(f'{len(todo)} tiles, about {total / 1e6:.0f} MB to fetch', flush=True)
    span, done, t0 = [], 0, time.time()

    def flush(span):
        nonlocal done
        start = span[0][0]
        end = max(o + l for o, l, *_ in span)
        data = fetch(url, a.h['data_offset'] + start, end - start)
        rows = [(z, x, (1 << z) - 1 - y, data[o - start:o - start + l]) for o, l, z, x, y in span]
        db.executemany('INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)', rows)
        done += end - start
        print(f'  {done / 1e6:.0f} MB, {len(rows)} tiles, {done / 1e6 / max(1, time.time() - t0):.1f} MB/s', flush=True)
    for item in todo:
        if span and (item[0] - (span[-1][0] + span[-1][1]) > 256 * 1024 or item[0] + item[1] - span[0][0] > 32 * 1024 * 1024):
            flush(span)
            db.commit()
            span = []
        span.append(item)
    if span:
        flush(span)
    zooms = db.execute('SELECT min(zoom_level), max(zoom_level) FROM tiles').fetchone()
    info = {'name': args.name or ('World' if args.world else 'Region'), 'format': 'pbf', 'type': 'baselayer',
            'minzoom': str(zooms[0]), 'maxzoom': str(zooms[1]), 'json': json.dumps({'vector_layers': meta.get('vector_layers', [])}),
            'attribution': '© OpenStreetMap contributors (ODbL), Protomaps', 'source': build}
    if args.bbox:
        info['bounds'] = args.bbox
    for k, v in info.items():
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (k, v))
    db.commit()
    db.execute('VACUUM')
    print('done', args.out, flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out')
    ap.add_argument('--world', action='store_true')
    ap.add_argument('--bbox')
    ap.add_argument('--minzoom', type=int, default=9)
    ap.add_argument('--maxzoom', type=int, default=None)
    ap.add_argument('--name')
    ap.add_argument('--build')
    a = ap.parse_args()
    if not a.world and not a.bbox:
        ap.error('--world or --bbox')
    if a.maxzoom is None:
        a.maxzoom = 8 if a.world else 15
    run(a)
