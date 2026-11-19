#!/usr/bin/env python3
"""The engine's request handler: the answers the native window asks for, over engine/library.py's file, ZIM and video serving.

Nothing here opens a port. app/engine_pipe.py calls init() and runs Handler through app/bridge.py, one request at a time,
over in-memory streams. The routes the window uses:

  /api/topics                 curated needs and learning routes, with source details resolved from catalog.json
  /api/find?q=&kind=          plain-language search (synonyms, grouping by document, trusted-source ranking)
  /api/retrieve?q=&k=&kind=   the passages Chat would cite (keyword, meaning-based and encyclopedia lists, fused)
  /api/doc/<id>               one catalog record (+ linked video/transcript)
  /api/transcript/<video id>  timed transcript cues
  /api/videos, /api/videos/<row>   the Videos screen's rows, and one row or a search of all
  /api/shelves, /api/shelf/<id>    browse the whole collection by plain-language shelf
  /api/places?q=              place finder (addresses, ZIP codes, towns, named places; the world's towns from GeoNames)
  /api/library, /api/assistant/status, POST /api/assistant/local, POST /api/chat, /api/setup/*

The assistant's tools call the helpers here directly (places, near, page_texts, render_page, transcript).
Nothing in library-data is written. Caches go to derived/cache.
"""
import functools, html, io, json, math, os, re, sqlite3, struct, sys, threading, time, traceback, urllib.parse, zlib
from pathlib import Path

import engine.paths as lastbook_paths  # noqa: E402  (data folder and program folder can differ in the packaged app)
from engine.paths import ro_uri  # noqa: E402
from engine import saved_pages, address_search  # noqa: E402
from engine.setup import api as setup_api  # noqa: E402  (the one module that may download: only when the person asks)
HERE = lastbook_paths.DATA  # holds derived/
APP = lastbook_paths.APP  # holds content/
LIB = lastbook_paths.LIB
CACHE = HERE / 'derived' / 'cache'
MAPDB = HERE / 'derived' / 'geo' / 'map.sqlite'
PLACEDB = HERE / 'derived' / 'geo' / 'places.sqlite'
WORLD_PLACEDB = HERE / 'derived' / 'geo' / 'world-places.sqlite'   # GeoNames: countries, regions, towns of 15,000+ (engine/setup/maps.py builds it in setup)
os.environ.setdefault('SURVIVAL_DISABLE_INDEX_WATCH', '1')
import engine.library as library  # noqa: E402  (provides Handler, search DB path, ZIM reader)
import retrieval, assistant  # noqa: E402
remote_records = library.remote_records  # videos moved to remote drives by the library's workers; none in the packaged app
INDEX = None  # retrieval.Index, created in init()

_pdf_lock = threading.Lock()  # PDFium is not thread-safe

# ---------------------------------------------------------------- catalog ----
_cat = {'mtime': 0, 'by_id': {}, 'by_path': {}, 'video_tx': {}, 'thumbs': {}}
_cat_lock = threading.Lock()


_excl = {'mtime': None, 'ids': set()}


def excluded_videos():
    p = LIB / 'video-exclusions.json'
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        return set()
    if m != _excl['mtime']:
        try:
            ids = set(json.loads(p.read_text(encoding='utf-8')).get('videos', {}))
            _excl.update(mtime=m, ids=ids | {'metadata-' + i for i in ids})
            _cat['mtime'] = 0  # re-filter the catalog with the new list
        except (ValueError, OSError):
            pass
    return _excl['ids']


def catalog():
    excluded_videos()
    p = LIB / 'catalog.json'
    m = p.stat().st_mtime
    if m != _cat['mtime']:
        with _cat_lock:
            if m != _cat['mtime']:
                try:
                    recs = json.loads(p.read_text(encoding='utf-8'))
                except ValueError:  # being rewritten by the indexer right now; keep the old copy
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
                _cat.update(mtime=m, by_id=by_id, by_path={r['path']: r for r in recs}, video_tx=tx, thumbs=thumbs)
    return _cat


