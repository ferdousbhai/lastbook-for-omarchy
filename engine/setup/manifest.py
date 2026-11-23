"""What can be installed: manifest-v1.json, bundled with the app (packaging/arch/manifest-v1.json), or fetched once
from the download site when the person asks (Setup.check). It lists upstream Lastbook's packs only. A fetched one is used only if it parses, says version 1 and every address in it is
allowed (download.url_allowed)."""
import json
import re
import threading
from pathlib import Path

from . import download

REMOTE = 'https://downloads.lastbook.org/manifest-v1.json'
SHA = re.compile(r'^[0-9a-f]{64}$')
LIBRARY_HOSTS = frozenset({'downloads.lastbook.org'})   # packs come from Lastbook's own download site only


def _from(url, hosts, allowed):
    """An allowed https address, and in the product (allowed is None) on one of the hosts for this kind of file."""
    if not download.url_allowed(url, allowed):
        return False
    if allowed is not None:
        return True
    import urllib.parse
    return (urllib.parse.urlsplit(url).hostname or '').lower() in hosts
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$')


def bundled_paths():
    root = Path(__file__).resolve().parents[2]
    return [root / 'setup' / 'manifest-v1.json',                 # the installed package
            root / 'packaging' / 'arch' / 'manifest-v1.json']    # a checkout: the copy the package ships


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate(m, allowed=None):
    """The manifest, cleaned to the fields setup uses, or None if anything in it is wrong."""
    if not isinstance(m, dict) or m.get('version') != 1:
        return None
    packs = []
    for p in m.get('packs') or []:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or not NAME.match(p['id']):
            return None
        revision = p.get('revision', '')
        if not isinstance(revision, str) or (revision and not NAME.fullmatch(revision)):
            return None
        files = []
        for f in p.get('files') or []:
            if not (isinstance(f, dict) and isinstance(f.get('name'), str) and NAME.match(f['name']) and _int(f.get('size'))
                    and isinstance(f.get('sha256'), str) and SHA.match(f['sha256']) and _from(f.get('url'), LIBRARY_HOSTS, allowed)):
                return None
            files.append({'name': f['name'], 'url': f['url'], 'size': f['size'], 'sha256': f['sha256'], 'kind': f.get('kind') or 'zip'})
        available = bool(p.get('available')) and bool(files)
        if available and not _int(p.get('installed_bytes')):
            return None
        packs.append({'id': p['id'], 'title': str(p.get('title') or p['id']), 'summary': str(p.get('summary') or ''),
                      'records': p.get('records') if _int(p.get('records')) else None, 'revision': revision,
                      'installed_bytes': p.get('installed_bytes') if _int(p.get('installed_bytes')) else None,
                      'download_bytes': sum(f['size'] for f in files), 'available': available, 'files': files})
    return {'version': 1, 'published': str(m.get('published') or ''), 'packs': packs}


def load_bundled(paths=None):
    for p in paths or bundled_paths():
        try:
            m = validate(json.loads(Path(p).read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
        if m:
            return m
    return {'version': 1, 'published': '', 'packs': []}


def merge(bundled, fetched):
    """What the app was built with stays: for an id the bundled manifest pins (it has files),
    its addresses, sizes and SHA-256 are kept and the fetched copy may only change whether it is available. A fetched copy may
    add ids, and give the files of an id the app was built without (a pack published later). One bad publish never takes an
    option away."""
    built = {x['id']: x for x in bundled.get('packs', [])}
    merged = []
    for x in fetched.get('packs', []):
        b = built.pop(x['id'], None)
        pinned = b is not None and b.get('files')
        merged.append({**b, 'available': bool(x.get('available'))} if pinned else x)
    return {**fetched, 'packs': merged + list(built.values())}


class Manifest:
    """The bundled manifest at once; a fetched one is merged into it when it arrives (merge(), whatever its date)."""

    def __init__(self, bundled=None, remote=REMOTE, allowed=None, context=None):
        self.current = bundled or load_bundled()
        self.source = 'bundled'
        self.remote, self.allowed, self.context = remote, allowed, context
        self.fetching = None

    def fetch_now(self, wait=10):
        """Fetch (again, if an earlier try has ended) and wait up to `wait` seconds for the answer."""
        if not self.remote:
            return
        if not (self.fetching and self.fetching.is_alive()):
            self.fetching = threading.Thread(target=self._fetch, daemon=True, name='setup-manifest')
            self.fetching.start()
        self.fetching.join(wait)

    def _fetch(self):
        data = download.fetch_small(self.remote, allowed=self.allowed, context=self.context)
        if not data:
            return
        try:
            m = validate(json.loads(data.decode('utf-8')), self.allowed)
        except (ValueError, TypeError, KeyError, AttributeError):     # not JSON, or JSON in another shape: the bundled one stays
            m = None
        if m:      # merge() keeps every pinned entry, so a fetched copy can only add or mark available, whatever its date
            self.current, self.source = merge(self.current, m), 'fetched'

    def pack(self, pid):
        return next((p for p in self.current['packs'] if p['id'] == pid), None)

    def public(self):
        strip = lambda f: {k: v for k, v in f.items() if k != 'url'}
        return {'source': self.source, 'published': self.current['published'],
                'packs': [{**p, 'files': [strip(f) for f in p['files']]} for p in self.current['packs']]}
