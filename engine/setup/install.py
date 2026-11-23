"""Unpacking a pack and switching to it, so that a working library is never broken.

  <parent>/LastbookData.setup/unpack/LastbookData    the new library, built here from the pack's zip files
  <parent>/LastbookData                             the library Lastbook opens

Every member of a zip is checked before anything is written: a relative name under LastbookData/, no '..', no drive or root,
no symbolic link, no device, and a total no larger than the manifest's installed_bytes. Upstream's US-only map data (DROPPED)
is not written at all. Files are written as <name>.partial and
renamed when whole, so an unpack that stops (a crash, power) carries on where it was. Switching is one rename on one disk.
An upgrade first moves the old library aside to LastbookData.old-<date>, puts the new one in its place, and only then brings
the person's own files across (the maps made on this computer): apply_pending, called by app/engine_pipe.py before the engine starts.
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


def _member_parts(info):
    name = info.filename
    if '\\' in name or '\x00' in name:
        raise Refused('unpack', f'The pack holds a file with a name Lastbook does not accept ({name[:80]!r}).')
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith('/') or (p.parts and ':' in p.parts[0]):
        raise Refused('unpack', f'The pack holds a file with an absolute path ({name[:80]!r}).')
    parts = [x for x in p.parts if x not in ('', '.')]
    if any(x == '..' for x in parts):
        raise Refused('unpack', f'The pack holds a file that would land outside the library ({name[:80]!r}).')
    if not parts or parts[0] != DATA_NAME:
        raise Refused('unpack', f'The pack holds a file outside its LastbookData folder ({name[:80]!r}).')
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
        raise Refused('unpack', f'The pack holds a link or a special file ({name[:80]!r}).')
    if info.flag_bits & 0x1:
        raise Refused('unpack', 'The pack is encrypted.')
    return parts


# Upstream's US-only map data, which upstream's packs ship and which this fork never reads (its maps are made on this
# computer: engine/setup/maps.py). Paths under LastbookData; a folder goes whole, and a database with its SQLite side files.
# Not unpacked (check_zip), taken out of a library that has them (drop_upstream_maps), and not looked for by repair (dropped).
DROPPED = ('derived/geo/national.sqlite', 'derived/geo/national-status.json', 'derived/geo/map.sqlite', 'derived/geo/places.sqlite',
           'derived/geo/water.sqlite', 'derived/geo/terrain.sqlite', 'derived/geo/crops.sqlite', 'derived/geo/socal-cells.json',
           'derived/geo/addresses', 'library-data/geography/resources/atlas.sqlite', 'map-set.json')
SQLITE_SIDES = ('-wal', '-shm', '-journal')


def dropped(rel):
    """Whether this path under LastbookData is one of DROPPED, or inside one, or a SQLite side file of one."""
    rel = rel.strip('/')
    for side in SQLITE_SIDES:
        if rel.endswith(side):
            rel = rel[:-len(side)]
    return any(rel == d or rel.startswith(d + '/') for d in DROPPED)


def drop_upstream_maps(lib):
    """Removes DROPPED from a library: one just unpacked, or one an earlier version installed. Safe at every start: what is
    not there is skipped, a link is removed as a link (never followed), and what cannot be removed (a library on a disk that
    is read-only) is left as it is. Returns the bytes freed."""
    lib, freed = Path(lib), 0
    if not is_library(lib):
        return 0
    for rel in DROPPED:
        for p in [lib / rel] + [lib / (rel + side) for side in SQLITE_SIDES]:
            try:
                st = p.lstat()
            except OSError:
                continue
            try:
                if stat.S_ISDIR(st.st_mode):
                    freed += _tree_bytes(p)
                    shutil.rmtree(p)
                else:
                    p.unlink()
                    freed += st.st_size
            except OSError as e:
                print('setup: could not remove', rel, type(e).__name__, flush=True)
    return freed


def _tree_bytes(folder):
    n = 0
    for root, _, files in os.walk(folder):
        for f in files:
            try:
                n += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return n


def check_zip(path, installed_bytes=None):
    """(list of (info, parts) to write, their total bytes) once every member has passed, else Refused. Members that are
    DROPPED are checked like the rest and left out of both."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise Refused('unpack', f'The pack could not be opened as a zip file ({type(e).__name__}).')
    with z:
        members, total, kept, seen = [], 0, 0, set()
        for info in z.infolist():
            parts = _member_parts(info)
            key = '/'.join(parts).lower()      # two names that differ only in case would overwrite each other on a case-insensitive disk (a FAT or exFAT drive)
            if key in seen and not info.is_dir():
                raise Refused('unpack', f'The pack holds the same file twice ({info.filename[:80]!r}).')
            seen.add(key)
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Refused('unpack', f'The pack uses a kind of compression Lastbook does not accept ({info.filename[:80]!r}).')
            if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
                raise Refused('unpack', f'The pack is damaged: the size of a file in it is wrong ({info.filename[:80]!r}). Download it again.')
            total += info.file_size
            if not dropped('/'.join(parts[1:])):
                kept += info.file_size
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
    return members, kept


