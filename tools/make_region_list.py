#!/usr/bin/env python3
"""Makes packaging/arch/map-regions.json: the street-level map regions setup offers, from Geofabrik's region index
(https://download.geofabrik.de/index-v1.json): continents, countries, and parts of large countries, each with its
bounding box. Only names and boxes are taken; the map itself is cut from OpenStreetMap on the person's computer.

A region across the antimeridian (Alaska's Aleutians, Fiji, New Zealand's Chatham Islands) gets `bbox` [w, s, e, n] with
w > e, as GeoJSON writes it, and `bboxes`: its two real boxes, one each side of ±180, which the cut uses. Geofabrik draws
such regions as separate polygons on each side, so its extent is the shortest stretch of longitude holding every polygon.

  python3 tools/make_region_list.py [OUT [INDEX_FILE]]
"""
import json
import sys
import urllib.request
from pathlib import Path

INDEX = 'https://download.geofabrik.de/index-v1.json'


def _extent(parts):
    """[w, s, e, n] of a list of (w, s, e, n)."""
    return [min(p[0] for p in parts), min(p[1] for p in parts), max(p[2] for p in parts), max(p[3] for p in parts)]


def boxes(geometry):
    """[box] or, across the antimeridian, [east-side box, west-side box]; each [w, s, e, n]."""
    def walk(c, xs, ys):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x, xs, ys)
    polys = geometry['coordinates'] if geometry['type'] == 'MultiPolygon' else [geometry['coordinates']]
    parts = []
    for poly in polys:
        xs, ys = [], []
        walk(poly, xs, ys)
        parts.append((min(xs), min(ys), max(xs), max(ys)))
    w, s, e, n = _extent(parts)
    # The widest stretch of longitude no polygon touches. When it is wider than the one across ±180 (360 - (e - w)), the
    # region is the other way round the world: from that stretch's east end across ±180 to its west end.
    parts.sort()
    gap, reach = (0.0, None), parts[0][2]
    for p in parts[1:]:
        if p[0] - reach > gap[0]:
            gap = (p[0] - reach, (reach, p[0]))
        reach = max(reach, p[2])
    if gap[1] is None or gap[0] <= 360 - (e - w):
        return [[round(v, 4) for v in (w, s, e, n)]]
    west_end, east_start = gap[1]
    east = _extent([p for p in parts if p[0] >= east_start])
    west = _extent([p for p in parts if p[2] <= west_end])
    return [[round(v, 4) for v in east], [round(v, 4) for v in west]]


def bbox(geometry):
    """The region's extent as [w, s, e, n]; w > e across the antimeridian."""
    b = boxes(geometry)
    return b[0] if len(b) == 1 else [b[0][0], min(b[0][1], b[1][1]), b[1][2], max(b[0][3], b[1][3])]


def main(out, index=None):
    if index:
        feats = json.loads(Path(index).read_text(encoding='utf-8'))['features']
    else:
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
        row = {'id': p['id'], 'name': name, 'parent': p.get('parent', ''), 'bbox': bbox(f['geometry'])}
        if len(boxes(f['geometry'])) > 1:
            row['bboxes'] = boxes(f['geometry'])
        rows.append(row)
    rows.sort(key=lambda r: (r['parent'], r['name']))
    Path(out).write_text(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).replace('},{', '},\n{') + '\n', encoding='utf-8')
    print(f'{out}: {len(rows)} regions')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / 'packaging/arch/map-regions.json'),
         sys.argv[2] if len(sys.argv) > 2 else None)
