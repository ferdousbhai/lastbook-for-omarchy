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
import bisect
import csv
from array import array
import io
import json
import math
import os
import sqlite3
import time
import zipfile
import zlib
from collections import OrderedDict
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
# What the map server sends is read within limits far above anything real (a leaf directory of the planet is about 0.1 MB
# and 60,000 entries), so a damaged or hostile archive costs an error, not this computer's memory.
MAX_DIRECTORY = 64 * 1024 * 1024    # one directory, as sent and as unpacked
MAX_ENTRIES = 4_000_000             # entries in one directory
MAX_ID = 1 << 56                    # tile ids and run lengths: zoom 26 has 4^26 tiles
MAX_DEPTH = 4                       # directories inside directories
LEAVES_KEPT = 8                     # leaf directories held at once: tiles are read in id order, so the same few are asked for again
BAD_INDEX = 'The map server sent a map index Lastbook could not read. Try again later.'


def _varint(buf, pos):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError('a number too long')


def _directory(raw):
    """(ids, runs, lengths, offsets) of one PMTiles directory, each an array in tile id order. ValueError, IndexError or
    OverflowError when it is not one."""
    n, pos = _varint(raw, 0)
    if n > min(MAX_ENTRIES, len(raw)):      # each entry takes at least four bytes
        raise ValueError('too many entries')
    ids, runs, lengths, offsets = (array('q', bytes(8 * n)) for _ in range(4))     # 8 bytes a number: a list of them takes five times that
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
    if n and (last > MAX_ID or max(runs) > MAX_ID or max(lengths) > MAX_DIRECTORY or min(offsets) < 0):
        raise ValueError('a value out of range')
    return ids, runs, lengths, offsets


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


def cover(x0, y0, x1, y1, z, most=4096):
    """The tiles x0..x1 by y0..y1 of zoom z as runs of tile ids: [(lo, hi, whole)] in id order, hi exclusive.
    A square of the tile grid aligned to its own size is one unbroken run of ids on the Hilbert curve, so the box is cut
    into such squares, the largest that fit, and neighbouring runs are joined. whole False: the run also holds tiles
    outside the box (the caller leaves those out). That only happens for a box so large that following its edge tile by
    tile would take more than about `most` squares; the squares along the edge then stop at a larger size instead."""
    acc = sum(4 ** i for i in range(z))
    small = 1
    while 2 * (x1 - x0 + y1 - y0 + 2) > most * small:
        small *= 2
    out, stack = [], [(0, 0, 1 << z)]
    while stack:
        bx, by, s = stack.pop()
        if bx > x1 or by > y1 or bx + s - 1 < x0 or by + s - 1 < y0:
            continue
        whole = x0 <= bx and bx + s - 1 <= x1 and y0 <= by and by + s - 1 <= y1
        if whole or s <= small:
            lo = acc + (zxy_to_id(z, bx, by) - acc) // (s * s) * (s * s)
            out.append((lo, lo + s * s, whole))
        else:
            h = s // 2
            stack += [(bx, by, h), (bx + h, by, h), (bx, by + h, h), (bx + h, by + h, h)]
    out.sort()
    joined = []
    for lo, hi, whole in out:
        if joined and joined[-1][1] == lo and joined[-1][2] == whole:
            joined[-1] = (joined[-1][0], hi, whole)
        else:
            joined.append((lo, hi, whole))
    return joined


