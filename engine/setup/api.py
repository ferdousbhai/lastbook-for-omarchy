"""/api/setup/*: the engine side of first-run setup and of Library and downloads. Contract: docs/SETUP-API.md.

Two ways in, one module, both set up by app/engine_pipe.py (configure(...)) and reached only through the bridge:
  - setup mode (no library yet): a Bridge around SetupServer, whose Handler serves /api/setup/* here. Nothing else.
  - normal mode: server.py hands /api/setup/ to handle().
"""
import fcntl
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import download, hardware, here, install, maps
from .manifest import Manifest

DATA_NAME = install.DATA_NAME
WORK_SUFFIX = '.setup'
RECOMMEND_MARGIN = 5 * 10 ** 9     # beyond what the install needs, so the disk is not left full
RECOMMEND_MARGIN_MAX = 20 * 10 ** 9
MAX_BODY = 64 * 1024
WORLD_ESTIMATE = 650 * 10 ** 6     # the world map to city level (zoom 8) and place search, before it is measured
PLACES_ESTIMATE = 55 * 10 ** 6     # place search alone (a 14 MB download, a 40 MB file): for a library whose world map is here
ACTIVE = ('waiting', 'downloading', 'verifying', 'unpacking', 'activating')
ELSEWHERE = 'This download is running in another Lastbook window. Use that window to pause or cancel it.'
STAGED = 'What you downloaded is ready and waiting. Open the library first, then add to it.'


def _item(id, kind, title, total, state='waiting'):
    return {'id': id, 'kind': kind, 'title': title, 'state': state, 'bytes_done': 0, 'bytes_total': total, 'rate_bps': 0,
            'eta_s': None, 'error': None}


def _file_names(kind, entry):
    """The file names a pack is downloaded as (an osm item has none: it is made here)."""
    return [f['name'] for f in entry['files']] if entry and kind == 'pack' else []


def _now():
    # UTC, said so: a time zone that changes under a running engine (a laptop that travels) cannot make a job look hours long.
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _gb(n):
    return f'{n / 1e9:.0f} GB' if n >= 10e9 else f'{n / 1e9:.1f} GB'


def _staging(folder, files):
    """An unpack folder for exactly these files (their SHA-256s). What an earlier job left there for other files is removed
    first, since unpacking over it keeps any file that happens to have the right size; the same files carry on where
    they stopped."""
    key, mark = [f['sha256'] for f in files], folder.with_name(folder.name + '.json')
    if download.read_json(mark) != key:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise install.Refused('unpack', 'Lastbook could not clear what an earlier download left behind.')
        folder.parent.mkdir(parents=True, exist_ok=True)
        download.write_json(mark, key)


def _same_disk(a, b):
    try:
        return os.stat(hardware.existing(a)).st_dev == os.stat(hardware.existing(b)).st_dev
    except OSError:
        return False


class _Item:
    """Progress of one item (the pack, a map cut), with rate and time left."""

    def __init__(self, d):
        self.d = d
        self.t, self.at, self.rate = time.time(), d.get('bytes_done', 0), 0.0

    def progress(self, done, total, state=None):
        d = self.d
        if state and d['state'] != state:
            d['state'] = state
            self.t, self.at, self.rate = time.time(), done, 0.0
        d['bytes_done'], d['bytes_total'] = done, total
        now = time.time()
        if now - self.t >= 1.0:
            inst = max(done - self.at, 0) / (now - self.t)
            self.rate = inst if not self.rate else 0.6 * self.rate + 0.4 * inst
            self.t, self.at = now, done
            d['rate_bps'] = int(self.rate)
            d['eta_s'] = round((total - done) / self.rate) if self.rate > 0 else None


