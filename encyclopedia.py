"""Encyclopedia articles (ZIM files in library-data/collections) as citable passages for search.

Each ZIM file carries its own full-text index (Xapian, every word must match) and a title index, which find candidate
articles in milliseconds. They match words, not meaning ("bleeding from the leg" finds a museum), so with Recall running the
candidates are ranked by meaning: first each article's opening, then a few keyword-picked passages of the best two, and weak
ones are dropped. Each embedding costs graphics-card time (about 15 passages a second on an M1 Max), so a question embeds at
most about 26 short texts. Without Recall only articles whose title is mostly the question's own words are kept (matched
as whole, stemmed words by SQLite FTS5, not as letters inside other words), with their best-matching passage.

No vectors are kept on disk: the encyclopedias hold hundreds of thousands of articles, and only the few a question reaches are
ever embedded. Parsed articles and their vectors are cached in memory.
"""
import os, random, re, sqlite3, threading, urllib.parse
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
TITLE_SHARE = 0.5            # words only: at least this share of a title's words must be the question's (see _by_words)
TITLE_STOP = frozenset('a an and as at by for from in into of on or the to with'.split())
END_SECTIONS = re.compile(r'^(references|external links|see also|further reading|notes|sources|bibliography|citations|footnotes)$', re.I)


def match_expr(groups):
    """An FTS5 query for groups of words: each group's words OR-ed, the groups AND-ed, every word quoted."""
    def q(t):
        return '"' + t.replace('"', '""') + '"'
    return ' AND '.join('(' + ' OR '.join(q(t) for t in g) + ')' for g in groups)


class _Text(HTMLParser):
    """The readable text of a MediaWiki article, as (heading, text) sections; references, tables and navigation left out."""

    def __init__(self, lead_only=False):
        super().__init__(convert_charrefs=True)
        self.lead_only = lead_only  # stop at the first heading: the opening is all that ranking needs
        self.sections, self.heading, self.buf = [], '', []
        self.trails, self.trail, self.level = [], [], 0   # beside self.sections: each one's headings from the top down
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
            self.head, self.level = [], int(tag[1])
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
            self.trail = [(n, h) for n, h in self.trail if n < self.level] + [(self.level, self.heading)]
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
            self.trails.append(tuple(h for _, h in self.trail))
        self.buf = []

    def result(self):
        if not self.done:
            self._flush()
        return self.sections


def opening(text, most=320):
    """The first sentences of `text` that fit in `most` characters (written without a space after them in Chinese and
    Japanese); the first one cut at a word when even it does not fit."""
    text = ' '.join(text.split())
    if len(text) <= most:
        return text
    ends = [m.end() for m in re.finditer(r'[.!?](?=\s|$)|[\u3002\uff01\uff1f]', text[:most + 1])]
    return text[:ends[-1]] if ends else text[:most].rsplit(' ', 1)[0] + '\u2026'


