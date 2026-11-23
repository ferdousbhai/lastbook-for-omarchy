"""Lastbook from the command line, for scripts and agents: the library's search, pages, transcripts, places and guides as
JSON, with no window.

  lastbook search "treat a burn" --limit 5

The native program (native/src/main.rs) runs this file for every command but `skill`; `lastbook --help` lists them.
One JSON value on standard output; an error is one line on standard error. Exit codes, as Omarchy's commands have them:
0 success (including no matches), 1 any failure (invalid arguments, no library, unknown document); the line on standard
error says which.

It reads the same library as the window and may run beside it: it writes only the caches the engine already keeps
(rendered pages, encyclopedia openings). Only `open` starts a window, and only `index` builds the meaning-search index
(retrieval.py), in the foreground, until nothing is left to embed: what the user service lastbook-index.service runs, so
that the index does not need a window. Recall not being there is no error for `index`: it says so and exits 0.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]
if (HERE.parent / 'lib').is_dir():   # the installed package: its lib/ before the system (engine/paths.py INSTALLED)
    sys.path.insert(2, str(HERE.parent / 'lib'))

KINDS = ('any', 'manual', 'video', 'encyclopedia')
ROUTES = ('read', 'wiki', 'watch', 'guide', 'web', 'search', 'shelf', 'map', 'section', 'manage')   # native/src/ui.rs Route::from_key
MAX_PAGES = 10
ARTICLE_CHARS = 20000


class Failed(Exception):
    """A runtime error: its text is the one line the person sees (exit 1)."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f'lastbook: {message}', file=sys.stderr)
        sys.exit(1)


def bounded(low, high):
    def parse(v):
        n = int(v)
        if not low <= n <= high:
            raise argparse.ArgumentTypeError(f'must be a number from {low} to {high}')
        return n
    parse.__name__ = 'number'
    return parse


def page_list(v):
    """'3,4,7-9' -> [3, 4, 7, 8, 9]."""
    out = []
    for part in v.split(','):
        m = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+)\s*)?', part)
        if not m or int(m[1]) < 1 or (m[2] and int(m[2]) < int(m[1])):
            raise argparse.ArgumentTypeError('pages look like 3,4,5 or 3-5')
        if m[2] and int(m[2]) - int(m[1]) >= MAX_PAGES:
            raise argparse.ArgumentTypeError(f'at most {MAX_PAGES} pages per call')
        out.extend(range(int(m[1]), int(m[2] or m[1]) + 1))
    if len(out) > MAX_PAGES:
        raise argparse.ArgumentTypeError(f'at most {MAX_PAGES} pages per call')
    return out


def parser():
    ap = Parser(prog='lastbook', add_help=False)
    ap.add_argument('--data')
    sub = ap.add_subparsers(dest='command', required=True, parser_class=Parser)

    def cmd(name, *args):
        p = sub.add_parser(name, add_help=False)
        p.add_argument('--data', default=argparse.SUPPRESS)   # accepted before or after the command
        for names, opts in args:
            p.add_argument(*names, **opts)
        return p
    cmd('search', (['query'], {}), (['--kind'], {'choices': KINDS, 'default': 'any'}), (['--limit'], {'type': bounded(1, 20), 'default': 8}))
    cmd('read', (['doc_id'], {}), (['--pages'], {'type': page_list}), (['--section'], {}))
    cmd('page', (['doc_id'], {}), (['page'], {'type': bounded(1, 100000)}), (['--out'], {}), (['--width'], {'type': bounded(160, 1600), 'default': 1200}))
    cmd('transcript', (['video_id'], {}), (['--query'], {}))
    cmd('place', (['query'], {}), (['--nearby'], {}))
    cmd('guides')
    cmd('guide', (['id'], {}))
    cmd('status')
    cmd('index', (['--wait'], {'type': bounded(0, 86400), 'default': 0}), (['--pause'], {'type': bounded(0, 3600), 'default': 15}))
    cmd('open', (['target'], {}), (['at'], {'nargs': '?'}), (['--dry-run'], {'action': 'store_true'}))
    return ap