class Setup:
    def __init__(self, mode, settings_dir, default_parent, current=None, manifest=None, allowed=None, context=None,
                 on_finish=None, look_for_libraries=None, on_job=None):
        """mode: 'first-run' or 'manage'. current: the open library (manage). on_finish(folder): the app opens that library
        (it relaunches). look_for_libraries(): LastbookData folders found. on_job(state): called each time the job is saved,
        which is each time its state changes, so the window need not ask on a timer whether it has."""
        self.mode, self.current = mode, Path(current) if current else None
        self.settings_dir, self.default_parent = Path(settings_dir), Path(default_parent)
        self.manifest = manifest or Manifest(allowed=allowed, context=context)
        self.allowed, self.context = allowed, context
        self.on_finish, self.look_for_libraries, self.on_job = on_finish, look_for_libraries, on_job
        self.lock = threading.RLock()
        self.job, self.control, self.thread = None, None, None
        self._held = None          # the open job.lock while this process runs the job (_take)
        self.sources = {}          # file name -> a file already on this computer the person chose with `use`
        self.rejected = {}         # path -> (size, mtime) of a file already here that failed its SHA-256: not offered or used again
        self.job = self._load_job()

    # ------------------------------------------------------------------------------------------------- saved state --
    def _settings(self):
        return download.read_json(self.settings_dir / 'setup.json', {})

    def _save_settings(self, d):
        try:
            self.settings_dir.mkdir(parents=True, exist_ok=True)
            download.write_json(self.settings_dir / 'setup.json', d)
        except OSError as e:
            print('setup: could not save setup.json:', e, flush=True)

    def _load_job(self):
        """The saved job, from the app's own settings folder only, with its plan rebuilt from the manifest and the work folder
        recomputed. Anything that does not check out is no job: it never stops the app from starting."""
        try:
            job = self._checked_job(json.loads(_job_file(self.settings_dir).read_text(encoding='utf-8')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            if not isinstance(e, FileNotFoundError):
                print('setup: the saved job was not used:', type(e).__name__, flush=True)
            return None
        if not job:
            return None
        if job.get('state') in ACTIVE:      # this program stopped while it ran: carry on only when the person says so
            job['state'] = 'paused'
            for it in job['items']:
                if it['state'] in ACTIVE:
                    was = it['state']
                    it['state'] = 'paused'
                    if was != 'waiting':
                        it['paused_from'] = was       # what it was doing when this program stopped; dropped on resume
                    it['bytes_done'], it['bytes_total'] = self._done_after_restart(job['plan'], it, was)
                it['rate_bps'], it['eta_s'] = 0, None
        return job

    def _checked_job(self, job):
        """A job as this program would have written it, or None. Nothing from disk is used as a path or a file name unless it is
        what the plan's ids and the parent folder give: the pack comes from the manifest; work and target are worked
        out again; no `verified` list is kept (files are hashed again); a source must be a file of exactly the expected name."""
        plan, items = job['plan'], job['items']
        if not isinstance(plan, dict) or not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            return None
        if job.get('state') not in ACTIVE + ('paused', 'done', 'failed'):
            return None
        if plan.get('use_library'):
            lib = Path(plan['use_library'])
            if not install.is_library(lib):
                return None
            job['plan'] = {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}
            return job
        parent, target = Path(plan['parent']), Path(plan['target'])
        if target.parent != parent or (target.name != DATA_NAME and target != self.current):
            return None          # a library setup installs is called LastbookData; only the open one may have another name
        work = _work_for(target)
        ids = lambda key: (plan.get(key) or {}).get('id')
        pack = self.manifest.pack(ids('pack')) if plan.get('pack') else None
        if plan.get('pack') and not pack:
            return None
        wanted = {('pack', pack['id'])} if pack else set()      # a job of an earlier version's state map or address index is no job
        osm = [x for x in (plan.get('osm') or []) if x in ('world', 'places') or (isinstance(x, str) and maps.region(x))]
        wanted |= {('osm', x) for x in osm}
        if {(i.get('kind'), i.get('id')) for i in items} != wanted:
            return None
        names = _file_names('pack', pack)
        sources = {n: str(v) for n, v in (plan.get('sources') or {}).items()
                   if n in names and isinstance(v, str) and Path(v).name == n and Path(v).is_file()}
        switch = bool(plan.get('switch')) and pack is not None
        clean = {'pack': pack, 'parent': str(parent), 'target': str(target), 'work': str(work),
                 'sources': sources, 'explicit': bool(plan.get('explicit')), 'retried': {}, 'switch': switch, 'repair': bool(plan.get('repair')),
                 'osm': osm}
        if isinstance(plan.get('damaged'), list):
            clean['damaged'] = [str(x) for x in plan['damaged']]
        job['plan'] = clean
        return job

    def _done_after_restart(self, plan, it, was):
        """(bytes done, bytes total) for an item found mid-run at startup, from what is on disk for the step it was in."""
        if it['kind'] == 'osm':
            return 0, it['bytes_total']        # a map cut carries on from its part file when it runs (maps.Cut.run); until then nothing is claimed
        if was == 'unpacking' and plan.get('pack'):
            total = plan['pack']['installed_bytes']
            return min(install._tree_bytes(Path(plan['work']) / 'unpack'), total), total
        if it.get('source') == 'found':
            return it['bytes_total'], it['bytes_total']     # nothing to download: the file is already on this computer
        return self._on_disk(plan, it), it['bytes_total']

    @staticmethod
    def _on_disk(plan, it):
        """Bytes of this item already downloaded, from the files themselves."""
        n = 0
        for name in _file_names(it['kind'], plan.get(it['kind'])):
            for p in (Path(plan['work']) / 'downloads' / name, Path(plan['work']) / 'downloads' / (name + '.part')):
                try:
                    n += p.stat().st_size
                except OSError:
                    pass
        return n

    def _save_job(self):
        """To the app's own settings folder. A save that fails stops the job (raises OSError)."""
        job = self.job
        if not job:
            return
        f = _job_file(self.settings_dir)
        f.parent.mkdir(parents=True, exist_ok=True)
        download.write_json(f, job)
        if self.on_job:
            try:
                self.on_job(job['state'])
            except Exception:      # a window that cannot be told never stops the job
                traceback.print_exc()

    # --------------------------------------------------------------------------------------------------- the state --
    def current_info(self):
        if not self.current:
            return None
        try:
            metadata = json.loads((self.current / 'pack.json').read_text(encoding='utf-8'))
            pack = metadata.get('name')
            revision = metadata.get('content_revision', '')
            if not isinstance(revision, str):
                revision = ''
        except (OSError, ValueError, AttributeError):
            pack, revision = None, ''
        return {'folder': str(self.current), 'pack': pack, 'content_revision': revision,
                'osm': maps.installed(self.current / 'derived' / 'geo')}

    def found(self):
        """Files in app-owned storage, the selected library, or explicitly chosen by the person. A file is offered when its size matches; its
        SHA-256 is checked when it is used."""
        out, seen = [], set()
        if self.look_for_libraries:
            try:
                for lib in self.look_for_libraries():
                    lib = Path(lib)
                    if str(lib) not in seen and lib != self.current and install.is_library(lib):
                        seen.add(str(lib))
                        out.append({'kind': 'library', 'path': str(lib), 'name': lib.name})
            except Exception:
                traceback.print_exc()
        files = {f['name']: (p['id'], f['size']) for p in self.manifest.current['packs'] for f in p['files']}     # name -> (pack, size)
        places = [self.default_parent]
        if self.current:
            places.append(self.current)
        candidates = [Path(p) for p in self.sources.values()]
        for place in places:
            try:
                candidates.extend(c for c in place.iterdir() if c.name in files)
            except OSError:
                continue
        for c in candidates:
            key = str(c.resolve())
            if key in seen or not c.is_file():
                continue
            seen.add(key)
            pid, size = files.get(c.name, (None, None))
            st = c.stat()
            rejected = self.rejected.get(str(c)) == (st.st_size, st.st_mtime_ns)
            out.append({'kind': 'pack', 'path': str(c), 'name': c.name, 'id': pid, 'size': st.st_size,
                        'size_ok': size == st.st_size and not rejected, 'rejected': rejected})
        return out

    def hardware(self):
        extra = [self.default_parent] + ([self.current.parent] if self.current else [])
        return {'disks': hardware.candidate_disks(extra)}

    def recommend(self, hw):
        available = [p for p in self.manifest.current['packs'] if p['available']]
        small = self.manifest.pack('small')
        parent = self.current.parent if self.current else self.default_parent
        free, total = hardware.disk_of(parent)
        need = {p['id']: install.need_bytes(p) for p in available}
        if not small or not small['available']:
            return {'pack': None, 'folder': str(parent), 'need_bytes': need, 'why': 'No library is ready to download yet.'}
        margin = _margin(total)
        folder, elsewhere = parent, None
        if free < need['small'] + margin:   # the disk that holds the home folder (or the open library) whenever Small fits there; only then another
            elsewhere = next((d for d in hw['disks'] if not _same_disk(d['path'], parent)
                              and d['free_bytes'] >= need['small'] + _margin(d['total_bytes'])), None)
            if not elsewhere:
                return {'pack': None, 'folder': str(parent), 'need_bytes': need,
                        'why': f'The smallest library, {small["title"]}, needs {_gb(need["small"])} free, and this computer has {_gb(free)} free.'}
            folder = Path(elsewhere['path'])
        if elsewhere:
            name = Path(elsewhere['path']).name or elsewhere['path']
            why = (f'This computer’s own disk has {_gb(free)} free, not enough for any library, so {small["title"]} goes on {name}. '
                   'Keep that disk connected: Lastbook reads the library from it.')
        else:
            why = 'Small is selected to get started. You can choose a larger library.'
        return {'pack': 'small', 'folder': str(folder), 'need_bytes': need, 'why': why}

    def check(self):
        """The person asked whether newer libraries have been published: the one GET of the manifest, waited for briefly.
        Never made unasked: opening setup (state) reaches nothing."""
        self.manifest.fetch_now()
        return self.state()

    def state(self):
        hw, current = self.hardware(), self.current_info()
        # A library made before place search held villages has the world map but not the current place search
        # (maps.places_current): ticking "World map and place search" then makes only the place search again.
        world = WORLD_ESTIMATE if not (current and current['osm']['world']) else PLACES_ESTIMATE
        return {'mode': self.mode, 'hardware': hw, 'manifest': self.manifest.public(), 'current': current,
                'found': self.found(), 'recommend': self.recommend(hw), 'job': self.public_job(),
                'map_regions': maps.regions(), 'world_bytes': world, 'here': self.here(), 'map_offer': here.asking(self.settings_dir)}

    def here(self):
        """The street-level map to offer for where this computer seems to be (here.offer), or None. Never stops setup."""
        try:
            return here.offer(self.settings_dir, self.current / 'derived' / 'geo' if self.current else None)
        except Exception as e:
            print('setup: no map offer:', type(e).__name__, flush=True)
            return None

    def towns(self):
        """Whether the open library has place search, so the window can ask for a town and find it."""
        return bool(self.current and maps.places_current(self.current / 'derived' / 'geo' / maps.PLACES_FILE))

    def set_place(self, name, lat, lon, detail=''):
        """The town the person says they are in, kept for the time zone the computer has now (here.own_place)."""
        zone = (here.zone_place() or {}).get('zone')
        try:
            place = {'name': str(name)[:80], 'detail': str(detail or '')[:120], 'lat': float(lat), 'lon': float(lon)}
        except (TypeError, ValueError):
            place = None
        if not place or not zone:
            return {'error': 'That is not a place.', 'code': 'bad_place'}
        with _SETTINGS:
            s = self._settings()
            places = s.get('places') if isinstance(s.get('places'), dict) else {}
            s['places'] = {**places, zone: place}
            self._save_settings(s)
        return {'map_offer': here.asking(self.settings_dir), 'here': self.here(), 'towns': self.towns()}

    def set_offer(self, ask):
        """The person's switch: whether Lastbook offers the map of where the computer is (the window and the commands)."""
        with _SETTINGS:
            s = self._settings()
            s['map_offer'] = bool(ask)
            self._save_settings(s)
        return {'map_offer': bool(ask), 'here': self.here()}

    def public_job(self):
        with self.lock:
            if self._elsewhere():      # another window runs it: show its progress as that window saves it, and offer nothing
                try:
                    job = self._checked_job(json.loads(_job_file(self.settings_dir).read_text(encoding='utf-8')))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    job = None
                if job:
                    return {**{k: v for k, v in job.items() if k != 'plan'}, 'elsewhere': True}
            if not self.job:
                return None
            return {k: v for k, v in json.loads(json.dumps(self.job)).items() if k != 'plan'}

    # ------------------------------------------------------------------------------------------- one job, one process --
    def _take(self):
        """The job's lock, held by the one engine process whose thread is running the job. Every window has an engine of
        its own over the same settings folder and work folder; without this a second one would show the first one's
        download as paused, and its Cancel or Resume would delete or double what the first is writing. False when another
        process holds it."""
        if self._held:
            return True
        f = None
        try:
            lock = _job_file(self.settings_dir).with_name('job.lock')
            lock.parent.mkdir(parents=True, exist_ok=True)
            f = open(lock, 'w')
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if f:
                f.close()
            return False
        self._held = f
        return True

    def _release(self):
        f, self._held = self._held, None
        if f:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

    def _elsewhere(self):
        """Whether another engine process is running the job right now."""
        if self._held:
            return False
        if self._take():
            self._release()
            return False
        return True

    def _busy_elsewhere(self):
        return {'error': ELSEWHERE, 'code': 'busy'} if self._elsewhere() else None

    # -------------------------------------------------------------------------------------------------- the actions --
    def _parent_for(self, folder):
        if folder:
            p = Path(folder).expanduser()
            return p.parent if p.name == DATA_NAME else p
        return self.current.parent if self.current else self.default_parent

    def start(self, pack_id=None, folder=None, sources=None, explicit=False, repair=False, world=False, regions=()):
        if self._busy_elsewhere():
            return self._busy_elsewhere()
        if repair:
            return self._start_repair()
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            if self._staged():
                return {'error': STAGED, 'code': 'busy'}
            pack = self.manifest.pack(pack_id) if pack_id else None
            if pack_id and (not pack or not pack['available']):
                return {'error': 'That library is not available yet.', 'code': 'unavailable'}
            cur = self.current_info() or {}
            if folder and self.current and install._real(Path(folder).expanduser()) == install._real(self.current):
                folder = None      # the window sends the shown folder, which in manage mode is the open library itself
            same_revision = pack and (not pack.get('revision') or pack['revision'] == cur.get('content_revision', ''))
            if pack and self.mode == 'manage' and cur.get('pack') == pack['id'] and same_revision and not folder:
                pack = None        # the same library again: only a map is wanted
            parent = self._parent_for(folder)
            target = self.current if self.current and not folder else parent / DATA_NAME   # the open library as it is, whatever its name
            parent = target.parent
            have_osm = maps.installed(Path(target) / 'derived' / 'geo') if target == self.current or not pack else {'world': False, 'places': False, 'regions': []}
            osm = []
            if world or regions:       # street-level regions draw over the world map, so a region brings the world with it
                osm += [x for x in ('world', 'places') if not have_osm[x]]
            for rid in dict.fromkeys(regions or ()):
                if not isinstance(rid, str) or not maps.region(rid):
                    return {'error': 'Lastbook does not know that map region.', 'code': 'unavailable'}
                if rid not in {r['id'] for r in have_osm['regions']}:
                    osm.append(rid)
            if not pack and not osm:
                if self.mode == 'first-run':
                    return {'error': 'Choose a library first.', 'code': 'nothing'}
                return {'error': 'Nothing to add: this library is already here.', 'code': 'nothing'}
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {'error': f'Lastbook cannot make a folder there ({type(e).__name__}).', 'code': 'folder'}
            if not os.access(parent, os.W_OK):
                return {'error': 'Lastbook cannot save in that folder. Choose another.', 'code': 'folder'}
            if self.mode == 'first-run' and target.exists() and not install.is_library(target):
                if any(target.iterdir()):
                    return {'error': f'There is already a folder called {DATA_NAME} there that is not a library. Choose another place.', 'code': 'folder'}
            work = _work_for(target)
            srcs = dict(self.sources)
            srcs.update(sources or {})
            for f in self.found():
                if f.get('size_ok') and f['name'] not in srcs and f['kind'] == 'pack':
                    srcs[f['name']] = f['path']
            names = _file_names('pack', pack)
            have = set(srcs)
            need = install.need_bytes(pack, have) + (int(WORLD_ESTIMATE * maps.DISK_SLACK) + 512 * 2 ** 20 if 'world' in osm else 0)   # as the cut itself will ask (_do_osm)
            for name in names:     # what an earlier run already downloaded is on the disk already
                if name in have:
                    continue
                for done in (work / 'downloads' / (name + '.part'), work / 'downloads' / name):
                    try:
                        need -= os.path.getsize(done)
                    except OSError:
                        pass
            free, _ = hardware.disk_of(parent)
            if free < need:
                return {'error': f'This needs {_gb(need)} free on that disk, and it has {_gb(free)}. Free some space or choose another disk.',
                        'code': 'no_space', 'need_bytes': need, 'free_bytes': free}
            items = []
            if pack:
                items.append(_item(pack['id'], 'pack', pack['title'], pack['download_bytes']))
            for x in osm:          # sizes are known once each cut is measured, when its turn comes
                title = {'world': 'World map', 'places': 'Place search'}.get(x) or maps.region(x)['name'] + ' streets'
                items.append(_item(x, 'osm', title, WORLD_ESTIMATE if x == 'world' else 0))
            return self._begin(items, target, work, {
                'pack': pack, 'parent': str(parent), 'target': str(target), 'work': str(work),
                'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': explicit, 'retried': {}, 'switch': bool(pack), 'osm': osm})

    def _begin(self, items, target, work, plan, **extra):
        """The new job, saved and started (the caller holds the lock)."""
        self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'items': items, 'error': None, 'started': _now(), 'folder': str(target),
                    **extra, 'plan': plan}
        with _SETTINGS:
            s = self._settings()
            s['work'] = str(work)
            self._save_settings(s)
        _tag_sources(self.job)
        self._save_job()
        self._run()
        return self.public_job()

    def _start_repair(self):
        """Re-check the open library's files (pack-manifest.tsv). If any is wrong the pack is downloaded and switched to as
        in an upgrade. Nothing is downloaded if all is well."""
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            cur = self.current_info()
            if self.mode != 'manage' or not cur:
                return {'error': 'There is no library to repair yet.', 'code': 'nothing'}
            pack = self.manifest.pack(cur['pack']) if cur['pack'] else None
            if not pack or not pack['available']:
                return {'error': 'Lastbook cannot tell which library this is, so it cannot repair it. Download a library instead.', 'code': 'unavailable'}
            parent, target = self.current.parent, self.current
            work = _work_for(target)
            srcs = {f['name']: f['path'] for f in self.found() if f.get('size_ok') and f['kind'] == 'pack'}
            items = [_item(pack['id'], 'pack', pack['title'], pack['installed_bytes'])]
            names = _file_names('pack', pack)
            return self._begin(items, target, work, {
                'pack': pack, 'parent': str(parent), 'target': str(target), 'work': str(work),
                'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': False, 'retried': {}, 'switch': True, 'repair': True},
                repair=True)

    def _check_library(self, it, lib, control):
        """Files of the open library that are missing or differ from its pack-manifest.tsv (path, bytes, sha256). Upstream's
        US-only map data (install.DROPPED) is meant to be absent: it is not looked for, so its absence never fetches the pack."""
        try:
            rows = [l.split('\t') for l in (lib / 'pack-manifest.tsv').read_text(encoding='utf-8').splitlines()[1:] if l.strip()]
            rows = [(r[0], int(r[1]), r[2]) for r in rows if not install.dropped(r[0])]
        except (OSError, ValueError, IndexError):
            return ['pack-manifest.tsv']
        total = sum(r[1] for r in rows) or 1
        tracker, done, bad = _Item(it), 0, []
        self._set(it, 'verifying', bytes_done=0, bytes_total=total)
        for rel, size, digest in rows:
            p = lib / rel
            if '..' in Path(rel).parts or Path(rel).is_absolute() or not p.is_file() or p.stat().st_size != size:
                bad.append(rel)
                done += size
                tracker.progress(done, total, 'verifying')
                continue
            base = done
            if download._sha256(p, control, lambda d: tracker.progress(base + d, total, 'verifying')) != digest:
                bad.append(rel)
            done = base + size
        return bad

    def _run(self):
        self._take()
        self.control = download.Control()
        self.thread = threading.Thread(target=self._owned_work, args=(self.control,), daemon=True, name='setup-job')
        self.thread.control = self.control
        self.thread.start()

    def _live(self):
        """For the job's own thread, before it changes the job (the caller holds the lock): Stopped if its run was cancelled
        or another run has begun. Cancel waits 30 s for the thread; one that outlives that changes nothing afterwards."""
        c = getattr(threading.current_thread(), 'control', None)
        if c is not None and (c.why == 'cancelled' or c is not self.control):
            raise download.Stopped('cancelled')

    def _staged(self):
        """Whether a finished job's library is waiting for the restart that switches to it (the caller holds the
        lock). A new job would share its work folder, and cancelling that job would take the waiting download with it."""
        if not self.job or self.job['state'] != 'done':
            return False
        try:
            d = json.loads(_pending_file(self.settings_dir).read_text(encoding='utf-8'))
            return not d.get('switched') and os.path.exists(d['new'])     # still to be made, and still there
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def _unstage(self):
        """A switch staged by an earlier job is off: its staging folder is about to be changed."""
        try:
            _pending_file(self.settings_dir).unlink()
        except OSError:
            pass

    def pause(self):
        if self._busy_elsewhere():
            return self._busy_elsewhere()
        with self.lock:
            if self.job and self.job['state'] in ACTIVE and self.control:
                self.control.stop('paused')
        self._join()
        return self.public_job()

    def resume(self):
        if self._busy_elsewhere():
            return self._busy_elsewhere()
        with self.lock:
            if self.job and self.job['state'] == 'paused':
                self.job['state'] = 'waiting'
                for it in self.job['items']:
                    it.pop('paused_from', None)
                self._save_job()
                self._run()
        return self.public_job()

    def retry(self):
        if self._busy_elsewhere():
            return self._busy_elsewhere()
        with self.lock:
            if self.job and self.job['state'] == 'failed':
                self.job['state'], self.job['error'] = 'waiting', None
                self.job['plan']['retried'] = {}
                for it in self.job['items']:
                    if it['state'] != 'done':
                        it['state'], it['error'] = 'waiting', None
                self._save_job()
                self._run()
        return self.public_job()

    def cancel(self):
        if self._busy_elsewhere():
            return self._busy_elsewhere()
        with self.lock:
            job = self.job
            if not job:
                return None
            if job['state'] in ACTIVE and self.control:
                self.control.stop('cancelled')
        self._join()
        with self.lock:
            if job['state'] != 'done' or job['plan'].get('switch'):     # a staged library not yet switched to can still be dropped
                self._clean(job)
                try:
                    _pending_file(self.settings_dir).unlink()
                except OSError:
                    pass
                job['state'] = 'failed'
                job['error'] = {'code': 'cancelled', 'message': 'Cancelled. Nothing was changed.'}
                for it in job['items']:
                    if it['state'] != 'done':
                        it['state'] = 'failed'
                self._save_job()
        return self.public_job()

    def _join(self):
        t = self.thread
        if t and t is not threading.current_thread():
            t.join(30)

    def _clean(self, job):
        """What the job made in its work folder goes, and the half-made map a paused or failed cut keeps beside the
        library's maps (maps.Cut.run); the person's own files and any library stay."""
        work = Path(job['plan']['work'])
        for sub in ('downloads', 'unpack'):
            shutil.rmtree(work / sub, ignore_errors=True)
        if job['plan'].get('osm') and not job['plan'].get('switch'):     # cut straight into the open library
            try:
                maps.sweep(Path(self.current or job['plan']['target']) / 'derived' / 'geo')
            except OSError as e:
                print('setup: a half-made map was not removed:', e, flush=True)

    def use(self, path):
        if self._busy_elsewhere():
            return self._busy_elsewhere()
        p = Path(path or '').expanduser()
        if not str(path or '').strip() or not p.exists():
            return {'error': 'Lastbook cannot find that file or folder.', 'code': 'missing'}
        if p.is_dir():
            lib = p if install.is_library(p) else p / DATA_NAME
            if not install.is_library(lib):
                known = {f['name'] for pk in self.manifest.current['packs'] for f in pk['files']}
                inside = sorted(c for c in p.iterdir() if c.name in known and c.is_file()) if p.is_dir() else []
                if inside:          # the person explicitly chose this folder
                    return self.use(str(inside[0]))
                return {'error': f'“{p.name}” is not a Lastbook library. Choose the folder called {DATA_NAME}.', 'code': 'not_library'}
            if not os.access(lib, os.W_OK):
                return {'error': f'Lastbook cannot save in “{lib.name}”. Move it somewhere you can save to, then choose it there.', 'code': 'folder'}
            with self.lock:
                if self.job and self.job['state'] in ACTIVE:
                    return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
                self.job = {'id': uuid.uuid4().hex[:12], 'state': 'done', 'error': None, 'started': _now(), 'folder': str(lib),
                            'items': [_item('library', 'pack', lib.name, 0, state='done')],
                            'plan': {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}}
                return self.public_job()
        name = p.name
        known = {f['name']: (entry, f['size']) for entry in self.manifest.current['packs'] for f in entry['files']}
        if name not in known:
            return {'error': 'That is not a file Lastbook knows. Choose a Lastbook library zip.', 'code': 'unknown'}
        entry, size = known[name]
        if p.stat().st_size != size:
            return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(size)} of the published {name}. The download may not have finished.', 'code': 'size'}
        if not entry['available']:
            return {'error': 'That library is not available in this version.', 'code': 'unavailable'}
        self.sources[name] = str(p)
        return self.start(entry['id'], sources={name: str(p)}, explicit=True)

    def finish(self):
        with self.lock:
            job = self.job
            if not job or job['state'] != 'done':
                return {'error': 'Setup has not finished yet.', 'code': 'not_done'}
            plan = job['plan']
            folder = plan.get('use_library') or plan['target']
        if not self.on_finish:
            return {'ok': True, 'restart': True, 'folder': folder}
        threading.Timer(0.4, self.on_finish, [folder]).start()     # after this answer has reached the window
        return {'ok': True, 'folder': folder}

    # ------------------------------------------------------------------------------------------------------ the work --
    def _set(self, it, state, **kw):
        with self.lock:
            self._live()
            if it is not None:
                it['state'] = state
                it.update(kw)
            self.job['state'] = state
            self._save_job()

    def _fail(self, it, code, message, **extra):
        with self.lock:
            try:
                self._live()
            except download.Stopped:
                return      # cancelled meanwhile: the job already says so
            err = {'code': code, 'message': message, **extra}
            if it is not None:
                it['state'], it['error'], it['rate_bps'], it['eta_s'] = 'failed', err, 0, None
            self.job['state'], self.job['error'] = 'failed', err
            self._save_quietly()
        print(f'setup: failed ({code}): {message}', flush=True)

    def _save_quietly(self):
        try:
            self._save_job()
        except OSError as e:
            print('setup: could not save the job:', e, flush=True)

    def _owned_work(self, control):
        try:
            self._work(control)
        finally:
            with self.lock:
                if threading.current_thread() is self.thread:      # not when a later run has begun: the lock is then its
                    self._release()

    def _work(self, control):
        job = self.job
        plan = job['plan']
        it = None
        try:
            work = Path(plan['work'])
            (work / 'downloads').mkdir(parents=True, exist_ok=True)
            for it in job['items']:
                if it['state'] == 'done':
                    continue
                do = {'pack': self._do_pack, 'osm': self._do_osm}
                do[it['kind']](it, plan, work, control)
                with self.lock:
                    self._live()
                    it['state'], it['rate_bps'], it['eta_s'] = 'done', 0, None
                    self._save_job()
            it = None
            self._finish_job(plan, work)
        except download.Stopped as s:
            with self.lock:
                if s.why == 'paused' and control is self.control and control.why != 'cancelled':
                    if it is not None:
                        it['state'], it['rate_bps'], it['eta_s'] = 'paused', 0, None
                    job['state'] = 'paused'
                self._save_quietly()
        except download.Failed as e:
            self._fail(it, e.code, e.message, **e.extra)
        except install.Refused as e:
            self._fail(it, e.code, e.message)
        except OSError as e:
            if download.disk_full(e):
                self._fail(it, 'no_space', 'The disk filled up. Free some space, then try again.')
            else:
                traceback.print_exc()
                self._fail(it, 'unpack' if it and job['state'] == 'unpacking' else 'disk', f'Lastbook could not write a file ({type(e).__name__}: {e.strerror or e}).')
        except Exception as e:
            traceback.print_exc()
            self._fail(it, 'activate', f'Setup stopped on an internal error ({type(e).__name__}).')

    def _get_file(self, it, f, plan, work, control, offset):
        """The file, checked against its SHA-256: one already on this computer, or a download. Returns its path."""
        tracker = _Item(it)
        name, src = f['name'], plan['sources'].get(f['name'])
        if src and os.path.isfile(src) and name in plan.setdefault('verified', []):
            return src, False
        explicit_use = bool(plan.get('explicit'))
        if src and os.path.isfile(src):
            self._set(it, 'verifying')
            digest = download._sha256(src, control, lambda d: tracker.progress(offset + d, it['bytes_total'], 'verifying'))
            if digest == f['sha256']:
                plan['verified'].append(name)
                return src, False
            st = os.stat(src)
            self.rejected[src] = (st.st_size, st.st_mtime_ns)
            plan['sources'].pop(name, None)       # a retry downloads it instead; the file itself is left where it is, untouched
            it['source'] = 'download'
            plan['explicit'] = False
            if explicit_use:
                folder = Path(src).parent.name or str(Path(src).parent)
                raise download.Failed('found_mismatch', f'The {name} in {folder} doesn’t match the published file, so it was not used. '
                                                        'It was left as it is. Download it instead?', file=src)
            print(f'setup: {src} does not match; downloading instead', flush=True)
        dest = work / 'downloads' / name
        if not dest.exists():       # room for what is still to come (the space check at start counted a file already here as had)
            try:
                still = f['size'] - os.path.getsize(str(dest) + '.part')
            except OSError:
                still = f['size']
            free, _ = hardware.disk_of(work)
            if free < still + 512 * 2 ** 20:
                raise download.Failed('no_space', f'Downloading {name} needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.',
                                      need_bytes=still, free_bytes=free)
        while True:
            self._set(it, 'downloading')
            d = download.Download(f['url'], dest, f['size'], f['sha256'], control,
                                  lambda done, total, phase: tracker.progress(offset + done, it['bytes_total'], phase),
                                  allowed=self.allowed, context=self.context)
            try:
                return d.run(), True
            except download.BadChecksum:
                tries = plan['retried'].get(name, 0)
                if tries >= 1:
                    raise download.Failed('checksum', f'{name} did not match its published checksum twice. The file on the server may be damaged; try again later.')
                plan['retried'][name] = tries + 1
                print(f'setup: {name} failed its checksum; downloading it once more', flush=True)

    def _get_files(self, it, files, plan, work, control):
        """Every file of one item, each counted after the ones before it: [(path, ours)], ours when it is a download to delete."""
        got, offset = [], 0
        for f in files:
            got.append(self._get_file(it, f, plan, work, control, offset))
            offset += f['size']
        return got

    def _do_pack(self, it, plan, work, control):
        pack = plan['pack']
        if plan.get('repair') and 'damaged' not in plan:
            bad = self._check_library(it, Path(plan['target']), control)
            plan['damaged'] = bad
            print(f'setup: repair found {len(bad)} damaged or missing files', flush=True)
            if not bad:
                plan['switch'] = False
                return
            need = install.need_bytes(pack, set(plan['sources']))
            free, _ = hardware.disk_of(work.parent)
            if free < need:
                raise download.Failed('no_space', f'{len(bad)} files need fetching again. That needs {_gb(need)} free, and the disk has {_gb(free)}.')
            with self.lock:
                it['bytes_total'], it['bytes_done'] = pack['download_bytes'], 0
        staged = work / 'unpack'
        got = self._get_files(it, pack['files'], plan, work, control)
        tracker = _Item(it)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=pack['installed_bytes'])
        self._unstage()
        _staging(staged, pack['files'])
        # Every member checked before any is written; what is DROPPED is neither written nor counted.
        total = sum(install.check_zip(path, pack['installed_bytes'])[1] for path, _ in got)
        still = max(total - install._tree_bytes(staged), 0)
        free, _ = hardware.disk_of(work)
        if free < still + 512 * 2 ** 20:
            raise download.Failed('no_space', f'Unpacking needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.')
        for path, _ in got:
            lib = install.unpack(path, staged, pack['installed_bytes'], control, lambda d, t: tracker.progress(d, t, 'unpacking'))
        if pack.get('revision'):
            metadata = json.loads((lib / 'pack.json').read_text(encoding='utf-8'))
            if metadata.get('content_revision') != pack['revision']:
                raise install.Refused('unpack', 'The library content version does not match this download.')
        download.write_json(lib / 'setup-pack.json', {'pack': pack['id'], 'installed': _now(), 'files': [f['name'] for f in pack['files']]})
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _do_osm(self, it, plan, work, control):
        """The world map, place search, or one street-level region, made here from OpenStreetMap (engine/setup/maps.py).
        Into the staged library when one is being installed, else straight into the open one: the map picks it up when shown."""
        lib = (work / 'unpack' / DATA_NAME) if plan['switch'] else Path(self.current or plan['target'])
        geo = lib / 'derived' / 'geo'
        geo.mkdir(parents=True, exist_ok=True)
        tracker = _Item(it)
        self._set(it, 'downloading', bytes_done=0)
        if it['id'] == 'places':
            maps.build_places(geo / maps.PLACES_FILE, control, allowed=self.allowed, context=self.context)
            with self.lock:
                it['bytes_total'] = it['bytes_done'] = (geo / maps.PLACES_FILE).stat().st_size
            return
        if it['id'] == 'world':
            cut = maps.Cut(geo / maps.WORLD_FILE, None, 0, maps.WORLD_MAXZOOM, 'World', control, self.allowed, self.context)
        else:
            r = maps.region(it['id'])
            cut = maps.Cut(geo / f'regions-{maps.file_id(r["id"])}.mbtiles', maps.region_boxes(r), maps.REGION_MINZOOM, maps.REGION_MAXZOOM,
                           r['name'], control, self.allowed, self.context)
        total = cut.measure()
        free, _ = hardware.disk_of(geo)
        # The file, one row a tile: more than the bytes fetched. Less what a part file kept from an earlier run already holds.
        need = max(int(cut.disk * maps.DISK_SLACK) + 512 * 2 ** 20 - cut.kept_bytes(), 0)
        if free < need:
            raise download.Failed('no_space', f'{it["title"]} needs {_gb(need)} free, and the disk has {_gb(free)}. Free some space, then try again.',
                                  need_bytes=need, free_bytes=free)
        with self.lock:
            it['bytes_total'] = total
        cut.run(lambda done, total: tracker.progress(done, total))

    def _finish_job(self, plan, work):
        with self.lock:
            self._live()
            if plan['switch']:
                lib = work / 'unpack' / DATA_NAME
                if not install.is_library(lib):
                    raise install.Refused('activate', 'The new library is not complete.')
                install.stage_switch(_pending_file(self.settings_dir), lib, plan['target'])
            self.job['state'] = 'done'
            for it in self.job['items']:
                it['rate_bps'], it['eta_s'] = 0, None
            self._save_job()


