"""Where this computer seems to be, from what Omarchy already knows, and the street-level map to offer for it.

Nothing here reaches the network, and nothing is measured: it reads the weather location the person set in Omarchy
(~/.local/state/omarchy/settings/weather.json: exact), else the town they gave Lastbook when it asked (setup.json's
`places`, one for each time zone they have been asked in), else the system's time zone (/etc/localtime: an area,
often several regions wide), and whether the computer has a route out (the kernel's routing table: nothing is sent).

offer() is what setup, the window and the command line show: None, or the place and the regions whose streets would cover
it, when no installed street-level map does, the computer is online and the person has not switched the offer off
(`map_offer: false` in the app's setup.json; LASTBOOK_MAP_OFFER=0 for one run).
"""
import contextlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path

from . import download, maps

WEATHER = Path('.local/state/omarchy/settings/weather.json')    # under the home folder: omarchy-weather-location
LOCALTIME = Path('/etc/localtime')
ZONEINFO = Path('/usr/share/zoneinfo')
ROUTES = (Path('/proc/net/route'), Path('/proc/net/ipv6_route'))
NEAREST = 6      # a time zone names no region: this many are offered, those around its main city
QUIET_KM = 400   # ... and a map installed within this far of that city may well be the one: nothing is offered


def weather_place(home=None):
    """{name, lat, lon} of the weather location set in Omarchy, when it has coordinates; else None."""
    try:
        w = json.loads((Path(home or Path.home()) / WEATHER).read_text(encoding='utf-8'))
        lat, lon = float(w['latitude']), float(w['longitude'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {'name': str(w.get('name') or f'{lat:.3f},{lon:.3f}')[:80], 'lat': lat, 'lon': lon}


def _degrees(text):
    """ISO 6709 as zone1970.tab has it: ±DDMM[SS]±DDDMM[SS] -> (lat, lon)."""
    m = re.fullmatch(r'([+-])(\d\d)(\d\d)(\d\d)?([+-])(\d\d\d)(\d\d)(\d\d)?', text)
    if not m:
        raise ValueError(text)
    lat = (int(m[2]) + int(m[3]) / 60 + int(m[4] or 0) / 3600) * (-1 if m[1] == '-' else 1)
    lon = (int(m[6]) + int(m[7]) / 60 + int(m[8] or 0) / 3600) * (-1 if m[5] == '-' else 1)
    return lat, lon


def zone_place(localtime=LOCALTIME, zoneinfo=ZONEINFO):
    """{zone, name, area, lat, lon} of the system's time zone: its main city, and what the zone covers in tzdata's
    words ('' where the zone is its whole country). None for UTC, an unknown zone or no tzdata."""
    try:
        zone = os.path.realpath(localtime).split('/zoneinfo/', 1)[1]
        for line in (Path(zoneinfo) / 'zone1970.tab').read_text(encoding='utf-8').splitlines():
            cols = line.split('\t')
            if not line.startswith('#') and len(cols) >= 3 and cols[2] == zone:
                lat, lon = _degrees(cols[1])
                return {'zone': zone, 'name': zone.rsplit('/', 1)[-1].replace('_', ' '), 'area': cols[3] if len(cols) > 3 else '',
                        'lat': lat, 'lon': lon}
    except (OSError, ValueError, IndexError):
        pass
    return None


def own_place(settings_dir, zone=None):
    """{name, detail, lat, lon} of the town the person gave Lastbook for the time zone the computer is in now
    (setup.json's `places`, one a time zone; `detail`: its "region, country" in place search). A new time zone means
    they have travelled: the town given there is used, and the others are kept for when they are back."""
    s = download.read_json(Path(settings_dir) / 'setup.json', {})
    try:
        p = s['places'][(zone or zone_place())['zone']]
        lat, lon = float(p['lat']), float(p['lon'])
        return {'name': str(p['name'])[:80], 'detail': str(p.get('detail') or '')[:120], 'lat': lat, 'lon': lon} if -90 <= lat <= 90 and -180 <= lon <= 180 else None
    except (KeyError, TypeError, ValueError):
        return None


def online(routes=ROUTES):
    """Whether the computer has a default route (IPv4 or IPv6): read from the kernel, nothing is sent."""
    v4, v6 = routes
    try:
        for line in v4.read_text().splitlines()[1:]:
            cols = line.split()
            if len(cols) > 3 and cols[1] == '00000000' and int(cols[3], 16) & 1:   # destination 0.0.0.0, up
                return True
    except (OSError, ValueError):
        pass
    try:
        for line in v6.read_text().splitlines():
            cols = line.split()
            if len(cols) > 9 and cols[0] == '0' * 32 and cols[1] == '00' and cols[9] != 'lo':   # ::/0, not the loopback's
                return True
    except OSError:
        pass
    return False


def _km(lat, lon, r):
    """From a point to a region's nearest box: 0 inside it. Near enough to put regions in order."""
    def one(box):
        w, s, e, n = box
        dy = max(s - lat, 0, lat - n)
        dx = max(w - lon, 0, lon - e) * math.cos(math.radians(lat))
        return math.hypot(dy, dx) * 111
    return min(one(b) for b in maps.region_boxes(r))


def _candidates(zone):
    """The regions a time zone may mean, with how far each is from the zone's main city: the offered regions around
    it, those that hold it first (the smallest first), then the nearest."""
    near = [(_km(zone['lat'], zone['lon'], r), r) for r in maps.regions() if r['offered']]
    return sorted((x for x in near if x[0] <= QUIET_KM), key=lambda x: (round(x[0]), maps.street_tiles(x[1])))


def _address(geo, lat, lon):
    """The "region, country" of the nearest town (maps.nearest_town); '' without a gazetteer or a town near."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{Path(geo) / maps.PLACES_FILE}?mode=ro", uri=True)) as db:
            town = maps.nearest_town(db, lat, lon)
    except sqlite3.Error:
        return ''
    return town[1] or '' if town else ''


def asking(settings_dir):
    """Whether the offer is on: it is, until the person switches it off."""
    if os.environ.get('LASTBOOK_MAP_OFFER') == '0':     # for a script, and the tests
        return False
    s = download.read_json(Path(settings_dir) / 'setup.json', {})
    return not isinstance(s, dict) or s.get('map_offer') is not False


def offer(settings_dir, geo=None, home=None, is_online=None, zone=None):
    """The street-level map to offer, or None: for a computer that is online, somewhere no installed street-level map
    covers, whose owner has the offer on. `geo`: the open library's derived/geo (None in first-run setup, where nothing
    is installed).

    {place, source, exact, area, pick, regions: [{id, name}], text}. `source`: 'weather location', 'town you gave' or
    'time zone'. `exact`: the place is a point, so `regions` are those that cover it and `pick` is the one to choose
    for the person, when that is sure; a time zone is an area (`area`, in tzdata's words), so `regions` are the
    nearest few to choose from. `text`: the sentence a command passes on to an agent."""
    if is_online is None:
        is_online = os.environ.get('LASTBOOK_MAP_OFFER') == '1' or online()   # =1: taken to be online, for the QA scripts
    if not asking(settings_dir) or not is_online:
        return None
    have = {r['id'] for r in maps.installed(geo)['regions']} if geo else set()
    source = 'weather location'
    at = weather_place(home)
    if not at and (at := own_place(settings_dir, zone)):
        source = 'town you gave'
    if at:
        if geo:
            from engine import map_pois
            with map_pois.Regions(geo) as installed:
                if installed.covers(at['lat'], at['lon']):
                    return None
        address = at.get('detail') or (_address(geo, at['lat'], at['lon']) if geo else '')
        regions = [r for r in maps.covering(at['lat'], at['lon'], address) if r['id'] not in have][:3]
        place, area = at['name'], ''
        # The one to pick for them: the region the nearest town's address names, or the only one there is.
        named = [r for r in regions if maps._words(r['name'].split(' (')[0]) in maps._words(address)] if address else []
        pick = (named or regions)[0]['id'] if named or len(regions) == 1 else None
    else:
        zone = zone or zone_place()
        source = 'time zone'
        if not zone:
            return None
        around = [r for _, r in _candidates(zone)]
        if any(r['id'] in have for r in around):
            return None     # a map of that part of the world is here: a time zone cannot say more
        regions = around[:NEAREST]
        place, area, pick = zone['name'], zone['area'], None
    if not regions:
        return None
    names = ', '.join(r['name'] for r in regions)
    said = {'weather location': "from Omarchy's weather location", 'time zone': "from Omarchy's time zone", 'town you gave': 'the town its owner gave'}[source]
    text = (f"No street-level map is installed for where this computer seems to be ({place}, {said}"
            + (f': {area}' if area else '') + f"), so nearby help cannot be looked up there. The owner can add one in Lastbook's "
            f"Library and downloads (lastbook open manage:): {names}. Tell them once. They can switch this note off there.")
    return {'place': place, 'source': source, 'exact': bool(at), 'area': area, 'pick': pick,
            'regions': [{'id': r['id'], 'name': r['name']} for r in regions], 'text': text}
