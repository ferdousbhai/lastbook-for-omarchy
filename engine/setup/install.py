"""Unpacking a pack and switching to it, so that a working library is never broken.

  <parent>/LastbookData.setup/unpack/LastbookData    the new library, built here from the pack's zip files
  <parent>/LastbookData                             the library Lastbook opens

Every member of a zip is checked before anything is written: a relative name under LastbookData/, no '..', no drive or root,
no symbolic link, no device, and a total no larger than the manifest's installed_bytes. Files are written as <name>.partial and
renamed when whole, so an unpack that stops (a crash, power) carries on where it was. Switching is one rename on one disk.
An upgrade first moves the old library aside to LastbookData.old-<date>, puts the new one in its place, and only then brings
the person's own files across (their maps): apply_pending, called by app/engine_pipe.py before the engine starts.
Each step is written down first, so a switch cut short is finished at the next start. If the engine then cannot open the new
library, the old one is put back, once (roll_back). Once the new one has opened, the old one is deleted (api.opened;
is_old_of says which folders that may be).
"""
import json
import os
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from . import maps

DATA_NAME = 'LastbookData'
CATALOG = Path('library-data') / 'catalog.json'
SLACK = 1.02            # installed_bytes is measured; a zip's members may add a little in directory entries
SPACE_MARGIN = 2 * 10 ** 9
COPY = 4 * 1024 * 1024


class Refused(Exception):
    def __init__(self, code, message, kept=None):
        super().__init__(message)
        self.code, self.message, self.kept = code, message, kept


def is_library(folder):
    try:
        return (Path(folder) / CATALOG).is_file()
    except OSError:
        return False


def _member_parts(info, top_optional=False):
    name = info.filename
    if '\\' in name or '\x00' in name:
        raise Refused('unpack', f'The pack holds a file with a name Lastbook does not accept ({name[:80]!r}).')
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith('/') or (p.parts and ':' in p.parts[0]):
        raise Refused('unpack', f'The pack holds a file with an absolute path ({name[:80]!r}).')
    parts = [x for x in p.parts if x not in ('', '.')]
    if any(x == '..' for x in parts):
        raise Refused('unpack', f'The pack holds a file that would land outside the library ({name[:80]!r}).')
    if top_optional and parts and parts[0] != DATA_NAME:
        parts = [DATA_NAME] + parts     # a state map zip may hold its files at the top, without the LastbookData folder
    if not parts or parts[0] != DATA_NAME:
        raise Refused('unpack', f'The pack holds a file outside its LastbookData folder ({name[:80]!r}).')
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
        raise Refused('unpack', f'The pack holds a link or a special file ({name[:80]!r}).')
    if info.flag_bits & 0x1:
        raise Refused('unpack', 'The pack is encrypted.')
    return parts


MAP_FIXED = {'map-set.json', 'derived/geo/national-status.json', 'derived/geo/socal-cells.json', 'library-data/geography/resources/atlas.sqlite'}
MAP_DIRS = {'', 'derived', 'derived/geo', 'library-data', 'library-data/geography', 'library-data/geography/resources'}
MAP_SQLITE = re.compile(r'derived/geo/[A-Za-z0-9._-]+\.(sqlite|mbtiles)')   # .mbtiles: the world map and its regions (SQLite too)
SQLITE_SIDES = ('-wal', '-shm', '-journal')     # SQLite's own files beside a database: they move with it, or the new one could be damaged


def map_member(rel, is_dir=False):
    """Is this path (under LastbookData) one a state map zip may hold? docs/SETUP-API.md, State maps."""
    rel = rel.strip('/')
    if is_dir:
        return rel in MAP_DIRS
    return rel in MAP_FIXED or bool(MAP_SQLITE.fullmatch(rel))