def zim_names(lib):
    """The encyclopedia files library-data folder `lib` really holds. Its folder decides, so one rule covers every size of pack."""
    try:
        return sorted(p.name for p in (lib / 'collections').glob('*.zim'))
    except OSError:
        return []


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
            path = retrieval.IDX / 'encyclopedia-cache.sqlite'
            try:
                db = sqlite3.connect(path, check_same_thread=False)
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('SELECT count(*) FROM sqlite_master')
            except sqlite3.DatabaseError:       # a damaged cache is only a cache: set aside, made again
                db.close()
                os.replace(path, path.with_name(path.name + '.damaged'))
                db = sqlite3.connect(path, check_same_thread=False)
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

    # ---------------------------------------------------------------- shelf --
    def installed(self):
        """The window's Encyclopedias screen: [{'file', 'title', 'description', 'articles', 'main'}], one for each ZIM
        file in collections/. The folder decides, as it does for the /zim/ addresses the window opens, whether or not
        the catalog lists the file. `main` is the front page as a wiki path (see wiki_path), or None."""
        out = []
        for name in zim_names(self.lib):
            try:
                arc = self.archive(name)
            except Exception:  # one damaged file must not hide the others
                continue
            meta = lambda k: arc.get_metadata(k).decode('utf-8', 'replace') if k in arc.metadata_keys else ''
            main = self.wiki_path(name, follow(arc.main_entry).path) if arc.has_main_entry else None
            out.append({'file': name, 'title': meta('Title') or name.removesuffix('.zim'), 'description': meta('Description'),
                        'articles': arc.article_count, 'main': main})
        return out

    def random(self, name=None, tries=10):
        """A random article of encyclopedia `name` (any installed one when None): {'file', 'title', 'wiki', 'snippet'},
        or None. A random entry can be a redirect, a page with no text of its own (Appropedia's translations are
        mostly empty shells) or no article at all, so a few are drawn until one has an opening to show."""
        if name is None:
            names = zim_names(self.lib)
        elif os.path.basename(name) == name and name.endswith('.zim') and (self.lib / 'collections' / name).is_file():
            names = [name]
        else:
            return None
        for _ in range(tries if names else 0):
            pick = random.choice(names)
            try:
                entry = follow(self.archive(pick).get_random_entry())
            except Exception:
                continue
            a = self.article(pick, entry.path, lead_only=True)
            if a and a['sections']:
                return {'file': pick, 'title': a['title'], 'wiki': self.wiki_path(pick, a['path']), 'snippet': opening(a['sections'][0][1])}
        return None

    @staticmethod
    def wiki_path(name, path):
        """An article's address under /zim/, as the window's wiki screen takes it (and as record() gives it)."""
        return urllib.parse.quote(name) + '/' + urllib.parse.quote(path, safe='/')

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
                'path': 'zim/' + self.wiki_path(name, path),
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
            parser = _Text(lead_only)
            parser.feed(bytes(item.content).decode('utf-8', 'replace'))
            parser.close()
            sections = parser.result()
        except Exception:  # a missing or damaged entry costs this article only
            return None
        passages = [(h, c) for h, text in sections for c in retrieval.chunks_of(text)][:MAX_PASSAGES]
        art = {'title': entry.title, 'path': entry.path, 'sections': sections, 'trails': parser.trails, 'passages': passages}
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
                if k > len(hits):  # asked for more (a search of the encyclopedias alone): the next articles whose opening
                    took = {(n, a['path']) for _, n, a, _ in hits}   # clears the same bar, at no further embedding
                    lead_scores = vecs @ qv
                    for i in np.argsort(-lead_scores)[DEEP:]:
                        if lead_scores[i] < FLOOR:
                            break
                        n, a = leads[i][0], self.article(leads[i][0], leads[i][1][0])
                        if a and a['passages'] and (n, a['path']) not in took:
                            took.add((n, a['path']))
                            hits.append((float(lead_scores[i]), n, a, 0))
                hits.sort(key=lambda h: -h[0])
                return [self._hit(n, a, j, sem) for sem, n, a, j in hits[:k]]
        return self._by_words(words, groups, k)

    def _by_words(self, words, groups, k):
        """Without Recall. The encyclopedias' indexes return any article holding the words, so an article is kept only
        when its title is mostly the question's own words ("Burn" for "treat a burn", not "...treated straw as feed"),
        and its best passage holds at least two of the question's ideas when the question has two. Words are compared
        by SQLite FTS5 under its Porter stemmer: whole words, "burns" = "burn", never letters inside another word.
        Best first: the most of the question's ideas named in the title, then the largest share of the title, then the
        most ideas in one passage, then the encyclopedias' own order."""
        cands = []
        for name, path in self.candidates(words, groups)[:CANDIDATES]:
            try:   # the article a candidate leads to: "Water" may be another name for "Water quality", which is what is read
                real = follow(self.archive(name).get_entry_by_path(path)).path
            except Exception:
                continue
            if (name, real) not in cands:
                cands.append((name, real))
        if not cands:
            return []
        db = sqlite3.connect(':memory:')
        try:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
            anyidea = ' OR '.join('(' + match_expr([g]) + ')' for g in groups)

            def ideas(rowids):
                """{rowid: how many of the question's ideas that row holds}."""
                n = dict.fromkeys(rowids, 0)
                for g in groups:
                    for (i,) in db.execute('SELECT rowid FROM t WHERE t MATCH ?', (match_expr([g]),)):
                        n[i] += 1
                return n
            titles = [re.sub(r'[_/]+', ' ', path) for _, path in cands]
            db.executemany('INSERT INTO t(rowid, x) VALUES (?,?)', enumerate(titles))
            kept = []
            for i, marked in db.execute("SELECT rowid, highlight(t, 0, '\x01', '\x02') FROM t WHERE t MATCH ?", (anyidea,)).fetchall():
                named = sum(len(re.findall(r'\w+', span)) for span in re.findall('\x01(.*?)\x02', marked))
                total = len([w for w in re.findall(r'\w+', titles[i].lower()) if w not in TITLE_STOP]) or 1
                if named / total >= TITLE_SHARE:
                    kept.append((min(1.0, named / total), i))
            kept.sort(key=lambda x: x[1])
            named_ideas = ideas(range(len(titles)))
            db.execute('DELETE FROM t')
            hits, need = [], min(2, len(groups))
            for share, i in kept:
                a = self.article(*cands[i])
                if not a or not a['passages']:
                    continue
                db.executemany('INSERT INTO t(rowid, x) VALUES (?,?)', enumerate(f'{h}\n{x}' for h, x in a['passages']))
                n = ideas(range(len(a['passages'])))
                db.execute('DELETE FROM t')
                j = max(n, key=lambda r: (n[r], -r))   # the earliest passage with the most ideas
                if n[j] >= (1 if share >= 1 else need):   # a title that is nothing but the question's words needs no more
                    hits.append((named_ideas[i], share, n[j], cands[i][0], a, j))
        finally:
            db.close()
        hits.sort(key=lambda h: (-h[0], -h[1], -h[2]))   # stable: ties stay in the encyclopedias' own order
        return [self._hit(name, a, j, None) for _, _, _, name, a, j in hits[:k]]

    def _hit(self, name, art, j, sem):
        doc_id = self.doc_id(name, art['path'])
        heading, text = art['passages'][j]
        return {'doc_id': doc_id, 'record': self.record(doc_id), 'heading': heading, 'text': text, 'sem': sem}

    def headings(self, doc_id):
        """The headings of an article's sections, in order, sub-sections included; None if the article is unknown."""
        parts = self.split_id(doc_id)
        art = parts and self.article(*parts)
        return list(dict.fromkeys(h for trail in art['trails'] for h in trail if h)) if art else None

    def read(self, doc_id, section=None, limit=6000):
        """(record, text) of an article, or of the sections whose heading contains `section`, each with the sections
        under it; None if unknown. The caller asks matched() first: when no heading contains `section` the text is empty,
        never the whole article passed off as that section."""
        parts = self.split_id(doc_id)
        art = parts and self.article(*parts)
        if not art:
            return None
        secs = art['sections']
        if section:
            secs = [secs[i] for i in self.matched(doc_id, section)]
        text = '\n\n'.join((f'## {h}\n' if h else '') + t for h, t in secs)
        return self.record(doc_id), text[:limit] + ('\n[…the article continues]' if len(text) > limit else '')

    def matched(self, doc_id, section):
        """Indexes (into the article's sections) of the sections under a heading that contains `section`: that heading's
        own text and its sub-sections' ("Treatment" brings "First aid" and "Antivenom" below it). [] when no heading
        does; None if the article is unknown."""
        parts = self.split_id(doc_id)
        art = parts and self.article(*parts)
        if not art:
            return None
        want = section.lower()
        return [i for i, trail in enumerate(art['trails']) if any(want in h.lower() for h in trail)]
