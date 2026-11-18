#!/usr/bin/env python3
"""Builds a downloadable map region: a map-set zip setup installs (engine/setup/install.py), and its manifest entry.

  python3 tools/build_map_region.py REGION OUTDIR [--world derived/geo/world.mbtiles] [--places derived/geo/world-places.sqlite]
                                    [--base-url https://…/maps/] [--maxzoom 15]
  python3 tools/build_map_region.py --list [FILTER]        # Geofabrik's region ids, e.g. indonesia, germany, us/california

REGION is a Geofabrik region id (https://download.geofabrik.de, 554 regions: continents, countries, and parts of large
countries). Its street-level tiles (zoom 9-15) come from the newest Protomaps world build (tools/map_extract.py) inside
the region's bounding box. A map set is self-contained, as the state maps were: the zip holds the world map, the world
gazetteer and the region's detail, so installing it replaces the library's map set in one checked step:

  map-set.json                                   {"name": REGION, "files": {path: bytes}}
  derived/geo/world.mbtiles                      the world to zoom 8
  derived/geo/world-places.sqlite                GeoNames gazetteer (tools/build_world_places.py)
  derived/geo/regions-<REGION>.mbtiles           the region, zoom 9-15

The manifest entry printed at the end goes into the release manifest's "maps" list; its URL is --base-url + the zip name.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

INDEX = 'https://download.geofabrik.de/index-v1.json'
HERE = Path(__file__).resolve().parent


def regions():
    with urllib.request.urlopen(urllib.request.Request(INDEX, headers={'User-Agent': 'lastbook-map-region/1'}), timeout=300) as r:
        return json.load(r)['features']


def bbox(geometry):
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)
    walk(geometry['coordinates'])
    return min(xs), min(ys), max(xs), max(ys)


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('region', nargs='?')
    ap.add_argument('outdir', nargs='?')
    ap.add_argument('--list', nargs='?', const='', metavar='FILTER')
    ap.add_argument('--world', default='derived/geo/world.mbtiles')
    ap.add_argument('--places', default='derived/geo/world-places.sqlite')
    ap.add_argument('--base-url', default='https://example.invalid/maps/')
    ap.add_argument('--maxzoom', type=int, default=15)
    a = ap.parse_args()
    feats = regions()
    if a.list is not None:
        for f in feats:
            p = f['properties']
            if a.list.lower() in (p['id'] + p['name']).lower():
                print(f"{p['id']:40} {p['name']}  (parent: {p.get('parent', '-')})")
        return
    if not a.region or not a.outdir:
        ap.error('REGION and OUTDIR')
    f = next((f for f in feats if f['properties']['id'] == a.region), None)
    if f is None:
        raise SystemExit(f'no Geofabrik region {a.region!r} (try --list)')
    name = f['properties']['name']
    rid = a.region.replace('/', '-')
    w, s, e, n = bbox(f['geometry'])
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    region_db = out / f'regions-{rid}.mbtiles'
    subprocess.run([sys.executable, '-I', str(HERE / 'map_extract.py'), str(region_db), '--bbox', f'{w},{s},{e},{n}',
                    '--minzoom', '9', '--maxzoom', str(a.maxzoom), '--name', name], check=True)
    members = {'derived/geo/world.mbtiles': Path(a.world), 'derived/geo/world-places.sqlite': Path(a.places),
               f'derived/geo/regions-{rid}.mbtiles': region_db}
    for rel, p in members.items():
        if not p.is_file():
            raise SystemExit(f'missing {p} (for {rel})')
    zpath = out / f'Lastbook-Map-{rid}.zip'
    mapset = {'name': rid, 'title': name, 'files': {rel: p.stat().st_size for rel, p in members.items()}}
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_STORED, allowZip64=True) as z:   # tiles are compressed already
        z.writestr('map-set.json', json.dumps(mapset, indent=1))
        for rel, p in members.items():
            z.write(p, rel)
    installed = sum(mapset['files'].values())
    entry = {'id': rid, 'title': name, 'kind': 'region', 'installed_bytes': installed, 'available': True,
             'includes': 'Streets, buildings, water and help points for the region, the world map, and a world gazetteer',
             'files': [{'name': zpath.name, 'url': a.base_url + zpath.name, 'size': zpath.stat().st_size, 'sha256': sha256(zpath), 'kind': 'zip'}]}
    print(json.dumps(entry, indent=1))


if __name__ == '__main__':
    main()