def _tag_sources(job):
    """Each item's source: 'found' when all its files are already on this computer (plan.sources), else 'download'."""
    plan = job['plan']
    for it in job['items']:
        names = _file_names(it['kind'], plan.get(it['kind']))
        it['source'] = 'found' if names and all(n in plan['sources'] for n in names) else 'download'


def _margin(total):
    """Room left free beyond what an install needs: 5% of the disk, at least 5 GB and at most 20 GB (5% of an 8 TB disk would
    rule out every library)."""
    return max(RECOMMEND_MARGIN, min(int((total or 0) * 0.05), RECOMMEND_MARGIN_MAX))


# ------------------------------------------------------------------------------------------------ startup and the app --
def _work_for(target):
    """Setup's work folder for a library: <its name>.setup beside it (LastbookData.setup for every library setup installs)."""
    return Path(target).with_name(Path(target).name + WORK_SUFFIX)


def _job_file(settings_dir):
    """The job and the pending switch live in the app's own settings folder, never in the work folder beside a library:
    a LastbookData.setup can arrive with a folder or a drive, and what is in it is not obeyed."""
    return Path(settings_dir) / 'setup' / 'job.json'


def _pending_file(settings_dir):
    return Path(settings_dir) / 'setup' / 'pending.json'


def startup(settings_dir, only=None):
    """app/engine_pipe.py calls this first: a staged switch is made now, before the engine opens anything. Returns the library to open
    (or None to look as usual) and a note: {target, old, error}."""
    settings_dir = Path(settings_dir)
    s = download.read_json(settings_dir / 'setup.json')
    if s is None:
        return None, None
    work = s.get('work')
    if not work or not isinstance(work, str):
        return None, None
    if only is not None and install._real(work) != install._real(_work_for(only)):
        return None, None        # opened with --data: a switch staged for another library waits for a normal start
    pend = _pending_file(settings_dir)
    try:
        lib, note = install.apply_pending(pend, work)
        if note and note.get('failed'):
            _record_failed(settings_dir, note.pop('failed'))
        return lib, note
    except Exception as e:        # whatever is wrong with it, the app still starts, on the library as it is
        # The pending switch stays: what it points at may be many GB of finished download. The next start tries again,
        # and Cancel in Library and downloads drops it.
        print('setup: the pending switch was not carried out:', type(e).__name__, e, flush=True)
        return None, None


