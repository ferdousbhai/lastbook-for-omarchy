"""A small made-up library for the bridge tests. Built once under derived/app-build/testdata; holds no model."""
import hashlib, json, os, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'derived' / 'app-build' / 'testdata'
LIB = DATA / 'library-data'
CANARY = b'CANARY-MUST-NEVER-BE-SERVED'
MiB = 1024 * 1024


def pattern(offset, n):
    """Bytes that say where they are, so a wrong slice cannot pass."""
    first = offset // 4
    words = b''.join((i & 0xffffffff).to_bytes(4, 'big') for i in range(first, first + n // 4 + 2))
    return words[offset - first * 4:offset - first * 4 + n]


def write_pattern(path, size):
    with open(path, 'wb') as f:
        for off in range(0, size, MiB):
            f.write(pattern(off, min(MiB, size - off)))


def link_out():
    """The link the escape checks need: a served folder pointing at a file outside the library. Made again on every run, so one that was removed or points somewhere else is put back."""
    link, target = LIB / 'documents' / 'out.txt', DATA / 'outside-canary.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_bytes(CANARY)      # the 'ready' branch below does not rebuild the fixture: a deleted canary would leave a link that resolves to nothing
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)


def ensure():
    if (DATA / 'ready').exists():
        link_out()
        return DATA
    for d in ('videos', 'documents', 'pages', 'tools', 'logs', 'collections', 'transcripts', 'assets'):
        (LIB / d).mkdir(parents=True, exist_ok=True)
    (DATA / 'derived' / 'geo').mkdir(parents=True, exist_ok=True)
    (DATA / 'derived' / 'cache').mkdir(parents=True, exist_ok=True)
    (DATA / 'tmp').mkdir(exist_ok=True)
    write_pattern(LIB / 'videos' / 'a.mp4', 20 * MiB)
    write_pattern(LIB / 'videos' / 'b.webm', 3 * MiB)
    with open(LIB / 'videos' / 'huge.mkv', 'wb') as f:
        f.truncate(200 * MiB)
    with open(LIB / 'documents' / 's.pdf', 'wb') as f:
        f.write(pattern(0, MiB))
    write_pattern(LIB / 'documents' / 'big.pdf', 70 * MiB)
    write_pattern(LIB / 'documents' / 'big.bin', 40 * MiB)
    write_pattern(LIB / 'documents' / 'over.bin', 33 * MiB)
    write_pattern(LIB / 'documents' / 'mid.bin', 5 * MiB)
    (LIB / 'documents' / 'plain.txt').write_text('plain text file\n')
    (LIB / 'documents' / 'x.info.json').write_bytes(CANARY)
    (LIB / 'documents' / '.hidden').write_bytes(CANARY)
    (LIB / 'documents' / 'video-auth-copy.json').write_bytes(CANARY)
    (LIB / 'video-auth.json').write_bytes(CANARY)
    (LIB / 'canary-root.txt').write_bytes(CANARY)
    (LIB / 'tools' / 'secret.txt').write_bytes(CANARY)
    (LIB / 'logs' / 'x.log').write_bytes(CANARY)
    (DATA / 'outside-canary.txt').write_bytes(CANARY)
    link_out()
    (LIB / 'documents' / 'hl.txt').write_bytes(CANARY)
    os.link(LIB / 'documents' / 'hl.txt', LIB / 'pages' / 'hl2.txt')
    (LIB / 'pages' / 's.html').write_text('<!doctype html><title>Saved</title><p>saved page</p>')
    (LIB / 'pages' / 'a.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    (LIB / 'reader.css').write_text('body{}')
    recs = []
    for rid, kind, path in [('pdfsmall', 'pdf', 'documents/s.pdf'), ('pdfbig', 'pdf', 'documents/big.pdf'), ('saved', 'html', 'pages/s.html'), ('vida', 'video', 'videos/a.mp4'), ('txt', 'text', 'documents/plain.txt')]:
        recs.append({'id': rid, 'kind': kind, 'path': path, 'title': rid, 'category': 'test', 'collection': 'Test', 'publisher': 'Test', 'bytes': (LIB / path).stat().st_size})
    (LIB / 'catalog.json').write_text(json.dumps(recs))
    (LIB / 'summary.json').write_text('{"resources": 5}')
    con = sqlite3.connect(LIB / 'search.sqlite')
    con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
    con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('pdfsmall', 'pdfsmall', 'Test', 'test', 'documents/s.pdf', 1, 'boil water for one minute'))
    con.commit()
    con.close()
    (DATA / 'ready').write_text('1')
    return DATA


if __name__ == '__main__':
    print(ensure())
