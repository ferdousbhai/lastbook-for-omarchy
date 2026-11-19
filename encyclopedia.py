"""Encyclopedia articles (ZIM files in library-data/collections) as citable passages for Chat.

Each ZIM file carries its own full-text index (Xapian, every word must match) and a title index, which find candidate
articles in milliseconds. They match words, not meaning ("bleeding from the leg" finds a museum), so with Recall running the
candidates are ranked by meaning: first each article's opening, then a few keyword-picked passages of the best two, and weak
ones are dropped. Each embedding costs graphics-card time (about 15 passages a second on an M1 Max), so a question embeds at
most about 26 short texts. Without Recall only articles whose title names an idea in the question are kept, with their
best-matching passage.

No vectors are kept on disk: the encyclopedias hold hundreds of thousands of articles, and only the few a question reaches are
ever embedded. Parsed articles and their vectors are cached in memory.
"""
import re, sqlite3, threading, urllib.parse
from collections import OrderedDict
from html.parser import HTMLParser

import numpy as np

import retrieval
from engine.library import follow

ARTICLE_PREFIX = 'zim:'      # doc ids: zim:<file name>/<article path>
PER_ZIM = 6                  # full-text candidates per encyclopedia for all the question's words
CANDIDATES = 20              # articles whose opening is ranked by meaning
LEAD = 500                   # characters of an article's opening used to rank it
DEEP, PASSAGES = 2, 3        # best articles, and keyword-picked passages of each, ranked by meaning too
MAX_PASSAGES = 40            # passages kept per parsed article
CACHE = 1                    # bump when LEAD or the passage chunking changes: cached openings and vectors start over
FLOOR = 0.73                 # measured with tools/eval: off-topic articles score 0.55-0.71, useful ones 0.74-0.85
SKIP_CLASSES = re.compile(r'\b(reference|references|reflist|mw-references-wrap|navbox|infobox|hatnote|navigation-not-searchable|'
                          r'zim-footer|template-databox|metadata|mw-editsection|thumb|toc|sidebar|ambox|noprint)\b')
