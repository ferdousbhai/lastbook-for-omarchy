#!/usr/bin/env python3
"""Makes packaging/arch/map-regions.json: the street-level map regions setup offers, from Geofabrik's region index
(https://download.geofabrik.de/index-v1.json): continents, countries, and parts of large countries, each with its
bounding box. Only names and boxes are taken; the map itself is cut from OpenStreetMap on the person's computer.

  python3 tools/make_region_list.py [OUT]
"""
import json
import sys
import urllib.request
from pathlib import Path

INDEX = 'https://download.geofabrik.de/index-v1.json'


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
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def main(out):
    with urllib.request.urlopen(urllib.request.Request(INDEX, headers={'User-Agent': 'lastbook-region-list/1'}), timeout=300) as r:
        feats = json.load(r)['features']
    rows = []
    for f in feats:
        p = f['properties']
        if not f.get('geometry'):
            continue
        name = p['name']
        if name == p['id'] and '/' in name:       # Geofabrik names the US states by id: us/new-york -> New York
            name = name.split('/')[-1].replace('-', ' ').title().replace(' Of ', ' of ')
        rows.append({'id': p['id'], 'name': name, 'parent': p.get('parent', ''), 'bbox': bbox(f['geometry'])})
    rows.sort(key=lambda r: (r['parent'], r['name']))
    Path(out).write_text(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).replace('},{', '},\n{') + '\n', encoding='utf-8')
    print(f'{out}: {len(rows)} regions')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / 'packaging/arch/map-regions.json'))
