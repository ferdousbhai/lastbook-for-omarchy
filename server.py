#!/usr/bin/env python3
"""The engine's request handler: the answers the native window asks for, over engine/library.py's file, ZIM and video serving.

Nothing here opens a port. app/engine_pipe.py calls init() and runs Handler through app/bridge.py, one request at a time,
over in-memory streams. The routes the window uses:

  /api/topics                 curated needs and learning routes, with source details resolved from catalog.json
  /api/find?q=&kind=          plain-language search (synonyms, grouping by document, trusted-source ranking)
  /api/retrieve?q=&k=&kind=   the best passages (keyword, meaning-based and encyclopedia lists, fused), with where each came from
  /api/doc/<id>               one catalog record
  /api/text/<id>              a document's extracted text, one string per page (the reader's find in page)
  /api/related?doc=|path=     up to five other documents on the same subject (the readers' Related)
  /api/search-status          where meaning search stands, as `lastbook status` reports it (the Search screen's line)
  /api/transcript/<video id>  timed transcript cues
  /api/videos, /api/videos/all?q=  the Videos screen's rows, and a search of every video
  /api/shelves, /api/shelf/<id>    browse the whole collection by plain-language shelf
  /api/places?q=              place finder (the world's countries, regions and towns from GeoNames, or a point as LAT,LON)
  /api/near?lat=&lon=[&to=LAT,LON]  the Map screen's nearest help around a point (or, where no street map covers it, the
                              regions that would), or how far another point is from it
  /api/encyclopedias, /api/encyclopedia/random?file=  the encyclopedias with their front pages, and a random article
  /api/library, /api/setup/*

The command line (app/cli.py) calls the helpers here directly (hybrid, places, near, page_texts, transcript).
Nothing in library-data is written. Caches go to derived/cache.
"""
import contextlib, functools, html, io, json, math, os, re, sqlite3, struct, sys, threading, traceback, unicodedata, urllib.parse, zlib
from pathlib import Path

import engine.paths as lastbook_paths  # noqa: E402  (data folder and program folder can differ in the packaged app)
from engine.paths import ro_uri  # noqa: E402
from engine import saved_pages, map_pois  # noqa: E402
from engine.setup import api as setup_api  # noqa: E402  (the one module that may download: only when the person asks)
from engine.setup import maps as setup_maps  # noqa: E402  (the street-level regions setup offers, with their boxes)
HERE = lastbook_paths.DATA  # holds derived/
APP = lastbook_paths.APP  # holds content/
LIB = lastbook_paths.LIB
CACHE = HERE / 'derived' / 'cache'
WORLD_PLACEDB = HERE / 'derived' / 'geo' / 'world-places.sqlite'   # GeoNames: countries, regions, places of 500+ (engine/setup/maps.py builds it in setup)
import engine.library as library  # noqa: E402  (the Handler base, the search index's path, the ZIM reader)
import retrieval, encyclopedia  # noqa: E402
INDEX = None  # retrieval.Index, created in init()
ENC = None    # encyclopedia.Encyclopedias (ZIM articles as passages), created in init()

_pdf_lock = threading.Lock()  # PDFium is not thread-safe

# ---------------------------------------------------------------- catalog ----
_cat = {'mtime': 0, 'by_id': {}, 'by_path': {}, 'video_tx': {}, 'thumbs': {}}
_cat_lock = threading.Lock()


_excl = {'mtime': None, 'ids': set()}


def excluded_videos():
    """Ids of the videos the library owner rejected (library-data/video-exclusions.json), and of their details and transcript records."""
    p = LIB / 'video-exclusions.json'
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        if _excl['mtime'] is not None:      # the list was there and has been removed: its videos come back
            _excl.update(mtime=None, ids=set())
            _cat['mtime'] = 0
        return set()
    if m != _excl['mtime']:
        try:
            ids = set(json.loads(p.read_text(encoding='utf-8')).get('videos', {}))
            _excl.update(mtime=m, ids=ids | {'metadata-' + i for i in ids} | {i.replace('video-', 'transcript-', 1) for i in ids})
            _cat['mtime'] = 0  # re-filter the catalog with the new list
        except (ValueError, OSError):
            pass  # half-written file: keep the previous list
    return _excl['ids']


class LibraryError(Exception):
    """The library itself cannot be read (catalog or keyword index damaged): said as it is, never as "no results"."""


def catalog():
    excluded_videos()
    p = LIB / 'catalog.json'
    m = p.stat().st_mtime
    if m != _cat['mtime']:
        with _cat_lock:
            if m != _cat['mtime']:
                try:
                    recs = json.loads(p.read_text(encoding='utf-8'))
                    if not isinstance(recs, list) or not all(isinstance(r, dict) for r in recs):
                        raise ValueError('not a list of records')
                except ValueError:  # being rewritten right now: keep the old copy
                    if not _cat.get('loaded'):  # there is none (a process that just started): an empty library would be a lie
                        raise LibraryError('The library catalog (library-data/catalog.json) cannot be read: it is damaged or incomplete.') from None
                    return _cat
                excl = excluded_videos()
                if excl:  # rejected videos never appear, even if a catalog rebuild brings them back
                    bad_paths = {r['path'] for r in recs if r['id'] in excl}
                    recs = [r for r in recs if r['id'] not in excl and r.get('video_id') not in excl
                            and not (r.get('video_path') and r['video_path'] in bad_paths)]
                by_id = {r['id']: r for r in recs}
                tx, thumbs = {}, {}
                for r in recs:
                    if r['kind'] == 'transcript' and r.get('video_path'):
                        tx[r['video_path']] = r
                    if r['kind'] == 'metadata' and r.get('video_path') and r.get('thumbnail_path'):
                        thumbs[r['video_path']] = r['thumbnail_path']
                links = {r[k]: r['path'] for r in recs for k in ('source_url', 'final_url') if r.get(k) and isinstance(r[k], str)}   # saved_pages.rewrite
                _cat.update(loaded=True, mtime=m, by_id=by_id, by_path={r['path']: r for r in recs}, video_tx=tx, thumbs=thumbs, links=links)
    return _cat


TRUST = [  # (pattern on publisher+collection, label shown to people, ranking weight)
    (r'World Health|WHO\b|ICRC|IFRC|Red Cross|Red Crescent|Médecins|Medecins|MSF|UNICEF|UNHCR|United Nations|FAO\b|PAHO|Sphere|St John Ambulance|CAWST', 'International health & humanitarian guidance', 1.5),
    (r'Hesperian|Where There Is No|Where Women|Book for Midwives|Community Guide to Environmental|Health Actions|Workers\' Guide', 'Hesperian community health guide', 1.45),
    (r'FEMA|Ready\.gov|CDC|Centers for Disease|EPA|Environmental Protection|NOAA|National Weather|USDA|Food Safety|National Park|USGS|Department|Agency|U\.S\.|US Army|Army|Navy|Census|NIOSH|OSHA|NASA|FAA|Cal OES|County of|City of|Government|Ministry|Minist[eè]r|Directorate|Bureau of|Civil Protection|Public Health|Health Service|NHS\b|Federal', 'Government guidance', 1.4),
    (r'National Center for Home Food Preservation|Extension|University|UC |College|Agricultural|Food Hero|UWyo', 'University / extension service', 1.35),
    (r'MIT|OpenStax|OpenCourseWare|NIST', 'Course or textbook', 1.1),
    (r'Kiwix|Wikipedia|Wikibooks|Appropedia|MDWiki', 'Community-written encyclopedia', 1.0),
    (r'Coalcracker|Corporals Corner|Canterbury|Primitive Technology|creator-', 'Hobbyist demonstration video', 0.8),
]


def trust(r):
    s = f"{r.get('publisher', '')} {r.get('collection', '')} {r.get('category', '')}"
    for rx, label, w in TRUST:
        if re.search(rx, s):
            return label, w
    if r.get('equipment_class') or r.get('model'):
        return 'Manufacturer manual (model-specific)', 1.05
    return 'Reference', 1.0


