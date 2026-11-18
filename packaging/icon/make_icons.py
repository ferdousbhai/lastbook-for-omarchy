"""Every size of Lastbook's app icon, from one master.

    python3 packaging/icon/make_icons.py            # writes the files listed below
    python3 packaging/icon/make_icons.py --master   # also rebuilds the masters from the chosen source image first

Source: book-eyes.png, the owner's choice of 12 Nov 2026 (the smiling open book with eyes and yellow rays), an opaque 1254 px
image of a cream tile on black. --master lifts the drawing off it (the ink, unmixed from the cream so the edges stay smooth) and
draws a new tile under it, so nothing of the black surround or the old tile edge is left:

  packaging/icon/art.png            the drawing alone, transparent, as it sat on the source tile (1024 px, tile-relative)
  packaging/icon/lastbook-1024.png  THE master: Apple's macOS grid, an 824 px tile at 100,100 of 1024, continuous corners, soft
                                    shadow, transparent outside the tile
  packaging/icon/tile-1024.png      the same tile edge to edge (a 2% margin), no shadow: the desktop icon, web app icons

Then, from the masters (needs Pillow and numpy):

  web/icon.svg, web/icon-192.png, web/icon-512.png               the app's own pages
"""
import base64, io, sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SOURCE = Path('/Users/you/Documents/Codex/2026-11-12/lastbook-release-readiness/work/app-icon-options/book-eyes.png')
CREAM = (248, 243, 234)     # replaced by the source's own tile colour when --master runs; kept in tile.txt
N = 1024


def squircle(size, box, exp=4.6, ss=4):
    """A mask of a superellipse tile (Apple's continuous corner, close enough at icon sizes), antialiased by supersampling."""
    x0, y0, w = box
    big = size * ss
    yy, xx = np.mgrid[0:big, 0:big].astype(np.float32)
    cx = cy = (x0 + w / 2) * ss
    r = w / 2 * ss
    d = np.abs((xx + .5 - cx) / r) ** exp + np.abs((yy + .5 - cy) / r) ** exp
    m = Image.fromarray(((d <= 1) * 255).astype(np.uint8))
    return m.resize((size, size), Image.LANCZOS)


def lift_art():
    """The drawing off the source: (art RGBA scaled so the source tile is N wide, tile colour)."""
    src = np.asarray(Image.open(SOURCE).convert('RGB')).astype(np.float32)
    h, w, _ = src.shape
    dark = (src.mean(axis=2) < 110).astype(np.uint8) * 255
    fill = Image.fromarray(dark).copy()     # a copy: an image over a numpy buffer may not take the fill
    for c in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        ImageDraw.floodfill(fill, c, 128)          # the black surround: dark and joined to a corner
    outside = np.asarray(fill) == 128
    inside = Image.fromarray(((~outside) * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(25))   # off the tile's own edge
    inside = np.asarray(inside) > 0
    ys, xs = np.where(~outside)
    tx0, tx1, ty0, ty1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    bright = src[inside & (src.mean(axis=2) > 200)]
    bg = np.median(bright, axis=0)
    # c = a*ink + (1-a)*bg with the ink as dark as it can be: a is the largest fall below the paper in any channel
    a = np.clip(((bg - src) / bg).max(axis=2), 0, 1)
    a = np.where(a < .06, 0, (a - .06) / .94)          # paper grain is not ink
    a = a * inside
    ink = np.where(a[..., None] > 0, (src - (1 - a[..., None]) * bg) / np.maximum(a[..., None], 1e-3), 0)
    rgba = np.dstack([np.clip(ink, 0, 255), a * 255]).astype(np.uint8)
    art = Image.fromarray(rgba, 'RGBA').crop((tx0, ty0, tx1, ty1))
    return art.resize((N, N), Image.LANCZOS), tuple(int(round(v)) for v in bg)


def tile(size, box, art, cream, shadow=False):
    x0, y0, w = box
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    mask = squircle(size, box)
    if shadow:   # Apple's template: a soft drop shadow in the margin under the tile
        sh = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        sm = mask.point(lambda v: v * 0.30)
        sh.putalpha(sm)
        sh = sh.transform(sh.size, Image.AFFINE, (1, 0, 0, 0, 1, -size * 10 / 1024)).filter(ImageFilter.GaussianBlur(size * 10 / 1024))
        out = Image.alpha_composite(out, sh)
    body = Image.new('RGBA', (size, size), cream + (255,))
    a = art.resize((int(round(w)), int(round(w))), Image.LANCZOS)
    body.alpha_composite(a, (int(round(x0)), int(round(y0))))
    body.putalpha(mask)
    return Image.alpha_composite(out, body)


def masters():
    art, cream = lift_art()
    art.save(HERE / 'art.png', optimize=True)
    (HERE / 'tile.txt').write_text('%d,%d,%d\n' % cream)
    tile(N, (100, 100, 824), art, cream, shadow=True).save(HERE / 'lastbook-1024.png', optimize=True)
    tile(N, (20, 20, 984), art, cream).save(HERE / 'tile-1024.png', optimize=True)
    flat = Image.new('RGBA', (N, N), cream + (255,))   # for iOS's apple-touch-icon: opaque, iOS rounds it itself
    flat.alpha_composite(art.resize((N, N), Image.LANCZOS))
    flat.convert('RGB').save(HERE / 'flat-1024.png', optimize=True)


def sized(img, px):
    return img.resize((px, px), Image.LANCZOS)


def png_bytes(img):
    b = io.BytesIO()
    img.save(b, 'PNG', optimize=True)
    return b.getvalue()


def write_svg(img, path, px=128):
    """An SVG that carries a PNG: browsers take it as a favicon at any size, and it keeps the files' names and links as they are."""
    data = base64.b64encode(png_bytes(sized(img, px))).decode()
    Path(path).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {px} {px}">'
                          f'<image width="{px}" height="{px}" href="data:image/png;base64,{data}" xlink:href="data:image/png;base64,{data}"/></svg>\n')


def main():
    if '--master' in sys.argv:
        masters()
    flat_tile = Image.open(HERE / 'tile-1024.png').convert('RGBA')
    sizes = {s: sized(flat_tile, s) for s in (192, 512)}
    write_svg(flat_tile, ROOT / 'web' / 'icon.svg')
    for s, im in sizes.items():
        im.save(ROOT / 'web' / f'icon-{s}.png', optimize=True)


if __name__ == '__main__':
    main()
