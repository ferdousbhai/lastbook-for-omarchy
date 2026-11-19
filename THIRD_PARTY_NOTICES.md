# Third-party notices

Lastbook builds on the work below. Each part keeps its own licence. The **Where** column says whether it is in this
repository, bundled into the package by the build, used from the system, or downloaded by setup.

**Checked** means the licence was read from the licence file or package metadata of the exact version the builds use.
**Upstream** means it is taken from the project's own published licence because no local copy was at hand when this list
was written. Check those before a public release.

Versions are the pins in `packaging/arch/PKGBUILD` (the two wheels), `native/Cargo.lock`, `packaging/llm/build_llm_linux.sh` and `llm_local.py`.

## Bundled into the package by the build

| Component | Version | Licence | Source | Checked |
|---|---|---|---|---|
| python-libzim | 3.13.1 | **GPL-3.0-or-later** | https://github.com/openzim/python-libzim | Checked: `libzim-3.13.1.dist-info/licenses/LICENSE` |
| libzim (C++, inside python-libzim's wheel) | 9 | GPL-2.0 | https://github.com/openzim/libzim | Upstream |
| Xapian and ICU 73, built into libzim | as built | Xapian: GPL-2.0-or-later; ICU: Unicode License | https://xapian.org, https://icu.unicode.org | Upstream (both found in the binary) |
| zstd and liblzma (xz), used by libzim | as built | zstd: BSD-3-Clause or GPL-2.0; liblzma: 0BSD/public domain | https://github.com/facebook/zstd, https://tukaani.org/xz/ | Upstream |
| pypdfium2 | 5.14.0 | Apache-2.0 or BSD-3-Clause; data files CC-BY-4.0 | https://github.com/pypdfium2-team/pypdfium2 | Checked: `pypdfium2-5.14.0.dist-info/licenses/` |
| PDFium, inside pypdfium2's `libpdfium.so` | as built by pdfium-binaries | BSD-3-Clause (The PDFium Authors) | https://pdfium.googlesource.com/pdfium, https://github.com/bblanchon/pdfium-binaries | Checked: `BUILD_LICENSES/pdfium.txt`, `pdfium-binaries.txt` |
| Libraries built into PDFium: abseil, agg 2.3, fast_float, FreeType, ICU, Little CMS, libjpeg-turbo, OpenJPEG, libpng, LLVM libc, simdutf, zlib | as built | Each under its own licence (Apache-2.0, BSD-style, FreeType/FTL, Unicode, MIT, IJG, zlib and others) | see each file | Checked: the full texts are in the wheel's `pypdfium2-5.14.0.dist-info/licenses/data/<platform>/BUILD_LICENSES/` |
| llama.cpp and ggml, in `lastbook-llm` (optional local AI runner) | tag b12032 (dde879bc) | MIT (Copyright the ggml authors) | https://github.com/ggml-org/llama.cpp | Checked: `LICENSE`; it ships beside the runner as `LICENSE-llama.cpp.txt` |
| nlohmann/json, used by llama.cpp's `common/` | as vendored in b12032 | MIT | https://github.com/nlohmann/json | Checked: `licenses/LICENSE-jsonhpp` |
| Rust crates linked into the window (`/usr/bin/lastbook`): gtk4-rs and gtk-rs-core (gtk4, glib, gio, cairo-rs, pango, pangocairo), webkit6, poppler-rs, rusqlite, serde, serde_json, pulldown-cmark, flate2, base64, async-channel, urlencoding, and what they depend on | as locked in `native/Cargo.lock` (113 packages, omui among them: see below) | MIT, or MIT OR Apache-2.0, for all but four: two under Zlib, one under Apache-2.0 WITH LLVM-exception, one under (MIT OR Apache-2.0) AND Unicode-3.0 | https://crates.io | Checked: each crate's package metadata (`cargo metadata`), not its licence file |

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

Build-only tools (CMake, Ninja, glslc/shaderc, Vulkan and SPIR-V headers, the compiler) are used to make the package, and
none of their code is bundled except as their licences allow.

## Used from the system, not bundled

Python 3, NumPy, Beautiful Soup 4 and soupsieve, GTK 4, WebKitGTK 6, Poppler, GStreamer and its plugins, SQLite, glib2, the
GCC runtime, the Vulkan loader and the graphics drivers come from Arch's own packages under their own licences (the
PKGBUILD's `depends`).

## Downloaded by setup, never bundled

| Component | Version | Licence | Source | Checked |
|---|---|---|---|---|
| Gemma 4 26B A4B instruct, GGUF quantization by Unsloth (`gemma-4-26B-A4B-it-UD-Q4_K_M.gguf`) | revision pinned in `llm_local.py` | Apache-2.0, per the model card | https://huggingface.co/unsloth (Google's Gemma 4 release) | Upstream: check the model card and its licence file before release |
| The map: OpenStreetMap data as vector tiles in Protomaps' basemap schema, cut from Protomaps' daily build | the newest build on the day of setup | Data: ODbL, © OpenStreetMap contributors. Tiles and schema: Protomaps (see its terms) | https://www.openstreetmap.org/copyright, https://docs.protomaps.com/basemaps/downloads | Upstream |
| Place names for map search (GeoNames) | the files on the day of setup | CC BY 4.0 | https://www.geonames.org | Upstream |
| The area list setup offers (names and bounding boxes from Geofabrik's region index; bundled as `map-regions.json`) | as generated by `tools/make_region_list.py` | Derived from OpenStreetMap, ODbL | https://download.geofabrik.de | Upstream |

## Used through another program

Meaning-based search asks Recall for Omarchy, over a local socket, for EmbeddingGemma vectors. Recall and its model are
installed and licensed separately; Lastbook neither bundles nor downloads them.

## Library content

The manuals, videos, encyclopedias and map data in the library packs are not part of this repository. Each pack has a
Sources and permissions page with the publisher and terms of every item. The US map files inside upstream's packs and state
maps use OpenStreetMap data (ODbL, © OpenStreetMap contributors), Microsoft building footprints (ODbL), Overture Maps
addresses and US government data such as USGS, NOAA, USDA and USACE.