def kind_label(r):
    k = r['kind']
    return {'pdf': 'PDF', 'html': 'Saved web page', 'video': 'Video', 'transcript': 'Transcript', 'pptx': 'Slides',
            'zim': 'Encyclopedia', 'metadata': 'Video details'}.get(k, k)


def year_of(r):
    for k in ('edition', 'publication_date', 'upload_date'):
        v = str(r.get(k) or '')
        m = re.search(r'(19|20)\d\d', v)
        if m:
            return m.group(0)
    m = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', r.get('title', ''))
    return m.group(0) if m else None


def public(r):
    """The fields the window needs, with friendly labels. Never includes local auth or worker state."""
    if not r:
        return None
    t, _ = trust(r)
    out = {'id': r['id'], 'title': clean_title(r), 'kind': r['kind'], 'kindLabel': kind_label(r),
           'publisher': r.get('publisher'), 'path': r['path'],
           'pages': r.get('pages') or r.get('slides'), 'duration': r.get('duration'), 'year': year_of(r), 'trust': t}
    if r['kind'] == 'video':
        th = catalog()['thumbs'].get(r['path'])
        out['thumb'] = th if th and (LIB / th).is_file() else None
    return out


def clean_title(r):
    t = r['title']
    t = re.sub(r'^(\d{3}(\s\d+)?)\s+', '', t)          # Hesperian chapter file numbers
    t = re.sub(r'\.pdf$', '', t, flags=re.I)
    if re.fullmatch(r'[\w\-]+', t) and '_' in t:
        t = t.replace('_', ' ')
    return t


# ------------------------------------------------------------------ topics ----
# The guides in the order they are listed: the most urgent first, then those to read ahead. One not named here goes last.
TOPIC_ORDER = ('hurt', 'disaster', 'water', 'shelter', 'food', 'power', 'sanitation', 'comms',
               'health', 'grow', 'preserve', 'repair', 'energy', 'land')


def topics():
    out = []
    rank = {name: i for i, name in enumerate(TOPIC_ORDER)}
    for f in sorted((APP / 'content' / 'topics').glob('*.json'), key=lambda f: (rank.get(f.stem, len(rank)), f.stem)):
        try:
            t = json.loads(f.read_text(encoding='utf-8'))
        except ValueError as e:
            out.append({'id': f.stem, 'error': str(e)})
            continue
        out.append(t)
    return out


def zim_names():
    return encyclopedia.zim_names(LIB)


def item_here(i, zims):
    """Is what this guide item opens in this library? An item that opens nothing is hidden, not sent Home with no message."""
    if i.get('zim'):
        parts = i['zim'].split('/')
        return len(parts) > 1 and parts[1] in zims
    return not i.get('doc') or i['doc'] in catalog()['by_id']


def resolve_item(i):
    i = dict(i)
    if i.get('doc'):
        r = catalog()['by_id'].get(i['doc'])
        i['src'] = public(r)
    return i


