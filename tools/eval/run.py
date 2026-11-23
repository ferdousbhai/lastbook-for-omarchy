#!/usr/bin/env python3
"""Retrieval check against a real data folder: the passages search gives for each question.

  LASTBOOK_DATA=<folder> python3 tools/eval/run.py [--out results.json] [--compare old.json]

Each question in questions.json lists title words that a good answer's source carries (`expect`, any one per entry: a
document of the Small pack, or an encyclopedia article). Prints where the first such source came (its rank), then
hit@1, hit@3, hit@8 and MRR over the questions, plus timing, and with --compare the passages that came or went.
There is no pass mark: the numbers are for comparing one way of searching with another on the same library.

Options:
  --no-recall           no meaning-based search: keyword and encyclopedia search only (what everyone has)

Searching writes the engine's caches under the data folder's derived/ (encyclopedia openings).
To leave a library's derived/ alone, point LASTBOOK_DATA at a folder whose library-data is a link to the real one.
"""
import argparse, json, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'app')]
CUTS = (1, 3, 8)

ap = argparse.ArgumentParser()
ap.add_argument('--out')
ap.add_argument('--compare')
ap.add_argument('-k', type=int, default=8)
ap.add_argument('--no-recall', action='store_true')
ap.add_argument('--quiet', action='store_true', help='the summary line only')
a = ap.parse_args()
QUESTIONS = json.loads((Path(__file__).parent / 'questions.json').read_text())


def summary(ranks):
    n = len(ranks)
    return {**{f'hit@{c}': sum(1 for r in ranks if r and r <= c) for c in CUTS}, 'mrr': round(sum(1 / r for r in ranks if r) / n, 3), 'n': n}


if a.no_recall:
    os.environ['LASTBOOK_RECALL_SOCKET'] = str(Path(tempfile.gettempdir()) / 'lastbook-eval-no-recall.sock')   # nothing listens there

import server  # noqa: E402
server.init(no_index=True)
runs, ranks, times, cold = {}, {}, [], []
say = (lambda *x: None) if a.quiet else print
for q in QUESTIONS:
    t = time.perf_counter()
    server.hybrid(q['q'], a.k)  # cold: articles parsed and embedded, the question embedded
    cold.append(time.perf_counter() - t)
    t = time.perf_counter()
    res = server.hybrid(q['q'], a.k)
    times.append(time.perf_counter() - t)
    rows = [{'title': r['title'], 'doc_id': r['doc_id'], 'page': r['page'], 't': r['t'], 'kind': r['kind'], 'by': r['found_by']} for r in res]
    runs[q['q']] = rows
    rank = next((i + 1 for i, r in enumerate(rows) if any(e.lower() in r['title'].lower() for e in q['expect'])), None)
    ranks[q['q']] = rank
    say(f"{'ok ' if rank else 'MISS'} {rank or '-':>2}  {q['q']}")
    for i, r in enumerate(rows):
        where = f"p{r['page']}" if r['page'] else (f"{int(r['t'])}s" if r['t'] is not None else '')
        say(f"      {i + 1}. {r['title'][:70]} {where} {'+'.join(r['by'])}")
n = len(QUESTIONS)
s = summary(list(ranks.values()))
index = server.INDEX
meaning = 'off'
if index and index.source and len(index.mat) and index.query_vector('water') is not None:
    meaning = f"on, {index.status.get('chunks')} passages of {index.status.get('docs')} documents"
say()
print('  '.join(f'{c} {s[c]}/{n}' for c in (f'hit@{x}' for x in CUTS)) + f"  MRR {s['mrr']:.3f}  median {sorted(times)[n // 2] * 1000:.0f} ms"
      f"  max {max(times) * 1000:.0f} ms  cold median {sorted(cold)[n // 2] * 1000:.0f} ms max {max(cold) * 1000:.0f} ms"
      f"  meaning={meaning}")
if a.out:
    Path(a.out).write_text(json.dumps({'summary': s, 'meaning': meaning, 'ranks': ranks, 'runs': runs}, indent=1))
if a.compare:
    old = json.loads(Path(a.compare).read_text())
    old = old.get('runs', old)
    for q, rows in runs.items():
        key = lambda r: (r['title'], r['page'], r['t'])
        was, now = {key(r) for r in old.get(q, [])}, {key(r) for r in rows}
        if was != now:
            print(f"\n{q}\n  + " + '\n  + '.join(f'{t[:60]} {p or ""}' for t, p, _ in now - was) + "\n  - " + '\n  - '.join(f'{t[:60]} {p or ""}' for t, p, _ in was - now))
