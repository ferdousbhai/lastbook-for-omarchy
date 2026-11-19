"""Lastbook from the command line, for scripts and agents: the library's search, pages, transcripts, places and guides as
JSON, with no window.

  lastbook search "treat a burn" --limit 5

The native program (native/src/main.rs) runs this file for every command but `skill`; `lastbook --help` lists them.
One JSON value on standard output; an error is one line on standard error. Exit codes: 0 success (including no
matches), 1 runtime error (no library, unknown document, no local AI), 2 invalid arguments.

It reads the same library as the window and may run beside it: it never starts the indexer and writes only the caches
the engine already keeps (rendered pages, encyclopedia openings). Only `ask` loads the local AI; only `open` starts a window.
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
ROUTES = ('read', 'wiki', 'watch', 'guide', 'web', 'search', 'shelf', 'map', 'section', 'manage', 'chat')   # native/src/ui.rs Route::from_key
MAX_PAGES = 10
ARTICLE_CHARS = 20000


class Failed(Exception):
    """A runtime error: its text is the one line the person sees (exit 1)."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f'lastbook: {message}', file=sys.stderr)
        sys.exit(2)


def bounded(low, high):
    def parse(v):
        n = int(v)
        if not low <= n <= high:
            raise argparse.ArgumentTypeError(f'must be {low}..{high}')
        return n
    parse.__name__ = 'number'
    return parse


def page_list(v):
    """'3,4,7-9' -> [3, 4, 7, 8, 9]."""
    out = []
    for part in v.split(','):
        m = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+)\s*)?', part)
        if not m or int(m[1]) < 1 or (m[2] and not int(m[1]) <= int(m[2]) <= int(m[1]) + MAX_PAGES):
            raise argparse.ArgumentTypeError('pages look like 3,4,5 or 3-5')
        out.extend(range(int(m[1]), int(m[2] or m[1]) + 1))
    if len(out) > MAX_PAGES:
        raise argparse.ArgumentTypeError(f'at most {MAX_PAGES} pages a call')
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
    cmd('ask', (['question'], {}))
    cmd('status')
    cmd('open', (['target'], {}), (['at'], {'nargs': '?'}), (['--dry-run'], {'action': 'store_true'}))
    return ap


# ------------------------------------------------------------------------------------------------------------ the engine --
def library(given):
    """The library folder (--data, LASTBOOK_DATA, the one the window opened last, the app's own), or Failed."""
    import main as app_main
    data = app_main.data_folder(given)
    found, why = app_main.check(data) if data is not None else (None, None)
    if found is None:
        raise Failed(why if why and (given or os.environ.get('LASTBOOK_DATA')) else 'No library yet: run `lastbook` to set one up.')
    return found


def engine(data, local_ai=False):
    """(server, assistant) over the library, as the window's engine has them, without its indexer."""
    os.environ['LASTBOOK_DATA'] = str(data)   # engine/paths.py reads it when first imported
    if local_ai:
        os.environ.setdefault('LASTBOOK_LOCAL_AI', '1')
    else:
        os.environ['LASTBOOK_LOCAL_AI'] = '0'
    import server
    import assistant
    server.init(no_index=True)
    return server, assistant


def head(S, r):
    """What names a document in every answer."""
    pub = S.public(r)
    out = {'doc_id': r['id'], 'title': pub['title'], 'publisher': pub['publisher'], 'year': pub['year'], 'kind': r['kind'], 'trust': pub['trust']}
    if r.get('encyclopedia'):
        out['encyclopedia'] = r['encyclopedia']
    if r.get('notes'):
        out['notes'] = r['notes'][:300]
    return out


def document(S, A, doc_id):
    r = A.record(doc_id)
    if not r or r['kind'] == 'metadata':
        raise Failed(f'No document with id {doc_id!r} in the library. Find ids with: lastbook search')
    return r


# -------------------------------------------------------------------------------------------------------------- commands --
def search(S, A, a):
    return {'query': a.query, 'results': A.hybrid(a.query[:200], a.limit, a.kind)}


def read(S, A, a):
    r = document(S, A, a.doc_id)
    if r['kind'] == 'article':
        _, text = A.ENC.read(a.doc_id, (a.section or '')[:80], ARTICLE_CHARS)
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
    return {**head(S, r), 'pages': len(texts), 'texts': [{'page': p, 'text': re.sub(r'[ \t]{2,}', ' ', texts[p - 1]).strip()} for p in pages]}


def page(S, A, a):
    r = document(S, A, a.doc_id)
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


def transcript(S, A, a):
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


