#!/usr/bin/env python3
"""The Map screen's nearest help (/api/near in server.py) as the window asks for it, through the bridge: the points around
a place a street map covers, nothing (and covered false) where none does, with the regions whose streets would cover it,
the distance to one point, and refusals. And
the order place search lists places in, when many share a name.

  PYTHONPATH=<deps> python3 -B app/tests/test_map_near.py [-v]

Uses the made-up library of the bridge tests, with a made-up street map of its own. Never packaged."""
import json, os, sqlite3, sys, tempfile, threading, unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
os.environ['LASTBOOK_DATA'] = str(fixture.ensure())
os.environ['LASTBOOK_RECALL_SOCKET'] = '/nonexistent/recall.sock'

import server  # noqa: E402
import bridge as B  # noqa: E402

server.init(no_index=True)
LAT, LON = -8.5, 115.25


class Sink(B.Sink):
    def __init__(self):
        self.status, self.parts, self.done = None, [], threading.Event()

    def head(self, status, reason, headers):
        self.status = status

    def data(self, chunk):
        self.parts.append(chunk)

    def finish(self, ok):
        self.done.set()


class NearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        geo = Path(tempfile.mkdtemp(prefix='lastbook-near-'))
        fixture.region_file(geo / 'regions-testland.mbtiles', {15: [
            ('hospital', 'RS Mapton', LAT + 0.004, LON), ('clinic', None, LAT, LON - 0.003), ('pharmacy', 'Apotek Satu', LAT, LON + 0.002),
            ('toilets', None, LAT - 0.001, LON), ('restaurant', 'Warung', LAT, LON)]})
        cls.geo, server.GEO = server.GEO, geo
        cls.bridge = B.Bridge(server)

    @classmethod
    def tearDownClass(cls):
        server.GEO = cls.geo

    def get(self, path):
        sink = Sink()
        self.bridge.start('GET', path, None, sink)
        self.assertTrue(sink.done.wait(30), path)
        return sink.status, json.loads(b''.join(sink.parts))

    def test_the_points_around_a_covered_place(self):
        status, out = self.get(f'/api/near?lat={LAT}&lon={LON}')
        self.assertEqual((status, out['covered']), (200, True))
        # Nearest first, each kind by the name `--nearby` gives it (toilet, not the map's toilets); no restaurant.
        self.assertEqual([(p['cls'], p['name']) for p in out['points']],
                         [('toilet', ''), ('pharmacy', 'Apotek Satu'), ('clinic', ''), ('hospital', 'RS Mapton')])
        self.assertEqual(sorted(out['points'][0]), ['cls', 'dir', 'km', 'label', 'lat', 'lon', 'name'])
        self.assertEqual((out['points'][1]['km'], out['points'][1]['dir']), (0.22, 'east'))

    def test_no_far_away_points_where_no_street_map_covers(self):
        # Instead, the streets that would cover it, from the bundled region list, smallest first. This library has no
        # place search to name the country, so both boxes that hold the point well inside are offered.
        self.assertEqual(self.get('/api/near?lat=12.5&lon=34.25'), (200, {'covered': False, 'points': [], 'regions': [
            {'id': 'ethiopia', 'name': 'Ethiopia', 'bytes': None, 'installed': False}, {'id': 'sudan', 'name': 'Sudan', 'bytes': None, 'installed': False}]}))
        self.assertEqual(self.get('/api/near?lat=25&lon=-40'), (200, {'covered': False, 'points': [], 'regions': []}))   # the open sea

    def test_the_regions_a_place_is_in_smallest_first(self):
        names = lambda lat, lon, address: [r['name'] for r in server.setup_maps.covering(lat, lon, address)]
        # Paris lies in Picardie's and Centre's boxes too, at their edges; the address names its own.
        self.assertEqual(names(48.8566, 2.3522, 'Île-de-France, France'), ['Ile-de-France', 'France'])
        self.assertEqual(names(-7.25, 112.75, 'East Java, Indonesia'), ['Java', 'Indonesia (with East Timor)'])
        # Nepal named: not India's or China's streets, whose boxes hold Kathmandu as well.
        self.assertEqual(names(27.7, 85.3, 'Bagmati Province, Nepal'), ['Nepal'])
        self.assertEqual(names(12.5, 34.25, 'Sennar, Sudan'), ['Sudan'])
        self.assertEqual(names(25.0, -40.0, ''), [])

    def test_how_far_a_point_is(self):
        self.assertEqual(self.get(f'/api/near?lat={LAT}&lon={LON}&to={LAT},{LON + 0.002}'), (200, {'km': 0.22, 'dir': 'east'}))

    def test_refusals(self):
        for q in ('', 'lat=91&lon=0', 'lat=x&lon=1', f'lat={LAT}&lon={LON}&to=somewhere', f'lat={LAT}&lon={LON}&to=1,200'):
            status, out = self.get('/api/near?' + q)
            self.assertEqual(status, 400, q)
            self.assertIn('error', out)



