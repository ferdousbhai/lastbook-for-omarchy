"""Local retrieval service behind the window's search and `lastbook search`.

Keyword search (the library's own FTS5/BM25 index, read only) + meaning-based search fused with reciprocal-rank fusion.
Meaning-based search uses the embedding model that Recall for Omarchy already runs on this computer (EmbeddingGemma, on the
graphics card), through Recall's owner-only Unix socket: no model of our own, no network. Without Recall, or while it is
busy or updating, meaning-based search is off and keyword search carries on.

Every passage carries provenance: catalog id, PDF file page or video timestamp, and which retriever found it.

The semantic index lives in derived/index/: semantic.sqlite (passages, per-document signatures, which model made the
vectors) and vectors.f16 (append-only float16 rows, row i = passage id i+1). It grows incrementally: documents whose
catalog signature changed or that are new get (re)embedded; a different model empties it. Nothing in library-data is written.

Who builds it: `lastbook index` (app/cli.py), which the user service lastbook-index.service runs with no window open
(packaging/systemd/), and the window's engine (Index.run_forever). One process at a time, whoever holds
derived/index/indexer.lock; every other process only reads, and maps the vector file instead of loading it. A document is
saved a few passages at a time, so an indexer that is stopped carries on where it was.
"""
import fcntl, functools, hashlib, html, json, os, re, shutil, socket, sqlite3, sys, threading, time
from pathlib import Path

import numpy as np

import engine.paths as lastbook_paths
from engine.paths import ro_uri
HERE = lastbook_paths.DATA  # holds derived/
LIB = lastbook_paths.LIB
IDX = HERE / 'derived' / 'index'
CHUNK, OVERLAP, MINLEN = 1100, 150, 80
BLOCK = 16384     # rows widened to float32 at a time while scoring: float16 matrix products get no BLAS and are ~15x slower
# What a passage must score to be offered. With the model Recall runs (EmbeddingGemma 2, q8) the cosine alone says little:
# every passage scores 0.45-0.63 against any question, and nonsense ("qwertyuiop", "'") scores 0.65-0.70 at best, as high
# as a real question about something the library does not hold. What tells them apart is how far a passage stands above
# the question's own background, the median over all passages. Measured on the installed library (30,372 passages of 225
# documents, 2026-11-22; 35 questions it covers, 20 off-topic, 10 gibberish): best passage minus median is 0.093-0.159
# for off-topic and gibberish (one at 0.171), 0.168-0.289 for questions the library covers. MARGIN 0.165 offers nothing
# for 29 of the former and something for all 35 of the latter.
FLOOR = 0.60      # cosine below which no passage is offered: nothing useful was measured under it
MARGIN = 0.165    # ... and a passage must stand this far above the question's median cosine over the whole index
INDEX_BATCH = 8   # passages the indexer sends at a time. Recall answers one request at a time and is no faster with 32
                  # (measured: ~6 passages a second from 4 up), so a search waits ~1 s behind the indexer, not ~5 s.
MAX_TRIES = 3     # passes in which one document may fail (unreadable, or Recall rejects it) before it is left for later
PARK_S = 3600     # ... and for how long: the documents behind it are not kept waiting
BUSY_PAUSE = 3    # seconds before the one second try of a batch Recall refused ("busy" comes and goes)
UNIT = 'lastbook-index.service'   # packaging/systemd/: runs `lastbook index` with no window open
PATH_UNIT = 'lastbook-index.path'   # ... whenever Recall's socket appears
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


def recall_installed():
    """Whether Recall for Omarchy is on this computer at all (running or not): its socket, its program or its service."""
    if recall_socket().exists() or shutil.which('recall'):
        return True
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return any(p.exists() for p in (Path('/usr/lib/recall-for-omarchy'), config / 'systemd/user/recall-for-omarchy.service',
                                    Path('/usr/lib/systemd/user/recall-for-omarchy.service')))