def _tiles(a, b, box):
    """(z, x, y) of the tile ids a..b-1: all of them, or with a box (x0, y0, x1, y1) those inside it."""
    for t in range(a, b):
        z, x, y = id_to_zxy(t)
        if box is None or (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
            yield z, x, y


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
        self.clustered, self.compression, self.tile_type = head[96] == 1, head[97], head[99]
        if self.tile_type != 1:
            raise download.Failed('network', 'The map archive does not hold vector tiles.')
        if self.root_len > MAX_DIRECTORY:
            raise download.Failed('network', BAD_INDEX)
        whole = self.root_off + self.root_len <= len(head)
        self.root = self._read(head[self.root_off:self.root_off + self.root_len] if whole else self.get(self.root_off, self.root_len))
        self._leaves = OrderedDict()

    def get(self, start, length):
        return download.fetch_range(self.url, start, length, self.control, allowed=self.allowed, context=self.context)

    def _read(self, data):
        """One directory as sent, parsed. Whatever is wrong with it is one plain failure."""
        try:
            if self.compression == 2:
                unpack = zlib.decompressobj(31)
                data = unpack.decompress(data, MAX_DIRECTORY)
                if unpack.unconsumed_tail:
                    raise ValueError('a directory too large')
            return _directory(data)
        except (ValueError, IndexError, OverflowError, zlib.error):
            raise download.Failed('network', BAD_INDEX) from None

    def _leaf(self, offset, length):
        key = (offset, length)
        leaf = self._leaves.get(key)
        if leaf is None:
            if length > MAX_DIRECTORY:
                raise download.Failed('network', BAD_INDEX)
            leaf = self._leaves[key] = self._read(self.get(self.leaf_off + offset, length))
            while len(self._leaves) > LEAVES_KEPT:
                self._leaves.popitem(last=False)
        else:
            self._leaves.move_to_end(key)
        return leaf

    def entries(self, lo, hi, d=None, end=None, depth=0):
        """Tile entries (id, run, length, offset) with lo <= id < hi, in id order; only the leaf directories that overlap
        are fetched, and only a few are kept."""
        if depth > MAX_DEPTH:
            raise download.Failed('network', BAD_INDEX)
        ids, runs, lengths, offsets = self.root if d is None else d
        i, n = max(bisect.bisect_right(ids, lo) - 1, 0), len(ids)
        while i < n and ids[i] < hi:
            tid, run = ids[i], runs[i]
            if run == 0:
                nxt = ids[i + 1] if i + 1 < n else (end if end is not None else 1 << 62)
                if nxt > lo:
                    yield from self.entries(lo, hi, self._leaf(offsets[i], lengths[i]), nxt, depth + 1)
            elif tid + run > lo:
                yield tid, run, lengths[i], offsets[i]
            i += 1


def newest_build(allowed=None, context=None):
    raw = download.fetch_small(BUILDS, limit=4 << 20, allowed=allowed, context=context, timeout=30)
    try:
        return BUILD_BASE + json.loads(raw)[-1]['key']
    except (TypeError, ValueError, KeyError, IndexError):
        raise download.Failed('network', 'Lastbook could not reach the map server to find the newest map. Try again later.')


PIECE = 16 * 1024 * 1024        # most of the archive fetched in one request
GAP = 256 * 1024                # unwanted bytes between two wanted tiles that are fetched rather than asked around
BATCH = 20_000                  # most archive entries written from one piece
KEPT_BYTES = 32 * 1024 * 1024   # tiles used again far from where they are stored (open sea, empty land), kept for the next use


def _full(e):
    return download.disk_full(e) or (isinstance(e, sqlite3.Error) and 'full' in str(e).lower())


class Cut:
    """One map file cut out of the newest build: the world (bbox None) or a region. measure() finds the tiles and their
    size without fetching any; run() fetches them into `dest` (moved into place when complete).

    Neither holds the list of tiles: both walk the archive's directory in tile id order, run by run (cover), which is also
    the order the tiles are stored in, so a region of any size takes the same few tens of MB of memory. Its time and its
    size on disk still grow with the region."""

    def __init__(self, dest, bbox, minzoom, maxzoom, name, control, allowed=None, context=None):
        self.dest, self.bbox, self.minzoom, self.maxzoom, self.name = Path(dest), bbox, minzoom, maxzoom, name
        self.control, self.allowed, self.context = control, allowed, context
        self.ranges, self.total, self.tiles, self.build = None, 0, 0, None

    def _entries(self):
        """(first id, one past the last, box or None, length, offset) for each archive entry holding wanted tile ids, in
        id order. One entry is one stored tile, or one tile's bytes used for a run of ids (open sea, empty land)."""
        seen = 0
        for lo, hi, box in self.ranges:
            for tid, run, length, offset in self.archive.entries(lo, hi):
                seen += 1
                if not seen & 1023:
                    self.control.check()
                yield max(tid, lo), min(tid + run, hi), box, length, offset

    def measure(self):
        """The bytes run() will fetch (and sets .tiles, the number of tiles it will write)."""
        self.build = newest_build(self.allowed, self.context)
        self.archive = Archive(self.build, self.control, self.allowed, self.context)
        if not self.archive.clustered:      # tiles stored in id order: every Protomaps build is, and reading in one pass needs it
            raise download.Failed('network', 'The map archive is not in the order Lastbook reads. Try again later.')
        if self.bbox is None:
            self.ranges = [(0, sum(4 ** z for z in range(self.maxzoom + 1)), None)]
        else:
            self.ranges = []
            for z in range(self.minzoom, self.maxzoom + 1):
                box = tile_range(*self.bbox, z)
                self.ranges += [(lo, hi, None if whole else box) for lo, hi, whole in cover(*box, z)]
        # Bytes are counted once however many tiles use them: `starts`/`ends` are the stretches of the archive counted so
        # far (ids in order means offsets in order, so they only grow at the end), and `back` the tiles stored outside them.
        tiles = total = high = 0
        starts, ends, back = [], [], set()
        for a, b, box, length, offset in self._entries():
            n = b - a if box is None else sum(1 for _ in _tiles(a, b, box))
            if not n:
                continue
            tiles += n
            if offset >= high:
                if ends and ends[-1] == offset:
                    ends[-1] = offset + length
                else:
                    starts.append(offset)
                    ends.append(offset + length)
                total, high = total + length, offset + length
            else:
                i = bisect.bisect_right(starts, offset) - 1
                if (i < 0 or ends[i] < offset + length) and (offset, length) not in back:
                    total += length
                    if len(back) < 100_000:
                        back.add((offset, length))
        self.total, self.tiles = total, tiles
        return self.total

    def run(self, progress=lambda done, total: None):
        """Fetches the tiles into <dest>.part and moves it into place. There is no carrying on from a part file: whatever
        stops it (cancel, pause, the network, a full disk) removes the part file, and the next run starts the cut over."""
        if self.ranges is None:
            self.measure()
        part = self.dest.with_name(self.dest.name + '.part')
        sides = [part] + [part.with_name(part.name + s) for s in ('-journal', '-wal', '-shm')]
        for p in sides:
            p.unlink(missing_ok=True)
        try:
            db = sqlite3.connect(part)
            try:
                db.executescript('''CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
                    CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);''')
                self._fill(db, progress)
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
        except BaseException as e:
            for p in sides:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            if _full(e):
                raise download.Failed('no_space', 'The disk filled up while the map was being made. Free some space, then try again.') from None
            raise
        return self.dest

    def _fill(self, db, progress):
        insert = 'INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)'
        done = 0

        def fetch(start, length):
            nonlocal done
            data = self.archive.get(self.archive.data_off + start, length)
            done += length
            progress(min(done, self.total), self.total)
            return data

        def rows(a, b, box, blob):
            for i, (z, x, y) in enumerate(_tiles(a, b, box)):
                if not i & 4095:
                    self.control.check()
                yield z, x, (1 << z) - 1 - y, blob
        batch, start, end = [], 0, 0      # entries whose bytes lie in one piece of the archive, start..end
        kept, kept_bytes = OrderedDict(), 0

        def flush():
            if batch:
                data = fetch(start, end - start)
                for a, b, box, length, offset in batch:
                    db.executemany(insert, rows(a, b, box, data[offset - start:offset - start + length]))
                db.commit()
                batch.clear()
        for entry in self._entries():
            a, b, box, length, offset = entry
            if box is not None and next(_tiles(a, b, box), None) is None:
                continue
            if batch and offset >= end and offset - end <= GAP and offset + length - start <= PIECE and len(batch) < BATCH:
                batch.append(entry)
                end = offset + length
            elif not batch or offset >= end:
                flush()
                batch.append(entry)
                start, end = offset, offset + length
            elif offset >= start and offset + length <= end:      # the same bytes as a tile already in this piece
                batch.append(entry)
            else:       # stored earlier in the archive than this piece
                blob = kept.get((offset, length))
                if blob is None:
                    blob = kept[(offset, length)] = fetch(offset, length)
                    kept_bytes += length
                    while kept_bytes > KEPT_BYTES and len(kept) > 1:
                        kept_bytes -= len(kept.popitem(last=False)[1])
                else:
                    kept.move_to_end((offset, length))
                db.executemany(insert, rows(a, b, box, blob))
        flush()


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
    try:
        _write_places(part, countries, admin1, cities)
    except BaseException as e:      # nothing half made is left in the library
        for p in (part, Path(str(part) + '-journal')):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if _full(e):
            raise download.Failed('no_space', 'The disk filled up while the place names were being saved. Free some space, then try again.') from None
        raise
    os.replace(part, dest)
    return Path(dest)


def _write_places(part, countries, admin1, cities):
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