# ------------------------------------------------------------------------------------------------------------ the engine --
def library(given):
    """The library folder (--data, LASTBOOK_DATA, the one the window opened last, the app's own), or Failed."""
    import main as app_main
    named = given or os.environ.get('LASTBOOK_DATA')
    data = app_main.data_folder(given)
    found, why = app_main.check(data) if data is not None else (None, None)
    if found is None and not named:
        found = first_library(app_main)
    if found is None:
        if why and named:
            raise Failed(why)
        remembered = app_main.remembered()
        if remembered is not None and not remembered.exists():      # a library was set up, and its folder is not there now
            raise Failed(f'The library folder {remembered} is not there (is its disk disconnected?). Connect it, or run `lastbook` to choose another.')
        raise Failed('No library yet: run `lastbook` to set one up.')
    return found


def first_library(app_main):
    """Setup has finished a first library and nothing has opened it yet (the window does that at its next start): the
    switch it staged is made here, as the window would, so a command right after setup finds the library. Only when no
    library is open anywhere: an upgrade waiting beside a library in use is the window's to make."""
    from engine.setup import api as setup_api
    settings = app_main.app_folders()[0]
    try:
        lib, note = setup_api.startup(settings)
    except Exception:
        return None
    if not lib:
        return None
    app_main.remember(lib)
    setup_api.opened(settings, lib, note)
    app_main.link_library(lib)
    return app_main.check(lib)[0]


def engine(data):
    """The engine (server.py) over the library, as the window has it, without its indexer thread (`index` runs the indexer
    itself, in the foreground)."""
    os.environ['LASTBOOK_DATA'] = str(data)   # engine/paths.py reads it when first imported
    import server
    server.init(no_index=True)
    return server


def head(S, r):
    """What names a document in every answer."""
    pub = S.public(r)
    out = {'doc_id': r['id'], 'title': pub['title'], 'publisher': pub['publisher'], 'year': pub['year'], 'kind': r['kind'], 'trust': pub['trust']}
    if r.get('encyclopedia'):
        out['encyclopedia'] = r['encyclopedia']
    if r.get('notes'):
        out['notes'] = r['notes'][:300]
    return out


def document(S, doc_id):
    r = S.record(doc_id)
    if not r or r['kind'] == 'metadata':
        raise Failed(f'No document with id {doc_id!r} in the library. Find ids with: lastbook search')
    return r


# -------------------------------------------------------------------------------------------------------------- commands --
def search(S, a):
    out = {'query': a.query, 'results': S.hybrid(a.query[:200], a.limit, a.kind)}
    nudge(S)
    return out


def read(S, a):
    r = document(S, a.doc_id)
    if r['kind'] == 'article':
        section = (a.section or '')[:80]
        if section and not S.ENC.matched(a.doc_id, section):
            heads = S.ENC.headings(a.doc_id) or []
            raise Failed(f'No section of {a.doc_id} has a heading containing {section!r}. '
                         + ('Its sections: ' + '; '.join(heads) if heads else 'It has no section headings') + '.')
        _, text = S.ENC.read(a.doc_id, section, ARTICLE_CHARS)
        return {**head(S, r), 'section': a.section or None, 'texts': [{'text': text}]}
    if r['kind'] == 'video':
        raise Failed(f'{a.doc_id} is a video: lastbook transcript {a.doc_id}')
    if r['kind'] == 'transcript':
        return {**head(S, r), 'texts': [{'text': (S.LIB / r['path']).read_text(encoding='utf-8', errors='replace')}]}
    texts = S.page_texts(r)
    if not texts:
        raise Failed(f'{a.doc_id} has no text the engine can read' + (': lastbook page shows a PDF page as a picture' if r['kind'] == 'pdf' else ''))
    pages = a.pages or [1]
    missing = [p for p in pages if p > len(texts)]
    if missing:
        raise Failed(f'Page {missing[0]} does not exist: the document has {len(texts)} pages')
    out = [{'page': p, 'text': re.sub(r'[ \t]{2,}', ' ', texts[p - 1]).strip()} for p in pages]
    if r['kind'] == 'pdf':
        for t in out:
            if not t['text']:   # a scan or a picture: its page as an image is what there is
                t['note'] = f"Page {t['page']} has no text the engine can read: lastbook page {a.doc_id} {t['page']} shows it as a picture."
        if all('note' in t for t in out):
            raise Failed(' '.join(t['note'] for t in out) if len(out) == 1 else
                         f"Pages {', '.join(str(p) for p in pages)} have no text the engine can read: lastbook page {a.doc_id} N shows a page as a picture.")
    return {**head(S, r), 'pages': len(texts), 'texts': out}


