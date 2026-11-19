"""/api/setup/*: the engine side of first-run setup and of About → Library and downloads. Contract: docs/SETUP-API.md.

Two ways in, one module, both set up by app/engine_pipe.py (configure(...)) and reached only through the bridge:
  - setup mode (no library yet): a Bridge around SetupServer, whose Handler serves /api/setup/* here. Nothing else.
  - normal mode: server.py hands /api/setup/ to handle().
"""
import hashlib
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import download, hardware, install, maps
from .manifest import Manifest

DATA_NAME = install.DATA_NAME
WORK_SUFFIX = '.setup'
RECOMMEND_MARGIN = 5 * 10 ** 9     # beyond what the install needs, so the disk is not left full
RECOMMEND_MARGIN_MAX = 20 * 10 ** 9
MAX_BODY = 64 * 1024
WORLD_ESTIMATE = 650 * 10 ** 6     # the world map to city level (zoom 8) and place search, before it is measured
ACTIVE = ('waiting', 'downloading', 'verifying', 'unpacking', 'activating')
UNPACK = {'pack': 'unpack', 'map': 'unpack-map', 'addresses': 'unpack-addresses'}     # where each kind unpacks, in the work folder


def _item(id, kind, title, total, state='waiting'):
    return {'id': id, 'kind': kind, 'title': title, 'state': state, 'bytes_done': 0, 'bytes_total': total, 'rate_bps': 0,
            'eta_s': None, 'error': None}


def _file_names(kind, entry):
    """The file names a manifest entry is downloaded as: the model's one file, a pack's, map's or address index's zips."""
    if not entry or kind not in ('model', *UNPACK):
        return []
    return [entry['file']] if kind == 'model' else [f['name'] for f in entry['files']]


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _gb(n):
    return f'{n / 1e9:.0f} GB' if n >= 10e9 else f'{n / 1e9:.1f} GB'


def _sha256(path, control, progress):
    h, done = hashlib.sha256(), 0
    total = os.path.getsize(path)
    with open(path, 'rb') as f:
        while True:
            control.check()
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
            progress(done, total)
    return h.hexdigest()


def _staging(folder, files):
    """An unpack folder for exactly these files (their SHA-256s). What an earlier job left there for other files is removed
    first, since unpacking over it keeps any file that happens to have the right size; the same files carry on where
    they stopped."""
    key, mark = [f['sha256'] for f in files], folder.with_name(folder.name + '.json')
    try:
        same = json.loads(mark.read_text(encoding='utf-8')) == key
    except (OSError, ValueError):
        same = False
    if not same:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise install.Refused('unpack', 'Lastbook could not clear what an earlier download left behind.')
        folder.parent.mkdir(parents=True, exist_ok=True)
        install._write_json(mark, key)


def _bring(src, dest, control, progress=lambda done, total: None):
    """The person's own file (one they chose, or one found in a library) at `dest`, with theirs left where it is, whole:
    it is never moved into a folder that a cancel or a failed switch clears. A hard link when the disk allows (no time,
    no room, the same bytes), else a copy made in pieces, so a pause or cancel stops it and what it wrote is removed."""
    src, dest = Path(src), Path(dest)
    tmp = dest.with_name(dest.name + '.partial')
    tmp.unlink(missing_ok=True)
    try:
        if os.path.samefile(src, dest):
            return          # already there (a repair that found the library's own model whole): nothing to bring
    except OSError:
        pass
    try:
        try:
            os.link(src, tmp)
        except OSError:
            total = src.stat().st_size
            free, _ = hardware.disk_of(dest.parent)
            if free < total + 512 * 2 ** 20:
                raise download.Failed('no_space', f'Copying {src.name} needs {_gb(total)} free, and the disk has {_gb(free)}. Free some space, then try again.',
                                      need_bytes=total, free_bytes=free)
            done = 0
            with open(src, 'rb') as f, open(tmp, 'wb') as out:
                while b := f.read(install.COPY):
                    control.check()
                    out.write(b)
                    done += len(b)
                    progress(done, total)
        control.check()
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _clear_partials(lib):
    """Removes a models/<file>.partial that _bring left beside the finished <file> (an earlier version left one after a
    repair that found nothing wrong). Only with the model itself there: nothing but a leftover copy is removed."""
    models = Path(lib) / 'models'
    try:
        found = [p for p in models.iterdir() if p.name.endswith('.partial')]
    except OSError:
        return
    for p in found:
        model = p.with_name(p.name[:-len('.partial')])
        if p.is_file() and not p.is_symlink() and model.is_file():
            try:
                p.unlink()
                print('setup: removed a leftover', p.name, flush=True)
            except OSError:
                pass


def _same_disk(a, b):
    try:
        pa, pb = Path(a), Path(b)
        while not pa.exists():
            pa = pa.parent
        while not pb.exists():
            pb = pb.parent
        return os.stat(pa).st_dev == os.stat(pb).st_dev
    except OSError:
        return False