TRUST = [  # (pattern on publisher+collection, label shown to people, ranking weight)
    (r'World Health|WHO\b|ICRC|IFRC|Red Cross|Médecins|MSF|UNICEF|Sphere|St John Ambulance|CAWST', 'International health & humanitarian guidance', 1.5),
    (r'Hesperian|Where There Is No|Where Women|Book for Midwives|Community Guide to Environmental|Health Actions|Workers\' Guide', 'Hesperian community health guide', 1.45),
    (r'FEMA|Ready\.gov|CDC|Centers for Disease|EPA|Environmental Protection|NOAA|National Weather|USDA|Food Safety|National Park|USGS|Department|Agency|U\.S\.|US Army|Army|Navy|Census|NIOSH|OSHA|NASA|FAA|Cal OES|California|County of|City of', 'Government guidance', 1.4),
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


def public(r, full=False):
    """The fields the window needs, with friendly labels. Never includes local auth or worker state."""
    if not r:
        return None
    t, _ = trust(r)
    out = {'id': r['id'], 'title': clean_title(r), 'kind': r['kind'], 'kindLabel': kind_label(r),
           'publisher': r.get('publisher'), 'collection': r.get('collection'), 'path': r['path'],
           'pages': r.get('pages') or r.get('slides'), 'duration': r.get('duration'), 'year': year_of(r),
           'trust': t, 'bytes': r.get('bytes')}
    if r['kind'] == 'video':
        tx = catalog()['video_tx'].get(r['path'])
        out['transcript'] = bool(tx or r.get('captions'))
        th = catalog()['thumbs'].get(r['path'])
        out['thumb'] = th if th and (LIB / th).is_file() else None
    if (r.get('searchability') or '').startswith('image-only'):
        out['imageOnly'] = True
    rem = remote_records().get(r['path']) if r['kind'] == 'video' else None
    if rem:
        out['remote'] = rem.get('host')
    if full:
        for k in ('notes', 'edition', 'retrieved_at', 'source_url', 'license', 'license_url', 'searchability',
                  'upload_date', 'publication_date', 'model', 'course', 'level', 'caption_type'):
            if r.get(k):
                out[k] = r[k]
        if r['kind'] == 'transcript' and r.get('video_path'):
            v = catalog()['by_path'].get(r['video_path'])
            if v:
                out['videoId'] = v['id']
        if r['kind'] == 'video':
            tx = catalog()['video_tx'].get(r['path'])
            if tx:
                out['transcriptId'] = tx['id']
                out['caption_type'] = tx.get('caption_type')
    return out


def clean_title(r):
    t = r['title']
    t = re.sub(r'^(\d{3}(\s\d+)?)\s+', '', t)          # Hesperian chapter file numbers
    t = re.sub(r'\.pdf$', '', t, flags=re.I)
    if re.fullmatch(r'[\w\-]+', t) and '_' in t:
        t = t.replace('_', ' ')
    return t


# ------------------------------------------------------------------ topics ----
def topics():
    out = []
    for f in sorted((APP / 'content' / 'topics').glob('*.json')):
        try:
            t = json.loads(f.read_text(encoding='utf-8'))
        except ValueError as e:
            out.append({'id': f.stem, 'error': str(e)})
            continue
        out.append(t)
    return out


def zim_names():
    """The encyclopedia files this library really holds. Its folder decides, so one rule covers every size of pack."""
    try:
        return sorted(p.name for p in (LIB / 'collections').glob('*.zim'))
    except OSError:
        return []


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
        i['src'] = public(r, full=False) if r else None
        if r:
            i['src']['notes'] = r.get('notes')
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
GENERAL = re.compile(r'first aid|emergency|basic|household|home|beginner|guide|handbook|where there is no', re.I)
KIND_FILTERS = {'read': ('pdf', 'html', 'pptx'), 'video': ('video', 'transcript'), 'all': None}


def fts_expr(groups):
    def q(t):
        return '"' + t.replace('"', '""') + '"'
    return ' AND '.join('(' + ' OR '.join(q(t) for t in g) + ')' for g in groups)


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
    """(rows, relaxed) from the keyword index: every idea in groups must match; with fewer than relax_below rows, any idea
    may (documents with more of them still rank higher). Rows are (doc_id, page, snippet, bm25), best first."""
    con = getattr(_fts_tls, 'con', None)
    if con is None:  # one read-only connection per thread, reused
        con = _fts_tls.con = sqlite3.connect(ro_uri(library.DB), uri=True, check_same_thread=False)
    sql = (f"SELECT doc_id,page,snippet(pages,6,?,?,' … ',{int(tokens)}),bm25(pages,0,6,1,1,0,0,1) "
           "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages,0,6,1,1,0,0,1) LIMIT ?")
    try:
        rows = con.execute(sql, (*marks, fts_expr(groups), limit)).fetchall()
        if len(rows) < relax_below and len(groups) > 1:
            return rows + con.execute(sql, (*marks, ' OR '.join('(' + fts_expr([g]) + ')' for g in groups), limit)).fetchall(), True
    except sqlite3.OperationalError:
        return [], False
    return rows, False


def find(q, kind='all', limit=30):
    catalog()  # the cache below is per catalog version (which includes video exclusions)
    return _find(q, kind, limit, _cat['mtime'], _excl['mtime'])


@functools.lru_cache(maxsize=256)
def _find(q, kind, limit, _cat_version, _excl_version):
    content, groups = query_groups(q)
    if not groups:
        return {'q': q, 'results': [], 'expanded': []}
    rows, relaxed = fts_rows(groups, 600, ('\u0001', '\u0002'), 28, relax_below=8)
    mode = 'some words' if relaxed else 'all words'
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
        if CLINICAL.search(r['title'] + ' ' + r.get('collection', '')) and not CLINICAL.search(q):
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
    expanded = sorted(set(w for g in groups for w in g[1:]) - set(content))
    return {'q': q, 'mode': mode, 'expanded': expanded, 'results': results}


def clean_snip(s):
    s = html.escape(re.sub(r'\s+', ' ', s or '')).replace('\u0001', '<mark>').replace('\u0002', '</mark>')
    return s


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


def shelf(sid, kind='all', q='', offset=0, limit=40):
    allowed = KIND_FILTERS.get(kind)
    rs = [r for r in catalog()['by_id'].values()
          if r['kind'] not in ('metadata', 'transcript', 'zim') and shelf_of(r) == sid
          and (not allowed or r['kind'] in allowed)
          and (not q or q.lower() in (r['title'] + ' ' + r.get('collection', '') + ' ' + r.get('publisher', '')).lower())]
    rs.sort(key=lambda r: (-trust(r)[1], r.get('collection', ''), r['title']))
    # group by collection so a 300-chapter manual shows as one entry
    groups = {}
    for r in rs:
        groups.setdefault(r.get('collection') or 'Other', []).append(r)
    items = [{'collection': c, 'count': len(v), 'trust': trust(v[0])[0], 'items': [public(x) for x in v[:6]]}
             for c, v in groups.items()]
    return {'total': len(rs), 'collections': items[offset:offset + limit], 'more': len(items) > offset + limit}


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
VROW_NOTE = {'coalcracker': 'Hobbyist channel', 'corporals': 'Hobbyist channel', 'canterbury': 'Hobbyist channel',
             'primitive': 'Hobbyist channel — no narration', 'courses': 'University lectures (MIT and others)'}


def vrow_of(r):
    c = r.get('category', '')
    for vid, _, cats in VROWS:
        if c in cats.split():
            return vid
    return 'courses'


def vcard(r):
    p = public(r)
    return {k: p.get(k) for k in ('id', 'title', 'publisher', 'duration', 'thumb', 'transcript', 'trust', 'year')}


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
                out.append(dict(vcard(r), guide=t['id'], guideTitle=t.get('title')))
    return out


def videos_home():
    rows = {}
    for r in catalog()['by_id'].values():
        if r['kind'] == 'video':
            rows.setdefault(vrow_of(r), []).append(r)
    res = [{'id': 'guides', 'title': 'Picked for the guides', 'note': 'Chosen to go with the survival guides', 'count': len(guide_videos()), 'items': guide_videos()[:16]}]
    for vid, title, _ in VROWS:
        rs = sorted(rows.get(vid, []), key=vsort)
        if VROW_NOTE.get(vid, '').startswith('Hobbyist'):
            rs.sort(key=lambda r: r.get('upload_date') or '', reverse=True)  # channels: newest first, not A–Z
        if rs:
            res.append({'id': vid, 'title': title, 'note': VROW_NOTE.get(vid), 'count': len(rs), 'items': [vcard(r) for r in rs[:16]]})
    return res


def videos_list(row, q='', length='', sort='', offset=0, limit=48):
    if row == 'guides':
        rs = [catalog()['by_id'][v['id']] for v in guide_videos()]
    else:
        rs = [r for r in catalog()['by_id'].values() if r['kind'] == 'video' and (row in ('', 'all') or vrow_of(r) == row)]
    if q:
        words = q.lower().split()
        rs = [r for r in rs if all(w in (r['title'] + ' ' + r.get('publisher', '')).lower() for w in words)]
    d = lambda r: r.get('duration') or 0
    if length == 'short':
        rs = [r for r in rs if d(r) and d(r) < 600]
    elif length == 'medium':
        rs = [r for r in rs if 600 <= d(r) < 1800]
    elif length == 'long':
        rs = [r for r in rs if d(r) >= 1800]
    if sort == 'short':
        rs.sort(key=d)
    elif sort == 'new':
        rs.sort(key=lambda r: r.get('upload_date') or '', reverse=True)
    elif sort == 'az':
        rs.sort(key=lambda r: r['title'].lower())
    elif row != 'guides':
        rs.sort(key=vsort)
    title = dict((v[0], v[1]) for v in VROWS).get(row, 'Picked for the guides' if row == 'guides' else 'All videos')
    return {'title': title, 'note': VROW_NOTE.get(row), 'total': len(rs), 'items': [vcard(r) for r in rs[offset:offset + limit]],
            'more': len(rs) > offset + limit}


# ------------------------------------------------------------- pages/text ----
def page_texts(r):
    """The document's text, one string per page. Cached per file version: a chat reads the same few documents many times."""
    p = LIB / (r.get('text_path') or f"text/{r['id']}.txt")  # catalog may be mid-rewrite and miss text_path
    try:
        st = p.stat()
    except OSError:
        return []
    return list(_page_texts(str(p), st.st_mtime_ns, st.st_size))


@functools.lru_cache(maxsize=32)
def _page_texts(path, _mtime_ns, _size):
    t = Path(path).read_text(encoding='utf-8', errors='replace')
    return tuple(t.split('\n\f\n') if '\n\f\n' in t else t.split('\f'))


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
    cues, cur = [], None
    for line in (LIB / path).read_text(encoding='utf-8', errors='replace').splitlines():
        m = re.match(r'(\d+):(\d\d):(\d\d)[.,](\d+)\s+-->', line) or re.match(r'(\d\d):(\d\d)[.,](\d+)\s+-->', line)
        if m:
            g = m.groups()
            t = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) if len(g) == 4 else int(g[0]) * 60 + int(g[1])
            cur = {'t': t, 'text': ''}
            cues.append(cur)
        elif cur is not None and line.strip() and not line.strip().isdigit():
            txt = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
            if txt and not cur['text'].endswith(txt):
                cur['text'] = (cur['text'] + ' ' + txt).strip()
    # auto captions repeat lines across cues; drop repeats and merge into ~sentence chunks
    out, last = [], ''
    for c in cues:
        t = c['text']
        if t.startswith(last) and last:
            t = t[len(last):].strip()
        if not t:
            continue
        last = c['text']
        if out and c['t'] - out[-1]['t'] < 12 and len(out[-1]['text']) < 220:
            out[-1]['text'] += ' ' + t
        else:
            out.append({'t': c['t'], 'text': t})
    return out