def page(S, a):
    r = document(S, a.doc_id)
    if r['kind'] != 'pdf':
        raise Failed(f'{a.doc_id} is not a PDF: only PDF pages can be drawn')
    if a.page > (r.get('pages') or 1):
        raise Failed(f"Page {a.page} does not exist: the document has {r.get('pages') or 1} pages")
    try:
        png = S.render_page(r, a.page, a.width)
    except Exception as e:
        raise Failed(f'Could not draw page {a.page} of {a.doc_id} ({type(e).__name__})') from None
    if a.out:
        out = Path(a.out).expanduser()
    else:
        cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'Lastbook' / 'pages'
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / (re.sub(r'[^\w.-]', '_', f"{r['id']}-{a.page}-{a.width}") + '.png')
    try:
        out.write_bytes(png)
    except OSError as e:
        raise Failed(f'Could not write {out} ({e.strerror})') from None
    return {'path': str(out.resolve()), 'doc_id': r['id'], 'title': S.public(r)['title'], 'page': a.page, 'width': a.width}


def transcript(S, a):
    r = S.catalog()['by_id'].get(a.video_id)
    if not r or r['kind'] != 'video':
        raise Failed(f'No video with id {a.video_id!r} in the library. Find ids with: lastbook search --kind video')
    got = S.transcript(a.video_id) or {}
    cues = got.get('cues') or []
    out = {**head(S, r), 'duration': S.public(r).get('duration'), 'source': got.get('source'), 'query': a.query or None}
    if a.query and cues:
        # The passages nearest the question by meaning; without Recall, the lines that share a word with it.
        near = S.INDEX.semantic(a.query[:200], 4, doc=a.video_id)
        if near:
            cues = [{'t': w['t'], 'text': w['text']} for w in sorted(near, key=lambda w: w['t'] or 0)]
        else:
            words = [w for w in re.findall(r'[a-z0-9]{3,}', a.query.lower()) if w not in S.STOP]
            cues = [c for c in cues if any(w in c['text'].lower() for w in words)]
        out['matched_by'] = 'meaning' if near else 'words'
    return {**out, 'lines': cues}


def place(S, a):
    kinds = [k.strip() for k in (a.nearby or '').split(',') if k.strip()]
    bad = [k for k in kinds if k not in S.HELP]
    if bad:
        print(f"lastbook: --nearby takes {', '.join(sorted(S.HELP))} (not {bad[0]})", file=sys.stderr)
        sys.exit(1)
    try:
        S.point(a.query)
    except ValueError as e:
        print(f'lastbook: {e}', file=sys.stderr)
        sys.exit(1)
    places = S.places(a.query[:100], 5)
    out = {'query': a.query, 'places': places}
    if kinds:
        # Help points only around a place that is surely the one asked for, and only where a street-level map covers it:
        # otherwise `nearby` is empty and `nearby_note` says why, in words an agent can pass on.
        first = places[0] if places else None
        out['nearby_of'] = out['covered'] = None
        out['nearby'] = []
        if first is None:
            from engine.setup import maps
            older = S.WORLD_PLACEDB.exists() and not maps.places_current(S.WORLD_PLACEDB)
            out['nearby_note'] = ('No place by that name in the offline gazetteer ('
                                  + ('this library has the older one, of towns of 15,000+ only: ticking "World map and place search" in '
                                     "Lastbook's setup makes the one with villages" if older else 'countries, regions, and places of 500 people or more')
                                  + '). Try the nearest larger town, or coordinates: lastbook place "LAT,LON".')
        elif first['match'] == 'partial':
            out['nearby_note'] = (f"The best match, {first['name']} ({first['detail']}), does not match every word of the query, so no help points "
                                  'are looked up around it. Try the nearest larger town, or coordinates: lastbook place "LAT,LON".')
        else:
            out['nearby_of'], out['covered'] = first['name'], first['onMap']
            if first['onMap']:
                out['nearby'] = S.near(first['lat'], first['lon'], kinds, 5)
            else:
                from engine.setup import maps
                installed = [r['name'] for r in maps.installed(S.GEO)['regions']]
                out['regions_installed'] = installed
                out['nearby_note'] = (f"No street-level map covers {first['name']}, so help points cannot be looked up there. "
                                      + (f"Installed: {', '.join(installed)}. " if installed else 'None is installed. ')
                                      + "Add the region's map in Lastbook's setup.")
    out['note'] = 'Offline map data: may be outdated, shows no closures or damage; distances are straight lines.'
    return out