class _Item:
    """Progress of one item (the pack, or the model), with rate and time left."""

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
                 on_finish=None, look_for_libraries=None, home=None, llm_local=None):
        """mode: 'first-run' or 'manage'. current: the open library (manage). on_finish(folder): the app opens that library
        (it relaunches). look_for_libraries(): LastbookData folders found."""
        self.mode, self.current = mode, Path(current) if current else None
        self.settings_dir, self.default_parent = Path(settings_dir), Path(default_parent)
        self.manifest = manifest or Manifest(allowed=allowed, context=context)
        self.allowed, self.context = allowed, context
        self.on_finish, self.look_for_libraries = on_finish, look_for_libraries
        self._llm = llm_local
        self.lock = threading.RLock()
        self.job, self.control, self.thread = None, None, None
        self.sources = {}          # file name -> a file already on this computer the person chose with `use`
        self.ai_measure, self.measuring = None, False
        self.rejected = {}         # path -> (size, mtime) of a file already here that failed its SHA-256: not offered or used again
        self.job = self._load_job()

    # ------------------------------------------------------------------------------------------------- saved state --
    def _settings(self):
        try:
            return json.loads((self.settings_dir / 'setup.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save_settings(self, d):
        try:
            self.settings_dir.mkdir(parents=True, exist_ok=True)
            install._write_json(self.settings_dir / 'setup.json', d)
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
        what the plan's ids and the parent folder give: pack, map and model come from the manifest; work and target are worked
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
        mp = self.manifest.map(ids('map')) if plan.get('map') else None
        model = self.manifest.model(ids('model')) if plan.get('model') else None
        addresses = self.manifest.address_index(ids('addresses')) if plan.get('addresses') else None
        if plan.get('addresses') and not addresses:
            return None
        if (plan.get('pack') and not pack) or (plan.get('map') and not mp) or (plan.get('model') and not model):
            return None
        wanted = {('pack', pack['id']) if pack else None, ('map', mp['id']) if mp else None, ('model', model['id']) if model else None, ('addresses', addresses['id']) if addresses else None} - {None}
        osm = [x for x in (plan.get('osm') or []) if x in ('world', 'places') or (isinstance(x, str) and maps.region(x))]
        wanted |= {('osm', x) for x in osm}
        if {(i.get('kind'), i.get('id')) for i in items} != wanted:
            return None
        names = [n for k, e in (('pack', pack), ('map', mp), ('addresses', addresses), ('model', model)) for n in _file_names(k, e)]
        sources = {n: str(v) for n, v in (plan.get('sources') or {}).items()
                   if n in names and isinstance(v, str) and Path(v).name == n and Path(v).is_file()}
        switch = bool(plan.get('switch')) and pack is not None
        clean = {'pack': pack, 'model': model, 'map': mp, 'addresses': addresses, 'parent': str(parent), 'target': str(target), 'work': str(work),
                 'sources': sources, 'explicit': bool(plan.get('explicit')), 'retried': {}, 'switch': switch, 'repair': bool(plan.get('repair')),
                 'osm': osm}
        if isinstance(plan.get('damaged'), list):
            clean['damaged'] = [str(x) for x in plan['damaged']]
        if plan.get('map_new'):
            clean['map_new'] = str(work / 'unpack-map' / DATA_NAME)
        if plan.get('model_file') and model:
            clean['model_file'] = str((target if switch else Path(self.current or target)) / 'models' / model['file'])
            if plan.get('model_sha256'):
                clean['model_sha256'] = model['sha256']
        job['plan'] = clean
        return job

    def _done_after_restart(self, plan, it, was):
        """(bytes done, bytes total) for an item found mid-run at startup, from what is on disk for the step it was in."""
        if it['kind'] == 'osm':
            return 0, it['bytes_total']        # a map cut starts over: its half-made file is set aside on the next run
        kind = it['kind'] if it['kind'] in ('map', 'addresses') and plan.get(it['kind']) else 'pack'
        if was == 'unpacking' and plan.get(kind):
            total = plan[kind]['installed_bytes']
            return min(_tree_size(Path(plan['work']) / UNPACK[kind]), total), total
        if it.get('source') == 'found':
            return it['bytes_total'], it['bytes_total']     # nothing to download: the file is already on this computer
        return self._on_disk(plan, it), it['bytes_total']

    @staticmethod
    def _on_disk(plan, it):
        """Bytes of this item already downloaded, from the files themselves."""
        kind = it['kind'] if it['kind'] in UNPACK and plan.get(it['kind']) else 'model'
        n = 0
        for name in _file_names(kind, plan.get(kind)):
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
        install._write_json(f, job)

    # --------------------------------------------------------------------------------------------------- the state --
    def llm(self):
        if self._llm is None:
            try:
                import llm_local
                self._llm = llm_local
            except Exception as e:
                print('setup: no llm_local:', type(e).__name__, e, flush=True)
                self._llm = False
        return self._llm or None

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
        m = self.manifest.model()
        model = None
        if m:
            p = self.current / 'models' / m['file']
            if p.is_file() and p.stat().st_size == m['size']:
                model = m['id']
        return {'folder': str(self.current), 'pack': pack, 'content_revision': revision, 'map_bytes': install.map_bytes(self.current), 'model': model, 'map': install.map_name(self.current), 'address_indexes': self.address_indexes(),
                'osm': maps.installed(self.current / 'derived' / 'geo')}

    def address_indexes(self):
        if not self.current:
            return []
        out = []
        for entry in self.manifest.current.get('address_indexes', []):
            path = self.current / 'derived' / 'geo' / 'addresses' / (entry['id'] + '.sqlite')
            try:
                receipt = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
                expected = {file['name']: file['sha256'] for file in entry['files']}
                if path.is_file() and path.stat().st_size == entry['installed_bytes'] and receipt.get('files') == expected:
                    out.append(entry['id'])
            except (OSError, ValueError, AttributeError):
                pass
        return out

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
        files = {}
        for p in self.manifest.current['packs']:
            for f in p['files']:
                files[f['name']] = ('pack', p['id'], f['size'])
        m = self.manifest.model()
        if m:
            files[m['file']] = ('model', m['id'], m['size'])
        for kind, key in (('map', 'maps'), ('addresses', 'address_indexes')):
            for x in self.manifest.current.get(key, []):
                for f in x['files']:
                    files[f['name']] = (kind, x['id'], f['size'])
        places = [self.default_parent]
        if self.current:
            places += [self.current / 'models', self.current]
        candidates = [Path(p) for p in self.sources.values()]
        for place in places:
            try:
                candidates.extend(c for c in place.iterdir() if c.name in files or (c.name.startswith('Lastbook-') and c.name.endswith('.zip')))
            except OSError:
                continue
        for c in candidates:
            key = str(c.resolve())
            if key in seen or not c.is_file():
                continue
            seen.add(key)
            kind, pid, size = files.get(c.name, ('pack', None, None))
            st = c.stat()
            rejected = self.rejected.get(str(c)) == (st.st_size, st.st_mtime_ns)
            out.append({'kind': kind, 'path': str(c), 'name': c.name, 'id': pid, 'size': st.st_size,
                        'size_ok': size == st.st_size and not rejected, 'rejected': rejected})
        return out

    def _model_path_here(self):
        m = self.manifest.model()
        if not m:
            return None
        for f in self.found():
            if f['kind'] == 'model' and f.get('size_ok'):
                return f['path']
        return None

    def ai_fit(self, hw):
        m = self.manifest.model()
        if not m:
            return {'fits': False, 'needs_bytes': None, 'estimate': True, 'why': 'No local AI is offered for this version.'}
        if self.ai_measure:
            return self.ai_measure
        path = self._model_path_here()
        if path and not self.measuring and self.llm():
            self.measuring = True
            threading.Thread(target=self._measure, args=(path,), daemon=True, name='setup-measure').start()
        return hardware.estimate_ai(hw['ram_bytes'], hw['gpus'], hw['unified_memory'], m['memory_bytes'])

    def _measure(self, path):
        try:
            r = hardware.measure_ai(self.llm(), path)
            if r:
                self.ai_measure = r
        except Exception:
            traceback.print_exc()

    def hardware(self):
        extra = [self.default_parent] + ([self.current.parent] if self.current else [])
        hw = hardware.detect(extra)
        hw['ai'] = self.ai_fit(hw)
        return hw

    def recommend(self, hw):
        available = [p for p in self.manifest.current['packs'] if p['available']]
        small = self.manifest.pack('small')
        parent = self.current.parent if self.current else self.default_parent
        free, total = hardware.disk_of(parent)
        model = self.manifest.model()
        need = {p['id']: install.need_bytes(p, None) for p in available}
        map_need = {x['id']: _map_need(x, ()) for x in self.manifest.current.get('maps', []) if x['available']}
        if not small or not small['available']:
            return {'pack': None, 'ai': False, 'folder': str(parent), 'need_bytes': need, 'why': 'No library is ready to download yet.'}
        margin = _margin(total)
        folder, elsewhere = parent, None
        if free < need['small'] + margin:   # the disk that holds the home folder (or the open library) whenever Small fits there; only then another
            elsewhere = next((d for d in hw['disks'] if not _same_disk(d['path'], parent)
                              and d['free_bytes'] >= need['small'] + _margin(d['total_bytes'])), None)
            if not elsewhere:
                return {'pack': None, 'ai': False, 'folder': str(parent), 'need_bytes': need,
                        'why': f'The smallest library, {small["title"]}, needs {_gb(need["small"])} free, and this computer has {_gb(free)} free.'}
            folder, home_free = Path(elsewhere['path']), free
            free, margin = elsewhere['free_bytes'], _margin(elsewhere['total_bytes'])
        left = free - need['small']
        ai = bool(model and hw['ai']['fits'] and left >= model['size'] + margin)
        if elsewhere:
            name = Path(elsewhere['path']).name or elsewhere['path']
            why = (f'This computer’s own disk has {_gb(home_free)} free, not enough for any library, so {small["title"]} goes on {name}. '
                   'Keep that disk connected: Lastbook reads the library from it.')
        else:
            why = 'Small is selected to get started. You can choose a larger library.'
        if model and hw['ai']['fits'] and not ai:
            why += ' The local AI fits this computer’s memory, but not on the disk as well.'
        elif model and not hw['ai']['fits']:
            why += ' ' + hw['ai']['why']
        return {'pack': 'small', 'ai': ai, 'folder': str(folder), 'need_bytes': need, 'map_need_bytes': map_need, 'address_need_bytes': {x['id']: _map_need(x, ()) for x in self.manifest.current.get('address_indexes', []) if x['available']}, 'why': why}

    def state(self):
        self.manifest.fetch_once()
        hw = self.hardware()
        return {'mode': self.mode, 'hardware': hw, 'manifest': self.manifest.public(), 'current': self.current_info(),
                'found': self.found(), 'recommend': self.recommend(hw), 'job': self.public_job(),
                'map_regions': maps.regions(), 'world_bytes': WORLD_ESTIMATE}

    def public_job(self):
        with self.lock:
            if not self.job:
                return None
            return {k: v for k, v in json.loads(json.dumps(self.job)).items() if k != 'plan'}

    # -------------------------------------------------------------------------------------------------- the actions --
    def _parent_for(self, folder):
        if folder:
            p = Path(folder).expanduser()
            return p.parent if p.name == DATA_NAME else p
        return self.current.parent if self.current else self.default_parent

    def start(self, pack_id=None, ai=False, folder=None, sources=None, explicit=False, repair=False, map_id=None, address_id=None, world=False, regions=()):
        if repair:
            return self._start_repair(ai)
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            pack = self.manifest.pack(pack_id) if pack_id else None
            if pack_id and (not pack or not pack['available']):
                return {'error': 'That library is not available yet.', 'code': 'unavailable'}
            cur = self.current_info() or {}
            if folder and self.current and install._real(Path(folder).expanduser()) == install._real(self.current):
                folder = None      # the window sends the shown folder, which in manage mode is the open library itself
            same_revision = pack and (not pack.get('revision') or pack['revision'] == cur.get('content_revision', ''))
            if pack and self.mode == 'manage' and cur.get('pack') == pack['id'] and same_revision and not folder:
                pack = None        # the same library again: only the AI is wanted
            model = self.manifest.model() if ai else None
            if ai and not model:
                return {'error': 'No local AI is offered for this version.', 'code': 'unavailable'}
            mp = self.manifest.map(map_id) if map_id else None
            if map_id and (not mp or not mp['available']):
                return {'error': 'That state’s map is not available yet.', 'code': 'unavailable'}
            requested_address = address_id
            address_id = address_id or map_id or (pack or {}).get('map') or cur.get('map')
            if address_id in ('base', 'us'):
                address_id = next(iter(cur.get('address_indexes', [])), None)
            addresses = self.manifest.address_index(address_id) if address_id else None
            if not addresses or not addresses['available']:
                if requested_address:
                    return {'error': 'That state’s address search is not available yet.', 'code': 'unavailable'}
                addresses = None
            parent = self._parent_for(folder)
            target = self.current if self.current and not folder else parent / DATA_NAME   # the open library as it is, whatever its name
            parent = target.parent
            if addresses and addresses['id'] in cur.get('address_indexes', []) and target == self.current:
                addresses = None
            if model and cur.get('model') and target == self.current:
                model = None       # already here, and an upgrade in place carries it over
            if mp and not pack and cur.get('map') == mp['id']:
                mp = None          # this state's map is the one here already
            have_osm = maps.installed(Path(target) / 'derived' / 'geo') if target == self.current or not pack else {'world': False, 'places': False, 'regions': []}
            osm = []
            if world or regions:       # street-level regions draw over the world map, so a region brings the world with it
                osm += [x for x in ('world', 'places') if not have_osm[x]]
            for rid in dict.fromkeys(regions or ()):
                if not isinstance(rid, str) or not maps.region(rid):
                    return {'error': 'Lastbook does not know that map region.', 'code': 'unavailable'}
                if rid not in {r['id'] for r in have_osm['regions']}:
                    osm.append(rid)
            if self.mode == 'first-run' and not pack and (model or mp or addresses or osm):
                return {'error': 'Choose a library first.', 'code': 'nothing'}
            if not pack and not model and not mp and not addresses and not osm:
                if self.mode == 'first-run':
                    return {'error': 'Choose a library first.', 'code': 'nothing'}
                return {'error': 'Nothing to add: this library and the local AI are already here.', 'code': 'nothing'}
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
                if f.get('size_ok') and f['name'] not in srcs and f['kind'] in ('pack', 'model', 'map', 'addresses'):
                    srcs[f['name']] = f['path']
            names = [n for k, e in (('pack', pack), ('model', model), ('map', mp), ('addresses', addresses)) for n in _file_names(k, e)]
            have = set(srcs)
            need = install.need_bytes(pack, model, have) + _map_need(mp, have) + _map_need(addresses, have) + (WORLD_ESTIMATE if 'world' in osm else 0)
            preserve_map = bool(pack and cur.get('pack') == pack['id'] and target == self.current and not mp and cur.get('map') not in (None, 'base'))
            if preserve_map:
                need += cur['map_bytes']
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
            if mp:                 # after the pack: with a pack, the map replaces the pack's own map set in the staged library
                items.append(_item(mp['id'], 'map', mp['title'], mp['download_bytes']))
            if addresses:
                items.append(_item(addresses['id'], 'addresses', addresses['title'] + ' address search', addresses['download_bytes']))
            for x in osm:          # sizes are known once each cut is measured, when its turn comes
                title = {'world': 'World map', 'places': 'Place search'}.get(x) or maps.region(x)['name'] + ' streets'
                items.append(_item(x, 'osm', title, WORLD_ESTIMATE if x == 'world' else 0))
            if model:
                items.append(_item(model['id'], 'model', model['title'], model['size']))
            return self._begin(items, target, work, {
                'pack': pack, 'model': model, 'map': mp, 'addresses': addresses, 'parent': str(parent), 'target': str(target), 'work': str(work),
                'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': explicit, 'retried': {}, 'switch': bool(pack), 'osm': osm})

    def _begin(self, items, target, work, plan, **extra):
        """The new job, saved and started (the caller holds the lock)."""
        self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'items': items, 'error': None, 'started': _now(), 'folder': str(target),
                    **extra, 'plan': plan}
        with _SETTINGS:
            s = self._settings()
            s['work'] = str(work)
            self._save_settings(s)
        for lib in dict.fromkeys(x for x in (Path(target), self.current) if x):
            _clear_partials(lib)
        _tag_sources(self.job)
        self._save_job()
        self._run()
        return self.public_job()

    def _start_repair(self, ai):
        """Re-check the open library's files (pack-manifest.tsv) and the model's SHA-256. What is wrong is fetched again: the pack is
        downloaded and switched to as in an upgrade, the model replaced in place. Nothing is downloaded if all is well."""
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            cur = self.current_info()
            if self.mode != 'manage' or not cur:
                return {'error': 'There is no library to repair yet.', 'code': 'nothing'}
            pack = self.manifest.pack(cur['pack']) if cur['pack'] else None
            if not pack or not pack['available']:
                return {'error': 'Lastbook cannot tell which library this is, so it cannot repair it. Download a library instead.', 'code': 'unavailable'}
            model = self.manifest.model() if (ai or cur['model']) else None
            parent, target = self.current.parent, self.current
            work = _work_for(target)
            srcs = {f['name']: f['path'] for f in self.found() if f.get('size_ok') and f['kind'] in ('pack', 'model')}
            items = [_item(pack['id'], 'pack', pack['title'], pack['installed_bytes'])]
            if model:
                items.append(_item(model['id'], 'model', model['title'], model['size']))
            names = _file_names('pack', pack) + _file_names('model', model)
            return self._begin(items, target, work, {
                'pack': pack, 'model': model, 'parent': str(parent), 'target': str(target), 'work': str(work),
                'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': False, 'retried': {}, 'switch': True, 'repair': True},
                repair=True)

    def _check_library(self, it, lib, control):
        """Files of the open library that are missing or differ from its pack-manifest.tsv (path, bytes, sha256)."""
        try:
            rows = [l.split('\t') for l in (lib / 'pack-manifest.tsv').read_text(encoding='utf-8').splitlines()[1:] if l.strip()]
            rows = [(r[0], int(r[1]), r[2]) for r in rows]
        except (OSError, ValueError, IndexError):
            return ['pack-manifest.tsv']
        if install.map_set(lib):      # a state map replaced the pack's own: its files are checked against map-set.json instead
            rows = [r for r in rows if not install.map_member(r[0])]
            try:
                install.check_map(lib)
            except install.Refused as e:
                print('setup: repair:', e.message, flush=True)
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
            if _sha256(p, control, lambda d, t: tracker.progress(base + d, total, 'verifying')) != digest:
                bad.append(rel)
            done = base + size
        return bad

    def _run(self):
        self.control = download.Control()
        self.thread = threading.Thread(target=self._work, args=(self.control,), daemon=True, name='setup-job')
        self.thread.control = self.control
        self.thread.start()

    def _live(self):
        """For the job's own thread, before it changes the job (the caller holds the lock): Stopped if its run was cancelled
        or another run has begun. Cancel waits 30 s for the thread; one that outlives that changes nothing afterwards."""
        c = getattr(threading.current_thread(), 'control', None)
        if c is not None and (c.why == 'cancelled' or c is not self.control):
            raise download.Stopped('cancelled')

    def _unstage(self):
        """A switch staged by an earlier job is off: its staging folder is about to be changed."""
        try:
            _pending_file(self.settings_dir).unlink()
        except OSError:
            pass

    def pause(self):
        with self.lock:
            if self.job and self.job['state'] in ACTIVE and self.control:
                self.control.stop('paused')
        self._join()
        return self.public_job()

    def resume(self):
        with self.lock:
            if self.job and self.job['state'] == 'paused':
                self.job['state'] = 'waiting'
                for it in self.job['items']:
                    it.pop('paused_from', None)
                self._save_job()
                self._run()
        return self.public_job()

    def retry(self):
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
        """What the job made in its work folder goes; the person's own files and any library stay."""
        work = Path(job['plan']['work'])
        for sub in ('downloads', *UNPACK.values()):
            shutil.rmtree(work / sub, ignore_errors=True)

    def use(self, path):
        p = Path(path or '').expanduser()
        if not str(path or '').strip() or not p.exists():
            return {'error': 'Lastbook cannot find that file or folder.', 'code': 'missing'}
        if p.is_dir():
            lib = p if install.is_library(p) else p / DATA_NAME
            if not install.is_library(lib):
                known = {f['name'] for pk in self.manifest.current['packs'] for f in pk['files']}
                model = self.manifest.model()
                if model:
                    known.add(model['file'])
                inside = sorted(c for c in p.iterdir() if c.name in known and c.is_file()) if p.is_dir() else []
                if inside:          # the person explicitly chose this folder
                    return self.use(str(inside[0]))
                return {'error': f'“{p.name}” is not a Lastbook library. Choose the folder called {DATA_NAME}.', 'code': 'not_library'}
            if not os.access(lib, os.W_OK):
                return {'error': f'Lastbook cannot save in “{lib.name}”. Move it somewhere you can save to, then choose it there.', 'code': 'folder'}
            with self.lock:
                if self.job and self.job['state'] in ACTIVE:
                    return {'error': 'A download is under way. Pause or cancel it first.', 'code': 'busy'}
                self.job = {'id': uuid.uuid4().hex[:12], 'state': 'done', 'error': None, 'started': _now(), 'folder': str(lib),
                            'items': [_item('library', 'pack', lib.name, 0, state='done')],
                            'plan': {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}}
                return self.public_job()
        name, known = p.name, {}        # file name -> (kind, entry, size); a pack's name wins, then a map's, an address index's, the model's
        for kind, key in (('pack', 'packs'), ('map', 'maps'), ('addresses', 'address_indexes')):
            for entry in self.manifest.current.get(key, []):
                for f in entry['files']:
                    known.setdefault(f['name'], (kind, entry, f['size']))
        m = self.manifest.model()
        if m:
            known.setdefault(m['file'], ('model', m, m['size']))
        if name not in known:
            return {'error': 'That is not a file Lastbook knows. Choose a Lastbook library zip or the model file.', 'code': 'unknown'}
        kind, entry, size = known[name]
        if p.stat().st_size != size:
            if kind == 'addresses':
                return {'error': 'The address index download is incomplete.', 'code': 'size'}
            what = 'model' if kind == 'model' else f'published {name}'
            return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(size)} of the {what}. The download may not have finished.', 'code': 'size'}
        if kind == 'pack' and not entry['available']:
            return {'error': 'That library is not available in this version.', 'code': 'unavailable'}
        self.sources[name] = str(p)
        if kind == 'pack':
            return self.start(entry['id'], False, None, {name: str(p)}, explicit=True)
        if self.mode != 'manage':
            return self.public_job() or {'job': None, 'noted': True}
        if kind == 'map':
            return self.start(None, False, None, {name: str(p)}, explicit=True, map_id=entry['id'])
        if kind == 'addresses':
            return self.start(sources={name: str(p)}, explicit=True, address_id=entry['id'])
        return self.start(None, True, None, {name: str(p)}, explicit=True)

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
                do = {'pack': self._do_pack, 'map': self._do_map, 'addresses': self._do_addresses, 'osm': self._do_osm}
                do.get(it['kind'], self._do_model)(it, plan, work, control)
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
                self._fail(it, 'unpack' if it and job['state'] == 'unpacking' else 'network', f'Lastbook could not write a file ({type(e).__name__}: {e.strerror or e}).')
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
            digest = _sha256(src, control, lambda d, t: tracker.progress(offset + d, it['bytes_total'], 'verifying'))
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
            need = install.need_bytes(pack, None, set(plan['sources']))
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
        total = sum(install.check_zip(path, pack['installed_bytes'])[1] for path, _ in got)   # every member checked before any is written
        still = max(total - _tree_size(staged), 0)
        free, _ = hardware.disk_of(work)
        if free < still + 512 * 2 ** 20:
            raise download.Failed('no_space', f'Unpacking needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.')
        for path, _ in got:
            lib = install.unpack(path, staged, pack['installed_bytes'], control, lambda d, t: tracker.progress(d, t, 'unpacking'))
        if pack.get('revision'):
            metadata = json.loads((lib / 'pack.json').read_text(encoding='utf-8'))
            if metadata.get('content_revision') != pack['revision']:
                raise install.Refused('unpack', 'The library content version does not match this download.')
        current = self.current_info() or {}
        if (not plan.get('repair') and not plan.get('map') and Path(plan['target']) == self.current and current.get('pack') == pack['id']
                and current.get('map') not in (None, 'base')):
            install.copy_map(self.current, lib)
        install._write_json(lib / 'setup-pack.json', {'pack': pack['id'], 'installed': _now(), 'files': [f['name'] for f in pack['files']]})
        for path, ours in got:
            if ours:
                download.remove(path)

    def _do_map(self, it, plan, work, control):
        """A state map: download, check, unpack (map files only) into unpack-map/. With a pack, it replaces the staged library's own
        map set at once; on its own it is staged and switched in when the app restarts (install._apply_map)."""
        mp = plan['map']
        got = self._get_files(it, mp['files'], plan, work, control)
        tracker = _Item(it)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=mp['installed_bytes'])
        staged = work / 'unpack-map'
        if not plan['switch']:
            self._unstage()
        _staging(staged, mp['files'])
        for path, _ in got:
            install.check_zip(path, mp['installed_bytes'], install.map_member)
        for path, _ in got:
            lib = install.unpack(path, staged, mp['installed_bytes'], control, lambda d, t: tracker.progress(d, t, 'unpacking'), allowed=install.map_member)
        if install.map_name(lib) != mp['id']:
            raise install.Refused('unpack', f'The map file says it is {install.map_name(lib)}, not {mp["id"]}.')
        if plan['switch']:
            install.replace_map_in(work / 'unpack' / DATA_NAME, lib)
            shutil.rmtree(staged, ignore_errors=True)
        else:
            plan['map_new'] = str(lib)
        for path, ours in got:
            if ours:
                download.remove(path)

    def _do_addresses(self, it, plan, work, control):
        entry = plan['addresses']
        relative = 'derived/geo/addresses/' + entry['id'] + '.sqlite'
        directories = {'', 'derived', 'derived/geo', 'derived/geo/addresses'}

        def allowed(name, is_dir=False):
            return name in directories if is_dir else name == relative

        def verify(lib):
            path = lib / relative
            if not path.is_file() or path.stat().st_size != entry['installed_bytes']:
                raise install.Refused('unpack', 'The address index is missing or the wrong size.')

        got = self._get_files(it, entry['files'], plan, work, control)
        for path, _ in got:
            install.check_zip(path, entry['installed_bytes'], allowed)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=entry['installed_bytes'])
        tracker = _Item(it)
        _staging(work / 'unpack-addresses', entry['files'])
        for path, _ in got:
            lib = install.unpack(path, work / 'unpack-addresses', entry['installed_bytes'], control,
                                 lambda done, total: tracker.progress(done, total, 'unpacking'), allowed=allowed, verify=verify)
        target = work / 'unpack' / DATA_NAME if plan['switch'] else Path(plan['target'])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._set(it, 'activating')
        os.replace(lib / relative, destination)
        install._write_json(destination.with_suffix('.json'), {'files': {file['name']: file['sha256'] for file in entry['files']}})
        for path, ours in got:
            if ours:
                download.remove(path)

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
        need = int(cut.disk * maps.DISK_SLACK) + 512 * 2 ** 20     # the file, one row a tile: more than the bytes fetched
        if free < need:
            raise download.Failed('no_space', f'{it["title"]} needs {_gb(need)} free, and the disk has {_gb(free)}. Free some space, then try again.',
                                  need_bytes=need, free_bytes=free)
        with self.lock:
            it['bytes_total'] = total
        cut.run(lambda done, total: tracker.progress(done, total))

    def _do_model(self, it, plan, work, control):
        m = plan['model']
        path, ours = self._get_file(it, {'name': m['file'], 'url': m['url'], 'size': m['size'], 'sha256': m['sha256']}, plan, work, control, 0)
        lib = (work / 'unpack' / DATA_NAME) if plan['switch'] else Path(self.current or plan['target'])
        dest = lib / 'models' / m['file']
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._set(it, 'activating')
        if ours:
            control.check()
            os.replace(path, dest)
        else:
            _bring(path, dest, control)
        with self.lock:
            self._live()
        plan['model_file'] = str(Path(plan['target']) / 'models' / m['file']) if plan['switch'] else str(dest)
        plan['model_sha256'] = m['sha256']           # what _get_file just matched: the streamed or re-read hash equalled this
        if not plan['switch'] and self.current:      # the library open now: the AI can use it at once
            self._mark_checked(dest, m['sha256'])

    def _mark_checked(self, path, sha256):
        llm = self.llm()
        if not llm:
            return
        try:
            llm.mark_checked(path, sha256)
            if getattr(llm, 'LOCAL', None) and getattr(llm.LOCAL, '_started', False):
                threading.Thread(target=llm.LOCAL.discover, daemon=True).start()
        except Exception as e:
            print('setup: mark_checked:', type(e).__name__, e, flush=True)

    def _finish_job(self, plan, work):
        with self.lock:
            self._live()
            if plan['switch']:
                lib = work / 'unpack' / DATA_NAME
                if not install.is_library(lib):
                    raise install.Refused('activate', 'The new library is not complete.')
                install.stage_switch(_pending_file(self.settings_dir), lib, plan['target'], plan.get('model_file'), model_sha256=plan.get('model_sha256'))
            elif plan.get('map_new'):
                install.stage_switch(_pending_file(self.settings_dir), plan['map_new'], plan['target'], kind='map')
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


