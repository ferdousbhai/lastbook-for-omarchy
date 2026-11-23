"""Lastbook desktop app: where the library is, and setup's hooks. The native window (native/) runs the engine through
app/engine_pipe.py, which uses these.

The data folder comes from --data, then LASTBOOK_DATA, then the person's remembered selection, then the app's own data
folder. Existing libraries elsewhere are opened only when the person chooses them. If no library is available, the engine
starts in setup (engine/setup/) to download one or choose an existing library.
Output goes to <data>/derived/logs/lastbook.log, rotated by size, because a packaged app has no console.
"""
import json
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

# The installed package, which alone has a lib/ folder. Worked out here, not from engine.paths: that module fixes the data
# folder when it is imported, and this one is imported before LASTBOOK_DATA is set.
INSTALLED = (Path(__file__).resolve().parents[1] / 'lib').is_dir()
LOG_LIMIT = 5 * 1024 * 1024
LOG_BACKUPS = 3


DATA_NAME = 'LastbookData'
CATALOG = os.path.join('library-data', 'catalog.json')


def version():
    v = Path(__file__).resolve().parents[1] / 'VERSION'
    return v.read_text().strip() if v.exists() else None


def app_folders():
    """(settings, this computer's app data): XDG's config and data folders."""
    home = Path.home()
    return (Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config') / 'Lastbook',
            Path(os.environ.get('XDG_DATA_HOME') or home / '.local' / 'share') / 'Lastbook')


def remembered():
    try:
        return Path(json.loads((app_folders()[0] / 'settings.json').read_text(encoding='utf-8'))['library'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remember(folder):
    """Written whole or not at all: to a file beside it, sent to the disk, then renamed over it. Written in place, a full
    disk or a power cut mid-write would leave an empty settings file, and the library chosen before would be forgotten."""
    settings = app_folders()[0] / 'settings.json'
    tmp = settings.with_name(f'.settings.json.{os.getpid()}')
    try:
        settings.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'library': str(folder)}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings)
    except OSError as e:   # it still opens this time; it asks again next time
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print('could not remember the library:', e, flush=True)


def link_library(data):
    """<app data>/library points at the open library's files (LastbookData/library-data), wherever the library lives: one
    short, stable path for the person and for an agent's grep, cat and jq. Only ever a link: a real file or folder of
    that name is left alone. Never stops the app."""
    link, target = app_folders()[1] / 'library', Path(data) / 'library-data'
    try:
        if os.path.lexists(link) and not link.is_symlink():
            return
        if link.is_symlink() and os.readlink(link) == str(target):
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(f'.library.{os.getpid()}')
        if os.path.lexists(tmp):
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, link)
    except OSError as e:
        print('could not link the library:', e, flush=True)


def writable(folder):
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def shown(folder):
    """A folder as the person knows it: its name, not its whole path."""
    return folder.name or str(folder)


def check(folder):
    """(the library, None) if folder is LastbookData or holds it, else (None, why not) in a plain sentence."""
    for f in (folder, folder / DATA_NAME):
        try:
            if not (f / CATALOG).is_file():
                continue
        except OSError:
            continue
        if writable(f):
            return f, None
        return None, f'Lastbook cannot save in “{shown(f)}”. Move the LastbookData folder somewhere you can save to, then choose it there.'
    return None, f'“{shown(folder)}” is not a Lastbook library. Choose the folder called LastbookData, inside the library you unzipped.'


def data_folder(arg):
    """The folder named by --data or LASTBOOK_DATA as given, else the first library found, else None."""
    if arg:
        return Path(arg)
    if os.environ.get('LASTBOOK_DATA'):
        return Path(os.environ['LASTBOOK_DATA'])
    return (libraries_here() or [None])[0]


# ---------------------------------------------------------------------------------------------------------------- setup --
def libraries_here():
    """The libraries of the remembered selection and app-owned data only."""
    out = []
    for folder in (remembered(), app_folders()[1] / DATA_NAME):
        if folder is not None and (found := check(folder)[0]) and found not in out:
            out.append(found)
    return out


def setup_options():
    """Setup's hooks. The window that runs setup supplies on_finish (app/engine_pipe.py)."""
    # default_parent: where setup puts LastbookData unless the person chooses, the app's own data folder
    return {'settings_dir': app_folders()[0], 'default_parent': app_folders()[1], 'look_for_libraries': libraries_here}


class Log(logging.handlers.RotatingFileHandler):
    """Stands in for stdout and stderr: keeps lastbook.log and LOG_BACKUPS older copies, each up to LOG_LIMIT."""
    terminator = ''

    def write(self, text):
        self.handle(logging.makeLogRecord({'msg': text}))
        return len(text)

    def isatty(self):
        return False

    def handleError(self, record):   # this is where errors would be printed
        pass


def open_log(data):
    """The installed app's output goes to the log. Run from source, it stays on the terminal and no log file is made."""
    if not INSTALLED:
        return
    logs = data / 'derived' / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = Log(logs / 'lastbook.log', maxBytes=LOG_LIMIT, backupCount=LOG_BACKUPS, encoding='utf-8', errors='replace')

