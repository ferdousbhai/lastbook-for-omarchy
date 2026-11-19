"""Lastbook desktop app: where the library is, and setup's hooks. The native window (native/) runs the engine through
app/engine_pipe.py, which uses these.

The data folder comes from --data, then LASTBOOK_DATA, then the person's remembered selection, then the app's own data
folder. Existing libraries elsewhere are opened only when the person chooses them. If no library is available, the engine
starts in setup (engine/setup/) to download one or choose an existing library.
Output goes to <data>/derived/logs/lastbook.log, rotated by size, because a packaged app has no console.

Sharing with a phone, a chosen port and running with no window all belong to the server version: python server.py.
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

FROZEN = bool(getattr(sys, 'frozen', False))
if not FROZEN:
    sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
LOG_LIMIT = 5 * 1024 * 1024
LOG_BACKUPS = 3


DATA_NAME = 'LastbookData'
CATALOG = os.path.join('library-data', 'catalog.json')


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
    settings = app_folders()[0] / 'settings.json'
    try:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'library': str(folder)}), encoding='utf-8')
    except OSError as e:   # it still opens this time; it asks again next time
        print('could not remember the library:', e, flush=True)


def places():
    """The remembered selection and app-owned data only."""
    yield remembered()
    yield app_folders()[1] / DATA_NAME


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
    for folder in places():
        if folder is not None and (found := check(folder)[0]):
            return found
    return None


# ---------------------------------------------------------------------------------------------------------------- setup --
def default_parent():
    """Where setup puts LastbookData unless the person chooses: the app's own data folder."""
    return app_folders()[1]


def libraries_here():
    out = []
    for folder in places():
        if folder is not None and (found := check(folder)[0]) and found not in out:
            out.append(found)
    return out


def setup_options():
    """Setup's hooks. The window that runs setup supplies on_finish (app/engine_pipe.py)."""
    return {'settings_dir': app_folders()[0], 'default_parent': default_parent(), 'look_for_libraries': libraries_here}


class RotatingLog:
    """Stands in for stdout and stderr: keeps lastbook.log and a few older copies, each up to LOG_LIMIT."""
    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.file = open(self.path, 'ab')

    def write(self, text):
        data = text.encode('utf-8', 'replace')
        with self.lock:
            if self.file.tell() + len(data) > LOG_LIMIT:
                self.rotate()
            self.file.write(data)
            self.file.flush()
        return len(text)

    def rotate(self):
        self.file.close()
        try:
            for i in range(LOG_BACKUPS - 1, 0, -1):
                older = self.path.with_name(f'{self.path.name}.{i}')
                if older.exists():
                    os.replace(older, self.path.with_name(f'{self.path.name}.{i + 1}'))
            os.replace(self.path, self.path.with_name(f'{self.path.name}.1'))
            self.file = open(self.path, 'ab')
        except OSError:  # could not rotate: start over rather than grow without limit
            self.file = open(self.path, 'wb')

    def flush(self):
        pass

    def isatty(self):
        return False


def open_log(data):
    logs = data / 'derived' / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    log = RotatingLog(logs / 'lastbook.log')
    if sys.stdout is None or FROZEN:
        sys.stdout = log
    if sys.stderr is None or FROZEN:
        sys.stderr = log
    return logs

