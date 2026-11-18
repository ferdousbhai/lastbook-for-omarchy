#!/usr/bin/env python3
"""Builds the world gazetteer the map and the assistant search: derived/geo/world-places.sqlite.

  python3 tools/build_world_places.py OUT.sqlite

From GeoNames (https://www.geonames.org, CC BY 4.0): every country, every first-level region (states, provinces), and
every city or town of 15,000 people or more (cities15000), with their alternate names so a place can be found in its own
language too. Same tables as places.sqlite (place, and place_fts over name and detail), so the engine searches both
the same way. `rank` orders results: countries 0, regions and cities over a million 1, over 100,000 2, the rest 3.
"""
import csv
import io
import sqlite3
import sys
import urllib.request
import zipfile

BASE = 'https://download.geonames.org/export/dump/'
UA = {'User-Agent': 'lastbook-gazetteer/1'}
csv.field_size_limit(10_000_000)


def get(name):
    with urllib.request.urlopen(urllib.request.Request(BASE + name, headers=UA), timeout=120) as r:
        return r.read()


def rows(text):
    return [r for r in csv.reader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE) if r and not r[0].startswith('#')]


def main(out):
    countries = {}
    for r in rows(get('countryInfo.txt').decode('utf-8')):
        # ISO, ISO3, ISO-Numeric, fips, Country, Capital, Area, Population, Continent, tld, ..., geonameid (16)
        countries[r[0]] = {'name': r[4], 'capital': r[5], 'population': int(r[7] or 0), 'id': r[16] if len(r) > 16 else ''}
    admin1 = {}
    for r in rows(get('admin1CodesASCII.txt').decode('utf-8')):
        admin1[r[0]] = (r[1], r[3])     # "US.CA" -> (California, geonameid)
    cities = zipfile.ZipFile(io.BytesIO(get('cities15000.zip'))).read('cities15000.txt').decode('utf-8')
    db = sqlite3.connect(out)
    db.executescript('''DROP TABLE IF EXISTS place; DROP TABLE IF EXISTS place_fts;
        CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
        CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');''')
    n = 0
    for r in rows(cities):
        # geonameid name asciiname alternatenames lat lon fclass fcode cc cc2 admin1 admin2 admin3 admin4 population ...
        name, ascii_, alts, lat, lon, fcode, cc, a1, pop = r[1], r[2], r[3], float(r[4]), float(r[5]), r[7], r[8], r[10], int(r[14] or 0)
        region = admin1.get(f'{cc}.{a1}', ('', ''))[0]
        country = countries.get(cc, {}).get('name', cc)
        kind = 'city' if pop >= 100_000 or fcode in ('PPLC', 'PPLA') else 'town'
        rank = 1 if pop >= 1_000_000 or fcode == 'PPLC' else 2 if pop >= 100_000 else 3
        # Alternate names (other languages and spellings) are searchable, but kept short: the first 12 that differ.
        others = [a for a in dict.fromkeys(alts.split(',')) if a and a not in (name, ascii_) and len(a) < 40][:12]
        detail = ', '.join(x for x in (region, country) if x)
        search_detail = detail + (' · ' + ' '.join([ascii_] + others) if (ascii_ != name or others) else '')
        db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)',
                   (name, kind, detail, lat, lon, rank, 'GeoNames (CC BY 4.0)'))
        rowid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.execute('INSERT INTO place_fts(rowid,name,detail) VALUES (?,?,?)', (rowid, name, search_detail))
        n += 1
    # Countries sit at their capital, regions at their largest listed city: a point to fly to.
    capital, largest = {}, {}
    for r in rows(cities):
        cc, a1, pop, lat, lon = r[8], r[10], int(r[14] or 0), float(r[4]), float(r[5])
        if r[7] == 'PPLC':
            capital[cc] = (lat, lon)
        key = f'{cc}.{a1}'
        if pop > largest.get(key, (-1,))[0]:
            largest[key] = (pop, lat, lon)
    for cc, c in countries.items():
        at = capital.get(cc) or max(((p, la, lo) for k, (p, la, lo) in largest.items() if k.startswith(cc + '.')), default=None)
        if not at:
            continue
        lat, lon = at[-2], at[-1]
        db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)', (c['name'], 'country', cc, lat, lon, 0, 'GeoNames (CC BY 4.0)'))
        db.execute('INSERT INTO place_fts(rowid,name,detail) VALUES (last_insert_rowid(),?,?)', (c['name'], cc))
    for key, (name, _) in admin1.items():
        if key not in largest:
            continue
        _, lat, lon = largest[key]
        country = countries.get(key.split('.')[0], {}).get('name', '')
        db.execute('INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)', (name, 'region', country, lat, lon, 1, 'GeoNames (CC BY 4.0)'))
        db.execute('INSERT INTO place_fts(rowid,name,detail) VALUES (last_insert_rowid(),?,?)', (name, country))
    db.commit()
    db.execute("INSERT INTO place_fts(place_fts) VALUES('optimize')")
    db.commit()
    db.execute('VACUUM')
    total = db.execute('SELECT count(*) FROM place').fetchone()[0]
    print(f'{out}: {total} places ({n} cities and towns, {len(countries)} countries, regions)')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
