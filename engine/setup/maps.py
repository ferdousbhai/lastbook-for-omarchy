"""The offline map, made on the person's computer during setup: nothing about it is hosted by Lastbook.

  - The world map (to city level, zoom 0-8, about 0.6 GB) and any street-level region (zoom 9-15) are cut out of the newest
    Protomaps daily build of OpenStreetMap (https://docs.protomaps.com/basemaps/downloads), a PMTiles archive read over
    HTTPS range requests through download.py, the one way out: only the tiles wanted are fetched. They are written as
    MBTiles (SQLite) files the window draws: derived/geo/world.mbtiles and derived/geo/regions-<id>.mbtiles.
  - Place search for the whole world is built from GeoNames (CC BY 4.0): every country, first-level region, and town of
    15,000 people or more, with alternate names: derived/geo/world-places.sqlite.
  - The regions offered are Geofabrik's (countries, and parts of large countries), listed with their bounding boxes in
    map-regions.json, bundled with the app (packaging/arch/map-regions.json; tools/make_region_list.py makes it).

Files are built beside their final name and moved into place when complete, so a stopped download leaves nothing half
made. Integrity comes from HTTPS to the allow-listed hosts with the pinned roots (there is no published checksum for a
cut of a daily build), and every tile directory and the gazetteer are parsed before anything is kept.
"""
import csv
import gzip
import io
import json
import math
import os
import sqlite3
import time
import zipfile
from pathlib import Path

from . import download

BUILDS = 'https://build-metadata.protomaps.dev/builds.json'
BUILD_BASE = 'https://build.protomaps.com/'
GEONAMES = 'https://download.geonames.org/export/dump/'
WORLD_MAXZOOM = 8
REGION_MINZOOM, REGION_MAXZOOM = 9, 15
WORLD_FILE, PLACES_FILE = 'world.mbtiles', 'world-places.sqlite'
# Our map files: they belong to no pack's map set, and an upgraded library keeps them.
def ours(name):
    return name in (WORLD_FILE, PLACES_FILE) or (name.startswith('regions-') and name.endswith('.mbtiles'))


# -------------------------------------------------------------------------------------------------------- regions --
def regions_file():
    """setup/map-regions.json in the installed app (the PKGBUILD puts it there), else the source tree's copy."""
    root = Path(__file__).resolve().parents[2]
    installed = root / 'setup' / 'map-regions.json'
    return installed if installed.is_file() else root / 'packaging' / 'arch' / 'map-regions.json'


_regions = None


