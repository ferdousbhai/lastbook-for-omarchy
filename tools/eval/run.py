#!/usr/bin/env python3
"""Retrieval check against a real data folder: what Chat would be handed for each question, before the model sees it.

  LASTBOOK_DATA=<folder> python3 tools/eval/run.py [--out results.json] [--compare old.json]

Each question in questions.json lists title words that a good top 8 should contain (`expect`, any one per entry).
Prints hits@8 and MRR over those, plus timing, and with --compare the passages that came or went."""
import argparse, json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'app')]
os.environ.setdefault('SURVIVAL_MODEL_BASE', 'http://127.0.0.1:9')

ap = argparse.ArgumentParser()
ap.add_argument('--out')
ap.add_argument('--compare')
ap.add_argument('-k', type=int, default=8)
a = ap.parse_args()

import server, assistant  # noqa: E402
server.init(no_index=True)
qs = json.loads((Path(__file__).parent / 'questions.json').read_text())
runs, hit, rr, times, cold = {}, 0, 0.0, [], []
for q in qs:
    t = time.perf_counter()
    assistant.hybrid(q['q'], a.k)  # cold: articles parsed and embedded, the question embedded
    cold.append(time.perf_counter() - t)
    t = time.perf_counter()
    res = assistant.hybrid(q['q'], a.k)
    times.append(time.perf_counter() - t)
    rows = [{'title': r['title'], 'doc_id': r['doc_id'], 'page': r['page'], 't': r['t'], 'kind': r['kind'], 'by': r['found_by']} for r in res]
    runs[q['q']] = rows
    rank = next((i + 1 for i, r in enumerate(rows) if any(e.lower() in r['title'].lower() for e in q['expect'])), None)
    hit += bool(rank)
    rr += 1 / rank if rank else 0
    print(f"{'ok ' if rank else 'MISS'} {rank or '-':>2}  {q['q']}")
    for i, r in enumerate(rows):
        where = f"p{r['page']}" if r['page'] else (f"{int(r['t'])}s" if r['t'] is not None else '')
        print(f"      {i + 1}. {r['title'][:70]} {where} {'+'.join(r['by'])}")
n = len(qs)
print(f"\nhits@{a.k} {hit}/{n}  MRR {rr / n:.3f}  median {sorted(times)[n // 2] * 1000:.0f} ms  max {max(times) * 1000:.0f} ms"
      f"  cold median {sorted(cold)[n // 2] * 1000:.0f} ms max {max(cold) * 1000:.0f} ms  meaning={'on, ' + str(server.INDEX.status.get('chunks')) + ' passages' if server.INDEX and server.INDEX.source and len(server.INDEX.mat) else 'off'}")
if a.out:
    Path(a.out).write_text(json.dumps(runs, indent=1))
if a.compare:
    old = json.loads(Path(a.compare).read_text())
    for q, rows in runs.items():
        key = lambda r: (r['title'], r['page'], r['t'])
        was, now = {key(r) for r in old.get(q, [])}, {key(r) for r in rows}
        if was != now:
            print(f"\n{q}\n  + " + '\n  + '.join(f'{t[:60]} {p or ""}' for t, p, _ in now - was) + "\n  - " + '\n  - '.join(f'{t[:60]} {p or ""}' for t, p, _ in was - now))