class PlaceOrderTests(unittest.TestCase):
    """server._places over a gazetteer made as engine/setup/maps.py makes it: largest places first, ids in that order."""

    @classmethod
    def setUpClass(cls):
        cls.db = sqlite3.connect(':memory:')
        cls.db.executescript("""CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
            CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');""")
        rows = [('Mumbai', 'Maharashtra, India', 1, 'Bombay Bombaim'), ('Houston', 'Texas, United States', 1, ''),
                ('Lyon 03', 'Auvergne, France', 2, ''), ('Lyon', 'Auvergne, France', 2, ''),
                ('Springfield', 'Missouri, United States', 2, ''), ('Springfield', 'Illinois, United States', 2, '')]
        rows += [(f'Amazon {i}', 'Somewhere', 3, '') for i in range(250)]       # more than one search takes in
        rows += [('Ama', 'Elsewhere', 5, ''), ('Bombay', 'Auckland, New Zealand', 5, ''), ('Texas', 'Hidalgo, Mexico', 5, ''),
                 ('Texas', 'United States', 1, '')]
        for i, (name, detail, rank, others) in enumerate(rows, 1):
            cls.db.execute('INSERT INTO place VALUES (?,?,?,?,0,?,?,?)', (i, name, 'town', detail, i / 10, rank, 'made up'))
            cls.db.execute('INSERT INTO place_fts(rowid, name, detail) VALUES (?,?,?)', (i, name, f'{detail} · {others}'))

    def first(self, q, n=1):
        return [(p['name'], p['detail'], p['match']) for p in server._places(self.db, q, 5)][:n]

    def test_a_name_many_places_share(self):
        self.assertEqual(self.first('Springfield', 2), [('Springfield', 'Missouri, United States', 'exact'),
                                                        ('Springfield', 'Illinois, United States', 'exact')])    # the larger first
        self.assertEqual(self.first('Lyon, France'), [('Lyon', 'Auvergne, France', 'exact')])   # not Lyon 03, which only starts so

    def test_a_large_place_by_its_old_name_before_a_village_of_that_name(self):
        self.assertEqual(self.first('Bombay', 2), [('Mumbai', 'Maharashtra, India', 'alternate name'), ('Bombay', 'Auckland, New Zealand', 'exact')])
        # Houston only lies in Texas: the places called Texas come first.
        self.assertEqual(self.first('Texas', 3), [('Texas', 'United States', 'exact'), ('Texas', 'Hidalgo, Mexico', 'exact'),
                                                  ('Houston', 'Texas, United States', 'partial')])

    def test_a_village_is_not_lost_behind_larger_places_its_name_starts(self):
        self.assertEqual(self.first('Ama'), [('Ama', 'Elsewhere', 'exact')])


if __name__ == '__main__':
    unittest.main()