def regions():
    """[{id, name, parent, bbox: [w, s, e, n]}] as bundled; [] if the list is missing."""
    global _regions
    if _regions is None:
        try:
            _regions = json.loads(regions_file().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            _regions = []
    return _regions


def region(rid):
    return next((r for r in regions() if r['id'] == rid), None)


def file_id(rid):
    """A Geofabrik id (us/california) as a file name part (us-california)."""
    return rid.replace('/', '-')


def installed(geo):
    """What map files this library has: {world: bool, places: bool, regions: [{id, name, bytes}]}."""
    geo = Path(geo)
    out = {'world': (geo / WORLD_FILE).is_file(), 'places': (geo / PLACES_FILE).is_file(), 'regions': []}
    by_file = {file_id(r['id']): r for r in regions()}
    for p in sorted(geo.glob('regions-*.mbtiles')) if geo.is_dir() else []:
        fid = p.name[len('regions-'):-len('.mbtiles')]
        r = by_file.get(fid)
        out['regions'].append({'id': r['id'] if r else fid, 'name': r['name'] if r else fid, 'bytes': p.stat().st_size})
    return out


# ------------------------------------------------------------------------------------------------- PMTiles reading --
def _varint(buf, pos):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _directory(raw):
    n, pos = _varint(raw, 0)
    ids, runs, lengths, offsets = [0] * n, [0] * n, [0] * n, [0] * n
    last = 0
    for i in range(n):
        d, pos = _varint(raw, pos)
        last += d
        ids[i] = last
    for i in range(n):
        runs[i], pos = _varint(raw, pos)
    for i in range(n):
        lengths[i], pos = _varint(raw, pos)
    for i in range(n):
        v, pos = _varint(raw, pos)
        offsets[i] = offsets[i - 1] + lengths[i - 1] if v == 0 and i > 0 else v - 1
    return list(zip(ids, runs, lengths, offsets))


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
    while acc + 4 ** z <= i:
        acc += 4 ** z
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


def tile_range(w, s, e, n, z):
    def tx(lon):
        return min((1 << z) - 1, max(0, int((lon + 180) / 360 * (1 << z))))

    def ty(lat):
        lat = math.radians(max(-85.0511, min(85.0511, lat)))
        return min((1 << z) - 1, max(0, int((1 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi) / 2 * (1 << z))))
    return tx(w), ty(n), tx(e), ty(s)


class Archive:
    """A remote PMTiles v3 archive (https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)."""

    def __init__(self, url, control, allowed=None, context=None):
        self.url, self.control, self.allowed, self.context = url, control, allowed, context
        head = self.get(0, 16384)
        if head[:7] != b'PMTiles' or head[7] != 3:
            raise download.Failed('network', 'The map server sent something that is not a map archive.')
        u64 = lambda o: int.from_bytes(head[o:o + 8], 'little')
        self.root_off, self.root_len = u64(8), u64(16)
        self.leaf_off, self.data_off = u64(40), u64(56)
        self.compression, self.tile_type = head[97], head[99]
        if self.tile_type != 1:
            raise download.Failed('network', 'The map archive does not hold vector tiles.')
        self.root = _directory(self._inflate(head[self.root_off:self.root_off + self.root_len]))
        self._leaves = {}

    def get(self, start, length):
        return download.fetch_range(self.url, start, length, self.control, allowed=self.allowed, context=self.context)

    def _inflate(self, data):
        return gzip.decompress(data) if self.compression == 2 else data

    def entries(self, lo, hi, d=None, end=None):
        """Tile entries (id, run, length, offset) with lo <= id < hi; only the leaf directories that overlap are fetched."""
        d = self.root if d is None else d
        for i, (tid, run, length, offset) in enumerate(d):
            nxt = d[i + 1][0] if i + 1 < len(d) else (end if end is not None else 1 << 62)
            if run == 0:
                if tid < hi and nxt > lo:
                    key = (offset, length)
                    if key not in self._leaves:
                        self._leaves[key] = _directory(self._inflate(self.get(self.leaf_off + offset, length)))
                    yield from self.entries(lo, hi, self._leaves[key], nxt)
            elif tid + run > lo and tid < hi:
                yield tid, run, length, offset


def newest_build(allowed=None, context=None):
    raw = download.fetch_small(BUILDS, limit=4 << 20, allowed=allowed, context=context, timeout=30)
    try:
        return BUILD_BASE + json.loads(raw)[-1]['key']
    except (TypeError, ValueError, KeyError, IndexError):
        raise download.Failed('network', 'Lastbook could not reach the map server to find the newest map. Try again later.')


class Cut:
    """One map file cut out of the newest build: the world (bbox None) or a region. measure() finds the tiles and their
    size without fetching any; run() fetches them into `dest` (moved into place when complete)."""

    def __init__(self, dest, bbox, minzoom, maxzoom, name, control, allowed=None, context=None):
        self.dest, self.bbox, self.minzoom, self.maxzoom, self.name = Path(dest), bbox, minzoom, maxzoom, name
        self.control, self.allowed, self.context = control, allowed, context
        self.todo, self.total, self.build = None, 0, None

    def measure(self):
        self.build = newest_build(self.allowed, self.context)
        self.archive = Archive(self.build, self.control, self.allowed, self.context)
        if self.bbox is None:
            spans = [(0, sum(4 ** z for z in range(self.maxzoom + 1)), None)]
        else:
            spans = []
            for z in range(self.minzoom, self.maxzoom + 1):
                x0, y0, x1, y1 = tile_range(*self.bbox, z)
                ids = [zxy_to_id(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)] if (x1 - x0 + 1) * (y1 - y0 + 1) <= 400_000 else None
                lo = min(ids) if ids else zxy_to_id(z, 0, 0)
                hi = max(ids) + 1 if ids else sum(4 ** i for i in range(z + 1))
                spans.append((lo, hi, (z, x0, y0, x1, y1)))
        todo = []
        for lo, hi, box in spans:
            for tid, run, length, offset in self.archive.entries(lo, hi):
                for k in range(run):   # a run is the same bytes for consecutive ids (ocean, empty land)
                    t = tid + k
                    if not lo <= t < hi:
                        continue
                    z, x, y = id_to_zxy(t)
                    if box and not (z == box[0] and box[1] <= x <= box[3] and box[2] <= y <= box[4]):
                        continue
                    todo.append((offset, length, z, x, y))
        todo.sort()
        self.todo = todo
        self.total = sum(length for offset, length in {(o, l) for o, l, *_ in todo})
        return self.total

    def run(self, progress=lambda done, total: None):
        if self.todo is None:
            self.measure()
        part = self.dest.with_name(self.dest.name + '.part')
        part.unlink(missing_ok=True)
        db = sqlite3.connect(part)
        db.executescript('''CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
            CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);''')
        done, span = 0, []

        def flush(span):
            nonlocal done
            start = span[0][0]
            end = max(o + l for o, l, *_ in span)
            data = self.archive.get(self.archive.data_off + start, end - start)
            db.executemany('INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)',
                           [(z, x, (1 << z) - 1 - y, data[o - start:o - start + l]) for o, l, z, x, y in span])
            db.commit()
            done += end - start
            progress(min(done, self.total), self.total)
        try:
            for item in self.todo:
                if span and (item[0] - (span[-1][0] + span[-1][1]) > 256 * 1024 or item[0] + item[1] - span[0][0] > 16 * 1024 * 1024):
                    flush(span)
                    span = []
                span.append(item)
            if span:
                flush(span)
            meta = {'name': self.name, 'format': 'pbf', 'type': 'baselayer', 'minzoom': str(self.minzoom if self.bbox else 0),
                    'maxzoom': str(self.maxzoom), 'attribution': '© OpenStreetMap contributors (ODbL), Protomaps',
                    'source': self.build.rsplit('/', 1)[-1], 'made': time.strftime('%Y-%m-%d')}
            if self.bbox:
                meta['bounds'] = ','.join(f'{v:.4f}' for v in self.bbox)
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', meta.items())
            db.commit()
        finally:
            db.close()
        os.replace(part, self.dest)
        return self.dest


# ------------------------------------------------------------------------------------------------------ gazetteer --
def build_places(dest, control, allowed=None, context=None):
    """The world gazetteer from GeoNames, same tables as places.sqlite (place, place_fts), so the engine searches it the
    same way. rank: countries 0, regions and cities over a million 1, over 100,000 2, the rest 3."""
    csv.field_size_limit(10_000_000)

    def get(name, limit):
        data = download.fetch_small(GEONAMES + name, limit=limit, allowed=allowed, context=context, timeout=60)
        if data is None:
            raise download.Failed('network', 'Lastbook could not download the place names. Try again later.')
        return data

    def rows(text):
        return [r for r in csv.reader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE) if r and not r[0].startswith('#')]
    control.check()
    countries = {r[0]: r[4] for r in rows(get('countryInfo.txt', 1 << 20).decode('utf-8'))}
    admin1 = {r[0]: r[1] for r in rows(get('admin1CodesASCII.txt', 4 << 20).decode('utf-8'))}
    control.check()
    cities = rows(zipfile.ZipFile(io.BytesIO(get('cities15000.zip', 16 << 20))).read('cities15000.txt').decode('utf-8'))
    if len(cities) < 10_000 or len(countries) < 200:
        raise download.Failed('network', 'The place names downloaded incomplete. Try again later.')
    part = Path(str(dest) + '.part')
    part.unlink(missing_ok=True)
    db = sqlite3.connect(part)
    db.executescript('''CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');''')
    src = 'GeoNames (CC BY 4.0)'
    capital, largest = {}, {}

    def add(name, kind, detail, lat, lon, rank, search_detail=None):
        cur = db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)', (name, kind, detail, lat, lon, rank, src))
        db.execute('INSERT INTO place_fts(rowid,name,detail) VALUES (?,?,?)', (cur.lastrowid, name, search_detail or detail))
    for r in cities:
        # geonameid name asciiname alternatenames lat lon fclass fcode cc cc2 admin1 admin2 admin3 admin4 population ...
        name, ascii_, alts, lat, lon, fcode, cc, a1, pop = r[1], r[2], r[3], float(r[4]), float(r[5]), r[7], r[8], r[10], int(r[14] or 0)
        detail = ', '.join(x for x in (admin1.get(f'{cc}.{a1}', ''), countries.get(cc, cc)) if x)
        others = [a for a in dict.fromkeys(alts.split(',')) if a and a not in (name, ascii_) and len(a) < 40][:12]
        kind = 'city' if pop >= 100_000 or fcode in ('PPLC', 'PPLA') else 'town'
        rank = 1 if pop >= 1_000_000 or fcode == 'PPLC' else 2 if pop >= 100_000 else 3
        add(name, kind, detail, lat, lon, rank, detail + ' · ' + ' '.join([ascii_] + others))
        if fcode == 'PPLC':
            capital[cc] = (lat, lon)
        if pop > largest.get(f'{cc}.{a1}', (-1,))[0]:
            largest[f'{cc}.{a1}'] = (pop, lat, lon)
    for cc, name in countries.items():
        at = capital.get(cc) or max(((p, la, lo) for k, (p, la, lo) in largest.items() if k.startswith(cc + '.')), default=None)
        if at:
            add(name, 'country', cc, at[-2], at[-1], 0)
    for key, name in admin1.items():
        if key in largest:
            _, lat, lon = largest[key]
            add(name, 'region', countries.get(key.split('.')[0], ''), lat, lon, 1)
    db.commit()
    db.execute("INSERT INTO place_fts(place_fts) VALUES('optimize')")
    db.commit()
    db.close()
    os.replace(part, dest)
    return Path(dest)