def place(S, A, a):
    kinds = [k.strip() for k in (a.nearby or '').split(',') if k.strip()]
    bad = [k for k in kinds if k not in S.HELP]
    if bad:
        print(f"lastbook: --nearby takes {', '.join(sorted(S.HELP))} (not {bad[0]})", file=sys.stderr)
        sys.exit(2)
    places = S.places(a.query[:100], 5)
    out = {'query': a.query, 'places': places}
    if kinds:
        first = places[0] if places else None
        on_map = bool(first and first.get('onMap'))
        out['nearby_of'] = first['name'] if on_map else None
        out['nearby'] = S.near(first['lat'], first['lon'], kinds, 5) if on_map else []
    out['note'] = 'Offline map data: may be outdated, shows no closures or damage; distances are straight lines.'
    return out


def guides(S, A, a):
    return {'guides': [{'id': t['id'], 'title': t.get('title'), 'short': t.get('short'), 'mode': t.get('mode'), 'keywords': t.get('keywords', [])}
                       for t in S.topics() if not t.get('error')]}


def guide(S, A, a):
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


def ask(S, A, a):
    if A.LOCAL:
        A.LOCAL.settle()   # as the window's engine does: the local AI may still be finding and checking its model file
    if not A.model_status()['online']:
        why = (A.LOCAL.status().get('message') if A.LOCAL else '') or 'set it up in the window (About), or use: lastbook search'
        raise Failed(f'No local AI to answer with: {why}')
    events = []
    A.chat([{'role': 'user', 'content': a.question}], None, events.append, lambda: False)
    done = next((e for e in events if e['type'] == 'done'), None)
    if done is None:
        err = next((e for e in events if e['type'] == 'error'), {})
        raise Failed(err.get('text') or 'The local AI gave no answer.')
    return {'question': a.question, 'answer': done['answer'], 'sources': done['sources'], 'consulted': done['consulted'],
            'steps': done['tools'], 'seconds': done['latency']['total_s']}


def status(S, A, a):
    import llm_local
    import retrieval
    from engine.setup import maps
    try:
        summary = json.loads((S.LIB / 'summary.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        summary = {}
    zims = S.zim_names()
    # The local AI as its saved state says, without checking the file or starting anything: `ask` does that.
    local = llm_local.LOCAL
    model = next((str(f / llm_local.FILE) for f in llm_local.places() if (f / llm_local.FILE).is_file()), None)
    ai = 'no runtime' if not local.runner else 'installed' if model else 'absent'
    index = S.INDEX
    try:
        recall = {'running': True, 'model': retrieval.Recall().capabilities(timeout=2).get('source')}
    except retrieval.RecallUnavailable as e:
        recall = {'running': False, 'error': str(e)}
    import main as app_main
    return {'version': app_main.version(),
            'library': str(S.LIB), 'data': str(S.HERE),
            'counts': {'documents': summary.get('resources', len(S.catalog()['by_id'])), 'pdfs': summary.get('pdfs'), 'pages': summary.get('pdf_pages'),
                       'web_pages': summary.get('web_pages'), 'videos': summary.get('videos'), 'encyclopedias': len(zims)},
            'encyclopedias': zims,
            'ai': {'state': ai, 'model': llm_local.NAME, 'file': model},
            'meaning_search': {'ready': bool(recall['running'] and index.source and len(index.mat) and recall.get('model') == index.source),
                               'recall': recall, 'passages': index.status.get('chunks'), 'documents': index.status.get('docs')},
            'maps': maps.installed(S.HERE / 'derived' / 'geo')}


def open_route(S, A, a):
    """The window's route for a target: a route key as it is, or a document id (and page, or seconds into a video)."""
    target = a.target
    kind = target.split(':', 1)[0]
    if kind in ROUTES and ':' in target:
        return target if a.at is None else f'{target}:{a.at}'
    r = document(S, A, target)
    at = a.at or ''
    if at and not at.isdigit():
        print('lastbook: open DOC_ID takes a page number (a video: seconds)', file=sys.stderr)
        sys.exit(2)
    if r['kind'] == 'article':
        return 'wiki:' + r['path'].removeprefix('zim/')
    if r['kind'] == 'video':
        return f"watch:{r['id']}:{at or 0}"
    return f"read:{r['id']}:{at or 1}"


def open_window(S, A, a):
    route = open_route(S, A, a)
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
            'ask': ask, 'status': status, 'open': open_window}


def main(argv=None):
    a = parser().parse_args(argv)
    out = sys.stdout
    sys.stdout = sys.stderr   # whatever the engine prints stays out of the JSON
    try:
        S, A = engine(library(a.data), local_ai=a.command == 'ask')
        result = COMMANDS[a.command](S, A, a)
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
