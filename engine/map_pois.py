"""Help points (hospitals, pharmacies, police…) read from the street-level map files the window draws.

    derived/geo/regions-<id>.mbtiles   OpenStreetMap vector tiles in Protomaps' basemap schema (engine/setup/maps.py makes them)

`lastbook place --nearby` (near() in server.py) asks here for the points of some kinds in a box around a place. They are in
the tiles' `pois` layer: every point at the file's deepest zoom (15), fewer and larger ones (the main hospitals, not the
pharmacies) at each zoom above it. So the search reads the deepest zoom close to the place and one zoom coarser each time
the box doubles beyond FULL_KM, which keeps a step to a few thousand small reads however far it has to look. A box is
measured in km on the ground (wider in degrees of longitude away from the equator) and goes on across the antimeridian.

The tiles are Mapbox Vector Tiles (https://github.com/mapbox/vector-tile-spec, v2), gzip-compressed. The protocol buffer is
decoded by hand, as native/src/mvt.rs does for the window: only the `pois` layer, and of it only `kind`, `name` and the point.
"""
import math
import sqlite3
import zlib
from pathlib import Path


def ro_uri(path):
    """As engine/paths.py has it, without importing that module, which fixes the library's folder when first imported."""
    return Path(path).resolve().as_uri() + '?mode=ro'

# What `--nearby` calls a kind (HELP in server.py) -> the `kind` values of the pois layer, where they differ. A kind's value
# is the OpenStreetMap tag value (amenity=hospital, shop=supermarket…). native/src/map.rs `help_kind` marks the same points.
KINDS = {'drinking_water': ('drinking_water', 'water_point'), 'toilet': ('toilets',), 'town_hall': ('townhall',),
         'bicycle_shop': ('bicycle',)}
FULL_KM = 35.0               # km each way read at the deepest zoom; each doubling beyond it reads one zoom coarser
EARTH_KM = 6371.0
MAX_TILE = 64 * 1024 * 1024  # a tile unpacks to well under 1 MB; a damaged one costs an error, not this computer's memory


def tile_kinds(cls):
    return KINDS.get(cls, (cls,))


def _varint(b, p):
    out = shift = 0
    while True:
        c = b[p]
        p += 1
        out |= (c & 0x7f) << shift
        if c < 0x80:
            return out, p
        shift += 7
        if shift > 63:
            raise ValueError('varint too long')


def _fields(b):
    """(field, wire type, value) of a message: an int for a varint, bytes for the rest."""
    p, end = 0, len(b)
    while p < end:
        key, p = _varint(b, p)
        wire = key & 7
        if wire == 0:
            v, p = _varint(b, p)
        else:
            if wire == 2:
                n, p = _varint(b, p)
            elif wire in (1, 5):
                n = 8 if wire == 1 else 4
            else:
                raise ValueError('wire type')
            if p + n > end:
                raise ValueError('cut short')
            v = b[p:p + n]
            p += n
        yield key >> 3, wire, v


def _packed(b):
    out, p = [], 0
    while p < len(b):
        v, p = _varint(b, p)
        out.append(v)
    return out


def _string(value):
    """A layer value as text, or None when it is a number or a boolean."""
    for f, wire, v in _fields(value):
        if f == 1 and wire == 2:
            return v.decode('utf-8', 'replace')
    return None


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def pois(data, wanted):
    """The points of a tile's `pois` layer whose kind is in `wanted`: [(kind, name, x, y)], x and y from 0 to 1 across the
    tile. Raises ValueError, IndexError or zlib.error on a tile that is not one (TypeError never: a field of the wrong
    wire type is a ValueError)."""
    if data[:2] == b'\x1f\x8b':
        unpack = zlib.decompressobj(31)
        data = unpack.decompress(data, MAX_TILE)
        if unpack.unconsumed_tail or not unpack.eof:
            raise ValueError('tile too large, or cut short')
    out = []
    for f, wire, layer in _fields(data):
        if f != 3 or wire != 2:
            continue
        keys, values, features, extent = [], [], [], 4096
        for f, wire, v in _fields(layer):
            if f in (1, 2, 3, 4) and wire != 2:   # name, feature, key and value are all length-delimited
                raise ValueError('wire type')
            if f == 1 and v != b'pois':   # a layer's name comes first: the buildings and roads are left unread
                features = []
                break
            if f == 2:
                features.append(v)
            elif f == 3:
                keys.append(v)
            elif f == 4:
                values.append(v)
            elif f == 5 and wire == 0:
                extent = v or 4096
        if not features or b'kind' not in keys:
            continue
        kind_key, name_key = keys.index(b'kind'), keys.index(b'name') if b'name' in keys else -1
        kind_of = {}   # value index -> its text, for the wanted kinds only
        for i, v in enumerate(values):
            text = _string(v)
            if text in wanted:
                kind_of[i] = text
        if not kind_of:
            continue
        for feature in features:
            tags, geometry, shape = (), (), 0
            for f, wire, v in _fields(feature):
                if f == 2 and wire == 2:
                    tags = _packed(v)
                elif f == 3 and wire == 0:
                    shape = v
                elif f == 4 and wire == 2:
                    geometry = _packed(v)
            props = dict(zip(tags[::2], tags[1::2]))
            kind = kind_of.get(props.get(kind_key))
            if kind is None or shape != 1 or len(geometry) < 3 or geometry[0] != 9:   # 9: one MoveTo, the point
                continue
            x, y = _zigzag(geometry[1]) / extent, _zigzag(geometry[2]) / extent
            if not (0 <= x < 1 and 0 <= y < 1):   # in the tile's margin: the neighbouring tile has it too
                continue
            name = values[props[name_key]] if props.get(name_key, len(values)) < len(values) else None
            out.append((kind, (_string(name) or '') if name is not None else '', x, y))
    return out