def kick(default_library=True):
    """Ask the user service to index now (`systemctl --user start --no-block`), and its path unit to watch for Recall, so
    the index is built with no window open. Returns whether systemd was asked. Never raises and never waits: no systemd,
    no unit (a source checkout) or a failed start is nothing to the caller, and starting a service that runs is nothing.
    default_library: False when the library in use is not the one `lastbook index` finds on its own (--data, LASTBOOK_DATA):
    the service indexes that one, so there is nothing to start. LASTBOOK_INDEX_KICK=0 turns this off; =1 allows it
    outside the installed package."""
    import subprocess
    want = os.environ.get('LASTBOOK_INDEX_KICK')
    if want == '0' or not default_library or not (lastbook_paths.INSTALLED or want == '1'):
        return False
    try:
        subprocess.Popen(['systemctl', '--user', 'start', '--no-block', PATH_UNIT, UNIT], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


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
        self.error, self.down_until = None, 0.0   # it answers: nobody waits out another caller's failure
        return reply

    def capabilities(self, timeout=3):
        reply = self.call({'op': 'capabilities'}, timeout)
        if 'embed' not in (reply.get('operations') or []):
            raise RecallUnavailable('this Recall has no embed operation')
        self.source = reply.get('source')
        return reply

    def embed(self, texts, timeout=120, batch=None):
        """float32 rows, unit length, one per text."""
        out = []
        size = min(batch or self.BATCH, self.BATCH)
        for i in range(0, len(texts), size):
            batch = texts[i:i + size]
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


PAGED = ('pdf', 'html', 'pptx')   # the kinds of document that are read as pages of text


def text_file(r):
    """The file of a document's text, one page after another."""
    return LIB / (r.get('text_path') or f"text/{r['id']}.txt")  # catalog may be mid-rewrite and miss text_path


def page_texts(r):
    """The document's text, one string per page. Cached per file version: the same few documents are read many times."""
    p = text_file(r)
    try:
        st = p.stat()
    except OSError:
        return []
    return list(_page_texts(str(p), st.st_mtime_ns, st.st_size))


@functools.lru_cache(maxsize=32)
def _page_texts(path, _mtime_ns, _size):
    t = Path(path).read_text(encoding='utf-8', errors='replace')
    return tuple(t.split('\n\f\n') if '\n\f\n' in t else t.split('\f'))


def said(path):
    """(seconds, text) of each cue of a VTT file that adds something, without what the cue before said (auto captions
    roll: each cue is the line before, then the new one)."""
    last = ''
    for at, t in cues(path):
        if last and t.startswith(last):
            t = t[len(last):].strip()
        if t:
            last = t
            yield at, t


def vtt_for(r, cat):
    """The caption file a video's transcript is read from: its transcript record's, else its first .vtt caption, whichever
    is there first; None if neither is."""
    tx = cat['video_tx'].get(r['path'])
    vtts = ([tx['vtt_path']] if tx and tx.get('vtt_path') else []) + [c for c in r.get('captions', []) if c.endswith('.vtt')]
    return next((v for v in vtts if (LIB / v).is_file()), None)


def vtt_windows(path, span=45):
    """Timed transcript windows (~45 s) so answers can cite a moment in a video."""
    out, start, buf = [], None, []
    for t, txt in said(path):
        if start is None:
            start = t
        buf.append(txt)
        if t - start >= span:
            out.append((start, ' '.join(buf)))
            start, buf = None, []
    if buf:
        out.append((start or 0, ' '.join(buf)))
    return [(t, norm_ws(x)) for t, x in out if len(x) > 40]


SCHEMA = '''CREATE TABLE IF NOT EXISTS chunk(id INTEGER PRIMARY KEY, doc_id TEXT, page INTEGER, t REAL, text TEXT, active INTEGER DEFAULT 1);
  CREATE INDEX IF NOT EXISTS chunk_doc ON chunk(doc_id);
  CREATE INDEX IF NOT EXISTS chunk_active ON chunk(active);
  CREATE TABLE IF NOT EXISTS docstate(doc_id TEXT PRIMARY KEY, sig TEXT, nchunks INTEGER, at REAL);
  CREATE TABLE IF NOT EXISTS gaps(doc_id TEXT PRIMARY KEY, reason TEXT);
  CREATE TABLE IF NOT EXISTS staging(doc_id TEXT PRIMARY KEY, sig TEXT, digest TEXT, total INTEGER);
  CREATE TABLE IF NOT EXISTS retry(doc_id TEXT PRIMARY KEY, sig TEXT, tries INTEGER, at REAL, reason TEXT);
  CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);'''
# chunk.active: 1 a passage search may return; 0 one that was replaced, removed or given up; 2 one of a document that is
# still being embedded (its rows and vectors are saved batch by batch and switched to 1 together when the last is in).
# staging: the document being embedded, so a later run carries on after its saved passages. retry: documents that could not
# be read or that Recall would not embed, how often, when. docstate: what is finished (sig = the version that was embedded).


class _Stopped(Exception):
    """The indexer was asked to stop (Index.stop): what is saved stays, the rest waits for the next run."""


def damaged(e):
    """Whether a SQLite error says the file itself cannot be read (not busy, not missing: those pass)."""
    return type(e) is sqlite3.DatabaseError


@functools.lru_cache(maxsize=None)
def _file_hash(path, _mtime_ns, _size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def file_sig(path):
    """A short hash of a file's content, read once per version of the file."""
    st = path.stat()
    return _file_hash(str(path), st.st_mtime_ns, st.st_size)


class Index:
    def __init__(self, catalog_fn, trust_fn, fts_db, recall=None, excluded_fn=lambda: set()):
        """excluded_fn: the ids of rejected videos and of their records (server.excluded_videos); never indexed or returned.
        Opening it writes nothing and reads only which model made the index and how much it holds: the vectors are mapped
        (not loaded) the first time a search needs them. Vectors past the committed rows may be another process's,
        mid-write, so the file is only ever cut by the process holding the indexer lock (update())."""
        self.catalog_fn, self.trust_fn, self.fts_db, self.excluded = catalog_fn, trust_fn, fts_db, excluded_fn
        self.db_path = IDX / 'semantic.sqlite'
        self.vec_path = IDX / 'vectors.f16'
        self._tls = threading.local()
        self._conns = []                   # every thread's read connection, to close them with the index
        self._gen = 0                      # raised when semantic.sqlite is another file than before: read connections reopen
        self.lock = threading.RLock()
        self.recall = recall or Recall()   # for questions (and encyclopedia.py's ranker)
        # The indexer's own client: a question that times out must not stop the indexer, nor the other way round.
        self.indexer_recall = Recall(self.recall.path)
        self.stop = threading.Event()      # set to end update()/build() after the batch in hand
        self.busy_pause = BUSY_PAUSE
        self.source, self.dim = None, None
        self._mat = np.zeros((0, 0), dtype=np.float16)
        self._active = np.zeros(0, dtype=bool)
        self._loaded = self._indexing = False
        self._ident = None
        self._qcache = {}
        self.status = {'state': 'loaded', 'chunks': 0, 'docs': 0, 'pending': None, 'rate': None}
        self._peek()

    # ------------------------------------------------------------- the files --
    def _db(self):
        """Writer connection: autocommit, so no transaction is ever left open by accident; writes use tx()."""
        return sqlite3.connect(self.db_path, timeout=60, isolation_level=None, check_same_thread=False)

    def _writer(self):
        """The indexer's connection, with the tables made. A file SQLite cannot read is set aside and a new index begun:
        it is a cache, and the library must not wait on it."""
        IDX.mkdir(parents=True, exist_ok=True)
        for again in (False, True):
            db = None
            try:
                db = self._db()
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript(SCHEMA)
                return db
            except sqlite3.DatabaseError as e:
                if db is not None:
                    db.close()
                if again or not damaged(e):
                    raise
                self._set_aside(e)

    def _ro(self):
        """Per-thread read-only connection, reused (it can never take the write lock)."""
        c = getattr(self._tls, 'ro', None)
        if c is None or self._tls.gen != self._gen:
            if c is not None:
                c.close()
            c = sqlite3.connect(ro_uri(self.db_path), uri=True, timeout=30, isolation_level=None, check_same_thread=False)
            self._tls.ro, self._tls.gen = c, self._gen
            with self.lock:
                self._conns = [x for x in self._conns if x is not None][-64:] + [c]
        return c

    def close(self):
        """Close the read connections (one per thread that searched). The index opens them again when asked."""
        conns, self._conns = self._conns, []
        self._gen += 1
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass

    __del__ = close

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

    @property
    def mat(self):
        """The vectors, row i = passage id i+1: the file mapped read-only, so processes share it and none holds a copy."""
        self._ensure()
        return self._mat

    @property
    def active(self):
        self._ensure()
        return self._active

    def _ensure(self):
        if not self._loaded:
            with self.lock:
                if not self._loaded:
                    self._load()

    def _ident_now(self):
        """What changes whenever an indexer commits, appends vectors, or starts the index again."""
        out = []
        for p in (self.db_path, self.db_path.with_name(self.db_path.name + '-wal'), self.vec_path):
            try:
                st = p.stat()
                out.append((st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                out.append(None)
        return tuple(out)

    def refresh(self):
        """Pick up what another process (the indexer) wrote since we last looked: passages, a new model, a new index."""
        if self._indexing:
            return
        now = self._ident_now()
        if now == self._ident:
            return
        with self.lock:
            if self._ident and now[0] and self._ident[0] and now[0][0] != self._ident[0][0]:
                self._gen += 1   # semantic.sqlite was replaced (a damaged one set aside)
            if self._loaded:
                self._load()
            else:
                self._peek()

    def _empty(self):
        self._mat, self._active = np.zeros((0, self.dim or 0), dtype=np.float16), np.zeros(0, dtype=bool)

    def _unreadable(self, e):
        """A reader met an index file it cannot read: meaning-based search is off, everything else carries on. The next
        indexer pass sets the file aside and builds a new one."""
        self.source, self.dim = None, None
        self._empty()
        if not self.status.get('damaged'):
            print(f'semantic index: cannot read {self.db_path.name} ({e}); searching by keyword only', file=sys.stderr, flush=True)
        self.status.update(chunks=0, docs=0, damaged=str(e)[:120])

    def _read_meta(self, db):
        meta = dict(db.execute('SELECT key, value FROM meta').fetchall())
        self.source, self.dim = meta.get('source'), int(meta['dim']) if meta.get('dim') else None

    def _peek(self):
        """Which model, which width and how much, without touching the vectors: all that most commands need."""
        with self.lock:
            self._ident = self._ident_now()
            if not self.db_path.exists():
                self.source, self.dim = None, None
                self.status.update(chunks=0, docs=0)
                return
            try:
                db = self._ro()
                self._read_meta(db)
                n, docs = one(db, 'SELECT coalesce(sum(nchunks), 0), count(*) FROM docstate WHERE nchunks>0')
            except (sqlite3.DatabaseError, ValueError) as e:   # ValueError: a width that is not a number
                return self._unreadable(e)
            self.status.update(chunks=n, docs=docs)
            self.status.pop('damaged', None)

    def _load(self, truncate=False, db=None):
        """Map the vectors and mark which passages are live. truncate (only with the indexer lock held): cut the vectors
        past the committed rows, which an interrupted run left."""
        with self.lock:
            self._loaded = True
            self._ident = self._ident_now()
            if db is None and not self.db_path.exists():
                self.source, self.dim = None, None
                self._empty()
                self.status.update(chunks=0, docs=0)
                return
            try:
                db = db or self._ro()
                self._read_meta(db)
                rows = one(db, 'SELECT max(id) FROM chunk')[0] or 0
                ids = np.fromiter((i for (i,) in db.execute('SELECT id FROM chunk WHERE active=1')), dtype=np.int64)
                docs = one(db, 'SELECT count(*) FROM docstate WHERE nchunks>0')[0]
            except (sqlite3.DatabaseError, ValueError) as e:
                if truncate:      # the indexer: update() sets the file aside
                    raise
                return self._unreadable(e)
            try:
                size = self.vec_path.stat().st_size
            except OSError:
                size = 0
            if not self.dim:
                if truncate and size:   # vectors of no known width: a start-over that was cut short
                    self.vec_path.unlink()
                n = 0
            else:
                n = min(size // (self.dim * 2), rows)  # vectors past the committed rows: an interrupted run, or one mid-write
                if truncate and n * self.dim * 2 != size:
                    with open(self.vec_path, 'r+b') as f:
                        f.truncate(n * self.dim * 2)
            if n:
                self._mat = np.memmap(self.vec_path, dtype=np.float16, mode='r', shape=(n, self.dim))
            else:
                self._empty()
            act = np.zeros(n, dtype=bool)
            act[ids[ids <= n] - 1] = True
            self._active = act
            self.status.update(chunks=int(act.sum()), docs=docs)
            self.status.pop('damaged', None)

    def _set_aside(self, e):
        """semantic.sqlite cannot be read: keep it under another name (one copy) and start an empty index. Indexer only."""
        print(f'semantic index: {self.db_path.name} is damaged ({e}); set aside, building a new index', file=sys.stderr, flush=True)
        with self.lock:
            for suffix in ('-wal', '-shm'):
                self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
            if self.db_path.exists():
                os.replace(self.db_path, self.db_path.with_name(self.db_path.name + '.damaged'))
            self.vec_path.unlink(missing_ok=True)   # vectors mean nothing without their rows
            self._gen += 1
            self.source, self.dim = None, None
            self._qcache.clear()
            self._empty()
            self.status.update(chunks=0, docs=0, reset={'why': 'the index file was damaged', 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
            self.status.pop('damaged', None)

    # ------------------------------------------------------------- the model --
    def _match_model(self, db):
        """Ask Recall which model it runs. A different one than made the stored vectors, or the same name with vectors of
        another width, empties the index (they cannot be compared)."""
        src = self.indexer_recall.capabilities()['source']
        if not src:
            raise RecallUnavailable('Recall reported no model')
        self._read_meta(db)
        if src != self.source:
            return self._start_over(db, src, 'Recall model changed')
        if self.dim:
            width = self.indexer_recall.embed(['width'], timeout=60).shape[1]
            if self.indexer_recall.source != src:
                raise RecallUnavailable('Recall model changed while indexing')
            if width != self.dim:
                self._start_over(db, src, 'Recall vector width changed')

    def _start_over(self, db, src, why):
        had = one(db, 'SELECT count(*) FROM chunk')[0]

        def w(db):
            for table in ('chunk', 'docstate', 'gaps', 'staging', 'retry', 'meta'):
                db.execute(f'DELETE FROM {table}')
            db.execute("INSERT INTO meta VALUES ('source', ?)", (src,))
        with self.lock:
            self.tx(db, w)
            # A new file, not the old one cut to nothing: a process that has the old one mapped keeps what it mapped
            # until it looks again (refresh), instead of faulting on pages that are gone.
            self.vec_path.unlink(missing_ok=True)
            self._qcache.clear()
            self._load(db=db)
        if had:
            self.status['reset'] = {'why': why, 'passages_dropped': had, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    def query_vector(self, q):
        """The question as a vector, or None when meaning-based search cannot run now."""
        q = q.strip() if isinstance(q, str) else q
        if not q:
            return None
        self.refresh()
        if not self.source or not len(self.mat):
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
        """(doc_id, signature, priority, loader) for every text that can be searched by meaning. The signature changes
        when the text does: the catalog's hash of a document, the content of a caption file."""
        cat = self.catalog_fn()
        excl = self.excluded()
        out = []
        for r in cat['by_id'].values():
            if r['id'] in excl:
                continue
            k = r['kind']
            if k in PAGED:
                sig = f"{r.get('sha256')}:{r.get('pages')}"
                out.append((r['id'], sig, -self.trust_fn(r)[1], ('pages', r)))
            elif k == 'video':
                tx = cat['video_tx'].get(r['path'])
                vtt = vtt_for(r, cat)
                if vtt:
                    try:   # the file's content, not only its name: a corrected transcript is embedded again
                        sig = f'vtt:{vtt}:{file_sig(LIB / vtt)}'
                    except OSError:
                        sig = f'vtt:{vtt}:unread'
                    out.append((r['id'], sig, -self.trust_fn(r)[1], ('vtt', r, vtt)))
                elif tx and (LIB / tx['path']).is_file():
                    out.append((r['id'], 'tx:' + tx['sha256'], -self.trust_fn(r)[1], ('tx', r, tx)))
        out.sort(key=lambda x: x[2])
        return out

    def _texts(self, doc_id, how):
        """(page, t, text) passages of one document. Raises when the text cannot be read now (tried again later); an
        empty list means it was read and holds nothing to embed."""
        kind, r = how[0], how[1]
        if kind == 'pages':
            con = sqlite3.connect(ro_uri(self.fts_db), uri=True)
            try:
                rows = con.execute('SELECT page, body FROM pages WHERE doc_id=? ORDER BY CAST(page AS INTEGER)', (doc_id,)).fetchall()
            finally:
                con.close()
            if not rows:  # not in the keyword index (yet); fall back to the extracted text file
                if not text_file(r).is_file():
                    raise FileNotFoundError('not in the keyword index and no text file')
                rows = list(enumerate(page_texts(r), 1))
            return [(int(p), None, c) for p, b in rows for c in chunks_of(b)]
        if kind == 'vtt':
            return [(None, t, txt) for t, txt in vtt_windows(LIB / how[2])]
        return [(None, None, c) for c in chunks_of((LIB / how[2]['path']).read_text(encoding='utf-8', errors='replace'))]

    def _coverage(self, db, srcs=None):
        """({'total', 'indexed', 'no_text', 'pending', 'failed', 'passages'}, [(source, parked)] still to embed)."""
        srcs = self.sources() if srcs is None else srcs
        known = {d: (s, n) for d, s, n in db.execute('SELECT doc_id, sig, nchunks FROM docstate').fetchall()}
        try:
            retry = {d: (s, n, at) for d, s, n, at in db.execute('SELECT doc_id, sig, tries, at FROM retry').fetchall()}
        except sqlite3.OperationalError:   # an index from before that table, not yet opened by an indexer
            retry = {}
        now = time.time()
        out = {'total': len(srcs), 'indexed': 0, 'no_text': 0, 'pending': 0, 'failed': 0, 'passages': 0}
        todo = []
        for s in srcs:
            k = known.get(s[0])
            if k and k[0] == s[1]:
                out['indexed' if k[1] else 'no_text'] += 1
                out['passages'] += k[1]
                continue
            r = retry.get(s[0])
            parked = bool(r and r[0] == s[1] and r[1] >= MAX_TRIES and now - r[2] < PARK_S)
            out['failed' if parked else 'pending'] += 1
            todo.append((s, parked))
        return out, todo

    def coverage(self):
        """How much of the library the index covers, in documents: total (every document and captioned video that can be
        embedded), indexed (embedded, in their current version), no_text (read, nothing to embed), pending (still to
        embed), failed (could not be read or embedded for now: tried again in an hour), and passages. Reads only."""
        srcs = self.sources()
        if self.db_path.exists():
            try:
                return self._coverage(self._ro(), srcs)[0]
            except sqlite3.DatabaseError:
                pass
        return {'total': len(srcs), 'indexed': 0, 'no_text': 0, 'pending': len(srcs), 'failed': 0, 'passages': 0}

    def indexing_now(self):
        """Whether some process (this one too) is indexing at this moment: it holds indexer.lock."""
        try:
            with open(IDX / 'indexer.lock', 'rb') as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        return False

    def update(self):
        """One pass: embed new and changed documents. Returns the number of documents finished, -1 when another process is
        indexing. Recall being away is not an error: status['state'] is then 'waiting for Recall' and what was embedded
        so far is kept."""
        IDX.mkdir(parents=True, exist_ok=True)
        lockf = open(IDX / 'indexer.lock', 'a')
        for again in (False, True):
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:  # another process is indexing; just follow its progress
                if again:
                    lockf.close()
                    self.refresh()
                    cov = self.coverage()
                    self.status.update(state='indexing (other process)', total=cov['total'], indexed=cov['indexed'],
                                       pending=cov['pending'], failed=cov['failed'])
                    return -1
                time.sleep(0.2)   # or `lastbook status` is looking at the lock, for an instant (indexing_now)
        self._indexing = True
        try:
            for again in (False, True):
                try:
                    return self._pass()
                except sqlite3.DatabaseError as e:
                    if again or not damaged(e):
                        raise
                    self._set_aside(e)
        finally:
            self._indexing = False
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()

    def _pass(self):
        db = self._writer()
        try:
            try:
                self._match_model(db)
            except RecallUnavailable as e:
                self._waiting(db, e)
                return 0
            self.status.pop('recall', None)
            self._reconcile(db)
            self._load(truncate=True, db=db)
            return self._update_with(db)
        finally:
            db.close()

    def _waiting(self, db, e, srcs=None):
        cov = self._coverage(db, srcs)[0]
        self.status.update(state='waiting for Recall', recall=str(e), total=cov['total'], indexed=cov['indexed'],
                           pending=cov['pending'], failed=cov['failed'])
        self.status.pop('doc', None)

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
                db.execute('DELETE FROM staging WHERE doc_id=?', (d,))
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
                db.execute('DELETE FROM staging WHERE doc_id=?', (d,))
                db.execute('DELETE FROM retry WHERE doc_id=?', (d,))
                if forget_text:
                    db.execute('DELETE FROM gaps WHERE doc_id=?', (d,))
        self.tx(db, w)
        with self.lock:
            for i in ids:
                if i <= len(self._active):
                    self._active[i - 1] = False
        return len(ids)

    def _failed(self, db, doc_id, sig, reason):
        """This document could not be read or embedded in this pass. It is tried again in the next, and after MAX_TRIES
        left for PARK_S; nothing says it is indexed, and the documents behind it go on."""
        def w(db):
            row = one(db, 'SELECT sig, tries FROM retry WHERE doc_id=?', (doc_id,))
            tries = (row[1] if row and row[0] == sig else 0) + 1
            db.execute('INSERT OR REPLACE INTO retry VALUES (?,?,?,?,?)', (doc_id, sig, tries, time.time(), reason[:200]))
            return tries
        tries = self.tx(db, w)
        print(f'semantic index: {doc_id}: {reason[:200]} (try {tries} of {MAX_TRIES})', file=sys.stderr, flush=True)

    def _recall_well(self):
        """Whether Recall embeds at all right now: tells a document Recall will not take from Recall being away or busy."""
        r = self.indexer_recall
        r.down_until = 0.0
        try:
            r.capabilities()
            r.embed(['Water boils at one hundred degrees.'], timeout=60)
            return True
        except RecallUnavailable:
            return False

    def _aligned_start(self, db):
        """The id the next passage gets, once the vector file ends exactly where the rows do (row i = passage id i+1)."""
        rows = one(db, 'SELECT max(id) FROM chunk')[0] or 0
        want = rows * self.dim * 2
        size = self.vec_path.stat().st_size if self.vec_path.exists() else 0
        if size > want:   # vectors whose rows were never committed
            with open(self.vec_path, 'r+b') as f:
                f.truncate(want)
        elif size < want:  # rows without vectors: the file was cut or lost under us. Put it right, and take this pass again.
            self._reconcile(db)
            self._load(truncate=True, db=db)
            raise RuntimeError(f'vector file had {size // (self.dim * 2)} rows but the database {rows}; realigned')
        if len(self._mat) != rows:
            self._load(db=db)
        return rows + 1

    def _embed_doc(self, db, doc_id, sig, title, parts, note):
        """Embed one document INDEX_BATCH passages at a time, each batch saved (rows marked 2, vectors appended) as it
        comes, so a pass that ends early loses one batch at most; the last batch makes the document live. Returns the
        number of passages embedded in this call."""
        total = len(parts)
        digest = hashlib.sha256('\x1f'.join(p[2] for p in parts).encode('utf-8', 'replace')).hexdigest()[:32]
        st = one(db, 'SELECT sig, digest, total FROM staging WHERE doc_id=?', (doc_id,))
        have = one(db, 'SELECT count(*) FROM chunk WHERE doc_id=? AND active=2', (doc_id,))[0]
        if st != (sig, digest, total) or have > total:   # nothing saved, or saved from another version of the text

            def begin(db):
                db.execute('UPDATE chunk SET active=0 WHERE doc_id=? AND active=2', (doc_id,))
                db.execute('INSERT OR REPLACE INTO staging VALUES (?,?,?,?)', (doc_id, sig, digest, total))
            self.tx(db, begin)
            have = 0
        made = 0
        for i in range(have, total, INDEX_BATCH):
            if self.stop.is_set():
                raise _Stopped()
            batch = parts[i:i + INDEX_BATCH]
            texts = [DOCUMENT.format(title, p[2]) for p in batch]
            for again in (False, True):   # one more try: Recall says "busy" now and then
                try:
                    v = self.indexer_recall.embed(texts, batch=INDEX_BATCH).astype(np.float16)
                    break
                except RecallUnavailable:
                    if again or self.stop.wait(self.busy_pause):
                        raise
                    self.indexer_recall.down_until = 0.0
            if self.indexer_recall.source != self.source:
                raise RecallUnavailable('Recall model changed while indexing')  # the next pass empties the index and starts again
            with self.lock:
                if self.dim is None:  # the first vectors set the width
                    self.dim = v.shape[1]
                    self.tx(db, lambda db: db.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(self.dim),)))
                    self._empty()
                if v.shape[1] != self.dim:
                    raise RecallUnavailable('Recall vector width changed')   # the next pass sees it and starts again
                start = self._aligned_start(db)

                def w(db):
                    db.executemany('INSERT INTO chunk(id,doc_id,page,t,text,active) VALUES (?,?,?,?,?,2)',
                                   [(start + j, doc_id, p[0], p[1], p[2]) for j, p in enumerate(batch)])
                    with open(self.vec_path, 'ab') as f:  # vectors last, inside the transaction: a failed write never leaves orphan rows
                        f.write(v.tobytes())
                        f.flush()
                        os.fsync(f.fileno())
                self.tx(db, w)
                self._mat = np.memmap(self.vec_path, dtype=np.float16, mode='r', shape=(start - 1 + len(v), self.dim))
                self._active = np.concatenate([self._active, np.zeros(len(v), dtype=bool)])
            made += len(batch)
            note(made, i + len(batch), total)
        with self.lock:
            old = [i for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1', (doc_id,)).fetchall()]
            new = [i for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=2', (doc_id,)).fetchall()]
            if len(new) != total:   # its saved passages were dropped on the way (a realignment): not finished, taken again
                self.tx(db, lambda db: db.execute('DELETE FROM staging WHERE doc_id=?', (doc_id,)))
                raise RuntimeError(f'{doc_id}: {len(new)} of {total} passages saved')

            def done(db):
                db.execute('UPDATE chunk SET active=0 WHERE doc_id=? AND active=1', (doc_id,))
                db.execute('UPDATE chunk SET active=1 WHERE doc_id=? AND active=2', (doc_id,))
                db.execute('DELETE FROM gaps WHERE doc_id=?', (doc_id,))
                db.execute('DELETE FROM staging WHERE doc_id=?', (doc_id,))
                db.execute('DELETE FROM retry WHERE doc_id=?', (doc_id,))
                db.execute('INSERT OR REPLACE INTO docstate VALUES (?,?,?,?)', (doc_id, sig, total, time.time()))
            self.tx(db, done)
            for i in old:
                self._active[i - 1] = False
            for i in new:
                self._active[i - 1] = True
        return made

    def _no_text(self, db, doc_id, sig):
        """Read, and nothing in it to embed: finished, with the reason kept (gaps)."""
        def w(db):
            db.execute('UPDATE chunk SET active=0 WHERE doc_id=?', (doc_id,))
            db.execute('INSERT OR REPLACE INTO gaps VALUES (?,?)', (doc_id, 'no extractable text'))
            db.execute('DELETE FROM staging WHERE doc_id=?', (doc_id,))
            db.execute('DELETE FROM retry WHERE doc_id=?', (doc_id,))
            db.execute('INSERT OR REPLACE INTO docstate VALUES (?,?,0,?)', (doc_id, sig, time.time()))
        old = [i for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1', (doc_id,)).fetchall()]
        with self.lock:
            self.tx(db, w)
            for i in old:
                self._active[i - 1] = False

    def _update_with(self, db):
        srcs = self.sources()
        excl = self.excluded()
        live = {s[0] for s in srcs}
        # Prune: documents gone from the catalog, and rejected videos (also any stray passages they left behind).
        known = {d for t in ('docstate', 'staging', 'retry') for (d,) in db.execute(f'SELECT doc_id FROM {t}').fetchall()}
        stale = [d for d in known if d not in live]
        stray = [d for (d,) in db.execute(f"SELECT DISTINCT doc_id FROM chunk WHERE active=1 AND doc_id IN ({','.join('?' * len(excl))})", list(excl)).fetchall()] if excl else []
        pruned = 0
        if stale or stray:
            rejected = [d for d in set(stale) | set(stray) if d in excl]
            pruned += self._hide(db, [d for d in stale if d not in excl])
            pruned += self._hide(db, rejected, forget_text=True)
        cov, todo = self._coverage(db, srcs)
        due = [s for s, parked in todo if not parked]
        self.status.update(state='indexing' if due else 'up to date' if not todo else 'incomplete', pending=len(due), failed=cov['failed'],
                           total=cov['total'], indexed=cov['indexed'], pruned_last=pruned)
        t0, done, embedded, away = time.time(), 0, 0, None

        for doc_id, sig, _, how in due:
            if self.stop.is_set():
                break
            title = how[1].get('title') or 'none'
            try:
                parts = self._texts(doc_id, how)
            except Exception as e:   # the keyword index or the file could not be read just now: not "indexed", tried again
                self._failed(db, doc_id, sig, f'unreadable: {type(e).__name__}: {e}')
                continue
            if not parts:
                self._no_text(db, doc_id, sig)
                done += 1
                self.status.update(pending=len(due) - done)
                continue
            before = embedded

            def note(made, at, total):
                el = time.time() - t0
                self.status.update(doc={'id': doc_id, 'passages_done': at, 'passages': total}, chunks=int(self._active.sum()),
                                   rate=round((before + made) / el, 1) if el > 1 else None)
            try:
                embedded += self._embed_doc(db, doc_id, sig, title, parts, note)
            except _Stopped:
                break
            except RecallUnavailable as e:
                # What is saved of this document stays saved. Is it this document, or is Recall away?
                if 'changed' in str(e) or not self._recall_well() or self.indexer_recall.source != self.source:
                    print(f'semantic index: waiting for Recall ({e}) at {doc_id}', file=sys.stderr, flush=True)
                    away = e
                    break
                self._failed(db, doc_id, sig, f'Recall: {e}')
                continue
            done += 1
            self.status.update(pending=len(due) - done, indexed=self.status['indexed'] + 1, chunks=int(self._active.sum()))
        self.status.pop('doc', None)
        if away is not None:
            self._waiting(db, away, srcs)
            return done
        cov = self._coverage(db, srcs)[0]
        self.status.update(state='indexing' if cov['pending'] else 'incomplete' if cov['failed'] else 'up to date',
                           pending=cov['pending'], failed=cov['failed'], total=cov['total'], indexed=cov['indexed'],
                           chunks=int(self._active.sum()), docs=one(db, 'SELECT count(*) FROM docstate WHERE nchunks>0')[0])
        return done

    def build(self, wait=0, pause=15, report=None):
        """Index until nothing is pending, pass after pass, then say how it went: what `lastbook index` runs.
        wait: seconds to keep asking a Recall that is there (its socket is) but does not answer or is busy; a Recall that
        is not running ends the build at once. pause: seconds between two tries. report(status) is called about every
        half minute."""
        t0, done, since, errors = time.time(), 0, None, 0
        start = self.status.get('chunks') or 0
        self.busy_pause = min(BUSY_PAUSE, pause)
        ticker = threading.Event()
        if report:
            def tick():
                while not ticker.wait(30):
                    report(dict(self.status))
            threading.Thread(target=tick, daemon=True).start()
        try:
            while True:
                try:
                    n = self.update()
                    errors = 0
                except Exception as e:   # a pass that fell over (the realignment above, a full disk): twice more, then it is the caller's
                    errors += 1
                    if errors >= 3 or isinstance(e, RecallUnavailable):
                        raise
                    print(f'semantic index: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
                    if self.stop.wait(2):
                        outcome = 'stopped'
                        break
                    continue
                if n < 0:
                    outcome = 'indexing elsewhere'
                    break
                done += n
                state = self.status.get('state')
                if self.stop.is_set():
                    outcome = 'stopped'
                    break
                if state == 'waiting for Recall':
                    since = time.time() if n or since is None else since
                    if not self.indexer_recall.path.exists() or time.time() - since >= wait or self.stop.wait(pause):
                        outcome = 'stopped' if self.stop.is_set() else 'waiting for Recall'
                        break
                    self.indexer_recall.down_until = 0.0
                    continue
                since = None
                if not self.status.get('pending'):
                    outcome = 'incomplete' if self.status.get('failed') else 'complete'
                    break
                # Pending still: documents that failed in this pass. Each is tried MAX_TRIES times, a while apart (what
                # fails now may pass in a moment), then left for later.
                if self.stop.wait(pause):
                    outcome = 'stopped'
                    break
        finally:
            ticker.set()
        el = time.time() - t0
        made = max(0, (self.status.get('chunks') or 0) - start)
        return {'outcome': outcome, 'documents_indexed_now': done, 'passages_indexed_now': made, 'seconds': round(el, 1),
                'passages_per_second': round(made / el, 1) if made and el > 1 else None}

    def run_forever(self, every=300, retry=60):
        while True:
            wait = every
            try:
                n = self.update()
                if n >= 0:  # a successful pass clears any earlier error
                    self.status.pop('error', None)
                    self.status['last_ok'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                if self.status.get('state') in ('waiting for Recall', 'indexing'):   # indexing: documents that failed are tried again
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
    def semantic(self, q, k=60, allowed=None, floor=FLOOR, doc=None, margin=MARGIN):
        """Best passages for q (a question, or its query_vector): those at or above floor and at least margin above the
        question's median over all passages (FLOOR, MARGIN). doc: only that document's passages, best first, no floor."""
        qv = self.query_vector(q) if isinstance(q, str) else q
        if qv is None:
            return []
        self.refresh()
        with self.lock:
            m, act = self.mat, self.active
        if not len(m) or m.shape[1] != len(qv):
            return []
        try:
            return self._semantic(qv, m, act, k, allowed, floor, doc, margin)
        except sqlite3.DatabaseError as e:   # the index file went bad under us: by keyword only
            self._unreadable(e)
            return []

    def centroid(self, doc_id):
        """The average of a document's passage vectors, as one unit vector: what the document is about by meaning, to
        hand to semantic() in place of a question (server.related). None when the index holds none of its passages.
        It needs no Recall: the vectors are the index's own."""
        self.refresh()
        with self.lock:
            m = self.mat
        if not len(m):
            return None
        try:
            ids = [i - 1 for (i,) in self._ro().execute('SELECT id FROM chunk WHERE doc_id=? AND active=1 AND id<=?', (doc_id, len(m)))]
        except sqlite3.DatabaseError as e:
            self._unreadable(e)
            return None
        if not ids:
            return None
        v = m[ids].astype(np.float32).mean(axis=0)
        n = float(np.linalg.norm(v))
        return v / n if n else None

    def _semantic(self, qv, m, act, k, allowed, floor, doc, margin):
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
            live = act[:len(s)]
            s[~live] = -2
            if not live.any():
                return []
            cand = np.flatnonzero(s >= max(floor, float(np.median(s[live])) + margin))
            order = cand[np.argsort(-s[cand])]
        excl = self.excluded()
        out = []
        for b in range(0, len(order), 256):  # fetch candidates in batches, stop once k passed the filters
            ids = [int(i) + 1 for i in order[b:b + 256]]
            # active=1 again here: a passage hidden since the vectors were mapped is not returned
            rows = {r[0]: r for r in db.execute(f"SELECT id,doc_id,page,t,text FROM chunk WHERE active=1 AND id IN ({','.join('?' * len(ids))})", ids)}
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