def check_zip(path, installed_bytes=None, allowed=None):
    """(list of (info, parts), total bytes) once every member has passed, else Refused. allowed(rel, is_dir): a narrower rule."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise Refused('unpack', f'The pack could not be opened as a zip file ({type(e).__name__}).')
    with z:
        members, total, seen = [], 0, set()
        for info in z.infolist():
            parts = _member_parts(info, top_optional=allowed is not None)
            if allowed and not allowed('/'.join(parts[1:]), info.is_dir()):
                raise Refused('unpack', f'The map holds a file that is not part of a map ({info.filename[:80]!r}).')
            key = '/'.join(parts).lower()      # two names that differ only in case would overwrite each other on a case-insensitive disk (a FAT or exFAT drive)
            if key in seen and not info.is_dir():
                raise Refused('unpack', f'The pack holds the same file twice ({info.filename[:80]!r}).')
            seen.add(key)
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Refused('unpack', f'The pack uses a kind of compression Lastbook does not accept ({info.filename[:80]!r}).')
            if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
                raise Refused('unpack', f'The pack holds a stored file whose sizes disagree ({info.filename[:80]!r}).')
            total += info.file_size
            members.append((info, parts))
        # No two entries may share bytes: each one's local header (at least 30 bytes) and data must end before the next begins,
        # and the last before the central directory. Python's zipfile does not check this in every version that ships.
        spans = sorted(z.infolist(), key=lambda i: i.header_offset)
        end_of_data = getattr(z, 'start_dir', None)
        for a, b in zip(spans, spans[1:] + [None]):
            limit = b.header_offset if b is not None else end_of_data
            if limit is not None and a.header_offset + 30 + a.compress_size > limit:
                raise Refused('unpack', f'The pack holds entries that overlap ({a.filename[:80]!r}).')
    if installed_bytes is not None and total > installed_bytes * SLACK + 1024 * 1024:
        raise Refused('unpack', f'The pack unpacks to {total:,} bytes, more than the {installed_bytes:,} it should.')
    return members, total


def unpack(zip_path, into, installed_bytes, control, progress, allowed=None, verify=None):
    """Unpacks into <into>/LastbookData. progress(done, total). control.check() raises to pause or cancel. Resumable.
    allowed: map_member for a state map, which then needs map-set.json instead of a catalog."""
    members, total = check_zip(zip_path, installed_bytes, allowed)
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    done = 0
    with zipfile.ZipFile(zip_path) as z:
        for info, parts in members:
            control.check()
            dest = root.joinpath(*parts)
            if root not in dest.parents and dest != root:
                raise Refused('unpack', 'The pack holds a file that would land outside the library.')
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink():
                raise Refused('unpack', 'A link appeared in the unpack folder.')
            if dest.is_file() and dest.stat().st_size == info.file_size:
                done += info.file_size          # whole from an earlier run
                progress(done, total)
                continue
            part = dest.with_name(dest.name + '.partial')
            with z.open(info) as src, open(part, 'wb') as out:
                written = 0
                while True:
                    control.check()
                    b = src.read(COPY)
                    if not b:
                        break
                    written += len(b)
                    if written > info.file_size:        # stop at once, not after: a member never grows past its declared size
                        raise Refused('unpack', f'A file in the pack is larger than it says ({info.filename[:80]!r}).')
                    out.write(b)
                    done += len(b)
                    progress(done, total)
            if part.stat().st_size != info.file_size:
                raise Refused('unpack', f'A file in the pack came out the wrong size ({info.filename[:80]!r}).')
            os.replace(part, dest)
            t = time.mktime(info.date_time + (0, 0, -1))
            os.utime(dest, (t, t))
    lib = root / DATA_NAME
    if verify:
        verify(lib)
    elif allowed:
        check_map(lib)
    elif not is_library(lib):
        raise Refused('unpack', 'The unpacked pack has no library catalog in it.')
    return lib


# ------------------------------------------------------------------------------------------------------------ space --
def need_bytes(pack, have_files=()):
    """Room needed on the target disk: what is still to download, plus the unpacked size, plus SPACE_MARGIN."""
    n = SPACE_MARGIN
    if pack:
        n += sum(f['size'] for f in pack['files'] if f['name'] not in have_files) + (pack.get('installed_bytes') or 0)
    return n


# -------------------------------------------------------------------------------------------------------- switching --
def _write_json(path, d):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(d, indent=1), encoding='utf-8')
    os.replace(tmp, path)


def old_name(target):
    base = target.with_name(f'{target.name}.old-{time.strftime("%Y%m%d")}')
    p, i = base, 2
    while p.exists():
        p = base.with_name(f'{base.name}-{i}')
        i += 1
    return p


def is_old_of(path, lib):
    """Whether `path` is a folder switch() moved aside for the library `lib`, and so may be deleted: a real folder (never a link)
    directly beside the library, named exactly as old_name names one. Nothing else is ever deleted as an old library."""
    if not isinstance(path, str) or not path:
        return False
    p, lib = Path(path), Path(lib)
    return bool(p.name.startswith(lib.name) and re.fullmatch(r'\.old-\d{8}(-\d+)?', p.name[len(lib.name):])
                and _real(p.parent) == _real(lib.parent) and not p.is_symlink() and p.is_dir())


def stage_switch(pend, new_lib, target, kind=None):
    """Writes what the switch will do to `pend`, a file in the app's own settings folder (never the work folder), for
    apply_pending. Nothing is renamed yet."""
    d = {'new': str(new_lib), 'target': str(target), 'written': time.time()}
    if kind:
        d['kind'] = kind
    Path(pend).parent.mkdir(parents=True, exist_ok=True)
    _write_json(pend, d)
    return d


def _holds_theirs(folder):
    """Whether `folder` still has anything carry_over would have taken: a file in models/, an address index or a map of the person's."""
    folder = Path(folder)
    models = folder / 'models'
    if models.is_dir() and any(f.is_file() and not f.name.endswith('.partial') for f in models.iterdir()):
        return True
    geo = folder / 'derived' / 'geo'
    if (geo / 'addresses').is_dir() and any(re.fullmatch(r'[A-Z]{2}\.(sqlite|json)', f.name) for f in (geo / 'addresses').iterdir()):
        return True
    return geo.is_dir() and any(f.is_file() and maps.ours(f.name) for f in geo.iterdir())