def unpack(zip_path, into, installed_bytes, control, progress):
    """Unpacks into <into>/LastbookData, without what is DROPPED. progress(done, total). control.check() raises to pause or
    cancel. Resumable."""
    members, total = check_zip(zip_path, installed_bytes)
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
    if not is_library(lib):
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


def stage_switch(pend, new_lib, target):
    """Writes what the switch will do to `pend`, a file in the app's own settings folder (never the work folder), for
    apply_pending. Nothing is renamed yet."""
    d = {'new': str(new_lib), 'target': str(target), 'written': time.time()}
    Path(pend).parent.mkdir(parents=True, exist_ok=True)
    _write_json(pend, d)
    return d


def _holds_theirs(folder):
    """Whether `folder` still has a map carry_over would have taken."""
    geo = Path(folder) / 'derived' / 'geo'
    return geo.is_dir() and any(f.is_file() and maps.ours(f.name) for f in geo.iterdir())


def _foreign(folder):
    """A folder at the top of `folder` that no pack ships, so someone put it there (an upstream version's models/, say):
    the name of the first, or None. Nothing of it is carried or deleted."""
    try:
        return next((p.name for p in sorted(Path(folder).iterdir()) if p.is_dir() and p.name not in ('library-data', 'derived')), None)
    except OSError:
        return None


def carry_over(old, new):
    """What belongs to the person, not the pack, goes with them: the world map, its place search and its regions
    (engine/setup/maps.py)."""
    old, new = Path(old), Path(new)
    moved = []
    geo = old / 'derived' / 'geo'
    if geo.is_dir():
        for source in geo.iterdir():
            dest = new / 'derived' / 'geo' / source.name
            if source.is_file() and maps.ours(source.name) and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, dest)
                moved.append(dest)
    # The meaning-search index took this computer hours to build: it goes with them too, both files or neither. Each
    # document carries its own signature there, so whatever the new library changed is embedded again and no more.
    index, there = old / 'derived' / 'index', new / 'derived' / 'index'
    pair = ('semantic.sqlite', 'vectors.f16')
    if all((index / n).is_file() for n in pair) and not any((there / n).exists() for n in pair) \
            and not any((index / ('semantic.sqlite' + tail)).exists() and (index / ('semantic.sqlite' + tail)).stat().st_size for tail in ('-wal', '-journal')):
        there.mkdir(parents=True, exist_ok=True)
        for n in pair:
            os.replace(index / n, there / n)
            moved.append(there / n)
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
        extra = _foreign(old)
        if extra:       # not this program's to move or to delete: the old library stays, and the person is told
            raise Refused('carry', f'The new library is in place. The old one was kept as {old.name}, because it holds a folder '
                                   f'Lastbook does not use ({extra}). Take what you want from it, then delete it.', kept=old)
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
    if not _pending_ok(d, work) or not d.get('switched') or not d.get('old'):
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
        carry_over(broken, target)      # the maps went into the new one: they go back too
        # Listed for deletion only if nothing of the person's stayed behind (carry_over leaves a file whose name the
        # restored library already has).
        if kept is not None and not _holds_theirs(broken):
            kept.append(str(broken))
    except OSError as e:
        print('setup: not everything came back from', broken.name, type(e).__name__, e, flush=True)
    confirm(pend)
    return 'The new library did not open, so Lastbook put the old one back.'


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
    new = Path(work) / 'unpack' / DATA_NAME
    if 'kind' in d or _real(d['target']) != _real(target) or _real(d['new']) != _real(new):
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
    if not target.parent.exists():
        # The disk that holds the library is not there (an external drive not plugged in). Nothing is wrong with what was
        # staged: it waits for a start that can see it.
        print('setup: the pending switch waits: its folder is not there', flush=True)
        return None, None
    note = {'target': str(target), 'old': None}
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
                if is_library(d['new']):
                    # The download is whole and the old library was left (or put back) where it was: the switch stays
                    # pending, as it was before this start, so "try again" has something to try with.
                    d.pop('switching', None)
                    d.pop('old', None)
                    _write_json(pend, d)
                else:
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
