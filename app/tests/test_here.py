#!/usr/bin/env python3
"""Where the computer seems to be, and the map offered for it (engine/setup/here.py): read from files, nothing sent.

  python3 -B app/tests/test_here.py [-v]

Made-up home folders, time zone files and routing tables. Never packaged."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
from engine.setup import here, maps  # noqa: E402


class Here(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-here-'))
        self.settings = self.tmp / 'config'
        self.home = self.tmp / 'home'
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        os.environ.pop('LASTBOOK_MAP_OFFER', None)
        self.addCleanup(self.env.stop)

    def weather(self, **w):
        f = self.home / here.WEATHER
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(w))

    def zone(self, name='Asia/Tokyo'):
        zoneinfo = self.tmp / 'zoneinfo'
        (zoneinfo / name).parent.mkdir(parents=True, exist_ok=True)
        (zoneinfo / name).write_text('')
        (zoneinfo / 'zone1970.tab').write_text('# comment\nJP,AU\t+353916+1394441\tAsia/Tokyo\tEyre Bird Observatory\n'
                                               'NP\t+2743+08519\tAsia/Kathmandu\n')
        link = self.tmp / 'localtime'
        if link.is_symlink():
            link.unlink()
        link.symlink_to(zoneinfo / name)
        return here.zone_place(link, zoneinfo)

    def test_the_weather_location_needs_coordinates(self):
        self.assertIsNone(here.weather_place(self.home))
        self.weather(name='Malibu')
        self.assertIsNone(here.weather_place(self.home))
        self.weather(name='Ubud', latitude=-8.5069, longitude=115.2625)
        self.assertEqual(here.weather_place(self.home), {'name': 'Ubud', 'lat': -8.5069, 'lon': 115.2625})
        self.weather(name='Nowhere', latitude=120, longitude=0)
        self.assertIsNone(here.weather_place(self.home))

    def test_the_time_zone_gives_its_city(self):
        z = self.zone()
        self.assertEqual((z['zone'], z['name'], z['area']), ('Asia/Tokyo', 'Tokyo', 'Eyre Bird Observatory'))
        self.assertAlmostEqual(z['lat'], 35.6544, 3)
        self.assertAlmostEqual(z['lon'], 139.7447, 3)
        self.assertEqual(self.zone('Asia/Kathmandu')['area'], '')
        self.assertIsNone(self.zone('Etc/UTC'))

    def test_online_is_a_default_route(self):
        v4, v6 = self.tmp / 'route', self.tmp / 'ipv6_route'
        self.assertFalse(here.online((v4, v6)))
        v4.write_text('Iface\tDestination\tGateway\tFlags\nwlan0\t0001A8C0\t00000000\t0001\n')
        v6.write_text('0' * 32 + ' 00 ' + '0' * 32 + ' 00 ' + '0' * 32 + ' ffffffff 00000001 00000000 00200200 lo\n')
        self.assertFalse(here.online((v4, v6)))
        v4.write_text('Iface\tDestination\tGateway\tFlags\nwlan0\t00000000\t0101A8C0\t0003\n')
        self.assertTrue(here.online((v4, v6)))

    def test_an_exact_place_is_offered_the_regions_that_cover_it(self):
        self.weather(name='Kathmandu', latitude=27.7172, longitude=85.324)
        o = here.offer(self.settings, None, self.home, True)
        self.assertEqual((o['place'], o['source'], o['exact']), ('Kathmandu', 'weather location', True))
        self.assertEqual(o['regions'][0], {'id': 'nepal', 'name': 'Nepal'})
        self.assertIn('Nepal', o['text'])
        self.assertIn('lastbook open manage:', o['text'])

    def test_nothing_is_offered_offline_or_switched_off(self):
        self.weather(name='Kathmandu', latitude=27.7172, longitude=85.324)
        self.assertIsNone(here.offer(self.settings, None, self.home, False))
        self.settings.mkdir()
        (self.settings / 'setup.json').write_text('{"map_offer": false}')
        self.assertIsNone(here.offer(self.settings, None, self.home, True))
        (self.settings / 'setup.json').write_text('{"map_offer": true}')
        self.assertIsNotNone(here.offer(self.settings, None, self.home, True))
        os.environ['LASTBOOK_MAP_OFFER'] = '0'
        self.assertIsNone(here.offer(self.settings, None, self.home, True))

    def test_a_time_zone_offers_the_regions_around_its_city_and_is_quiet_when_one_is_here(self):
        o = here.offer(self.settings, None, self.home, True, zone=self.zone())
        self.assertEqual((o['place'], o['source'], o['exact']), ('Tokyo', 'time zone', False))
        ids = [r['id'] for r in o['regions']]
        self.assertIn('kanto', ids)
        self.assertLessEqual(len(ids), here.NEAREST)
        # A street map of one of them is in the library: a time zone cannot say it is the wrong one.
        geo = self.tmp / 'geo'
        geo.mkdir()
        (geo / f"regions-{maps.file_id('japan')}.mbtiles").write_bytes(b'')
        self.assertIsNone(here.offer(self.settings, geo, self.home, True, zone=self.zone()))


if __name__ == '__main__':
    unittest.main()