def carry_over(old, new):
    """What belongs to the person, not the pack, goes with them: address indexes, the world map and its regions
    (engine/setup/maps.py), and any file in models/ (an earlier version's local AI download: Lastbook no longer uses
    it, and never deletes it)."""
    old, new = Path(old), Path(new)
    moved = []
    src = old / 'models'
    if src.is_dir():
        (new / 'models').mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            dest = new / 'models' / f.name
            if f.is_file() and not f.name.endswith('.partial') and not dest.exists():      # a copy cut short is nobody's file
                os.replace(f, dest)
                moved.append(dest)
    addresses = old / 'derived' / 'geo' / 'addresses'
    if addresses.is_dir():
        dest_dir = new / 'derived' / 'geo' / 'addresses'
        dest_dir.mkdir(parents=True, exist_ok=True)
        for source in addresses.iterdir():
            dest = dest_dir / source.name
            if source.is_file() and re.fullmatch(r'[A-Z]{2}\.(sqlite|json)', source.name) and not dest.exists():
                os.replace(source, dest)
                moved.append(dest)
    geo = old / 'derived' / 'geo'
    if geo.is_dir():
        for source in geo.iterdir():
            dest = new / 'derived' / 'geo' / source.name
            if source.is_file() and maps.ours(source.name) and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, dest)
                moved.append(dest)
    return moved