SKIP_TAGS = {'style', 'script', 'table', 'figure', 'sup', 'noscript', 'math', 'svg'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
BLOCK = {'p', 'li', 'dd', 'dt', 'div', 'blockquote', 'pre', 'ul', 'ol', 'dl', 'section'}
END_SECTIONS = re.compile(r'^(references|external links|see also|further reading|notes|sources|bibliography|citations|footnotes)$', re.I)


class _Text(HTMLParser):
    """The readable text of a MediaWiki article, as (heading, text) sections; references, tables and navigation left out."""

    def __init__(self, lead_only=False):
        super().__init__(convert_charrefs=True)
        self.lead_only = lead_only  # stop at the first heading: the opening is all that ranking needs
        self.sections, self.heading, self.buf = [], '', []
        self.inside = False         # within #mw-content-text
        self.skip = None            # (tag, depth) of an element being left out
        self.head = None            # collecting a heading's text
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.skip:
            if tag == self.skip[0] and tag not in VOID:
                self.skip[1] += 1
            return
        a = dict(attrs)
        if not self.inside:
            self.inside = a.get('id') == 'mw-content-text'
            return
        if tag in SKIP_TAGS or SKIP_CLASSES.search(a.get('class') or ''):
            if tag not in VOID:
                self.skip = [tag, 1]
            return
        if tag in ('h2', 'h3', 'h4'):
            self._flush()
            if self.lead_only and self.sections:
                self.done = True
                return
            self.head = []
        elif tag in BLOCK or tag == 'br':
            self.buf.append('\n')

    def handle_endtag(self, tag):
        if self.done or not self.inside:
            return
        if self.skip:
            if tag == self.skip[0]:
                self.skip[1] -= 1
                if not self.skip[1]:
                    self.skip = None
            return
        if tag in ('h2', 'h3', 'h4') and self.head is not None:
            self.heading = re.sub(r'\s+', ' ', ''.join(self.head)).strip()
            self.head = None
            if END_SECTIONS.match(self.heading):
                self.done = True
        elif tag in BLOCK:
            self.buf.append('\n')

    def handle_data(self, data):
        if self.done or not self.inside or self.skip:
            return
        (self.head if self.head is not None else self.buf).append(data)

    def _flush(self):
        text = '\n'.join(re.sub(r'[ \t]+', ' ', line).strip() for line in ''.join(self.buf).split('\n'))
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            self.sections.append((self.heading, text))
        self.buf = []

    def result(self):
        if not self.done:
            self._flush()
        return self.sections


def article_sections(html, lead_only=False):
    p = _Text(lead_only)
    p.feed(html)
    p.close()
    return p.result()


class Encyclopedias:
    def __init__(self, lib, catalog_fn, open_archive, index):
        self.lib, self.catalog_fn, self.index = lib, catalog_fn, index
        self.archive = open_archive   # engine.library.open_zim: the open-archive cache, shared with the reader
        self._articles = OrderedDict()
        self._vecs = OrderedDict()
        self._lock = threading.Lock()
        self._disk = None             # what earlier searches worked out, kept between runs: see _cache()

    def _cache(self):
        """derived/index/encyclopedia-cache.sqlite: the opening of each article a search has looked at, and the vectors
        Recall gave for article text. A question about a topic seen before then costs no parsing and no embedding.
        Keyed by the encyclopedia's file name (it carries the edition) and, for vectors, the embedding model."""
        if self._disk is None:
            retrieval.IDX.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(retrieval.IDX / 'encyclopedia-cache.sqlite', check_same_thread=False)
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(f'''CREATE TABLE IF NOT EXISTS lead{CACHE}(zim TEXT, path TEXT, real TEXT, title TEXT, text TEXT, PRIMARY KEY(zim, path));
                CREATE TABLE IF NOT EXISTS vec{CACHE}(src TEXT, zim TEXT, path TEXT, part TEXT, v BLOB, PRIMARY KEY(src, zim, path, part));''')
            self._disk = db
        return self._disk

    def _lead(self, name, path):
        """(real path, title, opening text) of an article, from the cache or by reading it; None if it has no text."""
        with self._lock:
            row = self._cache().execute(f'SELECT real, title, text FROM lead{CACHE} WHERE zim=? AND path=?', (name, path)).fetchone()
        if row:
            return row if row[2] else None
        a = self.article(name, path, lead_only=True)
        row = (a['path'], a['title'], a['passages'][0][1][:LEAD]) if a and a['passages'] else (path, '', '')
        with self._lock:
            self._cache().execute(f'INSERT OR REPLACE INTO lead{CACHE} VALUES (?,?,?,?,?)', (name, path, *row))
            self._cache().commit()
        return row if row[2] else None

    # ---------------------------------------------------------------- files --
    def files(self):
        """{file name: catalog record} for each encyclopedia that is in the catalog and on disk."""
        out = {}
        for r in self.catalog_fn()['by_id'].values():
            if r['kind'] == 'zim' and r.get('path', '').startswith('collections/'):
                name = r['path'].split('/', 1)[1]
                if (self.lib / r['path']).is_file():
                    out[name] = r
        return out

    # ------------------------------------------------------------- articles --
    @staticmethod
    def doc_id(name, path):
        return f'{ARTICLE_PREFIX}{name}/{path}'

    @staticmethod
    def split_id(doc_id):
        if not doc_id.startswith(ARTICLE_PREFIX) or '/' not in doc_id:
            return None
        return tuple(doc_id[len(ARTICLE_PREFIX):].split('/', 1))

    def record(self, doc_id):
        """A catalog-shaped record for an article, or None. Its publisher and collection are the encyclopedia's."""
        parts = self.split_id(doc_id)
        if not parts:
            return None
        name, path = parts
        zr = self.files().get(name)
        art = zr and self.article(name, path)
        if not art:
            return None
        return {'id': doc_id, 'kind': 'article', 'title': art['title'], 'publisher': zr.get('publisher'),
                'collection': zr.get('collection'), 'category': 'encyclopedias', 'encyclopedia': zr.get('title'),
                'path': 'zim/' + urllib.parse.quote(name) + '/' + urllib.parse.quote(path, safe='/'),
                'edition': re.search(r'_(\d{4}-\d\d)\.zim$', name).group(1) if re.search(r'_(\d{4}-\d\d)\.zim$', name) else None,
                'notes': 'Community-written encyclopedia article: check safety-critical steps against a manual.'}

    def article(self, name, path, lead_only=False):
        """{'title', 'path', 'sections': [(heading, text)], 'passages': [(heading, text)]} for an article (redirects followed), or None.
        lead_only: just the opening, unless the whole article is cached already."""
        key = (name, path)
        with self._lock:
            for k in ((key, (key, 'lead')) if lead_only else (key,)):
                if k in self._articles:
                    self._articles.move_to_end(k)
                    return self._articles[k]
        try:
            arc = self.archive(name)
            entry = follow(arc.get_entry_by_path(path))
            item = entry.get_item()
            if 'html' not in item.mimetype:
                return None
            sections = article_sections(bytes(item.content).decode('utf-8', 'replace'), lead_only)
        except Exception:  # a missing or damaged entry costs this article only
            return None
        passages = [(h, c) for h, text in sections for c in retrieval.chunks_of(text)][:MAX_PASSAGES]
        art = {'title': entry.title, 'path': entry.path, 'sections': sections, 'passages': passages}
        with self._lock:
            self._articles[(key, 'lead') if lead_only else key] = art
            if len(self._articles) > 512:
                self._articles.popitem(last=False)
        return art

    # --------------------------------------------------------------- search --
    def candidates(self, words, groups):
        """(name, path) of articles the encyclopedias' own indexes find, most specific first and every encyclopedia in
        turn: articles with every word, titles naming an idea (or one of its everyday synonyms), then articles with all
        words but one."""
        from libzim.search import Query, Searcher
        from libzim.suggestion import SuggestionSearcher
        out, seen, arcs = [], set(), []
        for name in self.files():
            try:
                arcs.append((name, self.archive(name)))
            except Exception:  # one damaged file must not cost the answers from the others
                pass

        def add(name, find):
            try:
                paths = list(find())
            except Exception:
                return
            for path in paths:
                if (name, path) not in seen:
                    seen.add((name, path))
                    out.append((name, path))

        def ft(arc, ws, n):
            return Searcher(arc).search(Query().set_query(' '.join(ws))).getResults(0, n)
        for name, arc in arcs:
            if arc.has_fulltext_index:
                add(name, lambda: ft(arc, words, PER_ZIM))
        for name, arc in arcs:
            if arc.has_title_index:
                for w in [w for g in groups for w in g[:3] if len(w) > 3]:
                    add(name, lambda: SuggestionSearcher(arc).suggest(w).getResults(0, 2))
        if 2 < len(words) <= 7:
            for name, arc in arcs:
                if arc.has_fulltext_index:
                    for i in range(len(words)):
                        add(name, lambda: ft(arc, words[:i] + words[i + 1:], 2))
        return out

    def _embed(self, keys_texts):
        """Vectors for (key, title, text) triples, reusing cached ones. None if Recall cannot answer now."""
        src = self.index.source
        need = [(k, t, x) for k, t, x in keys_texts if (src, k) not in self._vecs]
        if need:                   # what an earlier run embedded
            with self._lock:
                for k, _, _ in need:
                    row = self._cache().execute(f'SELECT v FROM vec{CACHE} WHERE src=? AND zim=? AND path=? AND part=?', (src, k[0], k[1], str(k[2]))).fetchone()
                    if row:
                        self._vecs[(src, k)] = np.frombuffer(row[0], dtype=np.float16).astype(np.float32)
            need = [(k, t, x) for k, t, x in need if (src, k) not in self._vecs]
        if need:
            try:
                v = self.index.recall.embed([retrieval.DOCUMENT.format(t, x) for _, t, x in need], timeout=20)
            except retrieval.RecallUnavailable:
                return None
            if self.index.recall.source != src:
                return None
            with self._lock:
                for (k, _, _), row in zip(need, v):
                    self._vecs[(src, k)] = row
                self._cache().executemany(f'INSERT OR REPLACE INTO vec{CACHE} VALUES (?,?,?,?,?)',
                                          [(src, k[0], k[1], str(k[2]), np.asarray(row, dtype=np.float16).tobytes()) for (k, _, _), row in zip(need, v)])
                self._cache().commit()
                while len(self._vecs) > 20000:
                    self._vecs.popitem(last=False)
        return np.stack([self._vecs[(src, k)] for k, _, _ in keys_texts]) if keys_texts else np.zeros((0, 0))

    def search(self, words, groups, qv=None, k=DEEP):
        """Best article passages for a question's words: [{'doc_id', 'record', 'heading', 'text', 'sem'}], best first, one per article."""
        if not words:
            return []
        overlap = lambda x: sum(any(w in x.lower() for w in g) for g in groups)
        if qv is not None:
            leads, seen = [], set()
            for name, path in self.candidates(words, groups):
                lead = self._lead(name, path)
                if lead and (name, lead[0]) not in seen:   # two candidates can redirect to one article
                    seen.add((name, lead[0]))
                    leads.append((name, lead))
                if len(leads) >= CANDIDATES:
                    break
            vecs = self._embed([((n, real, 'lead'), title, text) for n, (real, title, text) in leads]) if leads else None
            if vecs is not None:
                deep = []      # the best articles' passages, embedded in one request to Recall
                for i in np.argsort(-(vecs @ qv))[:DEEP]:
                    n, a = leads[i][0], self.article(leads[i][0], leads[i][1][0])
                    if a and a['passages']:
                        picks = [0] + sorted(range(1, len(a['passages'])), key=lambda j: -overlap(a['passages'][j][1]))[:PASSAGES]
                        deep.append((n, a, picks))
                pv = self._embed([((n, a['path'], j), a['title'], (a['passages'][j][0] + ': ' if a['passages'][j][0] else '') + a['passages'][j][1])
                                  for n, a, picks in deep for j in picks]) if deep else None
                hits, at = [], 0
                for n, a, picks in deep if pv is not None else ():
                    s = pv[at:at + len(picks)] @ qv
                    at += len(picks)
                    best = int(np.argmax(s))
                    if s[best] >= FLOOR:
                        hits.append((float(s[best]), n, a, picks[best]))
                hits.sort(key=lambda h: -h[0])
                return [self._hit(n, a, j, sem) for sem, n, a, j in hits[:k]]
        # Words only: keep articles whose title names one of the ideas, with the passage that has most of them.
        hits = []
        for name, path in self.candidates(words, groups)[:CANDIDATES]:
            title = path.replace('_', ' ').lower()
            if not any(any(w in title for w in g) for g in groups):
                continue
            a = self.article(name, path)
            if a and a['passages'] and not any(h[2]['path'] == a['path'] for h in hits):
                score = [overlap(x) for _, x in a['passages']]
                j = int(np.argmax(score))
                hits.append((score[j], name, a, j))
        hits.sort(key=lambda h: -h[0])
        return [self._hit(n, a, j, None) for _, n, a, j in hits[:k]]

    def _hit(self, name, art, j, sem):
        doc_id = self.doc_id(name, art['path'])
        heading, text = art['passages'][j]
        return {'doc_id': doc_id, 'record': self.record(doc_id), 'heading': heading, 'text': text, 'sem': sem}

    def read(self, doc_id, section=None, limit=6000):
        """(record, text) of an article, or of the sections whose heading contains `section`; None if unknown."""
        parts = self.split_id(doc_id)
        art = parts and self.article(*parts)
        if not art:
            return None
        secs = art['sections']
        if section:
            picked = [s for s in secs if section.lower() in s[0].lower()]
            secs = picked or secs
        text = '\n\n'.join((f'## {h}\n' if h else '') + t for h, t in secs)
        return self.record(doc_id), text[:limit] + ('\n[…the article continues]' if len(text) > limit else '')
