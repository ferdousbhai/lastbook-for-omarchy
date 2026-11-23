#!/usr/bin/env python3
"""The map cut when it does not go well (engine/setup/maps.py): carrying on from a part file after a pause, a network
failure or a kill; part files left behind; an archive whose tiles cannot be read; place names that are not what they
should be. And one cut end to end over a local HTTPS server that breaks, stalls and is paused.

The made-up archive is app/tests/test_maps.py's, the server app/tests/test_download_stall.py's. No real internet.
Never packaged."""
import contextlib, io, json, shutil, sqlite3, sys, tempfile, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[1])]
from engine.setup import download, maps  # noqa: E402
from app.tests import test_download_stall as srv, test_maps as made  # noqa: E402

BOX = (-30.0, -25.0, 40.0, 30.0)        # across the middle of the grid, with open sea in it: runs, and tiles stored far back
BUILD = 'https://example.invalid/20260101.pmtiles'


def wanted(bbox, minzoom=3):
    out = {}
    for z in range(minzoom if bbox else 0, made.MAXZ + 1):
        x0, y0, x1, y1 = maps.tile_range(*bbox, z) if bbox else (0, 0, (1 << z) - 1, (1 << z) - 1)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                out[(z, x, (1 << z) - 1 - y)] = made.tile(z, x, y)
    return out


def written(path):
    db = sqlite3.connect(path)
    try:
        return ({(z, x, y): bytes(b) for z, x, y, b in db.execute('SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles')},
                dict(db.execute('SELECT * FROM metadata')))
    finally:
        db.close()


class Stop(BaseException):
    """What a test stops a cut with when it stands for the process dying: nothing handles it but run()'s cleanup."""


