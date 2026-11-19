"""The map made in setup (engine/setup/maps.py): PMTiles addressing, directory parsing, and keeping the person's map."""
import json
import tempfile
import unittest
from pathlib import Path

from engine.setup import install, maps


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
        self.assertEqual(maps._directory(raw), [(5, 1, 10, 0), (7, 2, 20, 10)])

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


if __name__ == '__main__':
    unittest.main()