def rolled_back(settings_dir):
    """app/engine_pipe.py calls this when the library a switch has just put in place does not open: the old one goes back
    (install.roll_back). A sentence saying so, or None when there was nothing to go back to."""
    work = _read_settings(settings_dir).get('work')
    if not work or not isinstance(work, str):
        return None
    kept = []
    try:
        said = install.roll_back(_pending_file(settings_dir), work, kept)
    except Exception as e:
        print('setup: the old library could not be put back:', type(e).__name__, e, flush=True)
        return None
    if kept:
        _record_failed(settings_dir, kept)
    return said


def _record_failed(settings_dir, paths):
    """setup.json's `failed`: libraries roll_back set aside, deleted when a library next opens (opened)."""
    with _SETTINGS:
        s = _read_settings(settings_dir)
        s['failed'] = _listed(s, 'failed') + [p for p in paths if p not in _listed(s, 'failed')]
        try:
            Path(settings_dir).mkdir(parents=True, exist_ok=True)
            download.write_json(Path(settings_dir) / 'setup.json', s)
        except OSError as e:
            print('setup: could not save setup.json:', e, flush=True)


_SETTINGS = threading.Lock()      # around a read-change-write of setup.json


def _read_settings(settings_dir):
    s = download.read_json(Path(settings_dir) / 'setup.json')
    return s if isinstance(s, dict) else {}