def transcript(video_id):
    cat = catalog()
    v = cat['by_id'].get(video_id)
    if not v:
        return None
    tx = cat['video_tx'].get(v['path'])
    vtts = []
    if tx and tx.get('vtt_path'):
        vtts.append(tx['vtt_path'])
    vtts += [c for c in v.get('captions', []) if c.endswith('.vtt')]
    for p in vtts:
        if (LIB / p).is_file():
            return {'source': (tx or {}).get('caption_type') or 'captions', 'notes': (tx or {}).get('notes'), 'cues': vtt_cues(p)}
    if tx and (LIB / tx['path']).is_file():
        return {'source': tx.get('caption_type') or 'transcript', 'notes': tx.get('notes'),
                'cues': [{'t': None, 'text': x} for x in (LIB / tx['path']).read_text(encoding='utf-8', errors='replace').split('\n\n') if x.strip()]}
    return {'source': None, 'cues': []}


# ---------------------------------------------- places and nearby help ----
# Read from the US map files an upstream pack or state map holds (map.sqlite, national.sqlite, water.sqlite, crops.sqlite,
# places.sqlite) and from world-places.sqlite, which setup builds. The window draws its own map (native/src/map.rs).
_tls = threading.local()


def mapdb():
    if not MAPDB.exists():
        return None
    if not hasattr(_tls, 'map'):
        _tls.map = sqlite3.connect(ro_uri(MAPDB), uri=True, check_same_thread=False)
    return _tls.map


