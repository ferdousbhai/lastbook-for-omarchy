"""Where things live.

APP   read-only program files (web/, content/). In the packaged app this is the package's own folder (sys._MEIPASS, set by app/engine_pipe.py).
DATA  the writable data folder holding library-data/ and derived/. Set with LASTBOOK_DATA (or server.py --data).
With LASTBOOK_DATA unset, DATA == APP: the same folder layout as running server.py from the source tree.
"""
import fcntl
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, 'frozen', False))
APP = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('LASTBOOK_DATA') or APP).resolve()
LIB = (DATA / 'library-data').resolve()
os.environ['LASTBOOK_LIBRARY'] = str(LIB)  # engine/library.py reads this


def ro_uri(path):
    """Read-only SQLite URI. A plain f'file:{path}' breaks on '#', '%' and '?' in a folder name."""
    return Path(path).resolve().as_uri() + '?mode=ro'


def try_lock(f):
    """Non-blocking exclusive lock on an open file; raises OSError if another handle holds it."""
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(f):
    fcntl.flock(f, fcntl.LOCK_UN)