def _map_need(mp, have):
    """Room a state map needs: what is still to download plus its unpacked size."""
    if not mp:
        return 0
    return sum(f['size'] for f in mp['files'] if f['name'] not in have) + (mp.get('installed_bytes') or 0)


def _margin(total):
    """Room left free beyond what an install needs: 5% of the disk, at least 5 GB and at most 20 GB (5% of an 8 TB disk would
    rule out every library)."""
    return max(RECOMMEND_MARGIN, min(int((total or 0) * 0.05), RECOMMEND_MARGIN_MAX))


def _tree_size(p):
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


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
    (or None to look as usual) and a note: {target, old, model_file, error}."""
    settings_dir = Path(settings_dir)
    try:
        s = json.loads((settings_dir / 'setup.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
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
        print('setup: the pending switch was not carried out:', type(e).__name__, e, flush=True)
        try:
            pend.unlink()
        except OSError:
            pass
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
            install._write_json(Path(settings_dir) / 'setup.json', s)
        except OSError as e:
            print('setup: could not save setup.json:', e, flush=True)


_SETTINGS = threading.Lock()      # around a read-change-write of setup.json


def _read_settings(settings_dir):
    try:
        s = json.loads((Path(settings_dir) / 'setup.json').read_text(encoding='utf-8'))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


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
            install._write_json(Path(settings_dir) / 'setup.json', s)
        except OSError:
            pass


def opened(settings_dir, data, note):
    """The library opened. Mark the model checked, clear the work folder, and delete the old library an upgrade moved aside
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
    if note and note.get('model_file') and note.get('model_sha256') and os.path.isfile(note['model_file']):
        try:
            import llm_local      # imported here, after LASTBOOK_DATA names the library: its state file lives there
            llm_local.mark_checked(note['model_file'], note['model_sha256'])
        except Exception as e:
            print('setup: mark_checked:', type(e).__name__, e, flush=True)
    olds = s.get('old') if isinstance(s.get('old'), list) else []
    if note and note.get('old') and note['old'] not in olds:
        olds = olds + [note['old']]
    if work:
        install.confirm(_pending_file(settings_dir))
        try:
            job = json.loads(_job_file(settings_dir).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            job = None
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
        install._write_json(settings_dir / 'setup.json', s)
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
    ('POST', 'start'): lambda s, b: s.start(b.get('pack'), bool(b.get('ai')), b.get('folder'), repair=bool(b.get('repair')), map_id=b.get('map') or None, address_id=b.get('address_id') or None,
                                         world=bool(b.get('world')), regions=[x for x in (b.get('regions') or []) if isinstance(x, str)][:50]),
    ('POST', 'pause'): lambda s, b: s.pause(),
    ('POST', 'resume'): lambda s, b: s.resume(),
    ('POST', 'cancel'): lambda s, b: s.cancel(),
    ('POST', 'retry'): lambda s, b: s.retry(),
    ('POST', 'use'): lambda s, b: s.use(b.get('path')),
    ('POST', 'finish'): lambda s, b: s.finish(),
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