def _listed(s, key):
    """setup.json's `old` (the folders an upgrade moved aside) or `failed` (libraries that did not open, set aside by
    roll_back): those still there, waiting to be deleted."""
    v = s.get(key)
    return [o for o in v if isinstance(o, str) and os.path.lexists(o)] if isinstance(v, list) else []


def _old_listed(s):
    return _listed(s, 'old')


def _deletable(data, olds, failed):
    """Each folder checked just before it goes: only one setup itself set aside, beside the open library."""
    return [o for o in olds if install.is_old_of(o, data)] + [f for f in failed if install.is_failed_of(f, data)]


def _delete_old(settings_dir, data, olds, failed=()):
    """Deletes the old libraries of `data` among olds and the libraries set aside as failed among `failed`, then drops
    from setup.json's lists what is gone."""
    for o in _deletable(data, olds, failed):
        print('setup: deleting the set-aside library', Path(o).name, flush=True)
        shutil.rmtree(o, ignore_errors=True)
    with _SETTINGS:
        s = _read_settings(settings_dir)
        s['old'] = _listed(s, 'old')
        if 'failed' in s:
            s['failed'] = _listed(s, 'failed')
        try:
            download.write_json(Path(settings_dir) / 'setup.json', s)
        except OSError:
            pass


def opened(settings_dir, data, note):
    """The library opened. Clear the work folder, and delete the old library an upgrade moved aside
    (the new one has opened, so it is not needed to go back to) and any library that did not open and was set aside
    (roll_back) at an earlier start. That runs in its own thread (many GB), which is returned (or None); the folders stay listed in setup.json
    until they are gone, so a delete this program did not live to finish carries on at the next start."""
    settings_dir = Path(settings_dir)
    with _SETTINGS:
        olds = _opened(settings_dir, note)
        # Not at the start that set one aside: the person has just been told the new library did not open.
        failed = [] if note and note.get('rolled_back') else _listed(_read_settings(settings_dir), 'failed')
    if not _deletable(data, olds, failed):
        return None
    t = threading.Thread(target=_delete_old, args=(settings_dir, data, olds, failed), daemon=True, name='setup-delete-old')
    t.start()
    return t


