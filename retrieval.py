"""Local retrieval service shared by the window's search and the assistant.

Keyword search (the library's own FTS5/BM25 index, read only) + meaning-based search fused with reciprocal-rank fusion.
Meaning-based search uses the embedding model that Recall for Omarchy already runs on this computer (EmbeddingGemma, on the
graphics card), through Recall's owner-only Unix socket: no model of our own, no network. Without Recall, or while it is
busy or updating, meaning-based search is off and keyword search carries on.

Every passage carries provenance: catalog id, PDF file page or video timestamp, and which retriever found it.

The semantic index lives in derived/index/: semantic.sqlite (passages, per-document signatures, which model made the
vectors) and vectors.f16 (append-only float16 rows, row i = passage id i+1). It grows incrementally: documents whose
catalog signature changed or that are new get (re)embedded; a different model empties it. Nothing in library-data is written.
"""
import functools, html, json, os, re, socket, sqlite3, sys, threading, time
from pathlib import Path

import numpy as np

import engine.paths as lastbook_paths
from engine.paths import ro_uri
HERE = lastbook_paths.DATA  # holds derived/
LIB = lastbook_paths.LIB
IDX = HERE / 'derived' / 'index'
CHUNK, OVERLAP, MINLEN = 1100, 150, 80
BLOCK = 16384     # rows widened to float32 at a time while scoring: float16 matrix products get no BLAS and are ~15x slower
FLOOR = 0.30      # cosine below which a passage is not worth offering (EmbeddingGemma; measured with tools/eval)
# EmbeddingGemma's task prompts, as Recall uses them (its src/model.rs).
QUERY = 'task: search result | query: {}'
DOCUMENT = 'title: {} | text: {}'


class RecallUnavailable(Exception):
    """Recall is not installed, not running, busy, or answered something we cannot use. Keyword search carries on."""


def recall_socket():
    """Recall's socket; LASTBOOK_RECALL_SOCKET names another (tests point it nowhere to run without a model)."""
    if os.environ.get('LASTBOOK_RECALL_SOCKET'):
        return Path(os.environ['LASTBOOK_RECALL_SOCKET'])
    run = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    return Path(run) / 'recall-for-omarchy.sock'


class Recall:
    """Client for Recall's shared inference API (api 1): newline-delimited JSON over its owner-only Unix socket."""
    BATCH = 32        # Recall's limit per text request
    RETRY_S = 30      # after a failure, do not try again for this long: every question would otherwise wait for it

    def __init__(self, path=None):
        self.path = Path(path) if path else recall_socket()
        self.source = None   # the model Recall reports, e.g. "google/embeddinggemma-2@bfcd298 …"; vectors from two never mix
        self.error = None
        self.down_until = 0.0

    def _request(self, value, timeout):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        buf = bytearray()
        try:
            s.connect(str(self.path))
            s.sendall(json.dumps(value).encode() + b'\n')
            while not buf.endswith(b'\n'):
                piece = s.recv(1 << 20)
                if not piece:
                    break
                buf += piece
                if len(buf) > 32 << 20:
                    raise RecallUnavailable('reply too large')
        except OSError as e:
            raise RecallUnavailable('not running' if isinstance(e, (FileNotFoundError, ConnectionRefusedError)) else type(e).__name__) from None
        finally:
            s.close()
        try:
            reply = json.loads(buf)
        except ValueError:
            raise RecallUnavailable('invalid reply') from None
        if not isinstance(reply, dict) or reply.get('api') != 1:
            raise RecallUnavailable('incompatible Recall version')
        if reply.get('error'):
            raise RecallUnavailable(str(reply['error'])[:200])
        return reply

    def call(self, value, timeout):
        if time.time() < self.down_until:
            raise RecallUnavailable(self.error or 'unavailable')
        try:
            reply = self._request({'api': 1, **value}, timeout)
        except RecallUnavailable as e:
            self.error, self.down_until = str(e), time.time() + self.RETRY_S
            raise
        self.error = None
        return reply

    def capabilities(self, timeout=3):
        reply = self.call({'op': 'capabilities'}, timeout)
        if 'embed' not in (reply.get('operations') or []):
            raise RecallUnavailable('this Recall has no embed operation')
        self.source = reply.get('source')
        return reply

    def embed(self, texts, timeout=120):
        """float32 rows, unit length, one per text."""
        out = []
        for i in range(0, len(texts), self.BATCH):
            batch = texts[i:i + self.BATCH]
            reply = self.call({'op': 'embed', 'kind': 'text', 'inputs': batch}, timeout)
            self.source = reply.get('source') or self.source
            try:
                found = reply.get('embeddings') or []
                vecs = [e.get('vector') if isinstance(e, dict) else None for e in found]
                if len(vecs) != len(batch) or any(not isinstance(v, list) or not v for v in vecs) or len({len(v) for v in vecs}) != 1:
                    raise ValueError
                rows = np.asarray(vecs, dtype=np.float32)
                if rows.ndim != 2 or not np.isfinite(rows).all() or (out and rows.shape[1] != out[0].shape[1]):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):     # not numbers (or an integer too large for one), not rows of one width: search goes on by keyword
                raise RecallUnavailable('invalid embedding batch') from None
            out.append(rows)
        return np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)