def switch(new_lib, target, old=None, resume=False):
    """The renames, then the person's own files brought across from the old library (carry_over). Returns the old library's
    new name, or None when there was none. Raises Refused with things as they were: nothing of the person's is moved until
    both renames have been made, so a rename that fails leaves the old library whole, maps in it.

    old: the name the old library goes to, when the caller chose and saved it beforehand (apply_pending). resume: this
    switch was begun before and cut short (the program stopped), so whatever of it is already done is taken as done and
    the rest is carried out. Raises Refused with code 'carry' if the new library is in place but something could not be
    brought across; .kept is then the old library, which still holds it."""
    new_lib, target = Path(new_lib), Path(target)
    old = Path(old) if old else None
    if is_library(new_lib):
        if target.exists():
            old = old or old_name(target)
            if os.path.lexists(old):
                raise Refused('activate', 'Lastbook could not move the old library aside: its new name is already taken.')
            try:
                os.replace(target, old)
            except OSError as e:
                raise Refused('activate', f'Lastbook could not move the old library aside ({type(e).__name__}). Close anything using it, then try again.')
        elif not (resume and old and old.exists()):
            old = None      # nothing was there: a first install
        try:
            os.replace(new_lib, target)
        except OSError as e:
            if old:
                os.replace(old, target)
            raise Refused('activate', f'Lastbook could not put the new library in place ({type(e).__name__}).')
    elif resume and is_library(target):
        old = old if old and old.exists() else None     # both renames were made before the program stopped
    else:
        raise Refused('activate', 'The new library is not complete.')
    if old and is_library(old):
        try:
            carry_over(old, target)
        except OSError as e:
            raise Refused('carry', f'The new library is in place, but Lastbook could not bring everything across from the old one '
                                   f'({type(e).__name__}). The old one was kept as {old.name}.', kept=old)
    return old


def is_failed_of(path, lib):
    """Whether `path` is a library roll_back set aside for `lib`, and so may be deleted: a real folder (never a link)
    directly beside the library, named exactly as roll_back names one (<name>.failed, <name>.failed-<n>)."""
    if not isinstance(path, str) or not path:
        return False
    p, lib = Path(path), Path(lib)
    return bool(p.name.startswith(lib.name) and re.fullmatch(r'\.failed(-\d+)?', p.name[len(lib.name):])
                and _real(p.parent) == _real(lib.parent) and not p.is_symlink() and p.is_dir())