def guides(S, a):
    return {'guides': [{'id': t['id'], 'title': t.get('title'), 'short': t.get('short'), 'mode': t.get('mode'), 'keywords': t.get('keywords', [])}
                       for t in S.topics() if not t.get('error')]}


def guide(S, a):
    t = next((t for t in S.topics_resolved() if t['id'] == a.id), None)
    if t is None:
        raise Failed(f'No guide with id {a.id!r}. List them with: lastbook guides')

    def item(i):
        """A card as an agent uses it: what it says and the page it opens (doc_id + page, or an encyclopedia article)."""
        out = {k: i[k] for k in ('label', 'text', 'note', 'page', 't') if i.get(k) is not None}
        if i.get('doc'):
            out['doc_id'] = i['doc']
            if i.get('src'):
                out.update(title=i['src']['title'], kind=i['src']['kind'])
        if i.get('zim'):
            out['doc_id'] = 'zim:' + i['zim'].strip('/').removeprefix('zim/')
        return out

    def step(c):
        return {**{k: v for k, v in c.items() if k != 'read'}, 'read': [item(i) for i in c.get('read', [])]}
    out = {k: t.get(k) for k in ('id', 'title', 'short', 'mode', 'intro')}
    out['choices'] = [step(c) for c in t.get('choices', [])]
    if t.get('route'):
        out['route'] = [step(c) for c in t['route']]
    for k in ('limits', 'prepare', 'learn'):
        out[k] = [item(i) for i in t.get(k, [])]
    out['related'] = t.get('related', [])
    return out


DEFAULT_LIBRARY = True   # main(): False when --data or LASTBOOK_DATA named the library (the index service finds its own)


def meaning(S):
    """Where meaning-based search stands, honestly: whether it can answer now, and how much of the library it covers."""
    import retrieval
    index = S.INDEX
    index.refresh()
    try:
        client = retrieval.Recall()
        recall = {'installed': True, 'running': True, 'model': client.capabilities(timeout=2).get('source')}
    except retrieval.RecallUnavailable as e:
        recall = {'installed': retrieval.recall_installed(), 'running': False, 'error': str(e)}
    cov = index.coverage()
    same = bool(recall['running'] and index.source and recall.get('model') == index.source)
    if same and index.dim:   # the same model name with vectors of another width cannot be compared either
        try:
            recall['width'] = int(client.embed(['width'], timeout=5).shape[1])
            same = recall['width'] == index.dim
        except retrieval.RecallUnavailable as e:
            same, recall['error'] = False, str(e)
    indexing = index.indexing_now()
    state = ('indexing' if indexing else 'Recall not installed' if not recall['installed'] else 'waiting for Recall' if not recall['running']
             else 'idle')
    covered = cov['indexed'] + cov['no_text']
    out = {'ready': bool(same and cov['passages']),                # it can answer now, from what is indexed so far
           'complete': not cov['pending'] and not cov['failed'],   # every document that can be embedded is
           'coverage': round(covered / cov['total'], 3) if cov['total'] else 1.0,
           'state': state, 'recall': recall,
           'passages': cov['passages'], 'documents': cov['indexed'], 'documents_total': cov['total'],
           'pending': cov['pending'], 'failed': cov['failed'], 'no_text': cov['no_text']}
    if index.status.get('damaged'):
        out['damaged'] = True   # the index file cannot be read: the next `lastbook index` builds a new one
    return out


def nudge(S, m=None):
    """Documents wait to be embedded, Recall is there and nobody is indexing: ask the index service to (retrieval.kick),
    which costs this command nothing. Only the installed program, on the library the service would find (kick decides)."""
    import retrieval
    if not retrieval.recall_socket().exists():
        return False
    try:
        pending = m['pending'] if m else S.INDEX.coverage()['pending']
        return bool(pending and not S.INDEX.indexing_now() and retrieval.kick(DEFAULT_LIBRARY))
    except Exception:   # never the reason a command fails
        return False