def _tile_xy(lat, lon, zoom):
    n = 1 << zoom
    lat = max(-85.0511, min(85.0511, lat))
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return max(0, min(n - 1, int(x))), max(0, min(n - 1, int(y)))


def _lon(x, zoom):
    return x / (1 << zoom) * 360 - 180


def _lat(y, zoom):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << zoom)))))


class Regions:
    """The street-level map files of a library's derived/geo folder, open for one lookup. Close it when done."""

    def __init__(self, geo, kinds=()):
        self.wanted = frozenset(k for cls in kinds for k in tile_kinds(cls))
        self.files = []    # (connection, minzoom, maxzoom)
        self.tiles = {}    # (zoom, x, y) -> [(kind, name, lon, lat)], or None where no file has the tile
        geo = Path(geo)
        for path in sorted(geo.glob('regions-*.mbtiles')) if geo.is_dir() else []:
            db = None
            try:
                db = sqlite3.connect(ro_uri(path), uri=True)
                meta = dict(db.execute("SELECT name, value FROM metadata WHERE name IN ('minzoom', 'maxzoom')"))
                self.files.append((db, int(meta.get('minzoom', 9)), int(meta.get('maxzoom', 15))))
            except (sqlite3.Error, ValueError):   # not a map file: the window draws nothing from it either
                if db is not None:
                    db.close()

    def close(self):
        for db, _, _ in self.files:
            db.close()
        self.files = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _blob(self, zoom, x, y):
        for db, low, high in self.files:
            if low <= zoom <= high:
                try:   # MBTiles counts rows from the south
                    row = db.execute('SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?',
                                     (zoom, x, (1 << zoom) - 1 - y)).fetchone()
                except sqlite3.Error:
                    continue
                if row and row[0]:
                    return row[0]
        return None

    def covers(self, lat, lon):
        """Whether a street-level map of this point is installed."""
        return any(self._blob(high, *_tile_xy(lat, lon, high)) is not None for high in {f[2] for f in self.files})

    def _tile(self, zoom, x, y):
        key = (zoom, x, y)
        if key not in self.tiles:
            blob = self._blob(zoom, x, y)
            if blob is None:
                self.tiles[key] = None
            else:
                try:
                    found = pois(blob, self.wanted)
                except (ValueError, IndexError, TypeError, zlib.error):
                    found = []
                self.tiles[key] = [(kind, name, round(_lon(x + px, zoom), 6), round(_lat(y + py, zoom), 6)) for kind, name, px, py in found]
        return self.tiles[key]

    def box(self, lat, lon, cls, km, inner=0.0):
        """The points of one kind in the box reaching `km` north, south, east and west of the place, which holds every
        point within km of it, leaving out the box of `inner` km that an earlier, smaller call read: [(cls, name, lon,
        lat)]. Out to FULL_KM that is every such point on the map; farther out, the ones the map shows from a lower zoom."""
        kinds = tile_kinds(cls)
        if not self.files or not set(kinds) <= self.wanted:
            return []
        deepest = max(f[2] for f in self.files)
        zoom = max(min(f[1] for f in self.files), deepest - max(0, math.ceil(math.log2(km / FULL_KM) - 1e-9)))
        n = 1 << zoom
        dlat, dlon = _span(lat, km)
        ilat, ilon = _span(lat, inner) if inner else (-1.0, -1.0)
        if dlon >= 180:
            columns = range(n)
        else:
            x0, x1 = math.floor((lon - dlon + 180) / 360 * n), math.floor((lon + dlon + 180) / 360 * n)
            columns = range(n) if x1 - x0 + 1 >= n else [x % n for x in range(x0, x1 + 1)]   # past +-180: the other side
        (_, y0), (_, y1) = _tile_xy(lat + dlat, lon, zoom), _tile_xy(lat - dlat, lon, zoom)
        out = []
        for x in columns:
            west = _wrap(_lon(x, zoom) - lon)
            for y in range(y0, y1 + 1):
                if -ilon <= west and west + 360 / n <= ilon and lat - ilat <= _lat(y + 1, zoom) and _lat(y, zoom) <= lat + ilat:
                    continue   # wholly inside what the earlier call read
                for kind, name, px, py in self._tile(zoom, x, y) or ():
                    dx, dy = abs(_wrap(px - lon)), abs(py - lat)
                    if kind in kinds and dx <= dlon and dy <= dlat and not (dx <= ilon and dy <= ilat):
                        out.append((cls, name, px, py))
        return out


def _wrap(d):
    """A difference of longitudes, -180..180: the short way round."""
    return (d + 180) % 360 - 180


def _span(lat, km):
    """(degrees of latitude, degrees of longitude) that a circle of km around a point at lat reaches north and east."""
    a = km / EARTH_KM
    s = math.sin(a) / max(math.cos(math.radians(lat)), 1e-9)
    return math.degrees(a), 180.0 if a >= math.pi / 2 or s >= 1 else math.degrees(math.asin(s))