def national_mapdb():
    path = HERE / 'derived/geo/national.sqlite'
    if not path.exists():
        return None
    if not hasattr(_tls, 'national_map'):
        _tls.national_map = sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False)
        _tls.national_map.execute('PRAGMA query_only=ON')
    return _tls.national_map


SOCAL_CELL = 0.05
_socal = {'cells': None}


def socal_cells():
    """0.05° cells holding detailed SoCal features; national rows inside them are duplicates and are skipped."""
    if _socal['cells'] is None:
        cache = HERE / 'derived/geo/socal-cells.json'
        try:
            _socal['cells'] = set(json.loads(cache.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            db = mapdb()
            cells = set() if db is None else {r[0] for r in db.execute(
                'SELECT DISTINCT CAST((minx+180)/? AS INT)*100000+CAST((miny+90)/? AS INT) FROM feat_idx WHERE maxx-minx<?',
                (SOCAL_CELL, SOCAL_CELL, SOCAL_CELL))}
            cache.write_text(json.dumps(sorted(cells)))
            _socal['cells'] = cells
    return _socal['cells']


def in_socal(lon, lat):
    return int((lon + 180) / SOCAL_CELL) * 100000 + int((lat + 90) / SOCAL_CELL) in socal_cells()


HELP = {'hospital': 'Hospital', 'clinic': 'Clinic', 'doctors': 'Doctor', 'pharmacy': 'Pharmacy', 'chemist': 'Pharmacy',
        'dentist': 'Dentist', 'fire_station': 'Fire station', 'police': 'Police', 'drinking_water': 'Drinking water tap',
        'fountain': 'Fountain (not necessarily drinkable)', 'water_tower': 'Water tower', 'water_works': 'Water works',
        'spring': 'Spring (untested)', 'toilet': 'Public toilet', 'shelter': 'Shelter (structure)',
        'community_centre': 'Community centre', 'town_hall': 'Town hall', 'school': 'School', 'library': 'Library',
        'supermarket': 'Supermarket', 'convenience': 'Convenience store', 'doityourself': 'Hardware / DIY store',
        'hardware': 'Hardware store', 'garden_centre': 'Garden centre', 'post_office': 'Post office',
        'camp_site': 'Campsite', 'veterinary': 'Vet', 'nursing_home': 'Care home', 'fuel': 'Fuel station',
        'car_repair': 'Car repair', 'bicycle_shop': 'Bicycle shop', 'wastewater_plant': 'Wastewater plant',
        'comms_tower': 'Communications tower', 'park': 'Park', 'courthouse': 'Courthouse',
        'college': 'College', 'university': 'University', 'kindergarten': 'Kindergarten', 'beach': 'Beach',
        'embassy': 'Embassy', 'mineral': 'Recorded mineral / mine', 'oil_gas_well': 'Recorded oil / gas well',
        'boat_launch': 'Boating facility', 'fish_stocking': 'Fish-stocking record',
        'dam': 'Dam (National Inventory of Dams)', 'reservoir': 'Reservoir', 'lake': 'Lake or pond', 'pond': 'Pond',
        'orchard': 'Orchard', 'vineyard': 'Vineyard', 'berries': 'Berry field', 'perennial_veg': 'Asparagus field'}


CROP_KINDS = {'orchard', 'vineyard', 'berries', 'perennial_veg'}
RESOURCE_KINDS = {'mineral', 'oil_gas_well', 'boat_launch', 'fish_stocking', 'dam', 'reservoir', 'lake', 'pond'} | CROP_KINDS
WATERDB = HERE / 'derived/geo/water.sqlite'
CROPDB = HERE / 'derived/geo/crops.sqlite'


def resource_dbs(kinds):
    """Point databases holding these resource kinds: the library's resource atlas and the derived water inventory."""
    water = {'dam', 'reservoir', 'lake', 'pond'}
    out = []
    if set(kinds) - water - CROP_KINDS and (LIB / 'geography/resources/atlas.sqlite').exists():
        out.append(LIB / 'geography/resources/atlas.sqlite')
    if set(kinds) & water and WATERDB.exists():
        out.append(WATERDB)
    if set(kinds) & CROP_KINDS and CROPDB.exists():
        out.append(CROPDB)
    return out


def resource_near(lat, lon, cls, limit):
    out = []
    for path in resource_dbs([cls]):
        with sqlite3.connect(ro_uri(path), uri=True) as db:
            radius = .15
            rows = []
            while radius <= 19.2:
                rows = db.execute("SELECT r.name,r.lat,r.lon,r.detail,r.source FROM spatial s CROSS JOIN resource r ON r.id=s.id WHERE r.kind=? AND s.minx>=? AND s.maxx<=? AND s.miny>=? AND s.maxy<=?", (cls,lon-radius,lon+radius,lat-radius,lat+radius)).fetchall()
                if cls == 'fish_stocking':
                    rows = list({(r[0], round(r[1], 3), round(r[2], 3), json.loads(r[3]).get('FishType')): r for r in rows}.values())
                if len(rows) >= limit:
                    break
                radius *= 2
            for name, y, x, raw, source in rows:
                detail = json.loads(raw)
                summary = _resource_summary(cls, detail) + (' · historical stocking record' if cls == 'fish_stocking' else '')
                dist, brg = haversine(lat, lon, y, x)
                out.append({'cls':cls,'label':HELP[cls],'name':name,'lat':y,'lon':x,'km':round(dist,2),'dir':brg,'detail':summary,'source':source})
    return sorted(out, key=lambda p:p['km'])[:limit]


def _resource_summary(cls, detail):
    keys = {'mineral': ('commod1', 'commod2', 'dev_stat'), 'oil_gas_well': ('WellTypeLabel', 'FieldName', 'API'),
            'fish_stocking': ('FishType', 'Counties'), 'boat_launch': ('Water_Body', 'Facility_Type'),
            'dam': ('purpose', 'river', 'county'), 'reservoir': ('reservoir_type', 'water_type', 'state'),
            'orchard': ('crop',), 'vineyard': ('crop',), 'berries': ('crop',), 'perennial_veg': ('crop',), 'lake': ('water_type', 'state'), 'pond': ('water_type', 'state')}.get(cls, ())
    out = [str(detail.get(k)) for k in keys if detail.get(k)]
    if detail.get('not_drinking_water'):
        out.insert(0, 'NOT drinking water')
    if detail.get('area_acres'):
        out.insert(0, f"{detail['area_acres']:,} acres")
    if detail.get('crop_acres'):
        out.append(f"about {round(detail['crop_acres']):,} acres in this block")
    if detail.get('normal_storage_af'):
        out.append(f"{int(float(detail['normal_storage_af'])):,} acre-ft stored")
    return ' · '.join(out)


@functools.lru_cache(maxsize=1)
def socal_classes():
    """Service classes the detailed SoCal map has; for anything else (e.g. fuel) the national points are the only ones."""
    db = mapdb()
    return frozenset(r[0] for r in db.execute('SELECT DISTINCT cls FROM poi')) if db else frozenset()


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
    db = mapdb()
    limit = max(1, min(limit, 50))
    out = []
    for cls in classes:
        if cls in RESOURCE_KINDS:
            out.extend(resource_near(lat, lon, cls, limit))
            continue
        sources = [d for d in (db, national_mapdb()) if d is not None]
        d = 0.02
        rows = []
        while d < 1.5 and len(rows) < limit:
            rows = []
            for source in sources:
                found = source.execute('SELECT cls,name,lon,lat FROM poi WHERE cls=? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?',
                                       (cls, lon - d, lon + d, lat - d, lat + d)).fetchall()
                if source is not db and db is not None and cls in socal_classes():
                    found = [r for r in found if not in_socal(r[2], r[3])]
                rows.extend(found)
            rows = list({(r[0],r[1],round(r[2],5),round(r[3],5)):r for r in rows}.values())
            d *= 2
        for c, name, x, y in rows:
            dist, brg = haversine(lat, lon, y, x)
            out.append({'cls': c, 'label': HELP.get(c, c), 'name': name, 'lat': y, 'lon': x, 'km': round(dist, 2), 'dir': brg})
    out.sort(key=lambda p: p['km'])
    out = [p for p in out if p['cls'] in RESOURCE_KINDS] + dedupe_named([p for p in out if p['cls'] not in RESOURCE_KINDS])
    out.sort(key=lambda p: p['km'])
    per = {}
    res = []
    for p in out:
        if per.get(p['cls'], 0) < limit:
            per[p['cls']] = per.get(p['cls'], 0) + 1
            res.append(p)
    return res


def haversine(la1, lo1, la2, lo2):
    R = 6371
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * R * math.asin(math.sqrt(a))
    b = (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
    return d, ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'][int((b + 22.5) // 45) % 8]


WATER_WORDS = {'lake', 'lakes', 'reservoir', 'res', 'dam', 'pond', 'ponds'}


def near_bonus(near, lat, lon):
    """0-8: places close to where the person is looking rank higher (strongly within ~60 km, gently out to ~600 km)."""
    if not near:
        return 0
    km = haversine(near[0], near[1], lat, lon)[0]
    return 6 * math.exp(-km / 60) + 2 * math.exp(-km / 600)


def word_sets(words):
    """All the words, then (for longer queries) each set with one word dropped — 'echo park lake' still finds 'Echo Lake'."""
    yield words
    if len(words) > 1:
        for i in range(len(words)):
            yield words[:i] + words[i + 1:]


def water_places(q, limit=10, near=None):
    """Named lakes, reservoirs and dams matching the query (generic words like 'lake' only help ranking)."""
    if not WATERDB.exists():
        return []
    words = [w.lower() for w in re.findall(r'\w+', q)]
    core = [w for w in words if w not in WATER_WORDS]
    if not core:
        return []
    db = sqlite3.connect(ro_uri(WATERDB), uri=True)
    try:
        try:
            rows, full = [], False
            for ws in word_sets(core):
                if rows and len(ws) < len(core) and full:
                    break  # every word matched: no need to loosen
                if ws:
                    rows += db.execute('SELECT r.kind,r.name,r.lat,r.lon,r.detail FROM water_fts f JOIN resource r ON r.id=f.rowid WHERE water_fts MATCH ? LIMIT 6000',
                                       (' '.join('"%s"*' % w.replace('"', '') for w in ws),)).fetchall()
                    if len(ws) == len(core):
                        full = bool(rows)
            rows = list(dict.fromkeys(rows))
        except sqlite3.OperationalError:  # index not built yet (import still running): slower scan
            cond = ' AND '.join("(r.name LIKE ? OR (r.kind='dam' AND r.detail LIKE ?))" for _ in core)
            rows = db.execute(f"SELECT r.kind,r.name,r.lat,r.lon,r.detail FROM resource r WHERE r.kind IN ('dam','reservoir','lake') AND r.name!='' AND {cond} LIMIT 300",
                              [x for w in core for x in (f'%{w}%', f'%{w}%')]).fetchall()
    finally:
        db.close()
    out = []
    for kind, name, lat, lon, raw in rows:
        d = json.loads(raw or '{}')
        text = (name + ' ' + (d.get('other_names') or '')).lower()
        score = (sum(w in text for w in words) * 2 + (core[0] in text) + (kind != 'dam') + (name.lower() == q.lower().strip()) * 3
                 + math.log10(float(d.get('area_acres') or d.get('surface_acres') or 1) + 1) / 3 + near_bonus(near, lat, lon))
        where = ', '.join(x for x in (d.get('county') and d['county'] + ' County', d.get('state')) if x)
        extra = f" · also called {d['other_names']}" if d.get('other_names') else ''
        away = ''
        if near:
            km = haversine(near[0], near[1], lat, lon)[0]
            away = f" · {km / 1.609:,.0f} mi away" if km >= 1.6 else ' · nearby'
        out.append((score, {'name': name, 'kind': {'dam': 'Dam', 'reservoir': 'Reservoir', 'lake': 'Lake'}[kind],
                            'detail': (where or 'United States') + extra + away, 'lat': lat, 'lon': lon, 'source': 'water', 'onMap': True}))
    out.sort(key=lambda t: -t[0])
    return [o for _, o in out[:limit]]


def places(q, limit=20, near=None):
    if address_search.is_address_query(q):
        return address_search.find_addresses(q, HERE / 'derived' / 'geo', limit=min(limit, 8), near=near)
    dbs = [sqlite3.connect(ro_uri(p), uri=True) for p in (PLACEDB, WORLD_PLACEDB) if p.exists()]
    if not dbs:
        return []
    words = re.findall(r'\w+', q)
    if not words:
        return []
    rows = []
    full = False
    for ws in word_sets(words):
        if full and len(ws) < len(words):
            break  # every word matched somewhere: no need to loosen
        if not ws:
            continue
        for db in dbs:   # the library's own gazetteer (US detail), then the world's
            rows += db.execute('SELECT p.name,p.kind,p.detail,p.lat,p.lon,p.rank,p.source FROM place_fts f JOIN place p ON p.id=f.rowid '
                               'WHERE place_fts MATCH ? ORDER BY p.rank, bm25(place_fts) LIMIT 200',
                               (' '.join('"%s"*' % w.replace('"', '') for w in ws),)).fetchall()
        if len(ws) == len(words):
            full = bool(rows)
    rows = list(dict.fromkeys(rows))
    def norm(t):
        return ' '.join(re.sub(r'\bsaint\b', 'st', re.sub(r'[^\w ]+', ' ', t.lower())).split())
    ql = norm(q)
    nat = national_mapdb()
    size = {}

    def importance(r):
        # GNIS has no population: use OpenStreetMap's city/town tag near the same point so the big Springfield comes first
        if nat is None or norm(r[0]) != ql or len(size) > 80:
            return 3
        k = (round(r[3], 2), round(r[4], 2))
        if k not in size:
            hit = nat.execute("SELECT MIN(CASE f.cls WHEN 'city' THEN 0 WHEN 'town' THEN 1 ELSE 2 END) FROM feat_idx i JOIN feat f ON f.id=i.id "
                              "WHERE i.minx BETWEEN ? AND ? AND i.miny BETWEEN ? AND ? AND i.minz<=10 AND f.layer='place' AND f.name=?",
                              (r[4] - .08, r[4] + .08, r[3] - .08, r[3] + .08, r[0])).fetchone()
            size[k] = 2 if hit[0] is None else hit[0]
        return size[k]
    rows.sort(key=lambda r: (norm(r[0]) != ql, not norm(r[0]).startswith(ql), r[5] > 2 and norm(r[0]) == ql and importance(r),
                             -round(near_bonus(near, r[3], r[4])), r[5]))
    seen, out = set(), []
    for name, kind, detail, lat, lon, rank, src in rows:
        k = (name.lower(), round(lat, 2), round(lon, 2))
        if k in seen:
            continue
        seen.add(k)
        out.append({'name': name, 'kind': kind, 'detail': detail, 'lat': lat, 'lon': lon, 'source': src,
                    'onMap': bool(pack_info()) or 32.5 < lat < 35.9 and -121.4 < lon < -114.1})
        if len(out) >= limit:
            break
    water = water_places(q, near=near)
    if water:  # a query that names a water word puts lakes and dams first; otherwise towns lead
        out = (water + out) if set(w.lower() for w in re.findall(r'\w+', q)) & WATER_WORDS else (out[:8] + water + out[8:])
    return out[:limit + len(water)]


_cov = {'at': 0}


def coverage():
    """What the assistant can and cannot read — reported, not hidden."""
    if time.time() - _cov['at'] > 300:
        cat = catalog()
        vids = [r for r in cat['by_id'].values() if r['kind'] == 'video']
        with_text = sum(1 for r in vids if cat['video_tx'].get(r['path']) or any(c.endswith('.vtt') for c in r.get('captions', [])))
        pdfs = [r for r in cat['by_id'].values() if r['kind'] == 'pdf']
        img = sum(1 for r in pdfs if (r.get('searchability') or '').startswith('image-only'))
        gaps = 0
        try:
            c = sqlite3.connect(ro_uri(retrieval.IDX / 'semantic.sqlite'), uri=True, timeout=5)
            gaps = c.execute('SELECT count(*) FROM gaps').fetchone()[0]
            c.close()
        except sqlite3.Error:
            pass
        _cov.update(at=time.time(), data={'videos': len(vids), 'videos_with_text': with_text, 'pdfs': len(pdfs), 'pdfs_image_only': img,
                                          'docs_without_text': gaps, 'remote_videos': len(remote_records()),
                                          'videos_excluded': len({i for i in excluded_videos() if i.startswith('video-')})})
    return _cov['data']


def pack_info():
    """The open download's own description, or None when this is not a pack. Read once: a pack does not change while it is open."""
    if 'pack' not in _pack:
        try:
            _pack['pack'] = json.loads((HERE / 'pack.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            _pack['pack'] = None
    return _pack['pack']


_pack = {}
SOURCES_PAGE = 'documents/lastbook/sources-and-permissions.html'   # every pack's own credits page, an ordinary record


# --------------------------------------------------------------- handler ----
class Handler(library.Handler):
    def send_json(self, obj, status=200):
        data = json.dumps(obj, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # The only parts of the library the window gets: no worker state, logs, tools, sign-in settings or reports.
    SERVED_PREFIXES = ('/api/', '/zim/', '/documents/', '/pages/', '/videos/', '/videos-external/', '/transcripts/', '/metadata/videos/', '/courses/', '/collections/', '/assets/')
    SERVED_ONLY = True  # engine/library.py: a file must really sit in one of these folders, whatever links lead there
    LIBRARY_ROUTES = ('/api/health', '/api/download-status', '/api/collection-search', '/api/search')  # answered in engine/library.py
    LIBRARY_PREFIXES = tuple(p for p in SERVED_PREFIXES if p not in ('/api/', '/zim/'))
    # What a browser runs scripts in when it is opened directly. An <img> never runs them and shows it as before.
    ACTIVE_SUFFIXES = ('', '.html', '.htm', '.shtml', '.xhtml', '.xht', '.svg', '.svgz', '.xml', '.xsl', '.xslt', '.rdf', '.mht', '.mhtml')
    # Library content keeps what it says and runs none of its code: saved web pages, and encyclopedia pages too.
    ACTIVE_TYPES = re.compile(r'(?i)html|xml|xsl|svg|hta|component|rfc822|multipart/related')  # the same, judged by what the browser will be told: the suffix list can't cover what a machine's own type list adds
    # No allow-popups: the one window opens nothing for a page anyway, so all it gave a page was another way to ask. No allow-downloads, no allow-forms.
    # It covers what a sandbox does not: <noscript> frames come alive with scripts off, and <form>, <base> and <object> still load things without any.
    NO_CODE_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; worker-src 'none'; object-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; "
                   "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; media-src 'self'; frame-src 'self'")
    SAVED_PAGE_CSP = 'sandbox; ' + NO_CODE_CSP
    ZIM_CSP = 'sandbox allow-same-origin; ' + NO_CODE_CSP   # same-origin so the reader can follow the entry's title. A page with no scripts can do nothing with it.

    def parse_request(self):
        """Runs before any GET, HEAD or POST handling. Python rewrites a leading '//' in self.path, so read the target as it was sent."""
        if not super().parse_request():
            return False
        words = self.requestline.split()
        target = words[1] if len(words) > 1 else ''
        if not target.startswith('/') or target.startswith('//'):
            self.close_connection = True
            self.send_error(400)
            return False
        return True

    def path_ok(self, path):
        if '\0' in path:
            return False
        if path != '/reader.css' and not path.startswith(self.SERVED_PREFIXES):
            return False
        if '/..' in path or path.lower().endswith('.info.json'):  # yt-dlp's .info.json can hold request headers
            return False
        if path.startswith(('/api/', '/zim/')):  # /zim/ file names are checked in engine/library.py
            return True
        return self.file_ok(self.translate_path(self.path), folders=True)

    def list_directory(self, path):
        self.send_error(404)

    def send_header(self, keyword, value):
        if keyword.lower() == 'content-type':
            self.sent_type = value
        super().send_header(keyword, value)

    def send_head(self):
        """Only files get here. /api/ and /zim/ are addresses, never folders, whatever the library folder holds."""
        if urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).startswith(('/api/', '/zim/')):
            self.send_error(404)
            return None
        page = self.saved_page_file()
        if not page:
            return super().send_head()
        data = saved_pages.rewrite(Path(page).read_bytes(), self.saved_page_part)
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
        """engine/library.py sends some saved pages from memory, and every encyclopedia page, through here."""
        if status == 200 and 'html' in mime and len(data) <= saved_pages.MAX_PAGE and urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).startswith(self.LIBRARY_PREFIXES):
            data = saved_pages.rewrite(data, self.saved_page_part)
            if '/documents/lastbook/' in urllib.parse.unquote(urllib.parse.urlsplit(self.path).path):
                data = saved_pages.own_look(data)
        elif status == 200 and ('html' in mime or 'svg' in mime) and urllib.parse.urlsplit(self.path).path.startswith('/zim/'):
            data = saved_pages.article(data)
            if 'html' in mime:
                data = saved_pages.article_look(data)
        super().send_data(data, mime, status)

    def end_headers(self):
        sent_type = getattr(self, 'sent_type', '')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.sent_type = ''
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(getattr(self, 'path', '')).path)  # no self.path yet on a malformed request line
        except ValueError:
            path = ''
        if path.startswith('/zim/'):
            # Every answer under /zim/: pages, redirects and errors alike.
            self.send_header('Content-Security-Policy', self.ZIM_CSP)
            self.send_header('X-DNS-Prefetch-Control', 'off')
        elif path.startswith(self.LIBRARY_PREFIXES) and (Path(path).suffix.lower() in self.ACTIVE_SUFFIXES or self.ACTIVE_TYPES.search(sent_type)):
            self.send_header('Content-Security-Policy', self.SAVED_PAGE_CSP)
            self.send_header('X-DNS-Prefetch-Control', 'off')   # a CSP does not cover the names a browser looks up ahead of a click
        elif path.startswith(self.LIBRARY_PREFIXES) and Path(path).suffix.lower() in ('.woff2', '.woff', '.ttf', '.otf'):
            self.send_header('Access-Control-Allow-Origin', 'null')   # the sandbox above leaves a saved page origin null, and fonts always load by CORS
        super().end_headers()

    def internal_error(self):
        """The details go to the log. What reaches the client names no file path or user."""
        traceback.print_exc()
        return self.send_json({'error': f'Server error ({sys.exc_info()[0].__name__}).'}, 500)

    def do_HEAD(self):
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if not self.path_ok(path):
                return self.send_error(404)
            return super().do_HEAD()
        except library.CLIENT_GONE:
            return
        except Exception:
            traceback.print_exc()
            return self.send_error(500)

    def do_POST(self):
        try:
            return self.handle_post()
        except library.CLIENT_GONE:
            return
        except Exception:
            return self.internal_error()

    def handle_post(self):
        rpath = urllib.parse.urlsplit(self.path).path
        if rpath.startswith('/api/setup/'):  # Library and local AI: engine/setup/api.py
            return setup_api.handle(self, 'POST', rpath)
        if rpath == '/api/assistant/local':   # the optional local AI: look again for the file, turn it on or off, stop it
            if not assistant.LOCAL:
                return self.send_json({'error': 'no local AI in this version'}, 404)
            try:
                n = int(self.headers.get('Content-Length') or 0)
                action = json.loads(self.rfile.read(min(max(n, 0), 2000)) or b'{}').get('action')
            except (ValueError, AttributeError):
                action = None
            if action == 'rescan':
                threading.Thread(target=assistant.LOCAL.discover, daemon=True).start()
            elif action in ('on', 'off'):
                assistant.LOCAL.set_enabled(action == 'on')
            elif action == 'stop':
                assistant.LOCAL.stop('asked')
            elif action == 'warm':   # Chat is open: start loading before the question comes
                assistant.warm_local()
            else:
                return self.send_json({'error': 'action is rescan, on, off, stop or warm'}, 400)
            return self.send_json(assistant.LOCAL.status())
        if rpath != '/api/chat':
            return self.send_json({'error': 'not found'}, 404)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n < 0:
                raise ValueError
            if n > 200_000:
                return self.send_json({'error': 'request too large'}, 413)
            req = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self.send_json({'error': 'bad request'}, 400)
        msgs = [m for m in (req.get('messages') or []) if isinstance(m, dict)][-12:]
        about = str(req.get('about') or '')[:200] or None
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        gone = [False]

        def emit(ev):
            if gone[0]:
                return
            try:
                self.wfile.write(b'data: ' + json.dumps(ev).encode() + b'\n\n')
                self.wfile.flush()
            except library.CLIENT_GONE:
                gone[0] = True  # the person pressed Stop or left; stop calling the model
        # Just after start the local AI may still be finding, checking or measuring its model file: wait for that to
        # settle rather than answering "no model" to the first question.
        waited = 0.0
        while assistant.LOCAL and waited < 600 and (assistant.LOCAL.state in ('checking', 'measuring')
                                                     or (assistant.LOCAL.state == 'absent' and waited < 5)):
            if waited == 0:
                emit({'type': 'state', 'state': 'starting'})
            time.sleep(0.5)
            waited += 0.5
        st = assistant.model_status()
        if not st['online']:
            emit({'type': 'error', 'error': 'model_unavailable', 'text': 'The local assistant model is not running right now.'})
            return
        try:
            assistant.chat(msgs, about, emit, lambda: gone[0])
        except Exception as e:
            traceback.print_exc()
            emit({'type': 'error', 'error': 'internal', 'text': f'The assistant stopped on an internal error ({type(e).__name__}).'})

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
                return self.send_json(public(r, full=True) if r else {'error': 'This item is not in the library catalog.'}, 200 if r else 404)
            if path.startswith('/api/transcript/'):
                t = transcript(path[16:])
                return self.send_json(t if t is not None else {'error': 'unknown video'}, 200 if t is not None else 404)
            if path == '/api/retrieve':
                return self.send_json({'q': qs.get('q', ''), 'results': assistant.hybrid(qs.get('q', '')[:200], min(20, int(qs.get('k', 8))), qs.get('kind', 'any')),
                                       'semantic': INDEX.status if INDEX else None})
            if path == '/api/assistant/status':
                return self.send_json(dict(assistant.model_status(), coverage=coverage()))
            if path == '/api/videos':
                return self.send_json(videos_home())
            if path.startswith('/api/videos/'):
                return self.send_json(videos_list(path[12:], qs.get('q', ''), qs.get('len', ''), qs.get('sort', ''), int(qs.get('offset', 0))))
            if path == '/api/shelves':
                return self.send_json(shelves())
            if path.startswith('/api/shelf/'):
                return self.send_json(shelf(path[11:], qs.get('kind', 'all'), qs.get('q', ''), int(qs.get('offset', 0))))
            if path == '/api/places':
                try:
                    near_pt = tuple(map(float, qs['near'].split(',')))[:2] if qs.get('near') else None
                except ValueError:
                    near_pt = None
                return self.send_json(places(qs.get('q', '')[:100], near=near_pt))
            if path == '/api/library':
                s = json.loads((LIB / 'summary.json').read_text(encoding='utf-8'))
                sources = (catalog()['by_path'].get(SOURCES_PAGE) or {}).get('id')
                return self.send_json({**{k: s.get(k) for k in ('resources', 'pdfs', 'pdf_pages', 'web_pages', 'videos', 'collections', 'document_bytes')},
                                       'zims': zim_names(), 'sources': sources})
            if path.startswith('/api/') and path not in self.LIBRARY_ROUTES:
                return self.send_json({'error': 'not found'}, 404)
            return super().do_GET()
        except library.CLIENT_GONE:
            return
        except Exception:  # a readable error instead of a dropped connection
            return self.internal_error()


def init(no_index=False):
    """Everything the engine needs before it can answer a request. Opens no socket."""
    global INDEX
    catalog()
    INDEX = retrieval.Index(catalog, trust, library.DB)
    assistant.init(sys.modules[__name__], INDEX)
    if not no_index:
        threading.Thread(target=INDEX.run_forever, daemon=True).start()