def roll_back(pend, work, kept=None):
    """The library a switch put in place did not open: the old one goes back, with what was carried across from it, and the
    one that failed is kept beside it as <name>.failed. Returns a sentence saying so, or None when there is nothing to go
    back to (no switch waiting to be confirmed, or no old library). It happens once: the pending switch is then over.
    kept: a list the failed library's folder is added to once everything of the person's has come back out of it, so
    the caller can list it for deletion (api.opened); one that still holds something of theirs is not added."""
    pend = Path(pend)
    try:
        d = json.loads(pend.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not _pending_ok(d, work) or d.get('kind') == 'map' or not d.get('switched') or not d.get('old'):
        return None
    target, old = Path(d['target']), Path(d['old'])
    if not is_old_of(str(old), target) or not is_library(old):
        return None
    broken, i = target.with_name(target.name + '.failed'), 2
    while os.path.lexists(broken):
        broken, i = target.with_name(f'{target.name}.failed-{i}'), i + 1
    os.replace(target, broken)
    os.replace(old, target)
    try:
        carry_over(broken, target)      # the maps and the address indexes went into the new one: they go back too
        # Listed for deletion only if nothing of the person's stayed behind (carry_over leaves a file whose name the
        # restored library already has).
        if kept is not None and not _holds_theirs(broken):
            kept.append(str(broken))
    except OSError as e:
        print('setup: not everything came back from', broken.name, type(e).__name__, e, flush=True)
    confirm(pend)
    return 'The new library did not open, so Lastbook put the old one back.'


# ----------------------------------------------------------------------------------------------------------- state maps --
def map_set(lib):
    """The map set's map-set.json ({name, files: {path: bytes}}), or None."""
    try:
        d = json.loads((Path(lib) / 'map-set.json').read_text(encoding='utf-8'))
        return d if isinstance(d, dict) and isinstance(d.get('files'), dict) else None
    except (OSError, ValueError):
        return None


def map_name(lib):
    """'base', 'us' or a state code: from map-set.json, else the pack's own pack.json, else 'base'."""
    d = map_set(lib)
    if d and d.get('name'):
        return str(d['name'])
    try:
        return str(json.loads((Path(lib) / 'pack.json').read_text(encoding='utf-8')).get('map') or 'base')
    except (OSError, ValueError, AttributeError):
        return 'base'


def check_map(lib):
    """Every file map-set.json lists is there at its size, and each is a map file."""
    d = map_set(lib)
    if not d:
        raise Refused('unpack', 'The map has no map-set.json in it.')
    for rel, size in d['files'].items():
        if not map_member(rel) or '..' in Path(rel).parts:
            raise Refused('unpack', f'The map lists a file that is not part of a map ({rel[:80]!r}).')
        p = Path(lib) / rel
        if not p.is_file() or p.stat().st_size != size:
            raise Refused('unpack', f'The map’s {rel} is missing or the wrong size.')
    return d


def map_files(lib):
    """The files of a library's map set as they are on disk, relative to it, with SQLite's side files and map-set.json."""
    lib = Path(lib)
    d = map_set(lib)
    rels = {r for r in d['files'] if map_member(r) and '..' not in Path(r).parts} if d else set()    # the fixed list only
    geo = lib / 'derived' / 'geo'
    if geo.is_dir():
        rels |= {f'derived/geo/{p.name}' for p in geo.iterdir()
                 if p.is_file() and map_member(f'derived/geo/{p.name}') and not maps.ours(p.name)}   # the world map is the person's
    rels |= {r for r in MAP_FIXED if (lib / r).is_file()}
    out = set()
    for r in rels:
        for side in ('',) + (SQLITE_SIDES if r.endswith(('.sqlite', '.mbtiles')) else ()):
            if (lib / (r + side)).is_file():
                out.add(r + side)
    return sorted(out)


def map_bytes(lib):
    """Space needed to keep the current detailed map during a content refresh."""
    total = 0
    for rel in map_files(lib):
        try:
            total += (Path(lib) / rel).stat().st_size
        except OSError:
            continue
    return total


def copy_map(old, new):
    """Keep an installed map while refreshing the same library's content."""
    old, new = Path(old), Path(new)
    for rel in map_files(new):
        os.unlink(new / rel)
    for rel in map_files(old):
        destination = new / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old / rel, destination)


def swap_map(lib, new, old_dir):
    """Puts the map set in `new` (a LastbookData holding only map files) into `lib`; the old set's files go to old_dir first.
    Renames only, on one disk. On any failure what was moved is put back and Refused is raised."""
    lib, new, old_dir = Path(lib), Path(new), Path(old_dir)
    check_map(new)
    moved_out, moved_in = [], []
    try:
        for rel in map_files(lib):
            dest = old_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(lib / rel, dest)
            moved_out.append(rel)
        for rel in map_files(new):
            dest = lib / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(new / rel, dest)
            moved_in.append(rel)
    except OSError as e:
        for rel in moved_in:
            os.replace(lib / rel, new / rel)
        for rel in moved_out:
            os.replace(old_dir / rel, lib / rel)
        raise Refused('activate', f'Lastbook could not put the new map in place ({type(e).__name__}).')


def replace_map_in(staged, new):
    """A pack being installed with a state map: the map's files replace the pack's own map set inside the staged library."""
    staged, new = Path(staged), Path(new)
    check_map(new)
    for rel in map_files(staged):
        os.unlink(staged / rel)
    for rel in map_files(new):
        (staged / rel).parent.mkdir(parents=True, exist_ok=True)
        os.replace(new / rel, staged / rel)


