# Third-party notices

Lastbook builds on the work below. Each part keeps its own licence. Each section's heading says whether it is in this
repository, bundled into the package by the build, used from the system, or downloaded by setup.

**Checked** means the licence was read from the licence file or package metadata of the exact version the builds use.
**Upstream** means it was read from what the project itself publishes (its source repository at that version or its
website) on 19 Nov 2026, because the package or download carries no licence file of its own for that part.

Versions are the pins in `packaging/arch/PKGBUILD` (the two wheels) and `native/Cargo.lock`.

## Bundled into the package by the build

| Component | Version | Licence | Source | Checked |
|---|---|---|---|---|
| python-libzim | 3.13.1 | **GPL-3.0-or-later** | https://github.com/openzim/python-libzim | Checked: `libzim-3.13.1.dist-info/licenses/LICENSE` |
| libzim (C++, inside python-libzim's wheel) | 9.8.2 | GPL-2.0-or-later | https://github.com/openzim/libzim | Upstream: `COPYING` (GPL version 2) and the source files' headers ("version 2 … or any later version") at tag 9.8.2; Arch's `libzim` package says the same. The version is what the wheel's `libzim.version` reports |
| Xapian and ICU, built into libzim | Xapian 1.4.23, ICU 73.2 | Xapian: GPL-2.0-or-later; ICU: Unicode License (Unicode, Inc. License Agreement - Data Files and Software) | https://xapian.org, https://icu.unicode.org | Upstream: Xapian's source headers at v1.4.23; ICU's `icu4c/LICENSE` at release-73-2. Versions as the wheel's `libzim.version` reports |
| zstd and liblzma (xz), used by libzim | zstd 1.5.7, liblzma 5.2.6 | zstd: BSD-3-Clause or GPL-2.0-only; liblzma: public domain | https://github.com/facebook/zstd, https://tukaani.org/xz/ | Upstream: zstd's `README.md`, `LICENSE` and `COPYING` at v1.5.7; xz's `COPYING` at v5.2.6. Versions as the wheel's `libzim.version` reports |
| pypdfium2 | 5.14.0 | Apache-2.0 or BSD-3-Clause; data files CC-BY-4.0 | https://github.com/pypdfium2-team/pypdfium2 | Checked: `pypdfium2-5.14.0.dist-info/licenses/` |
| PDFium, inside pypdfium2's `libpdfium.so` | as built by pdfium-binaries | BSD-3-Clause (The PDFium Authors) | https://pdfium.googlesource.com/pdfium, https://github.com/bblanchon/pdfium-binaries | Checked: `BUILD_LICENSES/pdfium.txt`, `pdfium-binaries.txt` |
| Libraries built into PDFium: abseil, agg 2.3, fast_float, FreeType, ICU, Little CMS, libjpeg-turbo, OpenJPEG, libpng, LLVM libc, simdutf, zlib | as built | Each under its own licence (Apache-2.0, BSD-style, FreeType/FTL, Unicode, MIT, IJG, zlib and others) | see each file | Checked: the full texts are in the wheel's `pypdfium2-5.14.0.dist-info/licenses/data/<platform>/BUILD_LICENSES/` |
| Rust crates linked into the window (`/usr/bin/lastbook`): gtk4-rs and gtk-rs-core (gtk4, glib, gio, cairo-rs, pango, pangocairo), webkit6, poppler-rs, rusqlite, serde, serde_json, flate2, base64, async-channel, urlencoding, and what they depend on | as locked in `native/Cargo.lock` (111 packages, omui among them: see below) | MIT, or a choice that includes MIT (mostly MIT OR Apache-2.0), for all but five: two under Zlib, one under Apache-2.0 WITH LLVM-exception, one under (MIT OR Apache-2.0) AND Unicode-3.0, and omui under MIT AND ISC | https://crates.io | Checked: each crate's package metadata (`cargo metadata`), not its licence file |

The window's look and shared widgets come from omui (https://github.com/ferdousbhai/omui, MIT), which is linked into
`/usr/bin/lastbook` and brings two more notices with it:

- **Strata** (https://github.com/lgse/strata), MIT, Copyright (c) 2026 LGSE Ltd. omui's theme logic and the conventions
  of its stylesheet are adapted from Strata's source; omui's own `THIRD_PARTY_NOTICES.md` lists the parts.
- **Lucide** 1.35.0 (https://github.com/lucide-icons/lucide), ISC, Copyright (c) 2026 Lucide Icons and Contributors; the
  icons Lucide derives from Feather are MIT, Copyright (c) 2013-present Cole Bemis. The window's icons are Lucide's,
  each stroke outlined as a fill and the files renamed, inside the binary.

The ISC License, for Lucide:

```
Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

The MIT License, for Strata and for the Feather-derived icons:

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Build-only tools (Rust and the C compiler) are used to make the package, and none of their code is bundled except as
their licences allow.

## Used from the system, not bundled

Python 3, NumPy, Beautiful Soup 4 and soupsieve, GTK 4, WebKitGTK 6, Poppler, GStreamer and its plugins, SQLite, glib2 and
the GCC runtime come from Arch's own packages under their own licences (the PKGBUILD's `depends`).

## Downloaded by setup, never bundled

| Component | Version | Licence | Source | Checked |
|---|---|---|---|---|
| The map: OpenStreetMap data as vector tiles in Protomaps' basemap schema, cut from Protomaps' daily build | the newest build on the day of setup | The tiles: an ODbL Produced Work, © OpenStreetMap contributors (attribution required). The basemap's code: BSD-3-Clause, Protomaps LLC; its layer schema is adapted from Tilezen (MIT) | https://www.openstreetmap.org/copyright, https://docs.protomaps.com/basemaps/downloads, https://github.com/protomaps/basemaps | Upstream: Protomaps' downloads page and the basemaps repository's `LICENSE.md` |
| Place names for map search (GeoNames) | the files on the day of setup | CC BY 4.0 | https://www.geonames.org | Upstream: `readme.txt` beside the files on `download.geonames.org` |
| The area list setup offers (names and bounding boxes from Geofabrik's region index; bundled as `map-regions.json`) | as generated by `tools/make_region_list.py` | Derived from OpenStreetMap, ODbL | https://download.geofabrik.de | Upstream: Geofabrik's download pages say "Data: ODbL 1.0"; they state no separate terms for the index file |

## Used through another program

Meaning-based search asks Recall for Omarchy, over a local socket, for EmbeddingGemma vectors. Recall and its model are
installed and licensed separately; Lastbook neither bundles nor downloads them.

## Library content

The manuals, videos, encyclopedias and map data in the library packs are not part of this repository. Each pack has a
Sources and permissions page with the publisher and terms of every item. The US map files inside upstream's packs and state
maps use OpenStreetMap data (ODbL, © OpenStreetMap contributors), Microsoft building footprints (ODbL), Overture Maps
addresses and US government data such as USGS, NOAA, USDA and USACE.
