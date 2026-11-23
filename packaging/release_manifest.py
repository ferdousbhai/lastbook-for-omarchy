#!/usr/bin/env python3
"""Rewrites the manifest the package ships: packaging/arch/manifest-v1.json (docs/SETUP-API.md, "The manifest").

  python3 -I -B packaging/release_manifest.py [--packs manifest.json] [--out packaging/arch/manifest-v1.json]

Packs are upstream Lastbook's: they are copied from --packs, by default the shipped manifest
itself (or pass a copy of https://downloads.lastbook.org/manifest-v1.json to take upstream's newer list). Upstream's US
state maps and address indexes in it are left out: this fork makes its own maps (engine/setup/maps.py).
Every URL must be on an allowed host (engine/setup/download.py) or the build stops. The PKGBUILD installs the output as setup/manifest-v1.json.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from engine.setup import download, manifest        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    shipped = str(ROOT / 'packaging' / 'arch' / 'manifest-v1.json')
    ap.add_argument('--packs', default=shipped)
    ap.add_argument('--out', default=shipped)
    a = ap.parse_args()
    try:
        src = json.loads(Path(a.packs).read_text(encoding='utf-8'))
    except FileNotFoundError:
        src = {'version': 1, 'packs': []}
    packs = [{k: v for k, v in p.items() if k != 'map'} for p in src.get('packs') or [] if isinstance(p, dict)]
    if not packs:
        sys.exit(f'no packs: {a.packs} is missing or empty')
    out = {'version': 1, 'published': src.get('published') or time.strftime('%Y-%m-%d'), 'packs': packs}
    if manifest.validate(out) is None:
        bad = [f.get('url') for p in packs for f in p.get('files', []) if not download.url_allowed(f.get('url'))]
        sys.exit('the manifest does not pass engine/setup/manifest.validate' + (f'; addresses not allowed: {bad}' if bad else ''))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=2) + '\n', encoding='utf-8')
    print(f'{a.out}: {len(packs)} packs ({", ".join(p["id"] + ("" if p.get("available") else " later") for p in packs)})')


if __name__ == '__main__':
    main()