def _opened(settings_dir, note):
    s = _read_settings(settings_dir)
    work = Path(s['work']) if s.get('work') else None
    olds = s.get('old') if isinstance(s.get('old'), list) else []
    if note and note.get('old') and note['old'] not in olds:
        olds = olds + [note['old']]
    # A switch still waiting: not made at this start (the library's disk was away, another library was opened with --data)
    # or tried and refused. Its download stays, to be tried again at the next start or dropped with Cancel.
    waiting = os.path.lexists(_pending_file(settings_dir)) and (not note or (note.get('error') and not note.get('rolled_back')
                                                                               and not note.get('old')))
    if work and not waiting:
        install.confirm(_pending_file(settings_dir))
        job = download.read_json(_job_file(settings_dir))
        if note or (isinstance(job, dict) and job.get('state') == 'done'):
            shutil.rmtree(work, ignore_errors=True)
            s.pop('work', None)
            try:
                _job_file(settings_dir).unlink()
            except OSError:
                pass
    s['old'] = _old_listed({'old': olds})
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        download.write_json(settings_dir / 'setup.json', s)
    except OSError:
        pass
    return s['old']


SETUP = None


def configure(**kw):
    global SETUP
    SETUP = Setup(**kw)
    return SETUP


ROUTES = {
    ('GET', 'state'): lambda s, b: s.state(),
    ('GET', 'job'): lambda s, b: s.public_job(),
    ('GET', 'here'): lambda s, b: {'here': s.here(), 'map_offer': here.asking(s.settings_dir), 'towns': s.towns()},
    ('POST', 'check'): lambda s, b: s.check(),
    ('POST', 'start'): lambda s, b: s.start(b.get('pack'), b.get('folder'), repair=bool(b.get('repair')), world=bool(b.get('world')), regions=[x for x in (b.get('regions') or []) if isinstance(x, str)][:50]),
    ('POST', 'pause'): lambda s, b: s.pause(),
    ('POST', 'resume'): lambda s, b: s.resume(),
    ('POST', 'cancel'): lambda s, b: s.cancel(),
    ('POST', 'retry'): lambda s, b: s.retry(),
    ('POST', 'use'): lambda s, b: s.use(b.get('path')),
    ('POST', 'finish'): lambda s, b: s.finish(),
    ('POST', 'place'): lambda s, b: s.set_place(b.get('name'), b.get('lat'), b.get('lon'), b.get('detail')),
    ('POST', 'offer'): lambda s, b: s.set_offer(b.get('ask') is not False),
}


