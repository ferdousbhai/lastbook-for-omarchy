#!/usr/bin/env python3
"""Read-only query helper for curating the survival library.
  libq.py search "words"        full-text, grouped by document (best pages)
  libq.py titles "regex"        catalog titles/collections matching a regex
  libq.py doc <id>              metadata for a catalog id
  libq.py page <id> <n> [chars] text of page n (PDF file page; 1 for html/video)
  libq.py toc <id>              first line of every page (quick outline)
  libq.py zim "words"           encyclopedia (ZIM) article search, in the ZIM files themselves

The library is looked for in --data (before the command or after it), then LASTBOOK_DATA, then this source tree, the
same data folder the app takes: the folder that holds library-data/. Nothing is asked of a server and no port is used.

  libq.py --data /path/to/LastbookData search "boil water"
"""
import json, os, re, sqlite3, sys
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
LIB = DB = None
def use(args):
    """Set LIB and DB from --data, LASTBOOK_DATA or this source tree; return the rest of the arguments."""
    global LIB, DB
    data = None
    if '--data' in args:                     # before the command or after it
        i = args.index('--data')
        if i + 1 >= len(args): sys.exit('--data needs a folder: the one that holds library-data/')
        data, args = args[i + 1], args[:i] + args[i + 2:]
    LIB = Path(data or os.environ.get('LASTBOOK_DATA') or HERE) / 'library-data'
    DB = LIB / 'search.sqlite'
    if not (LIB / 'catalog.json').is_file():
        sys.exit(f'No library at {LIB} (no catalog.json). Pass --data or set LASTBOOK_DATA to the folder that holds library-data/.')
    return args
_cat = None
def cat():
    global _cat
    if _cat is None: _cat = {r['id']: r for r in json.loads((LIB/'catalog.json').read_text(encoding='utf-8'))}
    return _cat
def pages(id):
    r = cat()[id]; t = (LIB/r['text_path']).read_text(encoding='utf-8', errors='replace')
    return t.split('\n\f\n') if r['kind'] != 'pdf' or '\n\f\n' in t else t.split('\f')
def main(a):
    if a[0] == 'search':
        terms = re.findall(r'[\w-]+', a[1])
        expr = ' AND '.join('"%s"' % t for t in terms)
        con = sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True)   # a plain f'file:{DB}' breaks on '#', '%' and '?' in a folder name
        rows = con.execute("SELECT doc_id,title,collection,page,snippet(pages,6,'[',']','…',20),bm25(pages,0,5,2,1,0,0,1) s FROM pages WHERE pages MATCH ? ORDER BY s LIMIT 300", (expr,)).fetchall()
        g = {}
        for d, t, c, p, sn, s in rows:
            if d.startswith('metadata-'): continue
            g.setdefault(d, [t, c, []])[2].append((p, sn))
        for d, (t, c, ps) in list(g.items())[:int(a[2]) if len(a) > 2 else 25]:
            print(f'{d} | {t[:80]} | {c[:45]} | pages {[p for p,_ in ps[:8]]}')
            print('    ', ps[0][1].replace('\n', ' ')[:200])
    elif a[0] == 'titles':
        rx = re.compile(a[1], re.I)
        for r in cat().values():
            if r['kind'] == 'metadata': continue
            if rx.search(r['title'] + ' ' + r['collection']):
                print(f"{r['id']} | {r['kind']} | {r['title'][:90]} | {r['collection'][:40]} | {r.get('pages') or ''}p {r.get('duration') or ''}s")
    elif a[0] == 'doc':
        print(json.dumps(cat()[a[1]], indent=1))
    elif a[0] == 'page':
        ps = pages(a[1]); n = int(a[2])
        if not 1 <= n <= len(ps): sys.exit(f'{a[1]} has {len(ps)} pages; there is no page {n}.')
        print(f'[{len(ps)} pages]'); print(ps[n-1][:int(a[3]) if len(a) > 3 else 4000])
    elif a[0] == 'toc':
        for i, p in enumerate(pages(a[1]), 1):
            line = ' '.join(p.split())[:110]
            print(i, line)
    elif a[0] == 'zim':
        try:
            from libzim.reader import Archive
            from libzim.search import Searcher, Query
        except ImportError:
            sys.exit('libzim is not installed (PYTHONPATH=$(bin/test-deps) has it).')
        files = sorted((LIB/'collections').glob('*.zim'))
        if not files: print('This library holds no ZIM files.')
        for path in files:
            try:
                arc = Archive(path)
                if not arc.has_fulltext_index: print(f'{path.name}: no full-text index, not searched'); continue
                for p in Searcher(arc).search(Query().set_query(a[1])).getResults(0, 8):
                    print(f'zim/{path.name}/{p} | {arc.get_entry_by_path(p).title}')
            except Exception as e:           # one damaged file must not cost the answers from the others
                print(f'{path.name}: skipped ({type(e).__name__})')
if __name__ == '__main__':
    for out in (sys.stdout, sys.stderr): out.reconfigure(encoding='utf-8', errors='replace')   # page text is not all in the terminal's own encoding (LANG=C)
    rest = use(sys.argv[1:])
    if not rest or rest[0] not in ('search', 'titles', 'doc', 'page', 'toc', 'zim'): sys.exit(__doc__)
    try:
        main(rest)
    except IndexError:                       # a command without what it needs
        sys.exit(__doc__)
    except KeyError as e:
        sys.exit(f'No record with the id {e.args[0]} in this library.')
