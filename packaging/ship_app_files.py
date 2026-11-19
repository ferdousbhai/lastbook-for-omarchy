"""The content/ a package build ships, written to a folder of its own. The build packages that folder, never the source folder.

  python -I -B ship_app_files.py OUT

content/ stays as a household's own library reads it. The difference is in ship_edits.json, applied here to a copy:
general wording for the home area, no working names, guide items for things no pack holds taken out. Stops with an error if a listed word is left.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EDITS = json.loads((HERE / 'ship_edits.json').read_text(encoding='utf-8'))
LEFT = re.compile('|'.join(EDITS['forbidden']), re.I)
PREFIXES = EDITS['id_prefixes']
TEXT, DROP_TEXTS = EDITS['text'], set(EDITS['drop_texts'])
used = set()


def new_id(doc):
    for old, new in PREFIXES.items():
        if doc.startswith(old):
            return new + doc[len(old):]
    return doc


def dropped(item):
    if not isinstance(item, dict):
        return item in DROP_TEXTS or item in EDITS['drop_keywords']
    drop = EDITS['drop_items']
    doc, zim = item.get('doc') or '', item.get('zim') or ''
    return doc.startswith(tuple(drop['doc_prefixes'])) or any(z in zim for z in drop['zim_contains'])


def clean(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = new_id(v) if k == 'doc' else clean(v)
        if isinstance(value.get('read'), list) and value['read'] and not out['read']:
            return None    # a step or choice whose only cards were dropped
        return out
    if isinstance(value, list):
        kept = [clean(v) for v in value if not dropped(v)]
        return [v for v in kept if v is not None]
    if isinstance(value, str) and value in TEXT:
        used.add(value)
        return TEXT[value]
    return value


def main():
    out = Path(sys.argv[1])
    (out / 'content' / 'topics').mkdir(parents=True, exist_ok=True)
    written = []
    for rel in ['gaps.json'] + [f'topics/{p.name}' for p in sorted((ROOT / 'content' / 'topics').glob('*.json'))]:
        data = clean(json.loads((ROOT / 'content' / rel).read_text(encoding='utf-8')))
        if rel == 'gaps.json':
            data.pop('_about', None)
        (out / 'content' / rel).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        written.append(out / 'content' / rel)
    bad = [str(EDITS['text'].keys() - used)] if EDITS['text'].keys() - used else []    # an edit that matched nothing: the guide changed, so look again
    for p in written:
        m = LEFT.search(p.read_text(encoding='utf-8', errors='replace'))
        if m:
            bad.append(f'{p.relative_to(out)}: {p.read_text(encoding="utf-8", errors="replace")[max(0, m.start() - 40):m.end() + 40]!r}')
    if bad:
        sys.exit('Not shipping. Left in place:\n' + '\n'.join(bad))
    print(f'{len(written)} files in {out}')


if __name__ == '__main__':
    main()
