"""The map made in setup (engine/setup/maps.py): PMTiles addressing, directory parsing, and keeping the person's map. And the
help points `lastbook place --nearby` reads back out of a street map (engine/map_pois.py)."""
import functools
import gzip
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import map_pois
from engine.setup import download, install, maps

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture  # noqa: E402


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MapsTests(unittest.TestCase):
    def test_tile_ids_round_trip(self):
        self.assertEqual(maps.zxy_to_id(0, 0, 0), 0)
        self.assertEqual([maps.zxy_to_id(1, x, y) for x, y in ((0, 0), (0, 1), (1, 1), (1, 0))], [1, 2, 3, 4])
        for z, x, y in ((3, 5, 2), (8, 200, 17), (15, 26354, 15600)):
            self.assertEqual(maps.id_to_zxy(maps.zxy_to_id(z, x, y)), (z, x, y))

    def test_directory(self):
        # Two entries: ids 5 and 7, runs 1 and 2, lengths 10 and 20, offsets 0 then "right after the previous" (0).
        raw = varint(2) + varint(5) + varint(2) + varint(1) + varint(2) + varint(10) + varint(20) + varint(1) + varint(0)
        self.assertEqual([list(a) for a in maps._directory(raw)], [[5, 7], [1, 2], [10, 20], [0, 10]])

    def test_a_directory_out_of_bounds_is_refused(self):
        for raw in (varint(maps.MAX_ENTRIES + 1), varint(3) + varint(1), varint(1) + varint(maps.MAX_ID + 1) + varint(1) + varint(1) + varint(1),
                    varint(1) + varint(1) + varint(1) + varint(maps.MAX_DIRECTORY + 1) + varint(1), b'\xff' * 12):
            with self.assertRaises((ValueError, IndexError, OverflowError)):
                maps._directory(raw)
        a = maps.Archive.__new__(maps.Archive)
        a.compression = 2
        with self.assertRaises(maps.download.Failed):
            a._read(b'not gzip')
        with mock.patch.object(maps, 'MAX_DIRECTORY', 1000), self.assertRaises(maps.download.Failed):
            a._read(gzip.compress(bytes(100_000)))      # unpacks to far more than it may

    def test_cover_is_the_box_exactly(self):
        """Against listing every tile: boxes inside one quarter of the grid, across the middle where the Hilbert curve's
        quarters meet, a single tile, a whole zoom, and a box so long that its edge is followed in larger squares."""
        z = 7
        for box, most in (((3, 5, 9, 11), 4096), ((60, 60, 70, 70), 4096), ((0, 63, 127, 64), 4096), ((17, 99, 17, 99), 4096),
                          ((0, 0, 127, 127), 4096), ((5, 30, 120, 90), 16), ((63, 0, 64, 127), 8)):
            x0, y0, x1, y1 = box
            want = {maps.zxy_to_id(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
            runs = maps.cover(*box, z, most=most)
            self.assertEqual(runs, sorted(runs))
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(runs, runs[1:])), box)       # no id twice
            got, exact = set(), set()
            for lo, hi, whole in runs:
                ids = set(range(lo, hi))
                (exact if whole else got).update(ids)
            self.assertLessEqual(exact, want, box)                                          # a whole run holds nothing else
            self.assertLessEqual(want, exact | got, box)                                    # and nothing wanted is missed
            self.assertEqual({t for t in exact | got if maps.id_to_zxy(t)[1:] in {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}}, want)
            if most == 4096:
                self.assertEqual(exact, want, box)
            else:
                self.assertLess(len(runs), 8 * most)

    def test_a_box_the_wrong_way_round_is_refused(self):
        with self.assertRaises(ValueError):
            maps.cover(10, 0, 3, 5, 4)          # west of east, as a box across the antimeridian: it once came back empty
        with self.assertRaises(ValueError):
            maps.cover(0, 6, 3, 5, 4)

    def test_cover_union_holds_each_tile_once(self):
        z = 6
        for boxes, most in ((((3, 5, 20, 30), (10, 10, 40, 40)), 4096), (((0, 0, 5, 63), (60, 0, 63, 63)), 4096),
                            (((2, 2, 50, 50), (2, 2, 50, 50)), 4096), (((1, 1, 60, 40), (30, 20, 62, 62)), 8)):
            want = {maps.zxy_to_id(z, x, y) for x0, y0, x1, y1 in boxes for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
            with mock.patch.object(maps, 'cover', functools.partial(maps.cover, most=most)):
                runs = maps.cover_union(boxes, z)
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(runs, runs[1:])), boxes)     # in order, no id twice
            got = [t for lo, hi, keep in runs for t in range(lo, hi)
                   if keep is None or any(b[0] <= maps.id_to_zxy(t)[1] <= b[2] and b[1] <= maps.id_to_zxy(t)[2] <= b[3] for b in keep)]
            self.assertEqual(sorted(got), sorted(want), boxes)

    def test_regions_across_the_antimeridian_are_two_boxes(self):
        """Each region's boxes stay off the other side of the world: none is the width of the world but Antarctica."""
        crossing = {r['id'] for r in maps.regions() if r['bbox'][0] > r['bbox'][2]}
        self.assertLessEqual({'us/alaska', 'us-pacific', 'far-eastern-fed-district', 'new-zealand', 'american-oceania', 'fiji', 'kiribati', 'tuvalu'}, crossing)
        for r in maps.regions():
            boxes = maps.region_boxes(r)
            self.assertTrue(all(b[0] <= b[2] and b[1] <= b[3] for b in boxes), r['id'])
            if r['id'] != 'antarctica':
                self.assertLess(sum(b[2] - b[0] for b in boxes), 300, r['id'])
            if r['id'] in crossing:
                self.assertEqual(len(boxes), 2, r['id'])
                self.assertEqual((boxes[0][0], boxes[1][2]), (r['bbox'][0], r['bbox'][2]))       # east side, then west side
        alaska = maps.region_boxes(maps.region('us/alaska'))
        self.assertEqual(sum(b[2] - b[0] for b in alaska) // 1, 58)        # 171.76..180 and -180..-129.8, not 360 degrees
        self.assertEqual(maps.region_boxes({'bbox': [170, -20, -175, -10]}), [[170, -20, 180.0, -10], [-180.0, -20, -175, -10]])

    def test_the_region_list_finds_the_short_way_round(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('make_region_list', Path(__file__).resolve().parents[2] / 'tools' / 'make_region_list.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        sq = lambda w, s, e, n: [[[w, s], [e, s], [e, n], [w, n], [w, s]]]
        fiji = {'type': 'MultiPolygon', 'coordinates': [sq(172.7, -25, 180, -9.7), sq(-180, -24.3, -176.2, -13.9)]}
        self.assertEqual(tool.boxes(fiji), [[172.7, -25, 180, -9.7], [-180, -24.3, -176.2, -13.9]])
        self.assertEqual(tool.bbox(fiji), [172.7, -25, -176.2, -9.7])
        oceania = {'type': 'MultiPolygon', 'coordinates': [sq(-174, -17, -165, -10), sq(141, 9, 169, 22), sq(-179.9, -3, -160, 6)]}
        self.assertEqual(tool.boxes(oceania), [[141, 9, 169, 22], [-179.9, -17, -160, 6]])
        europe = {'type': 'MultiPolygon', 'coordinates': [sq(-32, 29, 10, 81), sq(5, 40, 46, 70)]}
        self.assertEqual(tool.boxes(europe), [[-32, 29, 46, 81]])
        band = {'type': 'Polygon', 'coordinates': sq(-180, -90, 180, -60)}
        self.assertEqual(tool.bbox(band), [-180, -90, 180, -60])

    def test_tile_range_covers_bali(self):
        x0, y0, x1, y1 = maps.tile_range(114.4, -8.9, 115.7, -8.0, 9)
        self.assertLessEqual(x0, x1)
        self.assertLessEqual(y0, y1)
        self.assertEqual((x0, y0), (418, 267))

    def test_regions_are_bundled(self):
        ids = {r['id'] for r in maps.regions()}
        self.assertGreater(len(ids), 500)
        self.assertIn('monaco', ids)
        self.assertEqual(maps.region('us/california')['name'], 'California')

    def test_the_world_map_is_the_persons(self):
        with tempfile.TemporaryDirectory() as t:
            old, new = Path(t, 'old'), Path(t, 'new')
            for lib in (old, new):
                (lib / 'derived' / 'geo').mkdir(parents=True)
            for name in ('world.mbtiles', 'world-places.sqlite', 'regions-monaco.mbtiles'):
                (old / 'derived' / 'geo' / name).write_bytes(b'x')
            install.carry_over(old, new)
            self.assertEqual(sorted(p.name for p in (new / 'derived' / 'geo').iterdir()),
                             ['regions-monaco.mbtiles', 'world-places.sqlite', 'world.mbtiles'])


MAXZ = 6


def tile(z, x, y):
    """What the made-up map holds: open sea west of a line (one tile's bytes for many tiles, in runs and far apart), a
    little empty land, and a tile of its own everywhere else."""
    if x < (1 << z) * 3 // 8:
        return b'sea' * 9
    if (x + y) % 11 == 0:
        return b'empty land' * 3
    return f'{z}/{x}/{y}'.encode() * (1 + (x * 7 + y) % 5)


def directory(entries):
    """entries: [(id, run, length, offset)] as PMTiles serializes them."""
    out = bytearray(varint(len(entries)))
    last = 0
    for tid, *_ in entries:
        out += varint(tid - last)
        last = tid
    for key in (1, 2):
        for e in entries:
            out += varint(e[key])
    for i, e in enumerate(entries):
        out += varint(0 if i and e[3] == entries[i - 1][3] + entries[i - 1][2] else e[3] + 1)
    return gzip.compress(bytes(out))


def archive(leaf=37):
    """A clustered PMTiles v3 archive of zooms 0..MAXZ: header, root, leaf directories, tile data."""
    data, where, entries = bytearray(), {}, []
    for tid in range(sum(4 ** z for z in range(MAXZ + 1))):
        blob = tile(*maps.id_to_zxy(tid))
        if blob not in where:
            where[blob] = len(data)
            data += blob
        if entries and entries[-1][3] == where[blob] and entries[-1][0] + entries[-1][1] == tid:
            entries[-1][1] += 1         # the same bytes for the next id: a run
        else:
            entries.append([tid, 1, len(blob), where[blob]])
    leaves, root = bytearray(), []
    for i in range(0, len(entries), leaf):
        raw = directory(entries[i:i + leaf])
        root.append((entries[i][0], 0, len(raw), len(leaves)))
        leaves += raw
    root = directory(root)
    head = bytearray(127)
    head[:8] = b'PMTiles\x03'
    for at, v in ((8, 127), (16, len(root)), (40, 127 + len(root)), (48, len(leaves)), (56, 127 + len(root) + len(leaves)), (64, len(data))):
        head[at:at + 8] = v.to_bytes(8, 'little')
    head[96], head[97], head[98], head[99] = 1, 2, 1, 1
    return bytes(head) + root + bytes(leaves) + bytes(data), len(root)


class CutTests(unittest.TestCase):
    """A map cut from a made-up archive, against the tiles listed one by one."""

    @classmethod
    def setUpClass(cls):
        cls.bytes, cls.root_len = archive()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.asked = []

        def fetch(url, start, length, control, **kw):
            control.check()
            self.asked.append((start, length))
            return self.bytes[start:start + length]
        for name, value in (('fetch_range', fetch),):
            p = mock.patch.object(download, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(maps, 'newest_build', lambda *a: 'https://example.invalid/20260101.pmtiles')
        p.start()
        self.addCleanup(p.stop)

    def cut(self, bbox, minzoom=3, name='t'):
        return maps.Cut(Path(self.tmp.name) / f'{name}.mbtiles', bbox, minzoom if bbox else 0, MAXZ, 'Test', download.Control())

    def wanted(self, bbox, minzoom=3):
        out = {}
        for z in range(minzoom if bbox else 0, MAXZ + 1):
            x0, y0, x1, y1 = maps.tile_range(*bbox, z) if bbox else (0, 0, (1 << z) - 1, (1 << z) - 1)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    out[(z, x, (1 << z) - 1 - y)] = tile(z, x, y)
        return out

    def written(self, path):
        db = sqlite3.connect(path)
        try:
            return {(z, x, y): bytes(b) for z, x, y, b in db.execute('SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles')}, dict(db.execute('SELECT * FROM metadata'))
        finally:
            db.close()

    def test_boxes_of_every_kind_hold_exactly_their_tiles(self):
        boxes = {'small': (100.0, -20.0, 130.0, 10.0), 'across the middle': (-30.0, -25.0, 40.0, 30.0), 'sea': (-170.0, 40.0, -120.0, 70.0),
                 'one tile': (10.0, 10.0, 10.1, 10.1), 'the width of the world': (-180.0, -60.0, 180.0, 75.0), 'world': None}
        for name, bbox in boxes.items():
            c = self.cut(bbox, name=name.replace(' ', '-'))
            want = self.wanted(bbox)
            total = c.measure()
            self.assertEqual(c.tiles, len(want), name)
            self.assertEqual(total, sum(len(b) for b in set(want.values())), name)       # bytes used by many tiles count once
            seen = []
            c.run(lambda done, total: seen.append((done, total)))
            got, meta = self.written(c.dest)
            self.assertEqual(got, want, name)
            self.assertEqual(seen[-1], (total, total), name)
            self.assertEqual(seen, sorted(seen), name)
            self.assertEqual((meta['maxzoom'], meta['source']), (str(MAXZ), '20260101.pmtiles'))
            self.assertFalse(c.dest.with_name(c.dest.name + '.part').exists())
            self.assertLessEqual(len(c.archive._leaves), maps.LEAVES_KEPT)

    def test_a_box_too_long_to_follow_tile_by_tile_is_still_exact(self):
        bbox = (-100.0, -50.0, 150.0, 60.0)
        with mock.patch.object(maps, 'cover', functools.partial(maps.cover, most=8)):
            c = self.cut(bbox)
            c.measure()
            self.assertTrue(any(box for _, _, box in c.ranges))       # some runs hold tiles outside the box, to be left out
            c.run()
        want = self.wanted(bbox)
        self.assertEqual(self.written(c.dest)[0], want)
        self.assertEqual((c.tiles, c.total), (len(want), sum(len(b) for b in set(want.values()))))

    def test_a_region_of_several_boxes_holds_their_tiles_once(self):
        """Across the antimeridian (a box west > east, or its two boxes) and boxes that overlap: each tile written once,
        and nothing of the world between."""
        for name, bbox, parts in (('across', (150.0, 30.0, -160.0, 60.0), [(150.0, 30.0, 180.0, 60.0), (-180.0, 30.0, -160.0, 60.0)]),
                                  ('two', [(150.0, 30.0, 180.0, 60.0), (-180.0, 40.0, -160.0, 50.0)], None),
                                  ('overlapping', [(0.0, 0.0, 40.0, 40.0), (20.0, 20.0, 60.0, 60.0)], None)):
            c = self.cut(bbox, minzoom=0, name=name)
            want = {}
            for part in parts or bbox:
                want.update(self.wanted(part, minzoom=0))
            total = c.measure()
            self.assertEqual(c.tiles, len(want), name)
            self.assertEqual(total, sum(len(b) for b in set(want.values())), name)
            self.assertEqual(c.disk, sum(len(b) + maps.ROW_BYTES for b in want.values()), name)     # one row a tile, each its own copy
            c.run()
            self.assertEqual(self.written(c.dest)[0], want, name)

    def test_only_the_directories_and_bytes_needed_are_asked_for(self):
        c = self.cut((100.0, -20.0, 130.0, 10.0))
        c.measure()
        head = 127 + self.root_len
        leaves = [a for a in self.asked if a[0] >= head]
        self.assertLess(sum(n for _, n in leaves), (len(self.bytes) - head) // 10)
        self.assertTrue(all(n <= maps.MAX_DIRECTORY for _, n in self.asked))

    def test_a_stopped_or_failed_cut_leaves_no_part_file(self):
        c = self.cut((100.0, -20.0, 130.0, 10.0))
        c.measure()
        part = c.dest.with_name(c.dest.name + '.part')

        def stop_midway(done, total):
            self.assertTrue(part.exists())
            c.control.stop('cancelled')
        with self.assertRaises(download.Stopped):
            c.run(stop_midway)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        for error in (sqlite3.OperationalError('database or disk is full'), OSError(28, 'No space left on device')):
            c = self.cut((100.0, -20.0, 130.0, 10.0))
            with mock.patch.object(maps.Cut, '_fill', side_effect=error), self.assertRaises(download.Failed) as e:
                c.run()
            self.assertEqual(e.exception.code, 'no_space')
            self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_an_archive_not_in_id_order_is_refused(self):
        self.bytes = self.bytes[:96] + b'\x00' + self.bytes[97:]
        with self.assertRaises(download.Failed):
            self.cut(None).measure()



class HelpPointTests(unittest.TestCase):
    """A made-up town at 12.5 N, 34.25 E with a street map of its own (fixture.region_file)."""
    LAT, LON = 12.5, 34.25

    def region(self, t, points, **more):
        geo = Path(t, 'derived', 'geo')
        geo.mkdir(parents=True, exist_ok=True)
        fixture.region_file(geo / 'regions-test.mbtiles', points, **more)
        return geo

    def test_a_tile_gives_its_help_points_and_nothing_else(self):
        tile = fixture.map_tile({
            'buildings': [({'kind': 'hospital', 'name': 'Not a point of help'}, (5, 5))],
            'pois': [({'name': 'RS Test', 'name:en': 'Test Hospital', 'kind': 'hospital', 'min_zoom': 13}, (2048, 1024)),
                     ({'kind': 'pharmacy', 'min_zoom': 15}, (4095, 0)),                      # no name
                     ({'name': 'Warung', 'kind': 'restaurant'}, (100, 100)),
                     ({'name': 'Next door', 'kind': 'hospital'}, (-8, 300)),                 # the tile's margin: the neighbour's
                     ({'name': 'hospital', 'kind': 'school'}, (7, 7))]})
        want = [('hospital', 'RS Test', 0.5, 0.25), ('pharmacy', '', 4095 / 4096, 0.0)]
        self.assertEqual(map_pois.pois(tile, {'hospital', 'pharmacy'}), want)
        self.assertEqual(map_pois.pois(gzip.decompress(tile), {'hospital', 'pharmacy'}), want)   # not compressed: the same
        self.assertEqual(map_pois.pois(tile, {'castle'}), [])
        for damaged in (tile[:40], b'\x1f\x8b nothing', gzip.compress(b'\x1a\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff'), gzip.compress(b'\x1a\x7fpois')):
            with self.assertRaises((ValueError, IndexError, map_pois.zlib.error)):
                map_pois.pois(damaged, {'hospital'})
        with mock.patch.object(map_pois, 'MAX_TILE', 1000), self.assertRaises(ValueError):
            map_pois.pois(gzip.compress(bytes(100_000)), {'hospital'})                          # unpacks to far more than it may
        # A `pois` layer whose feature or value is a number where a message should be: refused as a tile that is not one.
        f, v = fixture._field, fixture._varint
        kind, hospital = f(3, b'kind'), f(4, f(1, b'hospital'))
        for layer in (f(1, b'pois') + v(2 << 3) + v(5) + kind + hospital, f(1, b'pois') + f(2, b'') + kind + v(4 << 3) + v(7)):
            with self.assertRaises(ValueError):
                map_pois.pois(gzip.compress(f(3, layer)), {'hospital'})

    def test_the_points_around_a_place(self):
        lat, lon = self.LAT, self.LON
        near = [('hospital', 'Town Hospital', lat + 0.01, lon), ('pharmacy', 'Corner Pharmacy', lat, lon - 0.015),
                ('hospital', 'Second Hospital', lat - 0.03, lon + 0.03), ('pharmacy', 'Far Pharmacy', lat + 0.5, lon),
                ('hospital', 'Regional Hospital', lat, lon + 0.5), ('toilets', None, lat + 0.001, lon), ('water_point', 'Standpipe', lat, lon + 0.001)]
        with tempfile.TemporaryDirectory() as t:
            geo = self.region(t, {15: near, 14: [near[4]]})
            with map_pois.Regions(geo, ['hospital', 'pharmacy', 'toilet', 'drinking_water']) as r:
                def names(cls, km, inner=0.0):
                    return [name for _, name, _, _ in r.box(lat, lon, cls, km, inner)]
                (cls, name, x, y), = r.box(lat, lon, 'hospital', 2.2)
                self.assertEqual((cls, name), ('hospital', 'Town Hospital'))
                self.assertAlmostEqual(x, lon, places=4)       # within a tile unit, about 0.3 m
                self.assertAlmostEqual(y, lat + 0.01, places=4)
                self.assertEqual(names('pharmacy', 2.2), ['Corner Pharmacy'])
                self.assertEqual(names('hospital', 4.4, 2.2), ['Second Hospital'])         # the ring only: not Town Hospital again
                self.assertEqual(sorted(names('hospital', 4.4)), ['Second Hospital', 'Town Hospital'])
                # Farther than FULL_KM the map is read a zoom coarser, where it shows the hospital and not the pharmacy.
                self.assertEqual(names('hospital', 70, 35), ['Regional Hospital'])
                self.assertEqual(names('pharmacy', 70, 35), [])
                self.assertEqual(names('hospital', 70), ['Regional Hospital'])
                # What --nearby calls a kind and what the map does.
                self.assertEqual([p[:2] for p in r.box(lat, lon, 'toilet', 2.2)], [('toilet', '')])
                self.assertEqual(names('drinking_water', 2.2), ['Standpipe'])
                self.assertEqual(r.box(lat, lon, 'police', 2.2), [])                       # not asked for when it was opened
                self.assertTrue(r.covers(lat, lon))
                self.assertFalse(r.covers(lat + 1, lon))
                self.assertFalse(r.covers(-lat, lon))

    def test_a_damaged_tile_or_file_gives_nothing(self):
        lat, lon = self.LAT, self.LON
        with tempfile.TemporaryDirectory() as t:
            x, y = map_pois._tile_xy(lat, lon, 15)
            geo = self.region(t, {15: [('hospital', 'Next Tile Hospital', lat + 0.012, lon)]}, tiles={(15, x, y): b'\x1f\x8b not a tile'})
            (geo / 'regions-broken.mbtiles').write_bytes(b'not SQLite')
            (geo / 'world.mbtiles').write_bytes(b'not read for help points')
            with map_pois.Regions(geo, ['hospital']) as r:
                self.assertEqual(len(r.files), 1)
                self.assertTrue(r.covers(lat, lon))
                self.assertEqual([p[1] for p in r.box(lat, lon, 'hospital', 2.2)], ['Next Tile Hospital'])
            with map_pois.Regions(Path(t, 'no-such-folder'), ['hospital']) as r:
                self.assertEqual((r.covers(lat, lon), r.box(lat, lon, 'hospital', 2.2)), (False, []))

    def test_a_tile_of_the_wrong_shape_is_left_out(self):
        lat, lon = self.LAT, self.LON
        f, v = fixture._field, fixture._varint
        bad = gzip.compress(f(3, f(1, b'pois') + v(2 << 3) + v(5) + f(3, b'kind') + f(4, f(1, b'hospital'))))
        with tempfile.TemporaryDirectory() as t:
            x, y = map_pois._tile_xy(lat, lon, 15)
            geo = self.region(t, {15: [('hospital', 'Next Tile Hospital', lat + 0.012, lon)]}, tiles={(15, x, y): bad})
            with map_pois.Regions(geo, ['hospital']) as r:
                self.assertEqual([p[1] for p in r.box(lat, lon, 'hospital', 2.2)], ['Next Tile Hospital'])

    def test_across_the_antimeridian(self):
        """Taveuni, Fiji, is cut by the 180th meridian: a hospital 1 km east of a place at 179.995 E is at -179.996."""
        lat = -16.85
        with tempfile.TemporaryDirectory() as t:
            geo = self.region(t, {15: [('hospital', 'Across', lat, -179.996), ('hospital', 'Same side', lat, 179.72)]})
            with map_pois.Regions(geo, ['hospital']) as r:
                self.assertEqual([p[1] for p in r.box(lat, 179.995, 'hospital', 2.2)], ['Across'])
                self.assertEqual(sorted(p[1] for p in r.box(lat, 179.995, 'hospital', 35)), ['Across', 'Same side'])
                self.assertEqual([p[1] for p in r.box(lat, -179.99, 'hospital', 35, 2.2)], ['Same side'])   # and from the other side

    def test_a_box_is_square_on_the_ground(self):
        """At 60 N a degree of longitude is half as long: a point 20 km east is 0.36 degrees away, in a 35 km box."""
        lat, lon = 60.0, 25.0
        with tempfile.TemporaryDirectory() as t:
            geo = self.region(t, {15: [('pharmacy', 'East', lat, lon + 0.36), ('pharmacy', 'Too far east', lat, lon + 0.7)]})
            with map_pois.Regions(geo, ['pharmacy']) as r:
                self.assertEqual([p[1] for p in r.box(lat, lon, 'pharmacy', 35)], ['East'])

    def test_the_most_detailed_file_with_a_tile_is_read_once(self):
        lat, lon = self.LAT, self.LON
        with tempfile.TemporaryDirectory() as t:
            geo = self.region(t, {15: [('police', 'Town Police', lat, lon)]})
            fixture.region_file(geo / 'regions-test-again.mbtiles', {15: [('police', 'Town Police', lat, lon), ('police', 'Harbour Police', lat + 0.03, lon)]})
            with map_pois.Regions(geo, ['police']) as r:   # two overlapping regions: a point is not given twice
                self.assertEqual(sorted(p[1] for p in r.box(lat, lon, 'police', 4.4)), ['Harbour Police', 'Town Police'])


if __name__ == '__main__':
    unittest.main()