def _apply_map(work, pend, d, target, note):
    """A state map staged for the open library: swap its files in; if the library then does not check out, swap the old ones back."""
    old_dir = Path(work) / 'map-old'
    note['map_old'] = str(old_dir)
    if not d.get('switched'):
        try:
            swap_map(target, d['new'], old_dir)
        except Refused as e:
            print('setup: map switch failed:', e.message, flush=True)
            pend.unlink()
            note['error'] = e.message
            return (target if is_library(target) else None), note
        d['switched'] = True
        _write_json(pend, d)
    try:
        check_map(target)
        ok = is_library(target)
    except Refused:
        ok = False
    if not ok and old_dir.is_dir():
        broken = Path(work) / 'map-failed'
        shutil.rmtree(broken, ignore_errors=True)
        for rel in map_files(target):
            (broken / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(target / rel, broken / rel)
        for root, _, files in os.walk(old_dir):
            for f in files:
                rel = Path(root, f).relative_to(old_dir)
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                os.replace(Path(root, f), target / rel)
        note['error'] = 'The new map did not open, so Lastbook put the old one back.'
    return target if is_library(target) else None, note


def _real(p):
    return os.path.realpath(str(p))


def _pending_ok(d, work):
    """Only what setup itself stages: the work folder's own library, its own staged copy, and a LastbookData.old-* beside it.
    Compared as real paths. Anything else is not a pending switch."""
    if not isinstance(d, dict) or not isinstance(d.get('target'), str) or not isinstance(d.get('new'), str):
        return False
    if not Path(work).name.endswith('.setup') or len(Path(work).name) <= len('.setup'):
        return False
    target = Path(work).with_name(Path(work).name[:-len('.setup')])     # the library the work folder belongs to, whatever its name
    new = Path(work) / ('unpack-map' if d.get('kind') == 'map' else 'unpack') / DATA_NAME
    if d.get('kind') not in (None, 'map') or _real(d['target']) != _real(target) or _real(d['new']) != _real(new):
        return False
    old = d.get('old')
    if old is not None and not (isinstance(old, str) and _real(Path(old).parent) == _real(target.parent)
                                and Path(old).name.startswith(target.name + '.old-')):
        return False
    return True


def apply_pending(pend, work):
    """Runs at startup, before the engine opens anything. Returns (library folder or None, a note dict or None).
    pend: the pending switch in the app's own settings folder. If one was staged, do it; if the new library then does not
    open, put the old one back. A file that is not exactly what setup stages for this work folder is removed, not obeyed."""
    pend, work = Path(pend), Path(work)
    try:
        d = json.loads(pend.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, None
    if not _pending_ok(d, work):
        print('setup: a pending switch that does not match its work folder was ignored', flush=True)
        try:
            pend.unlink()
        except OSError:
            pass
        return None, None
    target = Path(d['target'])
    note = {'target': str(target), 'old': None}
    if d.get('kind') == 'map':
        return _apply_map(work, pend, d, target, note)
    if not d.get('switched'):
        # What is about to happen is written down before any rename, so a stop at any point leaves a state the next start
        # finishes: `switching` with the old library's new name first, `switched` once everything is across.
        begun = bool(d.get('switching'))
        if not begun:
            d['switching'], d['old'] = True, str(old_name(target)) if target.exists() else None
            _write_json(pend, d)
        try:
            old = switch(d['new'], target, d.get('old'), resume=begun)
        except Refused as e:
            print('setup: switch failed:', e.message, flush=True)
            note['error'] = e.message
            if e.code != 'carry':
                pend.unlink()
                return (target if is_library(target) else None), note
            old = None      # the old library still holds something of the person's: it is kept, never listed for deletion
        d['switched'], d['old'] = True, old and str(old)
        _write_json(pend, d)
    note['old'] = d.get('old')
    if not is_library(target):      # cannot happen after switch(); the engine's own failure to open it is roll_back's, from api.rolled_back
        kept = []
        said = roll_back(pend, work, kept)
        if said:
            rolled_back_note(note, said)
            note['failed'] = kept
    return target if is_library(target) else None, note


def rolled_back_note(note, said):
    """The note once the old library is back: nothing to delete. The library set aside is deleted at a later start, not
    this one (api.opened)."""
    note.update(error=said, old=None, rolled_back=True)
    return note


def confirm(pend):
    """The new library opened: the switch is done, and there is no going back to the old one (api.opened then deletes it)."""
    try:
        Path(pend).unlink()
    except OSError:
        pass