def topics_resolved():
    res = []
    try:
        gaps = json.loads((APP / 'content' / 'gaps.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        gaps = {}
    zims = zim_names()

    def here(items):
        return [resolve_item(i) for i in items if item_here(i, zims)]

    def still_useful(step):  # one that had things to open and has none left
        return step.get('goto') or step['read'] or not step['had']

    for t in topics():
        t = dict(t)
        t['gaps'] = gaps.get(t['id'], {})
        for k in ('prepare', 'learn'):
            t[k] = here(t.get(k, []))
        # A warning stands without its source. It only loses the citation that would open nothing.
        t['limits'] = [resolve_item(i) if item_here(i, zims) else {'text': i.get('text')} for i in t.get('limits', [])]
        for k in ('choices', 'route'):
            steps = [dict(c, read=here(c.get('read', [])), had=bool(c.get('read'))) for c in t.get(k, [])]
            t[k] = [{a: b for a, b in c.items() if a != 'had'} for c in steps if still_useful(c)]
        res.append(t)
    return res


# ------------------------------------------------------------------ search ----
STOP = set('a an and are as at be but by can do does for from get got have help how i if in into is it its me my need '
           'no not of on or our please should so some that the their them then there these they this to up us was we '
           'what when where which who why will with without you your'.split())
# Everyday words -> words the manuals actually use. Each group is OR-ed; groups are AND-ed.
SYN = {
    'cut': ['wound', 'wounds', 'laceration', 'bleeding'], 'cuts': ['wound', 'wounds', 'laceration'],
    'bleed': ['bleeding', 'haemorrhage', 'hemorrhage'], 'bleeding': ['haemorrhage', 'hemorrhage', 'blood loss'],
    'blood': ['bleeding'], 'burn': ['burns', 'scald'], 'burned': ['burns'], 'burnt': ['burns'],
    'broken': ['fracture', 'fractures'], 'bone': ['fracture', 'bones'], 'sprain': ['sprains', 'strain', 'twisted'],
    'poop': ['faeces', 'feces', 'excreta', 'latrine'], 'pee': ['urine'], 'toilet': ['latrine', 'toilets', 'sanitation', 'excreta'],
    'sewage': ['sewer', 'wastewater', 'excreta'], 'trash': ['garbage', 'refuse', 'waste', 'rubbish'], 'garbage': ['refuse', 'waste', 'rubbish'],
    'purify': ['purification', 'disinfect', 'disinfection', 'treatment'], 'clean': ['disinfect', 'safe'],
    'drinking': ['potable', 'drinking'], 'filter': ['filtration', 'filters'], 'bleach': ['chlorine', 'hypochlorite'],
    'blackout': ['outage', 'power outage'], 'outage': ['blackout'], 'electricity': ['electric', 'power'],
    'power': ['electricity', 'outage'], 'light': ['lighting', 'flashlight', 'lantern'], 'flashlight': ['torch', 'lantern'],
    'generator': ['generators'], 'solar': ['photovoltaic', 'pv'], 'battery': ['batteries'],
    'cook': ['cooking', 'stove'], 'cooking': ['stove', 'cook'], 'stove': ['stoves', 'cooking'], 'fridge': ['refrigerator', 'refrigerated'],
    'spoiled': ['spoilage', 'unsafe food'], 'rotten': ['spoilage', 'spoiled'],
    'canning': ['canned', 'canner'], 'pickle': ['pickling', 'pickled'],
    'cold': ['hypothermia', 'cold weather', 'winter'], 'freezing': ['hypothermia', 'frostbite'], 'hot': ['heat'],
    'heat': ['heat stroke', 'heat exhaustion', 'hyperthermia', 'extreme heat'], 'heatstroke': ['heat stroke'],
    'smoke': ['air quality', 'wildfire smoke'], 'quake': ['earthquake'], 'earthquake': ['earthquakes', 'seismic'],
    'flood': ['flooding', 'floods'], 'tsunami': ['tsunamis'], 'fire': ['fires', 'wildfire'], 'wildfire': ['wildfires', 'fire'],
    'baby': ['newborn', 'infant', 'babies'], 'pregnant': ['pregnancy'], 'birth': ['childbirth', 'labor', 'delivery'],
    'kid': ['child', 'children'], 'kids': ['children', 'child'], 'old': ['elderly', 'older'],
    'fever': ['temperature', 'fevers'], 'diarrhea': ['diarrhoea', 'dehydration'], 'diarrhoea': ['diarrhea'],
    'vomit': ['vomiting'], 'puke': ['vomiting'], 'sick': ['illness', 'disease'], 'medicine': ['medicines', 'drugs', 'medication'],
    'pills': ['medicines', 'tablets', 'medication'], 'painkiller': ['analgesic', 'paracetamol', 'pain'],
    'heart': ['cardiac', 'heart attack'], 'cpr': ['resuscitation', 'chest compressions'], 'choking': ['choke', 'airway'],
    'unconscious': ['unresponsive', 'recovery position'], 'bite': ['bites'], 'snake': ['snakebite', 'snakes'],
    'sting': ['stings'], 'allergic': ['allergy', 'anaphylaxis'], 'infection': ['infected', 'infections'],
    'teeth': ['tooth', 'dental'], 'tooth': ['teeth', 'dental'], 'stress': ['anxiety', 'mental health'], 'depressed': ['depression'],
    'grow': ['growing', 'planting', 'garden'], 'garden': ['gardening', 'vegetable'], 'seeds': ['seed', 'seed saving'],
    'plant': ['planting', 'sowing'], 'soil': ['compost', 'soils'], 'potato': ['potatoes'], 'potatoes': ['potato'],
    'chickens': ['poultry', 'hens'], 'chicken': ['poultry'], 'goat': ['goats'], 'bees': ['beekeeping', 'honey bee'],
    'radio': ['radios', 'amateur radio', 'ham'], 'ham': ['amateur radio'], 'phone': ['telephone', 'cell phone', 'mobile'],
    'map': ['maps', 'navigation'], 'compass': ['navigation', 'bearing'], 'lost': ['navigation', 'lost'],
    'shelter': ['shelters', 'sheltering'], 'tent': ['tents', 'tarp', 'shelter'], 'warm': ['warmth', 'heating', 'insulation'],
    'fix': ['repair', 'repairs'], 'repair': ['repairs', 'maintenance'], 'engine': ['engines', 'small engine'],
    'weld': ['welding'], 'wood': ['woodworking', 'timber', 'lumber'], 'knife': ['knives', 'sharpening'], 'sharpen': ['sharpening'],
    'knot': ['knots'], 'rope': ['ropes', 'knots'], 'pump': ['pumps'], 'well': ['wells', 'borehole'],
    'rain': ['rainwater'], 'gas': ['natural gas', 'propane', 'gas leak'], 'leak': ['leaks', 'leaking'],
    'carbon': ['monoxide'], 'co': ['carbon monoxide'], 'mold': ['mould'], 'rats': ['rodents', 'rodent'], 'mosquito': ['mosquitoes'],
}
CLINICAL = re.compile(r'surg|clinical|district hospital|essential drugs|tuberculosis|obstetric|cholera epidemic|measles epidemic|anaesth|war surgery', re.I)


# Collections whose name says "clinical" but which are written for people with no clinician at hand (Hesperian's Where
# There Is No Doctor / Dentist, A Book for Midwives): general guidance, never put after it.
LAY_CARE = {'low-resource clinical care'}


def for_clinicians(r):
    """Whether a record is a surgeon's or clinician's text, by its title or its collection's name."""
    collection = r.get('collection', '')
    return bool(CLINICAL.search(r['title'] + ' ' + ('' if collection.lower() in LAY_CARE else collection)))


GENERAL = re.compile(r'first aid|emergency|basic|household|home|beginner|guide|handbook|where there is no', re.I)
KIND_FILTERS = {'read': retrieval.PAGED, 'video': ('video', 'transcript'), 'all': None}


fts_expr = encyclopedia.match_expr


PHRASES = [  # everyday phrases -> one idea, checked before single words
    (r"power (cut|outage|failure)|no (power|electricity)|lights? (are )?out|blackout", ['outage', 'blackout', 'power outage']),
    (r"(phone|cell|mobile)( is| was)? (dead|flat|out of battery)|(dead|flat) (phone|battery)", [['phone', 'phones', 'cell phone'], ['charge', 'charging', 'charger', 'power bank']]),
    (r"(water|tap) (is )?(off|not working|stopped)|no (running )?water", ['water']),
    (r"toilet (is )?(not working|won'?t flush|broken)|can'?t flush", ['latrine', 'toilet', 'excreta']),
    (r"heart attack", ['heart attack', 'chest pain']),
    (r"(passed|blacked) out|fainted", ['unconscious', 'fainting', 'unresponsive']),
    (r"food (went|gone|is) bad|spoiled food", ['spoilage', 'food safety']),
    (r"can'?t breathe|not breathing|stopped breathing", ['breathing', 'resuscitation', 'airway']),
]


def query_groups(q):
    q = q.lower().replace('\u2019', "'")
    phrase_groups = []
    for rx, g in PHRASES:
        if re.search(rx, q):
            phrase_groups += g if isinstance(g[0], list) else [g]
            q = re.sub(rx, ' ', q)
    words = [w.lower() for w in re.findall(r"[\w'-]+", q)]
    words = [w.strip("'") for w in words if w.strip("'")]
    content = [w for w in words if w not in STOP] or ([] if phrase_groups else words)
    groups = []
    for w in content:
        g = [w] + SYN.get(w, [])
        if w.endswith('s') and len(w) > 4 and w[:-1] not in g:
            g.append(w[:-1])
        groups.append(g)
    groups = phrase_groups + groups
    content = [g[0] for g in phrase_groups] + content
    return content, groups


_fts_tls = threading.local()


def fts_rows(groups, limit, marks=('', ''), tokens=40, relax_below=10):
    """Rows from the keyword index: every idea in groups must match; with fewer than relax_below rows, any idea
    may (documents with more of them still rank higher). Rows are (doc_id, page, snippet, bm25), best first."""
    con = _fts_con()
    sql = (f"SELECT doc_id,page,snippet(pages,6,?,?,' … ',{int(tokens)}),bm25(pages,0,6,1,1,0,0,1) "
           "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages,0,6,1,1,0,0,1) LIMIT ?")
    try:
        rows = con.execute(sql, (*marks, fts_expr(groups), limit)).fetchall()
        if len(rows) < relax_below and len(groups) > 1:
            return rows + con.execute(sql, (*marks, ' OR '.join('(' + fts_expr([g]) + ')' for g in groups), limit)).fetchall()
    except sqlite3.OperationalError as e:
        if re.search(r'fts5: syntax error|unterminated string|no such column|unknown special query', str(e)):
            return []  # a question FTS5 cannot parse matches nothing
        # An index with no table or one that cannot be read is not "no results": the library is damaged, and says so.
        raise LibraryError(f'The keyword index (library-data/search.sqlite) cannot be searched: {e}.') from None
    except sqlite3.DatabaseError as e:
        raise LibraryError(f'The keyword index (library-data/search.sqlite) cannot be read: {e}.') from None
    return rows


def _fts_con():
    """This thread's read-only connection to the keyword index, the pack's own search.sqlite."""
    con = getattr(_fts_tls, 'con', None)
    if con is None:
        con = _fts_tls.con = sqlite3.connect(ro_uri(library.DB), uri=True, check_same_thread=False)
    return con


def find(q, kind='all', limit=30):
    catalog()  # the cache below is per catalog version (which includes video exclusions)
    return _find(q, kind, limit, _cat['mtime'], _excl['mtime'])


@functools.lru_cache(maxsize=256)
def _find(q, kind, limit, _cat_version, _excl_version):
    groups = query_groups(q)[1]
    if not groups:
        return {'q': q, 'results': []}
    rows = fts_rows(groups, 600, ('\u0001', '\u0002'), 28, relax_below=8)
    full = catalog()
    cat, by_path = full['by_id'], full['by_path']
    docs = {}
    allowed = KIND_FILTERS.get(kind)
    for doc_id, page, snip, score in rows:
        r = cat.get(doc_id)
        if not r or r['kind'] == 'metadata':
            continue
        if r['kind'] == 'transcript':  # show the video itself, with the transcript hit
            v = by_path.get(r.get('video_path'))
            if v:
                r, page = v, None
        if allowed and r['kind'] not in allowed:
            continue
        d = docs.get(r['id'])
        if not d:
            d = docs[r['id']] = {'r': r, 'score': 0.0, 'hits': []}
        if len(d['hits']) < 3 and not any(h['page'] == page for h in d['hits']):
            d['hits'].append({'page': page if r['kind'] == 'pdf' else None, 'snippet': snip})
        d['n'] = d.get('n', 0) + 1
        if d['n'] == 1:
            d['score'] = -score
        elif d['n'] <= 4:
            d['score'] += -score * 0.1  # a little credit for a few more matching pages, not for sheer length
    out = []
    for d in docs.values():
        r = d['r']
        t, w = trust(r)
        title = r['title'].lower()
        tb = sum(1 for g in groups if any(x in title for x in g)) / len(groups)
        s = d['score'] * w * (1 + tb)
        if re.search(r'Spanish|español|\bkit de\b', r['title'] + r.get('notes', ''), re.I):
            s *= 0.5
        if for_clinicians(r) and not CLINICAL.search(q):
            s *= 0.6  # written for surgeons/clinicians; still found, but after general guidance
        if re.search(r'^index$|front matter|table of contents|credits|^contents', clean_title(r), re.I):
            s *= 0.3  # index pages match everything but rarely answer anything
        if GENERAL.search(r['title']):
            s *= 1.25
        out.append((s, d))
    out.sort(key=lambda x: -x[0])
    results = []
    for s, d in out[:limit]:
        p = public(d['r'])
        p['hits'] = [{'page': h['page'], 'snippet': clean_snip(h['snippet'])} for h in d['hits']]
        results.append(p)
    return {'q': q, 'results': results}


def clean_snip(s):
    s = html.escape(re.sub(r'\s+', ' ', s or '')).replace('\u0001', '<mark>').replace('\u0002', '</mark>')
    return s


# --------------------------------------------------------------- passages ----
def _fts(q, limit=80):
    """(doc_id, page, snippet) from the keyword index, best first."""
    _, groups = query_groups(q)
    return [row[:3] for row in fts_rows(groups, limit)] if groups else []


def _src(r, page=None, t=None, excerpt='', excerpt_len=600, **extra):
    """A passage of catalog record r with where it came from: what search results and `lastbook search` carry."""
    pub = public(r)
    d = {'doc_id': r['id'], 'title': pub['title'], 'publisher': pub['publisher'], 'year': pub['year'], 'kind': r['kind'],
         'trust': pub['trust'], 'pages': pub['pages'], 'page': page if r['kind'] == 'pdf' else None, 't': t,
         'excerpt': re.sub(r'\s+', ' ', excerpt or '')[:excerpt_len], 'notes': (r.get('notes') or '')[:300]}
    if r['kind'] == 'video':
        d.update(duration=pub.get('duration'), thumb=pub.get('thumb'))
    elif r['kind'] == 'article':
        d.update(wiki=r['path'].removeprefix('zim/'), encyclopedia=r.get('encyclopedia'))
    d.update(extra)
    return d


def record(doc_id):
    """The catalog record for a library document, or the record of an encyclopedia article (zim:<file>/<path>)."""
    r = catalog()['by_id'].get(doc_id)
    if r is None and ENC and doc_id.startswith(encyclopedia.ARTICLE_PREFIX):
        r = ENC.record(doc_id)
    return r


def _allowed(kind):
    cat = catalog()['by_id']
    # 'library': everything but the encyclopedias, which take a second longer (the window asks for them separately)
    ok = {'any': None, 'library': None, 'manual': KIND_FILTERS['read'], 'video': KIND_FILTERS['video'], 'encyclopedia': ()}.get(kind or 'any')
    return lambda d: d in cat and cat[d]['kind'] not in ('metadata', 'zim') and (ok is None or cat[d]['kind'] in ok)


def hybrid(q, k=8, kind='any'):
    """Best passages for q from keyword and meaning-based search, fused, with provenance."""
    cat = catalog()
    allowed = _allowed(kind)
    qv = INDEX.query_vector(q)  # once: each embedding is a round trip to Recall
    moments = {}

    def moment(video_id):
        """(seconds, passage) of the video's best moment for the question by meaning; (None, None) without one."""
        if video_id not in moments:
            best = INDEX.semantic(qv, 1, doc=video_id) if qv is not None else []
            moments[video_id] = (best[0]['t'], best[0]['text']) if best else (None, None)
        return moments[video_id]
    kw = []
    for doc_id, page, snip in _fts(q, 120):
        r = cat['by_id'].get(doc_id)
        if not r:
            continue
        if r['kind'] == 'transcript':  # credit the video itself
            v = cat['by_path'].get(r.get('video_path'))
            if not v:
                continue
            doc_id, page = v['id'], None
        if not allowed(doc_id):
            continue
        t = None
        if cat['by_id'][doc_id]['kind'] == 'video':
            # The index knows which video, not when: take the video's best moment now, so that this hit and the same
            # moment found by meaning are one result (their ranks add up) and not the same passage listed twice.
            t, passage = moment(doc_id)
            snip = passage or snip
        kw.append((doc_id, page if cat['by_id'][doc_id]['kind'] == 'pdf' else None, t, snip))
    sem = INDEX.semantic(qv, 80, allowed) if qv is not None else []
    sem_l = [(s['doc_id'], s['page'] if cat['by_id'][s['doc_id']]['kind'] == 'pdf' else None, s['t'], s['text'], s['sem']) for s in sem]
    content, groups = query_groups(q)
    only_articles = kind == 'encyclopedia'
    enc = ENC.search(content, groups, qv, k if only_articles else encyclopedia.DEEP) if ENC and kind in ('any', 'encyclopedia') else []
    recs = {e['doc_id']: e['record'] for e in enc}
    rec = lambda d: cat['by_id'].get(d) or recs[d]
    key = lambda d, p, t: (d, p, None if t is None else int(t // 45))
    qwords = [w for w in re.findall(r'[a-z0-9-]{3,}', q.lower()) if w not in STOP]
    models = [w for w in re.findall(r'[a-z0-9-]+', q.lower()) if re.search(r'\d', w) and re.search(r'[a-z]', w) and len(w) >= 3]
    lists = [('keyword', [key(*x[:3]) for x in kw]), ('meaning', [key(*x[:3]) for x in sem_l]),
             ('encyclopedia', [key(e['doc_id'], None, None) for e in enc])]
    fused = retrieval.rrf(lists)
    text = {}
    for d, p, t, snip in kw:
        text.setdefault(key(d, p, t), {'excerpt': snip, 't': t})
    for d, p, t, chunk, sc in sem_l:
        e = text.setdefault(key(d, p, t), {})
        e.update(excerpt=chunk, t=t, sem=round(sc, 3))  # the semantic chunk is the fuller passage
    for e in enc:
        text[key(e['doc_id'], None, None)] = {'excerpt': e['text'], 't': None, 'section': e['heading'], 'sem': e['sem']}
    out = []
    for kkey, e in fused.items():
        d, p, _ = kkey
        r = rec(d)
        _, weight = trust(r)
        score = e['score'] * (0.75 + 0.25 * weight)
        if for_clinicians(r) and not CLINICAL.search(q):
            score *= 0.55  # surgery/clinician texts after general guidance unless asked for
        title = (r['title'] + ' ' + str(r.get('model') or '')).lower()
        if models and any(m in title for m in models):
            score *= 3  # exact equipment model named in the question
        elif sum(1 for w in qwords if w in title) >= max(2, len(qwords) // 2):
            score *= 1.3
        out.append((score, kkey, e))
    out.sort(key=lambda x: -x[0])
    # Library pages are usually found by words and by meaning alike, an article only by the encyclopedia search, so in a
    # mixed list the pages come first and the articles after them, each in their own order, best first. A list of six or
    # more keeps its last place for the best article. A search of the encyclopedias alone is simply best first.
    articles = [] if only_articles else [x for x in out if x[1][0].startswith(encyclopedia.ARTICLE_PREFIX)]
    if articles:
        out = [x for x in out if not x[1][0].startswith(encyclopedia.ARTICLE_PREFIX)]
    res, per_doc = [], {}

    def take(found, places):
        for score, kkey, e in found:
            if len(res) >= places:
                break
            d, p, _ = kkey
            if per_doc.get(d, 0) >= 2:  # variety: at most two passages per document
                continue
            per_doc[d] = per_doc.get(d, 0) + 1
            info = text.get(kkey, {})
            extra = {'section': info['section']} if info.get('section') else {}
            res.append(_src(rec(d), p, info.get('t'), info.get('excerpt'), 900, found_by=e['ranks'], score=round(score * 1000, 2), **extra))
    take(out, k - 1 if articles and k >= 6 else k)
    take(articles, k)
    return res


def related(doc_id='', path='', k=5):
    """Up to k other documents on the subject of one, named by its id or by the address it is served at (a saved page's,
    an encyclopedia article's /zim/...). By meaning, those whose passages lie nearest the average of its own
    (retrieval.Index.centroid), which needs no Recall; for what the meaning index does not hold (an article, a document
    not embedded yet), the search for its title. Each opens at its start, so no page is named."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip('/')
    if path.startswith('zim/'):
        doc_id = encyclopedia.ARTICLE_PREFIX + path[4:]
    r = record(doc_id) if doc_id else catalog()['by_path'].get(path)
    if not r:
        return []
    base = _allowed('library')
    v = INDEX.centroid(r['id'])
    if v is not None:
        # Best first and no bar: FLOOR and MARGIN were measured for questions, and the nearest documents are related
        # however near they are.
        near = INDEX.semantic(v, 200, lambda d: d != r['id'] and base(d), floor=-1, margin=-2)
        found = [_src(catalog()['by_id'][s['doc_id']]) for s in near]
    else:
        found = [s for s in hybrid(clean_title(r), 12) if s['doc_id'] != r['id']]
    out = {}
    for s in found:
        out.setdefault(s['doc_id'], {**s, 'page': None, 't': None, 'excerpt': ''})
    return list(out.values())[:k]


# ------------------------------------------------------------------ shelves ----
SHELVES = [
    ('health', 'Health & first aid', r'medic|first-aid|care|health|dent|mental|obstet|midwi|clinic|surg'),
    ('water', 'Water & sanitation', r'water|rain|sanit|wash|hygien'),
    ('food', 'Food: growing, keeping & cooking', r'agri|food|seed|crop|garden(?!-power)|livestock|dairy|ferment|bee|farm|soil|grow|irrigat|oil-crop|grain'),
    ('safety', 'Disasters, safety & outdoors', r'survival|shelter|disaster|prepar|safety|hazard|wildfire|knot|weather|polar|defen|radiolog|rescue|fire'),
    ('power', 'Power & energy', r'energy|power|solar|generator|offgrid|battery|inverter|charge-controller'),
    ('comms', 'Radio, communication & navigation', r'communic|radio|navigation'),
    ('repair', 'Tools, repair & equipment', r'engine|mower|tractor|vehicle|hvac|compressor|bicycle|aerial|backhoe|bearing|sharpen|weld|repair|equipment|seal|pump|snow|motorcycle|truck|car|marine|forklift|workshop|hand-tool'),
    ('making', 'Making things & materials', r'manufact|craft|glass|ceram|textil|wood|paper|metal|materials|machin|leather|amate|lime|refractor|typeset|mill|mineral|production|raw-material'),
    ('build', 'Building & infrastructure', r'civil|infrastructure|building'),
    ('community', 'Community, teaching & recovery', r'communit|educat|teach|accessib|recover|accountab|organiz|continuity|inventory'),
    ('science', 'Science, maths & engineering courses', r'math|physic|chemistr|biolog|astronom|earth|ecolog|science|engineer|control|metrolog|quality|thermo|electromag|circuit|microelec|comput|digital|space|aviation|aircraft|laborator|measurement'),
    ('creators', 'Bushcraft & primitive-skills videos', r'^creator-'),
]


def shelf_of(r):
    c = r.get('category', '').lower()
    if c.startswith('creator-'):
        return 'creators'
    for sid, _, rx in SHELVES:
        if sid != 'creators' and re.search(rx, c):
            return sid
    return 'other'


def shelves():
    counts = {}
    for r in catalog()['by_id'].values():
        if r['kind'] in ('metadata', 'transcript', 'zim'):
            continue
        counts.setdefault(shelf_of(r), {'read': 0, 'video': 0})
        counts[shelf_of(r)]['video' if r['kind'] == 'video' else 'read'] += 1
    return [{'id': s, 'title': t, **counts.get(s, {'read': 0, 'video': 0})} for s, t, _ in SHELVES] + \
        [{'id': 'other', 'title': 'Other references', **counts.get('other', {'read': 0, 'video': 0})}]


def shelf(sid):
    rs = [r for r in catalog()['by_id'].values() if r['kind'] not in ('metadata', 'transcript', 'zim') and shelf_of(r) == sid]
    rs.sort(key=lambda r: (-trust(r)[1], r.get('collection', ''), r['title']))
    # grouped by collection, so a 300-chapter manual is one entry: the window shows a few and offers the rest
    groups = {}
    for r in rs:
        groups.setdefault(r.get('collection') or 'Other', []).append(r)
    return {'collections': [{'collection': c, 'count': len(v), 'items': [public(x) for x in v]} for c, v in groups.items()]}


# ------------------------------------------------------------------ videos ----
VROWS = [  # (id, title, exact catalog categories) — practical first, hobby channels and courses after
    ('health', 'First aid & health', 'first-aid medical care-continuity'),
    ('water', 'Water & sanitation', 'water-safety-planning water-treatment rainwater water-sanitation water-sampling sanitary-inspection pumps'),
    ('food', 'Growing & preserving food', 'food-preservation agriculture food-growing seed-saving grain-mill-assembly grain-mill-maintenance grain-mill-operation'),
    ('power', 'Power, generators & solar', 'generator generators offgrid-power'),
    ('outdoors', 'Shelter, fire & outdoor skills', 'field-survival fire-and-shelter knots weather communications-navigation'),
    ('repair', 'Tools, engines & repair', 'small-engine-repair lawn-mower garden-power-equipment snow-blower woodworking welding welding-equipment sharpening workshop milling-layout-and-fit bearing-maintenance seals-installation road-vehicles marine heavy-ag-machinery machining-tool-geometry measurement'),
    ('making', 'Making things by hand', 'craft-manufacturing-including-ceramics-and-textiles glass-manufacturing-processes refractory-installation refractory-process-control typesetting-printing-binding bark-to-amate-paper'),
    ('coalcracker', 'Coalcracker Bushcraft', 'creator-coalcracker-bushcraft'),
    ('corporals', 'Corporals Corner', 'creator-corporals-corner'),
    ('canterbury', 'Dave Canterbury', 'creator-david-canterbury'),
    ('primitive', 'Primitive Technology', 'creator-primitive-technology'),
    ('courses', 'Courses, lectures & more', ''),
]
NEWEST_FIRST = {'coalcracker', 'corporals', 'canterbury', 'primitive'}   # one channel each: its videos by date, not A–Z


def vrow_of(r):
    c = r.get('category', '')
    for vid, _, cats in VROWS:
        if c in cats.split():
            return vid
    return 'courses'


def vcard(r):
    p = public(r)
    return {k: p.get(k) for k in ('id', 'title', 'publisher', 'duration', 'thumb')}


def vsort(r):
    t = trust(r)[1]
    return (-t, not catalog()['video_tx'].get(r['path']) and not r.get('captions'), not catalog()['thumbs'].get(r['path']), r['title'])


def guide_videos():
    out, seen = [], set()
    for t in topics():
        items = [i for c in t.get('choices', []) for i in c.get('read', [])] + t.get('learn', []) + t.get('prepare', []) + \
            [i for st in t.get('route', []) for i in st.get('read', [])]
        for i in items:
            r = catalog()['by_id'].get(i.get('doc') or '')
            if r and r['kind'] == 'video' and r['id'] not in seen:
                seen.add(r['id'])
                out.append(vcard(r))
    return out


def videos_home():
    rows = {}
    for r in catalog()['by_id'].values():
        if r['kind'] == 'video':
            rows.setdefault(vrow_of(r), []).append(r)
    picked = guide_videos()
    res = [{'title': 'Picked for the guides', 'count': len(picked), 'items': picked}]
    for vid, title, _ in VROWS:
        rs = sorted(rows.get(vid, []), key=vsort)
        if vid in NEWEST_FIRST:
            rs.sort(key=lambda r: r.get('upload_date') or '', reverse=True)
        if rs:
            res.append({'title': title, 'count': len(rs), 'items': [vcard(r) for r in rs]})
    return res


def videos_search(q):
    words = q.lower().split()
    rs = sorted((r for r in catalog()['by_id'].values()
                 if r['kind'] == 'video' and all(w in (r['title'] + ' ' + r.get('publisher', '')).lower() for w in words)), key=vsort)
    return {'total': len(rs), 'items': [vcard(r) for r in rs]}


# ------------------------------------------------------------- pages/text ----
page_texts = retrieval.page_texts


def png_bytes(rgb):
    """8-bit RGB array to PNG with the standard library, so the package does not need Pillow."""
    h, w = rgb.shape[:2]
    raw = b''.join(b'\0' + rgb[y, :, :3].tobytes() for y in range(h))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def render_page(r, n, width):
    width = max(160, min(1600, width))
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"{r['id']}-{n}-{width}.png"
    if not out.exists():
        import pypdfium2
        with _pdf_lock:
            pdf = pypdfium2.PdfDocument(str(LIB / r['path']))
            try:
                page = pdf[n - 1]
                # width is the longer side, as pdftoppm -scale-to did
                bitmap = page.render(scale=width / max(page.get_size()), rev_byteorder=True)
                data = png_bytes(bitmap.to_numpy())
            finally:
                pdf.close()
        tmp = CACHE / f".{r['id']}-{n}-{width}-{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, out)
        return data
    return out.read_bytes()


def vtt_cues(path):
    """A caption file's cues without their repeats, merged into ~sentence chunks."""
    out = []
    for at, t in retrieval.said(LIB / path):
        if out and at - out[-1]['t'] < 12 and len(out[-1]['text']) < 220:
            out[-1]['text'] += ' ' + t
        else:
            out.append({'t': at, 'text': t})
    return out


def transcript(video_id):
    cat = catalog()
    v = cat['by_id'].get(video_id)
    if not v:
        return None
    tx = cat['video_tx'].get(v['path'])
    vtt = retrieval.vtt_for(v, cat)
    if vtt:
        return {'source': (tx or {}).get('caption_type') or 'captions', 'cues': vtt_cues(vtt)}
    if tx and (LIB / tx['path']).is_file():
        return {'source': tx.get('caption_type') or 'transcript',
                'cues': [{'t': None, 'text': x} for x in (LIB / tx['path']).read_text(encoding='utf-8', errors='replace').split('\n\n') if x.strip()]}
    return {'source': None, 'cues': []}


# ---------------------------------------------- places and nearby help ----
# Places from world-places.sqlite (GeoNames: countries, regions, places of 500+; setup builds it), or a point given as
# "LAT,LON". Help points from the `pois` layer of the street-level maps the window draws (regions-*.mbtiles,
# engine/map_pois.py). Upstream's US map files, in a library that still has them, are not read.
GEO = HERE / 'derived' / 'geo'

# What `--nearby` takes: the kinds the street-level maps hold (engine/map_pois.py KINDS where the map's word differs).
HELP = {'hospital': 'Hospital', 'clinic': 'Clinic', 'doctors': 'Doctor', 'pharmacy': 'Pharmacy',
        'chemist': 'Drugstore / toiletries shop (may not sell medicines)', 'dentist': 'Dentist', 'fire_station': 'Fire station',
        'police': 'Police', 'drinking_water': 'Drinking water tap', 'fountain': 'Fountain (not necessarily drinkable)',
        'spring': 'Spring (untested)', 'toilet': 'Public toilet',
        'shelter': 'Roofed shelter (a bus or rain shelter, hut or post: not an emergency shelter)',
        'community_centre': 'Community centre', 'town_hall': 'Town hall', 'school': 'School', 'library': 'Library',
        'supermarket': 'Supermarket', 'convenience': 'Convenience store', 'doityourself': 'Hardware / DIY store',
        'hardware': 'Hardware store', 'garden_centre': 'Garden centre', 'post_office': 'Post office',
        'camp_site': 'Campsite', 'veterinary': 'Vet', 'nursing_home': 'Care home', 'fuel': 'Fuel station',
        'car_repair': 'Car repair', 'bicycle_shop': 'Bicycle shop', 'park': 'Park', 'courthouse': 'Courthouse',
        'college': 'College', 'university': 'University', 'kindergarten': 'Kindergarten', 'beach': 'Beach'}
NEAR_KM = map_pois.FULL_KM * 4   # how far --nearby looks: every point out to FULL_KM, larger ones beyond


def dedupe_named(rows):
    """The same shop is often mapped twice (a pharmacy node plus a chemist building): drop same-name points within ~80 m."""
    kept = {}
    out = []
    for r in rows:
        if r['name']:
            near_same = kept.setdefault(r['name'].lower(), [])
            if any(abs(r['lat'] - y) < 0.0007 and abs(r['lon'] - x) < 0.0009 for y, x in near_same):
                continue
            near_same.append((r['lat'], r['lon']))
        out.append(r)
    return out


def near(lat, lon, classes, limit=8):
    """The nearest points of each kind, nearest first by distance on the ground. The search reaches out in steps, each
    twice as far, and stops once `limit` points lie within the distance read: none nearer can have been missed. Every
    point out to map_pois.FULL_KM; beyond it, out to NEAR_KM, the larger ones the map shows at a coarser zoom."""
    limit = max(1, min(limit, 50))
    out = []
    with map_pois.Regions(GEO, classes) as regions:
        for cls in classes:
            found, km, read = {}, NEAR_KM / 64, 0.0
            while True:
                for c, name, x, y in regions.box(lat, lon, cls, km, read):   # only the ring this step adds
                    found[(name, round(x, 5), round(y, 5))] = (c, name, x, y)
                within = []
                for c, name, x, y in found.values():
                    dist, brg = haversine(lat, lon, y, x)
                    if dist <= km:
                        within.append({'cls': c, 'label': HELP.get(c, c), 'name': name, 'lat': y, 'lon': x, 'km': dist, 'dir': brg})
                within = dedupe_named(sorted(within, key=lambda p: p['km']))
                if len(within) >= limit or km >= NEAR_KM:
                    break
                read, km = km, km * 2
            out += within
    out = dedupe_named(sorted(out, key=lambda p: p['km']))
    per, res = {}, []
    for p in out:
        if per.get(p['cls'], 0) < limit:
            per[p['cls']] = per.get(p['cls'], 0) + 1
            res.append({**p, 'km': round(p['km'], 2)})
    return res


def covered(lat, lon):
    """Whether an installed street-level map covers the point: only there can help points be looked up."""
    with map_pois.Regions(GEO) as regions:
        return regions.covers(lat, lon)


# What the Map screen lists as nearest help: the kinds its markers show (native/src/map.rs HELP), as `--nearby` names them.
MAP_HELP = ('hospital', 'clinic', 'doctors', 'pharmacy', 'fire_station', 'police', 'drinking_water', 'shelter', 'toilet', 'supermarket')


def near_help(at, to=None):
    """The Map screen's answer for a point (lat, lon). With `to`, another point: how far it is, {km, dir}. Without:
    {covered, points}, the nearest of each of MAP_HELP as near() finds them, and none where no installed street-level map
    covers the point, rather than far-away ones from the map of somewhere else. There it also has `regions`: up to three
    whose streets setup can add for the point, smallest first, {id, name, bytes, installed} (`bytes` is the file's size
    once it is installed, else null: a cut's size is known only once it is measured). None for a point out at sea."""
    if to:
        km, d = haversine(*at, *to)
        return {'km': round(km, 2), 'dir': d}
    if covered(*at):
        return {'covered': True, 'points': near(*at, MAP_HELP, 5)}
    town = None
    if WORLD_PLACEDB.exists():
        with contextlib.closing(sqlite3.connect(ro_uri(WORLD_PLACEDB), uri=True)) as db:
            town = setup_maps.nearest_town(db, *at)
    have = {r['id']: r['bytes'] for r in setup_maps.installed(GEO)['regions']}
    return {'covered': False, 'points': [], 'regions': [{'id': r['id'], 'name': r['name'], 'bytes': have.get(r['id']), 'installed': r['id'] in have}
                                                        for r in setup_maps.covering(*at, town[1] or '' if town else '')[:3]]}


def haversine(la1, lo1, la2, lo2):
    R = 6371
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * R * math.asin(math.sqrt(a))
    b = (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
    return d, ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'][int((b + 22.5) // 45) % 8]


COORDS = re.compile(r'\s*([+-]?\d+(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d+(?:\.\d+)?)\s*')


def point(q):
    """(lat, lon) of a query that is a point in decimal degrees ("-8.5069,115.2625"), None for a query that names a place
    (it has a letter in it, or no number). ValueError for one that is neither: numbers that are no point on Earth."""
    if re.search(r'[^\W\d_]', q) or not re.search(r'\d', q):
        return None
    m = COORDS.fullmatch(q)
    if not m or not (-90 <= float(m[1]) <= 90 and -180 <= float(m[2]) <= 180):
        raise ValueError('a point is LAT,LON in decimal degrees: latitude -90 to 90, longitude -180 to 180 (e.g. -8.5069,115.2625)')
    return float(m[1]), float(m[2])


def word_sets(words):
    """All the words, then (for longer queries) each set with one word dropped — 'echo park lake' still finds 'Echo Lake'."""
    yield words
    if len(words) > 1:
        for i in range(len(words)):
            yield words[:i] + words[i + 1:]


def _fold(t):
    """A name as compared: lower case, no accents or punctuation, 'saint' as 'st'."""
    t = ''.join(c for c in unicodedata.normalize('NFKD', t.lower()) if not unicodedata.combining(c))
    return ' '.join(re.sub(r'\bsaint\b', 'st', re.sub(r'[^\w ]+', ' ', t)).split())


def _match(db, q, row_id, name, detail, loosened):
    """How a place answers the query: 'exact' (its name, maybe with its region or country: "Ubud, Bali"), 'alternate name'
    (every word, as one of the other names GeoNames knows it by: "Bombay" for Mumbai), or 'partial' (a word of the query
    left out, or only the start of a longer name: never a place to look around)."""
    if loosened:
        return 'partial'
    words, own, around = _fold(q).split(), _fold(name).split(), set(_fold(detail or '').split())
    if words[:len(own)] == own and set(words[len(own):]) <= around:
        return 'exact'
    if set(words) <= around:
        return 'partial'   # only the region or country it is in ("Bali" for a town on Bali)
    try:   # the other names are in the search index alone (engine/setup/maps.py): the whole query as words of one of them
        hit = db.execute('SELECT 1 FROM place_fts WHERE place_fts MATCH ? AND rowid=?', ('detail : "' + ' '.join(words) + '"', row_id)).fetchone()
    except sqlite3.OperationalError:
        hit = None
    return 'alternate name' if hit else 'partial'


def places(q, limit=20):
    """Places for a query, best first: {name, kind, detail, lat, lon, source, match, onMap}. `match` says how the place
    answers the query (_match; 'coordinates' for a point given as LAT,LON). `onMap`: an installed street-level map covers it,
    so help points can be looked up around it. A query of numbers that is no point on Earth finds nothing."""
    try:
        at = point(q)
    except ValueError:
        return []
    db = sqlite3.connect(ro_uri(WORLD_PLACEDB), uri=True) if WORLD_PLACEDB.exists() else None
    try:
        if at:
            town = setup_maps.nearest_town(db, *at) if db else None
            detail = f'{town[2]:.0f} km from {town[0]}, {town[1]}' if town else 'A point given by its coordinates'
            out = [{'name': ','.join(f'{v:.6f}'.rstrip('0').rstrip('.') for v in at), 'kind': 'point', 'detail': detail, 'lat': at[0], 'lon': at[1],
                    'source': 'coordinates', 'match': 'coordinates'}]
        else:
            out = _places(db, q, limit) if db else []
    finally:
        if db:
            db.close()
    with map_pois.Regions(GEO) as regions:
        for p in out:
            p['onMap'] = regions.covers(p['lat'], p['lon'])
    return out


def _places(db, q, limit):
    words = re.findall(r'\w+', q)
    if not words:
        return []
    def find(match):
        return db.execute('SELECT p.id,p.name,p.kind,p.detail,p.lat,p.lon,p.rank,p.source FROM place_fts f JOIN place p ON p.id=f.rowid '
                          'WHERE place_fts MATCH ? ORDER BY p.rank, bm25(place_fts) LIMIT 200', (match,)).fetchall()
    # The names with the query's words as they are, first: a village called Ama is not lost behind the 200 larger places
    # whose words only start with them (Amazonas, Amarillo, ...).
    phrase = '"%s"' % ' '.join(w.replace('"', '') for w in words)
    rows = [(r, False) for r in find('name : ' + phrase)]
    full = False
    for ws in word_sets(words):
        if full and len(ws) < len(words):
            break  # every word matched somewhere: no need to loosen
        found = find(' '.join('"%s"*' % w.replace('"', '') for w in ws))
        rows += [(r, len(ws) < len(words)) for r in found]
        if len(ws) == len(words):
            full = bool(found)
    ql = _fold(q)
    # The largest places (countries, regions, capitals, cities over a million: rank 0-1) with the query's words as they
    # are, not in their name or around it: in their other names. "Bombay" is Mumbai before it is a village called Bombay.
    big = ','.join(str(r[0][0]) for r in rows if r[0][6] <= 1)
    known = {i for (i,) in db.execute(f'SELECT rowid FROM place_fts WHERE place_fts MATCH ? AND rowid IN ({big})', (phrase,))} if big else set()

    def order(r):
        (row_id, name, _kind, detail, *_, rank, _src), loosened = r
        n = _fold(name)
        known_as = row_id in known and not set(ql.split()) <= set(n.split()) | set(_fold(detail or '').split())
        # First the places the query names, alone or with their region or country after it ("Lyon, France"), and those
        # largest ones it names another way; among them the larger first (rank, then id: the gazetteer lists places
        # largest first), and of one rank a name that starts with the query.
        return loosened, not (ql == n or ql.startswith(n + ' ') or known_as), rank, not n.startswith(ql), row_id
    rows.sort(key=order)
    seen, out = set(), []
    for (row_id, name, kind, detail, lat, lon, rank, src), loosened in rows:
        k = (name.lower(), round(lat, 2), round(lon, 2))
        if k in seen:
            continue
        seen.add(k)
        out.append({'name': name, 'kind': kind, 'detail': detail, 'lat': lat, 'lon': lon, 'source': src,
                    'match': _match(db, q, row_id, name, detail, loosened)})
        if len(out) >= limit:
            break
    return out


SOURCES_PAGE = 'documents/lastbook/sources-and-permissions.html'   # every pack's own credits page, an ordinary record


# --------------------------------------------------------------- handler ----
class Handler(library.Handler):
    # The only parts of the library the window gets: no worker state, logs, tools, sign-in settings or reports.
    LIBRARY_PREFIXES = tuple(f'/{f}/' for f in library.SERVED_FOLDERS)
    SERVED_PREFIXES = ('/api/', '/zim/', *LIBRARY_PREFIXES)

    def path_ok(self, path):
        if '\0' in path:
            return False
        if path != '/reader.css' and not path.startswith(self.SERVED_PREFIXES):
            return False
        if path.lower().endswith('.info.json'):  # yt-dlp's .info.json can hold request headers
            return False
        if path.startswith('/zim/'):
            # An entry's path is a name inside the archive, never a file path: "...middle..." is a title, not a way up.
            # Only a part that is exactly ".." is refused. The archive's own file name is checked in engine/library.py.
            return '..' not in path.split('/')
        if '/..' in path:
            return False
        if path.startswith('/api/'):
            return True
        return self.file_ok(self.translate_path(self.path), folders=True)

    def list_directory(self, path):
        self.send_error(404)

    def send_head(self):
        """Only files get here: do_GET answers every /api/ and /zim/ address itself, whatever the library folder holds."""
        page = self.saved_page_file()
        if not page:
            return super().send_head()
        try:
            links = catalog()['links']
        except (LibraryError, OSError):     # the page still opens; its links to other saved pages lead to the web
            links = {}
        data = saved_pages.rewrite(Path(page).read_bytes(), self.saved_page_part, links)
        if '/documents/lastbook/' in urllib.parse.unquote(urllib.parse.urlsplit(self.path).path):
            data = saved_pages.own_look(data)
        self.send_response(200)
        self.send_header('Content-Type', self.guess_type(page))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        return io.BytesIO(data)

    def saved_page_file(self):
        """The web page this library address shows, if it is one to serve through saved_pages.rewrite."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if not path.startswith(self.LIBRARY_PREFIXES):
            return None
        page = self.translate_path(self.path)
        if path.endswith('/'):
            page = os.path.join(page, 'index.html')
        ok = page.lower().endswith(('.html', '.htm')) and os.path.isfile(page) and self.file_ok(page, folders=True)
        return page if ok and os.path.getsize(page) <= saved_pages.MAX_READ else None

    def saved_page_part(self, src):
        """A transcript a saved page names, by the same rules as asking for it directly."""
        target = urllib.parse.urljoin(urllib.parse.urlsplit(self.path).path, src)
        path = urllib.parse.unquote(urllib.parse.urlsplit(target).path)
        if not target.startswith('/') or target.startswith('//') or '/..' in path or not path.startswith(self.LIBRARY_PREFIXES):
            return None
        real = self.translate_path(target)
        if not (os.path.isfile(real) and self.file_ok(real, folders=True) and os.path.getsize(real) <= saved_pages.MAX_TRANSCRIPT):
            return None
        return Path(real).read_bytes()

    def send_data(self, data, mime='application/json', status=200):
        """engine/library.py sends every encyclopedia page through here."""
        if status == 200 and ('html' in mime or 'svg' in mime) and urllib.parse.urlsplit(self.path).path.startswith('/zim/'):
            data = saved_pages.article(data)
            if 'html' in mime:
                data = saved_pages.article_look(data)
        super().send_data(data, mime, status)

    def internal_error(self):
        """The details go to the log. What reaches the window names no file path or user."""
        traceback.print_exc()
        return self.send_json({'error': f'Server error ({sys.exc_info()[0].__name__}).'}, 500)

    def do_POST(self):
        try:
            return self.handle_post()
        except library.CLIENT_GONE:
            return
        except Exception:
            return self.internal_error()

    def handle_post(self):
        rpath = urllib.parse.urlsplit(self.path).path
        if rpath.startswith('/api/setup/'):  # Library and downloads: engine/setup/api.py
            return setup_api.handle(self, 'POST', rpath)
        return self.send_json({'error': 'not found'}, 404)

    def do_GET(self):
        try:
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            if not self.path_ok(path):
                return self.send_json({'error': 'not found'}, 404)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if path.startswith('/api/setup/'):
                return setup_api.handle(self, 'GET', path)
            if path == '/api/topics':
                return self.send_json(topics_resolved())
            if path == '/api/find':
                return self.send_json(find(qs.get('q', '')[:200], qs.get('kind', 'all')))
            if path.startswith('/api/doc/'):
                r = catalog()['by_id'].get(path[9:])
                return self.send_json(public(r) if r else {'error': 'This item is not in the library catalog.'}, 200 if r else 404)
            if path.startswith('/api/transcript/'):
                t = transcript(path[16:])
                return self.send_json(t if t is not None else {'error': 'unknown video'}, 200 if t is not None else 404)
            if path == '/api/retrieve':
                return self.send_json({'q': qs.get('q', ''), 'results': hybrid(qs.get('q', '')[:200], min(20, int(qs.get('k', 8))), qs.get('kind', 'any'))})
            if path == '/api/related':
                return self.send_json({'results': related(qs.get('doc', ''), qs.get('path', ''))})
            if path.startswith('/api/text/'):
                r = catalog()['by_id'].get(path[10:])
                texts = page_texts(r) if r and r['kind'] in retrieval.PAGED else None
                return self.send_json({'pages': texts} if texts is not None else {'error': 'This item is not in the library catalog.'}, 200 if texts is not None else 404)
            if path == '/api/search-status':
                import cli   # app/cli.py: what `lastbook status` says of meaning search, said the same way here
                return self.send_json(cli.meaning(sys.modules[__name__]))
            if path == '/api/videos':
                return self.send_json(videos_home())
            if path == '/api/videos/all':
                return self.send_json(videos_search(qs.get('q', '')))
            if path == '/api/shelves':
                return self.send_json(shelves())
            if path.startswith('/api/shelf/'):
                return self.send_json(shelf(path[11:]))
            if path == '/api/places':
                return self.send_json(places(qs.get('q', '')[:100]))
            if path == '/api/near':
                try:   # both points as point() reads a query, so a bad one is an error, never a point at 0,0
                    at, to = point(f"{qs.get('lat', '')},{qs.get('lon', '')}"), point(qs['to']) if qs.get('to') else None
                except ValueError as e:
                    return self.send_json({'error': str(e)}, 400)
                if at is None or (qs.get('to') and to is None):
                    return self.send_json({'error': 'lat and lon are decimal degrees; to is LAT,LON'}, 400)
                return self.send_json(near_help(at, to))
            if path == '/api/encyclopedias':
                return self.send_json(ENC.installed())
            if path == '/api/encyclopedia/random':
                a = ENC.random(qs.get('file'))
                return self.send_json(a or {'error': 'No article with text was found in the encyclopedias.'}, 200 if a else 404)
            if path == '/api/library':
                s = json.loads((LIB / 'summary.json').read_text(encoding='utf-8'))
                sources = (catalog()['by_path'].get(SOURCES_PAGE) or {}).get('id')
                return self.send_json({**{k: s.get(k) for k in ('pdfs', 'pdf_pages', 'web_pages', 'videos', 'collections', 'document_bytes')}, 'sources': sources, 'index': dict(INDEX.status)})
            if path.startswith('/api/'):
                return self.send_json({'error': 'not found'}, 404)
            return super().do_GET()
        except library.CLIENT_GONE:
            return
        except Exception:  # a readable error instead of a dropped connection
            return self.internal_error()


def init(no_index=False):
    """Everything the engine needs before it can answer a request. Opens no socket."""
    global INDEX, ENC
    catalog()
    # Without the indexer this process only reads the index (the command line, beside a window that may be indexing).
    INDEX = retrieval.Index(catalog, trust, library.DB, excluded_fn=excluded_videos)
    ENC = encyclopedia.Encyclopedias(LIB, catalog, library.open_zim, INDEX)
    if not no_index:
        threading.Thread(target=INDEX.run_forever, daemon=True).start()