class ResumeTests(unittest.TestCase):
    """Cuts from the made-up archive, fetched through a stand-in for download.fetch_range that can fail at any request."""

    @classmethod
    def setUpClass(cls):
        cls.bytes, cls.root_len = made.archive()
        cls.data_off = int.from_bytes(cls.bytes[56:64], 'little')

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-cut-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.geo = self.tmp / 'derived' / 'geo'
        self.geo.mkdir(parents=True)
        self.asked, self.fail_at, self.build = [], None, BUILD
        self.out = io.StringIO()

        def fetch(url, start, length, control, **kw):
            control.check()
            if start >= self.data_off:              # a piece of tiles, not a directory
                self.asked.append((start, length))
                if self.fail_at is not None and len(self.asked) == self.fail_at[0]:
                    half = length // 2              # some of it arrives and is reported before it breaks
                    if kw.get('got') and half:
                        kw['got'](half)
                    raise self.fail_at[1]
            if kw.get('got'):
                kw['got'](length)
            return self.bytes[start:start + length]
        for target, name, value in ((download, 'fetch_range', fetch), (maps, 'newest_build', lambda *a: self.build),
                                    (maps, 'PIECE', 700), (maps, 'GAP', 40), (maps, 'BATCH', 9)):     # many small pieces
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def cut(self, bbox=BOX, name='regions-test', control=None, minzoom=3):
        return maps.Cut(self.geo / f'{name}.mbtiles', bbox, minzoom if bbox else 0, made.MAXZ, 'Test', control or download.Control())

    def run_cut(self, c, progress=lambda done, total: None):
        with contextlib.redirect_stdout(self.out):
            return c.run(progress)

    def part(self, c):
        return c.dest.with_name(c.dest.name + '.part')

    def test_a_cut_that_loses_the_network_at_any_piece_carries_on_to_the_same_map(self):
        """Broken at each piece in turn, then run again: every time the same tiles as an unbroken cut, nothing of the
        mark left in the file, and the pieces already written not fetched twice."""
        whole = self.cut(name='regions-whole')
        self.run_cut(whole)
        pieces = list(self.asked)
        want, want_meta = written(whole.dest)
        self.assertEqual(want, wanted(BOX))
        self.assertGreater(len(pieces), 20)
        self.assertNotIn(maps.MARK, want_meta)
        whole.dest.unlink()
        far_back = sum(n for i, (a, n) in enumerate(pieces) if any(a < b for b, _ in pieces[:i]))      # asked for out of order
        self.assertLess(far_back, sum(n for _, n in pieces) // 10)
        for k in range(1, len(pieces) + 1):
            self.asked.clear()
            self.fail_at = (k, download.Failed('network', 'The connection to the map server keeps breaking.'))
            c = self.cut()
            with self.assertRaises(download.Failed):
                self.run_cut(c)
            self.assertTrue(self.part(c).is_file(), k)
            self.assertFalse(c.dest.exists(), k)
            first, self.fail_at = list(self.asked), None
            self.asked.clear()
            seen = []
            c = self.cut()
            self.run_cut(c, lambda done, total: seen.append((done, total)))
            got, meta = written(c.dest)
            self.assertEqual(got, want, k)
            self.assertEqual(meta, want_meta, k)
            self.assertEqual(sorted(p.name for p in self.geo.iterdir()), ['regions-test.mbtiles'], k)
            self.assertEqual(seen[-1], (c.total, c.total), k)
            self.assertEqual(seen, sorted(seen), k)
            # Fetched twice: the piece that broke, and tiles stored far back in the archive (few, and kept in memory only).
            written_before = set().union(*(range(a, a + n) for a, n in first[:-1]))
            twice = sum(len(written_before.intersection(range(a, a + n))) for a, n in self.asked)
            self.assertLessEqual(twice, far_back + maps.PIECE, k)
            if k > 3:
                self.assertGreater(seen[0][0], 0, k)            # the progress shown starts from what was kept
                self.assertLess(len(self.asked), len(pieces), k)
            c.dest.unlink()

    def test_the_world_carries_on_too(self):
        c = self.cut(None, name='world')
        self.run_cut(c)
        n = len(self.asked)
        want = written(c.dest)[0]
        self.assertEqual(want, wanted(None))
        c.dest.unlink()
        for k in (1, n // 3, n // 2, n - 1, n):
            self.asked.clear()
            self.fail_at = (k, download.Failed('network', 'broken'))
            with self.assertRaises(download.Failed):
                self.run_cut(self.cut(None, name='world'))
            self.fail_at = None
            self.asked.clear()
            c = self.cut(None, name='world')
            self.run_cut(c)
            self.assertEqual(written(c.dest)[0], want, k)
            self.assertLessEqual(len(self.asked), n - k + 2 + n // 10, k)
            c.dest.unlink()

    def test_pause_keeps_the_part_file_and_resume_starts_where_it_stopped(self):
        control, seen = download.Control(), []

        def progress(done, total):
            seen.append(done)
            if done > total // 2:
                control.stop('paused')
        c = self.cut(control=control)
        with self.assertRaises(download.Stopped):
            self.run_cut(c, progress)
        part = self.part(c)
        self.assertTrue(part.is_file())
        tiles_then, meta = written(part)
        mark = json.loads(meta[maps.MARK])
        self.assertGreater(mark['done'], 0)
        self.assertLessEqual(mark['done'], seen[-1])            # the mark is never ahead of what was fetched
        self.assertGreater(len(tiles_then), 0)
        self.assertLessEqual(tiles_then.items(), wanted(BOX).items())
        self.asked.clear()
        again = []
        c = self.cut()
        self.run_cut(c, lambda done, total: again.append(done))
        self.assertEqual(again[0], mark['done'])                # honest: what was kept, not zero and not the old number
        self.assertEqual(written(c.dest)[0], wanted(BOX))
        self.assertIn('carrying on with regions-test.mbtiles', self.out.getvalue())
        self.assertFalse(part.exists())

    def test_cancel_a_full_disk_and_an_error_leave_no_part_file(self):
        for why in ('cancelled', OSError(28, 'No space left on device'), sqlite3.OperationalError('database or disk is full'), RuntimeError('a bug')):
            self.asked.clear()
            control = download.Control()
            c = self.cut(control=control)
            if why == 'cancelled':
                with self.assertRaises(download.Stopped):
                    self.run_cut(c, lambda done, total: done > total // 2 and control.stop('cancelled'))
            else:
                self.fail_at = (5, why)
                with self.assertRaises(RuntimeError if isinstance(why, RuntimeError) else download.Failed) as e:
                    self.run_cut(c)
                if not isinstance(why, RuntimeError):
                    self.assertEqual(e.exception.code, 'no_space')
                self.fail_at = None
            self.assertEqual(list(self.geo.iterdir()), [], why)

    def test_a_pause_then_a_cancel_is_cleaned_up_by_sweep(self):
        """What setup's cancel calls for a paused or failed cut (engine/setup/api.py): the part file goes, maps stay."""
        self.fail_at = (6, download.Stopped('paused'))
        c = self.cut()
        with self.assertRaises(download.Stopped):
            self.run_cut(c)
        size = self.part(c).stat().st_size
        for name in ('world.mbtiles', 'regions-other.mbtiles', 'world-places.sqlite', 'CA.sqlite', 'notes.part', 'regions-x.mbtiles.partial'):
            (self.geo / name).write_bytes(b'kept')
        self.assertGreaterEqual(maps.sweep(self.geo), size)
        self.assertEqual(sorted(p.name for p in self.geo.iterdir()),
                         ['CA.sqlite', 'notes.part', 'regions-other.mbtiles', 'regions-x.mbtiles.partial', 'world-places.sqlite', 'world.mbtiles'])
        self.assertEqual(maps.sweep(self.geo), 0)
        self.assertEqual(maps.sweep(self.tmp / 'no such folder'), 0)

    def test_what_a_killed_cut_left_is_carried_on_from_or_removed(self):
        """The process dies in the middle of a piece (kill, power cut): nothing of run()'s cleanup happens. The files are
        as SQLite left them, journal and all. The same cut carries on from the last piece that was wholly written;
        another map's cut, or the place names, removes them."""
        c = self.cut()
        died = self.tmp / 'died'
        commits = []

        class Dying(sqlite3.Connection):
            def commit(conn):
                commits.append(1)
                if len(commits) == 9:           # in the middle of a transaction: tiles written, the mark not yet committed
                    died.mkdir()
                    for p in self.geo.iterdir():
                        shutil.copy2(p, died / p.name)
                    raise Stop()
                return super().commit()
        real_connect = sqlite3.connect

        def connect(p, **kw):
            db = real_connect(p, factory=Dying, **kw)
            db.execute('PRAGMA cache_size = 1')     # so SQLite writes to the file inside a transaction, its journal beside it
            return db
        with mock.patch.object(maps.sqlite3, 'connect', connect), self.assertRaises(Stop):
            self.run_cut(c)
        self.assertEqual(list(self.geo.iterdir()), [])              # (this stand-in for dying does unwind, so run() cleaned up)
        self.assertEqual(sorted(p.name for p in died.iterdir()), ['regions-test.mbtiles.part', 'regions-test.mbtiles.part-journal'])
        half = sqlite3.connect(f'file:{died / "regions-test.mbtiles.part"}?immutable=1', uri=True)     # the file as it lies, journal ignored
        try:
            lying = half.execute('SELECT count(*) FROM tiles').fetchone()[0]
        except sqlite3.DatabaseError:
            lying = None                                            # torn: which is the point
        finally:
            half.close()
        for p in died.iterdir():
            shutil.copy2(p, self.geo / p.name)                      # ...so the files are put back as the kill left them
        db = sqlite3.connect(self.geo / 'regions-test.mbtiles.part')    # opening it undoes the half-written transaction
        before = json.loads(dict(db.execute('SELECT * FROM metadata'))[maps.MARK])
        tiles_at_mark = db.execute('SELECT count(*) FROM tiles').fetchone()[0]
        db.close()
        self.assertFalse((self.geo / 'regions-test.mbtiles.part-journal').exists())
        self.asked.clear()
        seen = []
        c = self.cut()
        self.run_cut(c, lambda done, total: seen.append(done))
        self.assertEqual(written(c.dest)[0], wanted(BOX))
        self.assertEqual(seen[0], before['done'])
        self.assertGreater(before['next'], 0)
        self.assertNotEqual(lying, tiles_at_mark)                   # the file alone held tiles the mark did not cover: the journal undid them
        self.assertEqual(sorted(p.name for p in self.geo.iterdir()), ['regions-test.mbtiles'])
        # Another map's cut removes what the kill left of this one.
        c.dest.unlink()
        for p in died.iterdir():
            shutil.copy2(p, self.geo / p.name)
        (self.geo / 'regions-test.mbtiles.part-journal').write_bytes(b'left')
        (self.geo / 'world-places.sqlite.part').write_bytes(b'left')
        other = self.cut(name='regions-other')
        self.run_cut(other)
        self.assertEqual(sorted(p.name for p in self.geo.iterdir()), ['regions-other.mbtiles'])

    def test_a_part_file_of_another_build_cut_or_kind_is_not_carried_on_from(self):
        def broken():
            self.fail_at = (8, download.Failed('network', 'broken'))
            self.asked.clear()
            with self.assertRaises(download.Failed):
                self.run_cut(self.cut())
            self.fail_at = None
            self.asked.clear()
        self.run_cut(self.cut(name='regions-whole'))
        n = len(self.asked)
        # Tomorrow's build: tiles of two builds are never mixed.
        broken()
        self.build = 'https://example.invalid/20260102.pmtiles'
        c = self.cut()
        seen = []
        self.run_cut(c, lambda done, total: seen.append(done))
        self.assertEqual(len(self.asked), n)
        self.assertLess(seen[0], c.total // 10)
        got, meta = written(c.dest)
        self.assertEqual((got, meta['source']), (wanted(BOX), '20260102.pmtiles'))
        c.dest.unlink()
        # The same file name for another box or zoom range (the region list changed with an update).
        broken()
        c = self.cut((-30.0, -25.0, 45.0, 30.0))
        self.run_cut(c)
        self.assertEqual(written(c.dest)[0], wanted((-30.0, -25.0, 45.0, 30.0)))
        c.dest.unlink()
        broken()
        c = self.cut(minzoom=4)
        self.run_cut(c)
        self.assertEqual(written(c.dest)[0], wanted(BOX, minzoom=4))
        c.dest.unlink()
        # Not a part file of ours at all: no mark, or not a database.
        for junk in (b'not a database at all' * 100, None):
            part = self.geo / 'regions-test.mbtiles.part'
            if junk:
                part.write_bytes(junk)
            else:
                db = sqlite3.connect(part)
                db.executescript('CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT); CREATE TABLE tiles (a, b, c, d);'
                                 "INSERT INTO tiles VALUES (1, 0, 0, x'00');")
                db.close()
            self.asked.clear()
            c = self.cut()
            self.run_cut(c)
            self.assertEqual(len(self.asked), n)
            self.assertEqual(written(c.dest)[0], wanted(BOX))
            c.dest.unlink()

    def test_tiles_packed_in_a_way_the_map_cannot_read_are_refused_before_any_is_fetched(self):
        good = self.bytes
        for packing, fine in ((1, True), (2, True), (0, False), (3, False), (4, False)):      # none, gzip; unknown, brotli, zstd
            self.bytes = good[:98] + bytes([packing]) + good[99:]
            self.asked.clear()
            c = self.cut()
            if fine:
                c.measure()
                continue
            with self.assertRaises(download.Failed) as e:
                c.measure()
            self.assertEqual(e.exception.code, 'network')
            self.assertIn('cannot read', e.exception.message)
            self.assertEqual(self.asked, [])
            self.assertEqual(list(self.geo.iterdir()), [])


# ---------------------------------------------------------------------------------------------------- place names --
def city(i, name=None, lat=None, lon=None, pop='20000', cc='AA', a1='01', fcode='PPL'):
    row = [''] * 19
    row[0], row[1], row[2], row[3] = str(1000 + i), name or f'Town {i}', f'Town {i}', f'Ville {i},Stadt {i}'
    row[4], row[5] = str(-60 + (i % 120)) if lat is None else lat, str(-170 + (i % 340)) if lon is None else lon
    row[6], row[7], row[8], row[10], row[14] = 'P', fcode, cc, a1, pop
    return '\t'.join(row)


def zipped(text, member=maps.PLACES_TXT):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, text)
    return b.getvalue()


class PlaceNamesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-places-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = self.tmp / maps.PLACES_FILE
        mock.patch.object(maps, 'MIN_PLACES', 10_000).start()      # a whole cities500.txt has 237,000 rows: these files are a small stand-in
        self.addCleanup(mock.patch.stopall)
        codes = [a + b for a in 'ABCDEFGHIJKLMNOP' for b in 'ABCDEFGHIJKLMNOP'][:210]
        self.countries = '# ISO\tISO3\tnum\tfips\tCountry\n' + '\n'.join(f'{c}\t{c}X\t1\t{c}\tLand {c}' for c in codes) + '\n'
        self.admin1 = 'AA.01\tNorth Province\tNorth Province\t1\n'
        self.cities = [city(i, fcode='PPLC' if i == 0 else 'PPL') for i in range(10_050)]
        self.out = io.StringIO()
        self.asked = []

    def build(self, countries=None, admin1=None, cities=None, control=None):
        files = {'countryInfo.txt': (self.countries if countries is None else countries),
                 'admin1CodesASCII.txt': (self.admin1 if admin1 is None else admin1),
                 maps.PLACES_ZIP: zipped('\n'.join(self.cities) + '\n') if cities is None else cities}

        def fetch(url, limit=1 << 20, **kw):
            self.asked.append((url, kw.get('control')))
            data = files[url.rsplit('/', 1)[-1]]
            data = data.encode('utf-8') if isinstance(data, str) else data
            return data if data is not None and len(data) <= limit else None
        with mock.patch.object(download, 'fetch_small', fetch), contextlib.redirect_stdout(self.out):
            return maps.build_places(self.dest, control or download.Control())

    def places(self):
        db = sqlite3.connect(self.dest)
        try:
            return db.execute('SELECT name, kind, detail, lat, lon FROM place').fetchall()
        finally:
            db.close()

    def failed(self, message=maps.BAD_PLACES, **kw):
        with self.assertRaises(download.Failed) as e:
            self.build(**kw)
        self.assertEqual(e.exception.code, 'network')
        self.assertEqual(e.exception.message, message)
        self.assertEqual(list(self.tmp.iterdir()), [])              # nothing half made

    def test_good_files_make_place_search(self):
        control = download.Control()
        self.build(control=control)
        rows = self.places()
        self.assertEqual(sum(1 for r in rows if r[1] in ('city', 'town')), 10_050)
        self.assertIn(('Land AA', 'country', 'AA'), [r[:3] for r in rows])
        self.assertIn(('North Province', 'region', 'Land AA'), [r[:3] for r in rows])
        self.assertEqual(next(r for r in rows if r[0] == 'Town 7')[2], 'North Province, Land AA')
        self.assertEqual(self.out.getvalue(), '')
        self.assertTrue(all(c is control for _, c in self.asked))   # each fetch can be paused or cancelled
        self.assertTrue(all(u.startswith('https://download.geonames.org/') for u, _ in self.asked))
        self.assertTrue(maps.places_current(self.dest))

    def test_larger_places_rank_first_and_alone_keep_their_other_names(self):
        self.cities[1:4] = [city(1, name='Small', pop='700'), city(2, name='Village', pop='', fcode='PPLA4'),
                            city(3, name='Big', pop='2000000')]
        self.cities.append(city(20000, name='Ruin', fcode='PPLQ'))         # an abandoned place is no place to look for help
        self.build()
        db = sqlite3.connect(self.dest)
        try:
            rank = dict(db.execute('SELECT name, rank FROM place'))
            ids = [n for (n,) in db.execute("SELECT name FROM place WHERE kind IN ('city', 'town') ORDER BY id")]
            known_as = lambda other: {n for (n,) in db.execute('SELECT p.name FROM place_fts f JOIN place p ON p.id = f.rowid '
                                                               'WHERE place_fts MATCH ?', (f'detail : "{other}"',))}
            alternates = (known_as('Ville 5'), known_as('Ville 1'))
        finally:
            db.close()
        self.assertEqual([rank[n] for n in ('Land AA', 'North Province', 'Town 0', 'Big', 'Town 5', 'Small', 'Village')], [0, 1, 1, 1, 3, 5, 5])
        self.assertEqual(ids[:2], ['Big', 'Town 0'])                     # largest first: the id says which is larger
        self.assertNotIn('Ruin', rank)
        self.assertEqual(alternates, ({'Town 5'}, set()))               # under 15,000 (Small): its name and its ASCII name only

    def test_a_place_search_made_before_villages_counts_as_missing(self):
        self.assertFalse(maps.places_current(self.dest))                 # none
        db = sqlite3.connect(self.dest)
        db.execute('CREATE TABLE place(id INTEGER PRIMARY KEY)')        # as an earlier version made it: no user_version
        db.commit()
        db.close()
        self.assertFalse(maps.places_current(self.dest))
        self.assertFalse(maps.installed(self.tmp)['places'])
        self.dest.write_bytes(b'not a database at all, but long enough to have a header where one would be' * 2)
        self.assertFalse(maps.places_current(self.dest))
        self.dest.unlink()
        self.build()
        self.assertTrue(maps.installed(self.tmp)['places'])

    def test_rows_that_cannot_be_read_are_left_out_and_the_rest_kept(self):
        """A line cut short, a latitude that is not a number, a place off the globe, a population that is not a number,
        no name: each once cost the whole place search, as 'an internal error'."""
        bad = [city(20001, name='Cut short').rsplit('\t', 8)[0], city(20002, name='No latitude', lat=''), city(20003, name='Words', lon='east'),
               city(20004, name='Off the globe', lat='123.0'), city(20005, name='Not a number', lat='nan'), city(20006, name='Many', pop='lots'),
               '\t'.join([''] * 19), 'x']
        self.cities = self.cities[:5000] + bad + self.cities[5000:] + [city(20010, name='Fine after them', pop='')]
        self.build()
        names = {r[0] for r in self.places()}
        self.assertEqual(sum(1 for r in self.places() if r[1] in ('city', 'town')), 10_051)
        self.assertIn('Fine after them', names)
        self.assertFalse(names & {'Cut short', 'No latitude', 'Words', 'Off the globe', 'Not a number', 'Many'})
        self.assertIn('8 of 10059 place name rows could not be read', self.out.getvalue())

    def test_a_byte_order_mark_and_short_rows_in_the_small_files(self):
        self.build(countries='﻿' + self.countries.split('\n', 1)[1] + 'ZZ\tZZX\n\n', admin1=self.admin1 + 'AA.02\nonly one field\n')
        rows = self.places()
        self.assertIn(('Land AA', 'country', 'AA'), [r[:3] for r in rows])          # the first country kept its code
        self.assertNotIn('﻿AA', {r[2] for r in rows})

    def test_too_few_rows_left_is_incomplete(self):
        self.cities = self.cities[:9000] + ['a\tb'] * 5000
        self.failed('The place names did not download completely. Try again later.')
        self.failed('The place names did not download completely. Try again later.', countries='AA\tAAX\t1\tAA\tLand AA\n')

    def test_a_changed_layout_is_one_plain_failure(self):
        self.cities = ['\t'.join(['x'] * 6)] * 12_000               # fewer columns everywhere
        self.failed('The place names did not download completely. Try again later.')

    def test_files_that_are_not_what_they_should_be(self):
        self.failed(cities=b'<html>Service unavailable</html>')                     # not a zip
        self.failed(cities=zipped('x', member='other.txt'))                         # no such member
        self.failed(cities=zipped(b'\xff\xfe\x00 not UTF-8 \xff'))
        good = zipped('\n'.join(self.cities))
        self.failed(cities=good[:len(good) // 2])                                   # cut short
        at = good.index(maps.PLACES_TXT.encode()) + 40
        self.failed(cities=good[:at] + bytes(b ^ 0x55 for b in good[at:at + 200]) + good[at + 200:])      # damaged inside
        self.failed(countries=b'\xff\xfe\xfd')
        self.assertIn('could not be read', self.out.getvalue())
        with self.assertRaises(download.Failed) as e:
            self.build(cities=b'x' * (maps.MAX_PLACES_ZIP + 1))                     # over the download's own limit: fetch_small gives None
        self.assertIn('could not download', e.exception.message)

    def test_a_zip_that_unpacks_to_far_more_than_place_names_is_not_unpacked(self):
        bomb = zipped('0' * (40 << 20))                 # 40 MB of one byte: about 40 KB to download
        self.assertLess(len(bomb), 100_000)
        real_read, reads = zipfile.ZipExtFile.read, []

        def read(f, n=-1):
            reads.append(n)
            return real_read(f, n)
        with mock.patch.object(maps, 'MAX_PLACES', 8 << 20), mock.patch.object(zipfile.ZipExtFile, 'read', read):
            self.failed(cities=bomb)
            self.assertEqual(reads, [])                 # refused on the size its header gives, nothing unpacked
            self.assertEqual(maps._unzipped(zipped('abc'), maps.PLACES_TXT, 8 << 20), b'abc')
            self.assertEqual(reads, [(8 << 20) + 1])    # and never read past the limit, whatever the header says
        self.assertGreater(maps.MAX_PLACES, 120 << 20)      # the real file is about 42 MB unpacked
        self.assertGreater(maps.MAX_PLACES_ZIP, 40 << 20)   # and about 14 MB to download

    def test_a_stop_is_a_stop_and_a_part_file_left_by_a_kill_goes(self):
        control = download.Control()
        control.stop('cancelled')
        with self.assertRaises(download.Stopped):
            self.build(control=control)
        (self.tmp / 'world-places.sqlite.part').write_bytes(b'left by a kill')
        (self.tmp / 'regions-gone.mbtiles.part').write_bytes(b'left by a kill')
        (self.tmp / 'regions-gone.mbtiles.part-journal').write_bytes(b'left by a kill')
        (self.tmp / 'world.mbtiles').write_bytes(b'a finished map')
        self.build()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['world-places.sqlite', 'world.mbtiles'])


# ------------------------------------------------------------------------------------------- over a real connection --
class OverTheWireTests(srv.ServerCase):
    """The same cut through download.fetch_range and a local HTTPS server: no stand-in between the cut and the socket."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.archive, _ = made.archive()
        cls.data_off = int.from_bytes(cls.archive[56:64], 'little')

    def setUp(self):
        super().setUp()
        self.files.data['20260101.pmtiles'] = self.archive
        self.geo = self.tmp / 'derived' / 'geo'
        self.geo.mkdir(parents=True)
        for target, name, value in ((maps, 'newest_build', lambda *a: f'{self.base}/20260101.pmtiles'), (maps, 'PIECE', 3000), (maps, 'GAP', 40),
                                    (download, 'BACKOFF', (0.02,) * 6)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def cut(self, control=None):
        return maps.Cut(self.geo / 'regions-test.mbtiles', BOX, 3, made.MAXZ, 'Test', control or download.Control(), ALLOWED, self.ctx)

    def data_requests(self):
        return [r for _, r in self.files.log if r and int(r.split('=')[1].split('-')[0]) >= self.data_off]

    def test_a_cut_over_a_line_that_breaks_and_answers_busy(self):
        self.files.cut['20260101.pmtiles'] = [None, None, None, 0, 150, None, 37, None, None, 0, 0, 1]
        self.files.status['20260101.pmtiles'] = [None, 503, None, None, None, None, 429, 500]
        seen = []
        with contextlib.redirect_stdout(self.out):
            c = self.cut()
            c.run(lambda done, total: seen.append((done, total)))
        self.assertEqual(written(c.dest)[0], wanted(BOX))
        self.assertEqual(seen[-1], (c.total, c.total))
        self.assertEqual(seen, sorted(seen))
        self.assertGreaterEqual(self.out.getvalue().count('a map part broke'), 5)
        self.assertEqual(sorted(p.name for p in self.geo.iterdir()), ['regions-test.mbtiles'])

    def test_pause_while_the_map_server_is_silent_then_resume(self):
        """A piece stops arriving half way. Pause is quick (the read is hung up, not waited out for 30 s), the part file
        stays, and the resumed cut asks for less than the whole and makes the same map."""
        whole = self.cut()
        whole.measure()
        measured = len(self.files.log)
        whole.run()
        n, requests = len(self.data_requests()), len(self.files.log) - measured
        want = written(whole.dest)[0]
        whole.dest.unlink()
        self.assertGreater(n, 8)
        control, seen = download.Control(), []
        c = self.cut(control)
        c.measure()
        with self.files.lock:       # one entry an answer from here on: half way through the run, an answer stops after 2 bytes
            self.files.silent['20260101.pmtiles'] = [None] * (requests // 2) + [2]
        error, took = self.stopped_in_time(lambda: c.run(lambda done, total: seen.append(done)), control,
                                           lambda: not self.files.silent.get('20260101.pmtiles'))
        self.assertIsInstance(error, download.Stopped)
        part = c.dest.with_name(c.dest.name + '.part')
        self.assertTrue(part.is_file())
        self.files.release.set()
        self.files.log.clear()
        again = []
        with contextlib.redirect_stdout(self.out):
            c = self.cut()
            c.run(lambda done, total: again.append(done))
        self.assertEqual(written(c.dest)[0], want)
        self.assertGreater(again[0], 0)
        self.assertLess(len(self.data_requests()), n)
        self.assertIn('carrying on with', self.out.getvalue())
        self.assertFalse(part.exists())


ALLOWED = srv.ALLOWED

if __name__ == '__main__':
    unittest.main()