def norm_ws(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def readable(text):
    """Skip table/spreadsheet debris and non-English runs that match every query by accident."""
    words = re.findall(r"[A-Za-z][a-z']+", text)
    if len(words) < 12:
        return False
    letters = sum(len(w) for w in words)
    common = sum(1 for w in words if w.lower() in COMMON)
    return letters / max(1, len(text)) > 0.55 and common / len(words) > 0.18


COMMON = set('the a an of to and in is are be for on with as by or that this it from at not can if your you use when should may will water have has which all but more they their each must after before into out other than any one two do does'.split())


def chunks_of(text):
    text = norm_ws(text)
    if len(text) < MINLEN:
        return []
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + CHUNK)
        if j < len(text):  # end at a sentence or word boundary
            k = text.rfind('. ', i + CHUNK // 2, j)
            j = k + 1 if k > 0 else (text.rfind(' ', i + CHUNK // 2, j) if text.rfind(' ', i + CHUNK // 2, j) > 0 else j)
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(j - OVERLAP, i + 1)
    return [c for c in out if len(c) >= MINLEN and readable(c)]


def cues(path):
    """[[seconds, text]] for each cue of a VTT file, tags taken out, as written (auto captions repeat lines across cues)."""
    out, cur = [], None
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        m = re.match(r'(?:(\d+):)?(\d\d):(\d\d)[.,]\d+\s+-->', line)
        if m:
            cur = [int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), '']
            out.append(cur)
        elif cur is not None and line.strip() and not line.strip().isdigit():
            t = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
            if t and not cur[1].endswith(t):
                cur[1] = (cur[1] + ' ' + t).strip()
    return out


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


def vtt_windows(path, span=45):
    """Timed transcript windows (~45 s) so answers can cite a moment in a video."""
    out, start, buf, last = [], None, [], ''
    for t, txt in cues(path):
        if txt.startswith(last) and last:
            txt = txt[len(last):].strip()
        last = txt or last
        if not txt:
            continue
        if start is None:
            start = t
        buf.append(txt)
        if t - start >= span:
            out.append((start, ' '.join(buf)))
            start, buf = None, []
    if buf:
        out.append((start or 0, ' '.join(buf)))
    return [(t, norm_ws(x)) for t, x in out if len(x) > 40]


class Index:
    def __init__(self, catalog_fn, trust_fn, fts_db, recall=None, excluded_fn=lambda: set(), truncate=True):
        """excluded_fn: the ids of rejected videos and of their records (server.excluded_videos); never indexed or returned.
        truncate=False for a process that never indexes: vectors past the committed rows may be another process's, mid-write."""
        self.catalog_fn, self.trust_fn, self.fts_db, self.excluded = catalog_fn, trust_fn, fts_db, excluded_fn
        IDX.mkdir(parents=True, exist_ok=True)
        self.db_path = IDX / 'semantic.sqlite'
        self.vec_path = IDX / 'vectors.f16'
        self._tls = threading.local()
        db = self._db()
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''CREATE TABLE IF NOT EXISTS chunk(id INTEGER PRIMARY KEY, doc_id TEXT, page INTEGER, t REAL, text TEXT, active INTEGER DEFAULT 1);
          CREATE INDEX IF NOT EXISTS chunk_doc ON chunk(doc_id);
          CREATE TABLE IF NOT EXISTS docstate(doc_id TEXT PRIMARY KEY, sig TEXT, nchunks INTEGER, at REAL);
          CREATE TABLE IF NOT EXISTS gaps(doc_id TEXT PRIMARY KEY, reason TEXT);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);''')
        db.close()
        self.lock = threading.RLock()
        self.recall = recall or Recall()
        self.source, self.dim = None, None
        self.mat = np.zeros((0, 0), dtype=np.float16)
        self.active = np.zeros(0, dtype=bool)
        self._qcache = {}
        self.status = {'state': 'starting', 'chunks': 0, 'docs': 0, 'pending': None, 'rate': None}
        self._load(truncate=truncate)

    def _db(self):
        """Writer connection: autocommit, so no transaction is ever left open by accident; writes use tx()."""
        db = sqlite3.connect(self.db_path, timeout=60, isolation_level=None, check_same_thread=False)
        db.execute('PRAGMA busy_timeout=60000')
        return db

    def _ro(self):
        """Per-thread read-only connection, reused (it can never take the write lock)."""
        c = getattr(self._tls, 'ro', None)
        if c is None:
            c = sqlite3.connect(ro_uri(self.db_path), uri=True, timeout=30, isolation_level=None, check_same_thread=False)
            self._tls.ro = c
        return c

    @staticmethod
    def tx(db, fn):
        """Run fn(db) inside BEGIN IMMEDIATE … COMMIT; always roll back on error."""
        db.execute('BEGIN IMMEDIATE')
        try:
            out = fn(db)
            db.execute('COMMIT')
            return out
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def refresh(self):
        """Pick up passages another process (the indexer) appended since we last loaded."""
        try:
            size = self.vec_path.stat().st_size
        except FileNotFoundError:
            return
        if not self.dim or size != len(self.mat) * self.dim * 2:
            self._load(truncate=False)

    def _load(self, truncate=True, db=None):
        db = db or self._ro()
        meta = dict(db.execute('SELECT key, value FROM meta').fetchall())
        self.source, self.dim = meta.get('source'), int(meta['dim']) if meta.get('dim') else None
        if not self.dim or not self.vec_path.exists():
            self.mat, self.active = np.zeros((0, self.dim or 0), dtype=np.float16), np.zeros(0, dtype=bool)
        else:
            raw = np.fromfile(self.vec_path, dtype=np.float16)
            n = min(len(raw) // self.dim, one(db, 'SELECT max(id) FROM chunk')[0] or 0)  # vectors past the committed rows: an interrupted run
            self.mat = raw[:n * self.dim].reshape(n, self.dim)
            act = np.zeros(n, dtype=bool)
            for (i,) in db.execute('SELECT id FROM chunk WHERE active=1 AND id<=?', (n,)).fetchall():
                act[i - 1] = True
            self.active = act
            if truncate and n * self.dim * 2 != self.vec_path.stat().st_size:
                with open(self.vec_path, 'r+b') as f:
                    f.truncate(n * self.dim * 2)
        self.status.update(chunks=int(self.active.sum()), docs=one(db, 'SELECT count(*) FROM docstate WHERE nchunks>0')[0])
        if self.status.get('state') == 'starting':
            self.status['state'] = 'loaded'

    # ------------------------------------------------------------- the model --
    def _match_model(self, db):
        """Ask Recall which model it runs. A different one than made the stored vectors empties the index (they cannot be compared)."""
        src = self.recall.capabilities()['source']
        if not src:
            raise RecallUnavailable('Recall reported no model')
        if src == self.source:
            return
        had = one(db, 'SELECT count(*) FROM chunk')[0]

        def w(db):
            db.execute('DELETE FROM chunk')
            db.execute('DELETE FROM docstate')
            db.execute('DELETE FROM gaps')
            db.execute('DELETE FROM meta')
            db.execute("INSERT INTO meta VALUES ('source', ?)", (src,))
        with self.lock:
            self.tx(db, w)
            if self.vec_path.exists():
                with open(self.vec_path, 'r+b') as f:
                    f.truncate(0)
            self._qcache.clear()
            self._load(truncate=False, db=db)
        if had:
            self.status['reset'] = {'why': 'Recall model changed', 'passages_dropped': had, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    def query_vector(self, q):
        """The question as a vector, or None when meaning-based search cannot run now."""
        if not q or not self.source or not len(self.mat):
            return None
        v = self._qcache.get(q)
        if v is None:
            try:
                v = self.recall.embed([QUERY.format(q)], timeout=10)[0]
            except RecallUnavailable:
                return None
            if self.recall.source != self.source or len(v) != self.dim:
                return None  # Recall moved to another model; the indexer empties and rebuilds the index
            if len(self._qcache) > 64:
                self._qcache.clear()
            self._qcache[q] = v
        return v

    # ------------------------------------------------------------ indexing --
    def sources(self):
        """(doc_id, signature, priority, loader) for every text the assistant may read."""
        cat = self.catalog_fn()
        excl = self.excluded()
        out = []
        for r in cat['by_id'].values():
            if r['id'] in excl:
                continue
            k = r['kind']
            if k in ('pdf', 'html', 'pptx'):
                sig = f"{r.get('sha256')}:{r.get('pages')}"
                out.append((r['id'], sig, -self.trust_fn(r)[1], ('pages', r)))
            elif k == 'video':
                tx = cat['video_tx'].get(r['path'])
                vtts = ([tx['vtt_path']] if tx and tx.get('vtt_path') else []) + [c for c in r.get('captions', []) if c.endswith('.vtt')]
                vtt = next((v for v in vtts if (LIB / v).is_file()), None)
                if vtt:
                    out.append((r['id'], 'vtt:' + vtt, -self.trust_fn(r)[1], ('vtt', r, vtt)))
                elif tx and (LIB / tx['path']).is_file():
                    out.append((r['id'], 'tx:' + tx['sha256'], -self.trust_fn(r)[1], ('tx', r, tx)))
        out.sort(key=lambda x: x[2])
        return out

    def _texts(self, doc_id, how):
        """(page, t, text) passages of one document."""
        kind, r = how[0], how[1]
        if kind == 'pages':
            con = sqlite3.connect(ro_uri(self.fts_db), uri=True)
            rows = con.execute('SELECT page, body FROM pages WHERE doc_id=? ORDER BY CAST(page AS INTEGER)', (doc_id,)).fetchall()
            con.close()
            if not rows:  # not in the keyword index (yet); fall back to the extracted text file
                rows = list(enumerate(page_texts(r), 1))
            return [(int(p), None, c) for p, b in rows for c in chunks_of(b)]
        if kind == 'vtt':
            return [(None, t, txt) for t, txt in vtt_windows(LIB / how[2])]
        return [(None, None, c) for c in chunks_of((LIB / how[2]['path']).read_text(encoding='utf-8', errors='replace'))]

    def update(self):
        """Embed new/changed documents. Returns number of documents processed, -1 when another process is indexing."""
        lockf = open(IDX / 'indexer.lock', 'w')
        try:
            lastbook_paths.try_lock(lockf)
        except OSError:  # another process is indexing; just follow its progress
            lockf.close()
            self.refresh()
            self.status['state'] = 'indexing (other process)'
            return -1
        try:
            db = self._db()
            try:
                try:
                    self._match_model(db)
                except RecallUnavailable as e:
                    self.status.update(state='waiting for Recall', recall=str(e))
                    return 0
                self.status.pop('recall', None)
                self._reconcile(db)
                self._load(truncate=True, db=db)
                return self._update_with(db)
            finally:
                db.close()
        finally:
            lastbook_paths.unlock(lockf)
            lockf.close()

    def _reconcile(self, db):
        """Vectors and passage rows must line up 1:1. If an earlier run left rows without vectors, drop those rows
        and mark their documents for re-embedding instead of refusing to index."""
        vec_rows = self.vec_path.stat().st_size // (self.dim * 2) if self.dim and self.vec_path.exists() else 0
        rows = one(db, 'SELECT max(id) FROM chunk')[0] or 0
        if rows <= vec_rows:
            return 0
        docs = [d for (d,) in db.execute('SELECT DISTINCT doc_id FROM chunk WHERE id>?', (vec_rows,)).fetchall()]

        def w(db):
            db.execute('DELETE FROM chunk WHERE id>?', (vec_rows,))
            for d in docs:
                db.execute('UPDATE chunk SET active=0 WHERE doc_id=?', (d,))
                db.execute('DELETE FROM docstate WHERE doc_id=?', (d,))
        self.tx(db, w)
        self.status['repaired'] = {'rows_without_vectors': rows - vec_rows, 'documents_requeued': docs[:20],
                                   'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        return rows - vec_rows

    def _hide(self, db, doc_ids, forget_text=False):
        """Deactivate every passage of these documents (removed, rejected or replaced)."""
        ids = [i for d in doc_ids for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1', (d,)).fetchall()]

        def w(db):
            for d in doc_ids:
                db.execute("UPDATE chunk SET active=0" + (", text=''" if forget_text else '') + " WHERE doc_id=?", (d,))
                db.execute('DELETE FROM docstate WHERE doc_id=?', (d,))
                if forget_text:
                    db.execute('DELETE FROM gaps WHERE doc_id=?', (d,))
        self.tx(db, w)
        with self.lock:
            for i in ids:
                if i <= len(self.active):
                    self.active[i - 1] = False
        return len(ids)

    def _update_with(self, db):
        known = dict(db.execute('SELECT doc_id, sig FROM docstate').fetchall())
        srcs = self.sources()
        excl = self.excluded()
        todo = [s for s in srcs if known.get(s[0]) != s[1]]
        live = {s[0] for s in srcs}
        # Prune: documents gone from the catalog, and rejected videos (also any stray passages they left behind).
        stale = [d for d in known if d not in live]
        stray = [d for (d,) in db.execute(f"SELECT DISTINCT doc_id FROM chunk WHERE active=1 AND doc_id IN ({','.join('?' * len(excl))})", list(excl)).fetchall()] if excl else []
        pruned = 0
        if stale or stray:
            rejected = [d for d in set(stale) | set(stray) if d in excl]
            pruned += self._hide(db, [d for d in stale if d not in excl])
            pruned += self._hide(db, rejected, forget_text=True)
        self.status.update(state='indexing' if todo else 'up to date', pending=len(todo), pruned_last=pruned)
        t0, done, nchunks = time.time(), 0, 0
        for doc_id, sig, _, how in todo:
            gap = None
            try:
                parts = self._texts(doc_id, how)
            except Exception as e:
                parts, gap = [], f'unreadable: {e}'[:200]
            if not parts and not gap:
                gap = 'no extractable text'
            title = how[1].get('title') or 'none'
            v = self.recall.embed([DOCUMENT.format(title, p[2]) for p in parts]).astype(np.float16) if parts else None
            if self.recall.source != self.source:
                raise RecallUnavailable('Recall model changed while indexing')  # the next pass empties the index and starts again
            with self.lock:
                if v is not None and self.dim is None:  # the first vectors set the width
                    self.dim = v.shape[1]
                    self.tx(db, lambda db: db.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(self.dim),)))
                    self.mat = np.zeros((0, self.dim), dtype=np.float16)
                if v is not None and v.shape[1] != self.dim:
                    raise RecallUnavailable('embedding width changed')
                old = [i for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1', (doc_id,)).fetchall()]
                start = (one(db, 'SELECT max(id) FROM chunk')[0] or 0) + 1
                if start - 1 != len(self.mat):  # vectors and rows drifted (e.g. an interrupted run): realign, don't fail
                    self._reconcile(db)
                    self._load(truncate=True, db=db)
                    start = (one(db, 'SELECT max(id) FROM chunk')[0] or 0) + 1
                    if start - 1 != len(self.mat):
                        raise RuntimeError(f'vector file has {len(self.mat)} rows but the database has {start - 1}; rebuild derived/index')

                def w(db):
                    db.execute('UPDATE chunk SET active=0 WHERE doc_id=?', (doc_id,))
                    if parts:
                        db.executemany('INSERT INTO chunk(id,doc_id,page,t,text,active) VALUES (?,?,?,?,?,1)',
                                       [(start + j, doc_id, p[0], p[1], p[2]) for j, p in enumerate(parts)])
                    if gap:
                        db.execute('INSERT OR REPLACE INTO gaps VALUES (?,?)', (doc_id, gap))
                    else:
                        db.execute('DELETE FROM gaps WHERE doc_id=?', (doc_id,))
                    db.execute('INSERT OR REPLACE INTO docstate VALUES (?,?,?,?)', (doc_id, sig, len(parts), time.time()))
                    if parts:  # append vectors last, inside the transaction: a failed write never leaves orphan rows
                        with open(self.vec_path, 'ab') as f:
                            f.write(v.tobytes())
                self.tx(db, w)
                if parts:
                    self.mat = np.concatenate([self.mat, v]) if len(self.mat) else v
                    self.active = np.concatenate([self.active, np.ones(len(v), dtype=bool)])
                for i in old:
                    self.active[i - 1] = False
            done += 1
            nchunks += len(parts)
            el = time.time() - t0
            self.status.update(pending=len(todo) - done, chunks=int(self.active.sum()),
                               rate=round(nchunks / el, 1) if el > 1 else None)
        self.status.update(state='up to date' if done == len(todo) else 'indexing',
                           docs=one(db, 'SELECT count(*) FROM docstate WHERE nchunks>0')[0])
        return done

    def run_forever(self, every=300, retry=60):
        while True:
            wait = every
            try:
                n = self.update()
                if n >= 0:  # a successful pass clears any earlier error
                    self.status.pop('error', None)
                    self.status['last_ok'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                if self.status.get('state') == 'waiting for Recall':
                    wait = retry
            except RecallUnavailable as e:
                self.status.update(state='waiting for Recall', recall=str(e))
                wait = retry
            except Exception as e:
                print(f'semantic index: {e}', file=sys.stderr, flush=True)
                self.status.update(state='error', error=type(e).__name__, error_at=time.strftime('%Y-%m-%dT%H:%M:%S'))  # shown to clients: no paths
                wait = retry
            time.sleep(wait)

    # ------------------------------------------------------------- search --
    def semantic(self, q, k=60, allowed=None, floor=FLOOR, doc=None):
        """Best passages for q (a question, or its query_vector). doc: only that document's passages, best first, no floor."""
        qv = self.query_vector(q) if isinstance(q, str) else q
        if qv is None:
            return []
        with self.lock:
            m, act = self.mat, self.active
        if not len(m):
            return []
        db = self._ro()
        if doc:
            ids = np.array([i - 1 for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1 AND id<=?', (doc, len(m)))], dtype=np.int64)
            if not len(ids):
                return []
            s = np.full(len(m), -2.0, dtype=np.float32)
            s[ids] = m[ids].astype(np.float32) @ qv
            order = ids[np.argsort(-s[ids])]
        else:
            s = np.concatenate([m[i:i + BLOCK].astype(np.float32) @ qv for i in range(0, len(m), BLOCK)])
            s[~act[:len(s)]] = -2
            cand = np.flatnonzero(s >= floor)
            order = cand[np.argsort(-s[cand])]
        excl = self.excluded()
        out = []
        for b in range(0, len(order), 256):  # fetch candidates in batches, stop once k passed the filters
            ids = [int(i) + 1 for i in order[b:b + 256]]
            rows = {r[0]: r for r in db.execute(f"SELECT id,doc_id,page,t,text FROM chunk WHERE id IN ({','.join('?' * len(ids))})", ids)}
            for i in ids:
                row = rows.get(i)
                if not row or row[1] in excl or (allowed and not allowed(row[1])):
                    continue
                out.append({'doc_id': row[1], 'page': row[2], 't': row[3], 'text': row[4], 'sem': float(s[i - 1])})
                if len(out) >= k:
                    return out
        return out


def one(db, sql, args=()):
    """First row of a query, always stepping the statement to completion (an unfinished statement keeps an old
    snapshot of the database open in WAL mode, which is how stale row counts crept in before)."""
    rows = db.execute(sql, args).fetchall()
    return rows[0] if rows else None


def rrf(lists, k=60):
    scores = {}
    for name, items in lists:
        items = list(dict.fromkeys(items))  # a page split into several passages counts once, at its best rank
        for rank, key in enumerate(items):
            e = scores.setdefault(key, {'score': 0.0, 'ranks': {}})
            e['score'] += 1 / (k + rank + 1)
            e['ranks'][name] = rank + 1
    return scores


if __name__ == '__main__':  # python retrieval.py  -> build/refresh the semantic index once
    sys.path.insert(0, str(lastbook_paths.APP))
    import server
    ix = Index(server.catalog, server.trust, server.library.DB, excluded_fn=server.excluded_videos)
    print('documents:', len(ix.sources()), flush=True)
    stop = threading.Event()

    def report():
        while not stop.wait(30):
            print(json.dumps(ix.status), flush=True)
    threading.Thread(target=report, daemon=True).start()
    n = ix.update()
    stop.set()
    print('done', n, json.dumps(ix.status), flush=True)