def _send_json(h, obj, status=200):
    data = json.dumps(obj, separators=(',', ':')).encode()
    h.send_response(status)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Content-Length', str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def handle(h, method, path):
    """Answers one /api/setup/ request on a BaseHTTPRequestHandler h."""
    name = path[len('/api/setup/'):].strip('/')
    fn = ROUTES.get((method, name))
    if not fn:
        return _send_json(h, {'error': 'not found'}, 404)
    body = {}
    if method == 'POST':
        try:
            n = int(h.headers.get('Content-Length') or 0)
            if n < 0 or n > MAX_BODY:
                return _send_json(h, {'error': 'request too large'}, 413)
            body = json.loads(h.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _send_json(h, {'error': 'bad request'}, 400)
    try:
        out = fn(SETUP, body)
    except Exception as e:
        traceback.print_exc()
        return _send_json(h, {'error': f'Setup stopped on an internal error ({type(e).__name__}).'}, 500)
    status = 200
    if isinstance(out, dict) and out.get('error') and 'code' in out:
        status = 409 if out['code'] in ('busy', 'not_done') else 400 if out['code'] != 'no_space' else 507
    return _send_json(h, out, status)


# ------------------------------------------------------------------------------------------------ setup mode's Handler --
class SetupHandler(BaseHTTPRequestHandler):
    """All there is in setup mode: /api/setup/*."""

    def log_message(self, *a):
        pass

    def _answer(self, method):
        import urllib.parse
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith('/api/setup/'):
            return handle(self, method, path)   # only an exact route name is answered
        return _send_json(self, {'error': 'not found'}, 404)

    def do_GET(self):
        return self._answer('GET')

    def do_POST(self):
        return self._answer('POST')


class SetupServer:
    """What app/bridge.py needs of an engine module: its Handler."""
    Handler = SetupHandler
