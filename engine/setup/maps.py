"""The offline map, made on the person's computer during setup: nothing about it is hosted by Lastbook.

  - The world map (to city level, zoom 0-8, about 0.6 GB) and any street-level region (zoom 9-15) are cut out of the newest
    Protomaps daily build of OpenStreetMap (https://docs.protomaps.com/basemaps/downloads), a PMTiles archive read over
    HTTPS range requests through download.py, the one way out: only the tiles wanted are fetched. They are written as
    MBTiles (SQLite) files the window draws: derived/geo/world.mbtiles and derived/geo/regions-<id>.mbtiles.
  - Place search for the whole world is built from GeoNames (CC BY 4.0): every country, first-level region, and place of
    500 people or more (villages too, and the seats of local governments), the larger ones with alternate names:
    derived/geo/world-places.sqlite.
  - The regions offered are Geofabrik's (countries, and parts of large countries), listed with their bounding boxes in
    map-regions.json, bundled with the app (packaging/arch/map-regions.json; tools/make_region_list.py makes it).

Files are built beside their final name (<name>.part) and moved into place when complete, so the map never reads a half
made one. A cut that is paused or loses the network keeps its part file and carries on from it when the same cut of the
same daily build is run again; anything else that stops it removes the part file, and whatever a killed process or a
dropped job left behind is removed when the next map file is made here (sweep). Integrity comes from HTTPS to the
allow-listed hosts with the pinned roots (there is no published checksum for a cut of a daily build); every tile
directory and the gazetteer are parsed before anything is kept, and an archive whose tiles are packed in a way the map
cannot read is refused before any are fetched (the tiles themselves are copied as they are).
"""
import bisect
import csv
from array import array
import io
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
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
    """[{id, name, parent, bbox: [w, s, e, n], offered}] as bundled; [] if the list is missing. A region across the
    antimeridian has w > e and also `bboxes`, its real box each side of ±180 (region_boxes). `offered`: whether setup
    offers its streets (_offered)."""
    global _regions
    if _regions is None:
        try:
            _regions = json.loads(regions_file().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            _regions = []
        parents = {r['parent'] for r in _regions if r.get('parent')}
        for r in _regions:
            r['offered'] = _offered(r, parents)
    return _regions


def street_tiles(r):
    """About how many street-level (zoom 15) map tiles a region's boxes hold: its size on the map, before it is cut."""
    row = lambda lat: (1 - math.asinh(math.tan(math.radians(max(-85.05, min(85.05, lat))))) / math.pi) / 2
    return sum((e - w) / 360 * (row(s) - row(n)) * 4 ** 15 for w, s, e, n in region_boxes(r))


def _offered(r, parents):
    """Offered are countries and their parts. Continents, and countries so large that street level runs to tens of GB
    (Russia, the US, Canada, Australia), are left out: their parts are offered instead."""
    has_parts = r['id'] in parents or any(o['id'].startswith(r['id'] + '/') for o in _regions)
    return bool(r.get('parent')) and not (has_parts and street_tiles(r) > 40e6)


def _words(t):
    """A name as compared: lower case, without accents or punctuation, a space each side."""
    t = ''.join(c for c in unicodedata.normalize('NFKD', t.lower()) if not unicodedata.combining(c))
    return ' ' + ' '.join(re.sub(r'\W+', ' ', t).split()) + ' '


def _country(r):
    """The country a region is part of: the region under a continent that holds it (a US state is its own, under `us`)."""
    while (up := region(r['parent'])) and up.get('parent'):
        r = up
    return r['id'].split('/')[0]


def _depth(r, lat, lon):
    """How far from its middle the point lies in the region's box: 0 at the middle, 1 at the edge (over 1 outside)."""
    return min(max(abs(lon - (w + e) / 2) / max((e - w) / 2, 1e-9), abs(lat - (s + n) / 2) / max((n - s) / 2, 1e-9))
               for w, s, e, n in region_boxes(r))


def covering(lat, lon, address=''):
    """The offered regions whose streets would put the point on the map, smallest first. Only boxes are known, and
    neighbours' boxes overlap, so a region `address` (the nearest town's "region, country") does not name is left out
    where the point lies near its box's edge, or in another country than the one named: Paris is in Picardie's box too."""
    inside = [r for r in regions() if r['offered'] and _depth(r, lat, lon) <= 1]
    named = {r['id'] for r in inside if _words(r['name'].split(' (')[0]) in _words(address)}
    countries = {_country(r) for r in inside if r['id'] in named}
    keep = [r for r in inside if r['id'] in named or (_depth(r, lat, lon) <= 0.85 and (not countries or _country(r) in countries))]
    return sorted(keep, key=street_tiles)


def region(rid):
    return next((r for r in regions() if r['id'] == rid), None)


def region_boxes(r):
    """The boxes [w, s, e, n] a region's map is cut from: its `bboxes`, else its `bbox`, split at ±180 when w > e."""
    if r.get('bboxes'):
        return [list(b) for b in r['bboxes']]
    w, s, e, n = r['bbox']
    return [[w, s, e, n]] if w <= e else [[w, s, 180.0, n], [-180.0, s, e, n]]


def file_id(rid):
    """A Geofabrik id (us/california) as a file name part (us-california)."""
    return rid.replace('/', '-')


def _part_files(part):
    """A part file and what SQLite may keep beside it."""
    return [part] + [part.with_name(part.name + s) for s in ('-journal', '-wal', '-shm')]


def sweep(geo, keep=None):
    """Removes the half-made map files in a library's derived/geo: <our file>.part and SQLite's files beside it, but
    not those of `keep` (a map file's final path). One setup job runs at a time and makes one map file at a time, so
    any other part file was left by a job that was killed, failed or dropped. Returns the bytes freed."""
    freed = 0
    geo = Path(geo)
    for p in sorted(geo.glob('*.part*')) if geo.is_dir() else []:
        name, _, tail = p.name.partition('.part')
        if not ours(name) or tail not in ('', '-journal', '-wal', '-shm') or (keep is not None and name == Path(keep).name):
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
        except OSError:
            pass
    return freed


def places_current(path):
    """Whether the gazetteer is there and made the way this version makes it (PLACES_VERSION, kept as SQLite's
    user_version). An older one (towns of 15,000+ only) still works, but counts as missing, so setup offers place search
    again. The file's header is read rather than opened as a database, which would take a lock."""
    try:
        with open(path, 'rb') as f:
            head = f.read(64)
    except OSError:
        return False
    return head[:16] == b'SQLite format 3\0' and int.from_bytes(head[60:64], 'big') >= PLACES_VERSION


def installed(geo):
    """What map files this library has: {world: bool, places: bool (and current: places_current), regions: [{id, name, bytes}]}."""
    geo = Path(geo)
    out = {'world': (geo / WORLD_FILE).is_file(), 'places': places_current(geo / PLACES_FILE), 'regions': []}
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
    tile would take more than about `most` squares; the squares along the edge then stop at a larger size instead.
    ValueError for a box the wrong way round (a west east of its east, as across the antimeridian: split it first)."""
    if x0 > x1 or y0 > y1:
        raise ValueError(f'tile box the wrong way round: {(x0, y0, x1, y1)}')
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


def cover_union(boxes, z):
    """The tiles of several boxes (x0, y0, x1, y1) of zoom z as runs of tile ids, each id once however many boxes hold
    it: [(lo, hi, boxes)] in id order, hi exclusive, boxes None when every id of the run is wanted, else the boxes whose
    tiles in the run are (the caller leaves the rest out)."""
    boxes = list(dict.fromkeys(tuple(b) for b in boxes))
    runs = sorted((lo, hi, whole, box) for box in boxes for lo, hi, whole in cover(*box, z))
    if len(boxes) == 1:
        return [(lo, hi, None if whole else (box,)) for lo, hi, whole, box in runs]
    # One box's runs never overlap, so at any id at most len(boxes) runs are open: a sweep over where runs start and end.
    cuts = sorted({p for lo, hi, _, _ in runs for p in (lo, hi)})
    out, open_, i = [], [], 0
    for a, b in zip(cuts, cuts[1:]):
        open_ = [r for r in open_ if r[1] > a]
        while i < len(runs) and runs[i][0] <= a:
            open_.append(runs[i])
            i += 1
        if not open_:
            continue
        keep = None if any(r[2] for r in open_) else tuple(sorted(r[3] for r in open_))
        if out and out[-1][1] == a and out[-1][2] == keep:
            out[-1] = (out[-1][0], b, keep)
        else:
            out.append((a, b, keep))
    return out


def _tiles(a, b, boxes):
    """(z, x, y) of the tile ids a..b-1: all of them, or with boxes ((x0, y0, x1, y1), ...) those inside any of them."""
    for t in range(a, b):
        z, x, y = id_to_zxy(t)
        if boxes is None or any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for box in boxes):
            yield z, x, y


class Archive:
    """A remote PMTiles v3 archive (https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)."""

    def __init__(self, url, control, allowed=None, context=None):
        self.url, self.control, self.allowed, self.context = url, control, allowed, context
        self.watch = download.Watch()       # one for the whole archive: a map that is getting nowhere ends, part by part or not
        head = self.get(0, 16384)
        if head[:7] != b'PMTiles' or head[7] != 3:
            raise download.Failed('network', 'The map server sent something that is not a map archive.')
        u64 = lambda o: int.from_bytes(head[o:o + 8], 'little')
        self.root_off, self.root_len = u64(8), u64(16)
        self.leaf_off, self.data_off = u64(40), u64(56)
        self.clustered, self.compression, self.tile_type = head[96] == 1, head[97], head[99]
        if self.tile_type != 1:
            raise download.Failed('network', 'The map server sent a map Lastbook cannot use. Try again later.')
        if head[98] not in (1, 2):      # tiles as they are, or gzip: all the map and --nearby read (brotli and zstd they do not)
            raise download.Failed('network', 'The map on the server is packed in a way this version of Lastbook cannot read. '
                                             'Update Lastbook, then try again.')
        if self.root_len > MAX_DIRECTORY:
            raise download.Failed('network', BAD_INDEX)
        whole = self.root_off + self.root_len <= len(head)
        self.root = self._read(head[self.root_off:self.root_off + self.root_len] if whole else self.get(self.root_off, self.root_len))
        self._leaves = OrderedDict()

    def get(self, start, length, got=None):
        return download.fetch_range(self.url, start, length, self.control, allowed=self.allowed, context=self.context,
                                    watch=self.watch, got=got)

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


def newest_build(allowed=None, context=None, control=None):
    raw = download.fetch_small(BUILDS, limit=4 << 20, allowed=allowed, context=context, timeout=30, control=control)
    try:
        return BUILD_BASE + json.loads(raw)[-1]['key']
    except (TypeError, ValueError, KeyError, IndexError):
        raise download.Failed('network', 'Lastbook could not reach the map server to find the newest map. Try again later.')


PIECE = 16 * 1024 * 1024        # most of the archive fetched in one request
GAP = 256 * 1024                # unwanted bytes between two wanted tiles that are fetched rather than asked around
BATCH = 20_000                  # most archive entries written from one piece
KEPT_BYTES = 32 * 1024 * 1024   # tiles used again far from where they are stored (open sea, empty land), kept for the next use
# An MBTiles row costs about this much beside its tile's bytes (row header, key, index entry: measured 34-37 bytes for
# small tiles). Partly filled SQLite pages add more for tiles of 1-3 KB (measured up to 1.4 times their bytes), so the
# space check also leaves DISK_SLACK.
ROW_BYTES = 40
DISK_SLACK = 1.4
MARK = 'lastbook-cut'           # the metadata row of a part file: which cut it is and how far it has got


def _full(e):
    return download.disk_full(e) or (isinstance(e, sqlite3.Error) and 'full' in str(e).lower())


class Cut:
    """One map file cut out of the newest build: the world (bbox None) or a region. measure() finds the tiles and their
    size without fetching any; run() fetches them into `dest` (moved into place when complete).

    Neither holds the list of tiles: both walk the archive's directory in tile id order, run by run (cover), which is also
    the order the tiles are stored in, so a region of any size takes the same few tens of MB of memory. Its time and its
    size on disk still grow with the region."""

    def __init__(self, dest, bbox, minzoom, maxzoom, name, control, allowed=None, context=None):
        """bbox: None for the world, a box [w, s, e, n] (w > e across the antimeridian), or a list of such boxes, cut as
        their union."""
        self.dest, self.bbox, self.minzoom, self.maxzoom, self.name = Path(dest), bbox, minzoom, maxzoom, name
        self.boxes = None
        if bbox is not None:
            self.boxes = [b for box in (bbox if isinstance(bbox[0], (list, tuple)) else [bbox]) for b in region_boxes({'bbox': box})]
        self.control, self.allowed, self.context = control, allowed, context
        self.ranges, self.total, self.tiles, self.disk, self.build = None, 0, 0, 0, None

    def _entries(self, since=0):
        """(first id, one past the last, box or None, length, offset) for each archive entry holding wanted tile ids, in
        id order. One entry is one stored tile, or one tile's bytes used for a run of ids (open sea, empty land).
        since: only the ids from that one on (where an earlier run of this cut got to)."""
        seen = 0
        for lo, hi, box in self.ranges:
            if hi <= since:
                continue
            lo = max(lo, since)
            for tid, run, length, offset in self.archive.entries(lo, hi):
                seen += 1
                if not seen & 1023:
                    self.control.check()
                yield max(tid, lo), min(tid + run, hi), box, length, offset

    def measure(self):
        """The bytes run() will fetch (and sets .tiles, the number of tiles it will write, and .disk, about the bytes the
        file takes: one row a tile, each with its own copy of the bytes, however many tiles share them in the archive)."""
        self.build = newest_build(self.allowed, self.context, self.control)
        self.archive = Archive(self.build, self.control, self.allowed, self.context)
        if not self.archive.clustered:      # tiles stored in id order: every Protomaps build is, and reading in one pass needs it
            raise download.Failed('network', 'The map server sent a map in a form Lastbook cannot read. Try again later.')
        if self.bbox is None:
            self.ranges = [(0, sum(4 ** z for z in range(self.maxzoom + 1)), None)]
        else:
            self.ranges = []
            for z in range(self.minzoom, self.maxzoom + 1):
                self.ranges += cover_union([tile_range(*b, z) for b in self.boxes], z)
        # Bytes are counted once however many tiles use them: `starts`/`ends` are the stretches of the archive counted so
        # far (ids in order means offsets in order, so they only grow at the end), and `back` the tiles stored outside them.
        tiles = total = high = disk = 0
        starts, ends, back = [], [], set()
        for a, b, box, length, offset in self._entries():
            n = b - a if box is None else sum(1 for _ in _tiles(a, b, box))
            if not n:
                continue
            tiles += n
            disk += n * (length + ROW_BYTES)
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
        self.total, self.tiles, self.disk = total, tiles, disk
        return self.total

    def _identity(self):
        """What makes two cuts the same: a part file of another build, region or zoom range is not carried on from."""
        return json.dumps([1, self.build, self.minzoom, self.maxzoom, self.boxes])

    def _kept(self, part):
        """{'next': the first tile id not yet written, 'done': bytes fetched} from a part file of this same cut, else None."""
        if not part.is_file():
            return None
        try:
            db = sqlite3.connect(part)      # a transaction cut short by a kill or a power cut is undone here (its journal)
            try:
                row = db.execute('SELECT value FROM metadata WHERE name = ?', (MARK,)).fetchone()
            finally:
                db.close()
            state = json.loads(row[0])
            if state['cut'] != self._identity():
                return None
            return {'next': int(state['next']), 'done': int(state['done'])}
        except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
            return None

    def kept_bytes(self):
        """The size on disk of a part file run() will carry on from (0: none), for whoever checks the free space first.
        After measure()."""
        part = self.dest.with_name(self.dest.name + '.part')
        try:
            return part.stat().st_size if self._kept(part) else 0
        except OSError:
            return 0

    def _mark(self, db, nxt, done):
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (MARK, json.dumps({'cut': self._identity(), 'next': nxt, 'done': done})))

    def run(self, progress=lambda done, total: None):
        """Fetches the tiles into <dest>.part and moves it into place. A pause or a network failure leaves the part file,
        marked with how far it got (in the same transaction as the tiles, so the mark is never ahead of them), and the
        next run of the same cut of the same build carries on from there. Anything else that stops it (cancel, a full
        disk, an error) removes the part file, and so does a run that finds a part file of another build or cut."""
        if self.ranges is None:
            self.measure()
        part = self.dest.with_name(self.dest.name + '.part')
        sides = _part_files(part)
        sweep(self.dest.parent, keep=self.dest)
        state = self._kept(part)
        if state is None:
            for p in sides:
                p.unlink(missing_ok=True)
        else:
            print(f'setup: carrying on with {self.dest.name} from {state["done"]} of {self.total} bytes', flush=True)
        try:
            db = sqlite3.connect(part)
            try:
                if state is None:
                    db.executescript('''CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
                        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
                        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);''')
                    state = {'next': 0, 'done': 0}
                    self._mark(db, 0, 0)
                    db.commit()
                self._fill(db, progress, state)
                meta = {'name': self.name, 'format': 'pbf', 'type': 'baselayer', 'minzoom': str(self.minzoom if self.bbox else 0),
                        'maxzoom': str(self.maxzoom), 'attribution': '© OpenStreetMap contributors (ODbL), Protomaps',
                        'source': self.build.rsplit('/', 1)[-1], 'made': time.strftime('%Y-%m-%d')}
                if self.boxes:
                    meta['bounds'] = ','.join(f'{v:.4f}' for v in (min(b[0] for b in self.boxes), min(b[1] for b in self.boxes),
                                                                    max(b[2] for b in self.boxes), max(b[3] for b in self.boxes)))
                db.execute('DELETE FROM metadata WHERE name = ?', (MARK,))
                db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', meta.items())
                db.commit()
            finally:
                db.close()      # what was not committed is undone: the part file is as its mark says
            os.replace(part, self.dest)
        except BaseException as e:
            carry_on = (isinstance(e, download.Stopped) and e.why == 'paused') or (isinstance(e, download.Failed) and e.code == 'network')
            if not carry_on:
                for p in sides:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
            if _full(e):
                raise download.Failed('no_space', 'The disk filled up while the map was being made. Free some space, then try again.') from None
            raise
        return self.dest

    def _fill(self, db, progress, state=None):
        insert = 'INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)'
        since, done = (state['next'], state['done']) if state else (0, 0)
        last = since            # one past the last tile id dealt with: written, or waiting in `batch`
        if done:
            progress(min(done, self.total), self.total)

        def fetch(start, length):
            nonlocal done
            told = 0

            def got(n):         # as the bytes arrive, so a large piece on a slow line still shows it is moving
                nonlocal done, told
                told, done = told + n, done + n
                progress(min(done, self.total), self.total)
            data = self.archive.get(self.archive.data_off + start, length, got)
            if told != length:
                done += length - told
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
                batch.clear()
            self._mark(db, last, done)      # every tile id below `last` is in the file now: where another run carries on
            db.commit()
        for entry in self._entries(since):
            a, b, box, length, offset = entry
            if box is not None and next(_tiles(a, b, box), None) is None:
                last = b
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
            last = b
        flush()


# ------------------------------------------------------------------------------------------------------ gazetteer --
PLACES_ZIP, PLACES_TXT = 'cities500.zip', 'cities500.txt'
MAX_PLACES_ZIP = 64 * 1024 * 1024     # cities500.zip as sent (it is about 14 MB)
MAX_PLACES = 256 * 1024 * 1024        # cities500.txt unpacked (it is about 42 MB): what the zip may unpack to
MIN_PLACES = 100_000                  # rows a whole cities500.txt has at least (it has about 237,000)
PLACES_VERSION = 2                    # 2: places of 500+. Files made from cities15000 before it have 0.
ALTERNATES_FROM = 15_000              # the people a place needs for its alternate names to be searched (see _write_places)
GONE = ('PPLH', 'PPLQ', 'PPLW')       # historical, abandoned and destroyed places: no place to look for help around
BAD_PLACES = 'The place names downloaded could not be read. Try again later.'


def _unzipped(data, name, most):
    """One member of a zip held in memory, unpacked to at most `most` bytes: whatever its header says of its size."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo(name)
        if info.file_size > most:
            raise ValueError('too large')
        with z.open(info) as f:
            out = f.read(most + 1)
    if len(out) > most:
        raise ValueError('too large')
    return out


def _city(r):
    """(name, ascii name, alternate names, lat, lon, feature code, country, admin1, population) of one row of
    cities500.txt, or None for a row that is not one (cut short, a number that is not one, a place off the globe).
    geonameid name asciiname alternatenames lat lon fclass fcode cc cc2 admin1 admin2 admin3 admin4 population ..."""
    try:
        name, lat, lon, pop = r[1], float(r[4]), float(r[5]), int(r[14] or 0)
        if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return name, r[2], r[3], lat, lon, r[7], r[8], r[10], pop
    except (IndexError, ValueError):
        return None


def _rank(pop, fcode):
    """Where a place comes among others its name matches as well (server.py sorts by it): capitals and cities over a
    million 1, over 100,000 2, over 15,000 3, over 1,000 4, the rest 5. Countries are 0 and regions 1."""
    if pop >= 1_000_000 or fcode == 'PPLC':
        return 1
    return next(r for r, least in ((2, 100_000), (3, 15_000), (4, 1_000), (5, 0)) if pop >= least)


def build_places(dest, control, allowed=None, context=None):
    """The world gazetteer from GeoNames, same tables as places.sqlite (place, place_fts), so the engine searches it the
    same way; rank as _rank gives it.
    A row that cannot be read is left out; too few left, or files that are not what they should be, is one plain failure."""
    csv.field_size_limit(10_000_000)

    def get(name, limit):
        data = download.fetch_small(GEONAMES + name, limit=limit, allowed=allowed, context=context, timeout=60, control=control)
        if data is None:
            raise download.Failed('network', 'Lastbook could not download the place names. Try again later.')
        return data

    def rows(data):
        # Decoded as read: cities500.txt as one string would take four bytes a character for its names in other scripts.
        text = io.TextIOWrapper(io.BytesIO(data), encoding='utf-8-sig', newline='')
        return (r for r in csv.reader(text, delimiter='\t', quoting=csv.QUOTE_NONE) if r and not r[0].startswith('#'))
    control.check()
    try:
        countries = {r[0]: r[4] for r in rows(get('countryInfo.txt', 1 << 20)) if len(r) > 4 and r[0] and r[4]}
        admin1 = {r[0]: r[1] for r in rows(get('admin1CodesASCII.txt', 4 << 20)) if len(r) > 1 and r[0] and r[1]}
        control.check()
        listed, cities = 0, []
        for r in rows(_unzipped(get(PLACES_ZIP, MAX_PLACES_ZIP), PLACES_TXT, MAX_PLACES)):     # one row at a time: all of them as lists would take several hundred MB
            listed += 1
            c = _city(r)
            if c:
                cities.append(c)
    except (zipfile.BadZipFile, zlib.error, csv.Error, KeyError, ValueError, EOFError, NotImplementedError, RuntimeError) as e:
        # not a zip, no such member, a size past the limit, not UTF-8, a line csv cannot take, a zip made in a way zipfile cannot read
        print(f'setup: the place names could not be read: {type(e).__name__}: {e}', flush=True)
        raise download.Failed('network', BAD_PLACES) from None
    if len(cities) < listed:
        print(f'setup: {listed - len(cities)} of {listed} place name rows could not be read and were left out', flush=True)
    if len(cities) < MIN_PLACES or len(countries) < 200:
        raise download.Failed('network', 'The place names did not download completely. Try again later.')
    control.check()
    sweep(Path(dest).parent, keep=dest)
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
    db.executescript(f'''PRAGMA user_version = {PLACES_VERSION};
        CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');''')
    src = 'GeoNames (CC BY 4.0)'
    capital, largest = {}, {}

    def add(name, kind, detail, lat, lon, rank, search_detail=None):
        cur = db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)', (name, kind, detail, lat, lon, rank, src))
        db.execute('INSERT INTO place_fts(rowid,name,detail) VALUES (?,?,?)', (cur.lastrowid, name, search_detail or detail))
    # Largest first, so among places of one rank the id says which is larger (server.py sorts by it last).
    for name, ascii_, alts, lat, lon, fcode, cc, a1, pop in sorted(cities, key=lambda c: -c[8]):        # as _city gives them
        if fcode in GONE:
            continue
        detail = ', '.join(x for x in (admin1.get(f'{cc}.{a1}', ''), countries.get(cc, cc)) if x)
        # A smaller place's other names are mostly the same name in other scripts, and each more name is another way
        # for a query to land on the wrong village as an "alternate name": only larger places keep them, up to 30 (the
        # Latin ones come first, so old names such as Madras for Chennai are among them).
        others = [a for a in dict.fromkeys(alts.split(',')) if a and a not in (name, ascii_) and len(a) < 40][:30] if pop >= ALTERNATES_FROM else []
        kind = 'city' if pop >= 100_000 or fcode in ('PPLC', 'PPLA') else 'town'
        add(name, kind, detail, lat, lon, _rank(pop, fcode), detail + ' · ' + ' '.join([ascii_] + others))
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
    db.execute('VACUUM')        # the merge leaves the index's earlier pieces as free pages: about a tenth of the file
    db.close()