def status(S, a):
    from engine.setup import maps
    try:
        summary = json.loads((S.LIB / 'summary.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        summary = {}
    zims = S.zim_names()
    m = meaning(S)
    if m['state'] == 'idle' and nudge(S, m):
        m['index_service'] = 'started'
    import main as app_main
    return {'version': app_main.version(),
            'library': str(S.LIB), 'data': str(S.HERE),
            'counts': {'documents': summary.get('resources', len(S.catalog()['by_id'])), 'pdfs': summary.get('pdfs'), 'pages': summary.get('pdf_pages'),
                       'web_pages': summary.get('web_pages'), 'videos': summary.get('videos'), 'encyclopedias': len(zims)},
            'encyclopedias': zims,
            'meaning_search': m,
            'maps': maps.installed(S.HERE / 'derived' / 'geo')}


def index(S, a):
    """Build the meaning-search index until nothing is pending (Index.build), then say where it stands. Progress goes to
    standard error, a JSON line about every half minute. `outcome`: complete; incomplete (some documents could not be
    read or embedded: `failed`); waiting for Recall or Recall not installed (nothing to embed with: not an error);
    indexing elsewhere (another process holds the indexer: it stands down); stopped (asked to: what is saved stays).
    --wait: seconds to keep asking a Recall that is running but not answering yet; --pause: seconds between two tries."""
    import signal
    ix = S.INDEX
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: ix.stop.set())   # ends after the batch in hand; nothing embedded is lost

    def report(st):
        print(json.dumps({'indexing': {k: st.get(k) for k in ('state', 'indexed', 'total', 'pending', 'failed', 'chunks', 'rate', 'doc')}}),
              file=sys.stderr, flush=True)
    out = ix.build(wait=a.wait, pause=a.pause, report=report)
    m = meaning(S)
    if out['outcome'] == 'waiting for Recall' and not m['recall']['installed']:
        out['outcome'] = 'Recall not installed'
    return {**out, 'meaning_search': m}


def open_route(S, a):
    """The window's route for a target: a route key as it is, or a document id (and page, or seconds into a video)."""
    target = a.target
    kind = target.split(':', 1)[0]
    if kind in ROUTES and ':' in target:
        return target if a.at is None else f'{target}:{a.at}'
    r = document(S, target)
    at = a.at or ''
    if at and not at.isdigit():
        print('lastbook: open DOC_ID takes a page number (a video: seconds)', file=sys.stderr)
        sys.exit(1)
    if r['kind'] == 'article':
        return 'wiki:' + r['path'].removeprefix('zim/')
    if r['kind'] == 'video':
        return f"watch:{r['id']}:{at or 0}"
    return f"read:{r['id']}:{at or 1}"


def open_window(S, a):
    route = open_route(S, a)
    program = os.environ.get('LASTBOOK_BIN') or '/usr/bin/lastbook'
    argv = [program, '--data', str(S.HERE)]
    if not a.dry_run:
        try:   # a window of its own, living on after this command
            subprocess.Popen(argv, env={**os.environ, 'LASTBOOK_OPEN': route}, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise Failed(f'Could not start {program} ({e.strerror})') from None
    return {'route': route, 'opened': not a.dry_run, 'command': argv}


COMMANDS = {'search': search, 'read': read, 'page': page, 'transcript': transcript, 'place': place, 'guides': guides, 'guide': guide,
            'status': status, 'index': index, 'open': open_window}


def main(argv=None):
    a = parser().parse_args(argv)
    out = sys.stdout
    sys.stdout = sys.stderr   # whatever the engine prints stays out of the JSON
    global DEFAULT_LIBRARY
    DEFAULT_LIBRARY = not (a.data or os.environ.get('LASTBOOK_DATA'))
    try:
        try:
            data = library(a.data)
        except Failed:
            if a.command != 'index' or not DEFAULT_LIBRARY:
                raise
            # The index service runs at every login, with or without a library: nothing to do is not a failure.
            out.write(json.dumps({'outcome': 'no library'}) + '\n')
            out.flush()
            return 0
        result = COMMANDS[a.command](engine(data), a)
    except Failed as e:
        print(f'lastbook: {e}', file=sys.stderr)
        return 1
    except Exception as e:   # one line, not a traceback: LASTBOOK_DEV shows where
        if os.environ.get('LASTBOOK_DEV'):
            raise
        print(f'lastbook: {a.command} failed ({type(e).__name__}: {e})', file=sys.stderr)
        return 1
    out.write(json.dumps(result, ensure_ascii=False) + '\n')
    out.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
