"""Where things live.

APP   read-only program files (content/). This folder's parent: the source tree, or the installed package's own folder.
INSTALLED  true in the installed package, which alone has a lib/ folder (its Python packages).
DATA  the writable data folder holding library-data/ and derived/. Set with LASTBOOK_DATA (app/engine_pipe.py does, from --data or the library it finds).
With LASTBOOK_DATA unset, DATA == APP.
"""
import os
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
INSTALLED = (APP / 'lib').is_dir()
DATA = Path(os.environ.get('LASTBOOK_DATA') or APP).resolve()
LIB = (DATA / 'library-data').resolve()


def ro_uri(path):
    """Read-only SQLite URI. A plain f'file:{path}' breaks on '#', '%' and '?' in a folder name."""
    return Path(path).resolve().as_uri() + '?mode=ro'
