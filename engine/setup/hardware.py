"""What this computer has that setup needs to know: the room on each disk."""
import os
import shutil
from pathlib import Path


def existing(path):
    """path, or the nearest folder above it that exists."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def disk_of(path):
    """(free, total) on the disk that holds path, or the nearest folder above it that exists."""
    u = shutil.disk_usage(existing(path))
    return u.free, u.total


def candidate_disks(extra=()):
    """Disk capacity for app-owned or explicitly selected paths only."""
    places = list(map(Path, extra))
    seen, out = set(), []
    for p in places:
        try:
            q = existing(p)
            dev = os.stat(q).st_dev
            if dev in seen:
                continue
            free, total = disk_of(q)
        except OSError:
            continue
        seen.add(dev)
        out.append({'path': str(p), 'free_bytes': free, 'total_bytes': total})
    return out
