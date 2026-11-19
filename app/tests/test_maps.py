"""The map made in setup (engine/setup/maps.py): PMTiles addressing, directory parsing, and keeping the person's map."""
import functools
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.setup import download, install, maps


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
            for name in ('world.mbtiles', 'world-places.sqlite', 'regions-monaco.mbtiles', 'CA.sqlite'):
                (old / 'derived' / 'geo' / name).write_bytes(b'x')
            (old / 'map-set.json').write_text(json.dumps({'name': 'CA', 'files': {'derived/geo/CA.sqlite': 1}}))
            self.assertEqual(sorted(install.map_files(old)), ['derived/geo/CA.sqlite', 'map-set.json'])   # a map swap leaves ours alone
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


if __name__ == '__main__':
    unittest.main()
