# Third-party notices

Lastbook builds on the work below. Each part keeps its own licence. The **Where** column says whether it is in this
repository, bundled into the package by the build, used from the system, or downloaded by setup.

**Checked** means the licence was read from the licence file or package metadata of the exact version the builds use.
**Upstream** means it is taken from the project's own published licence because no local copy was at hand when this list
was written. Check those before a public release.

Versions are the pins in `packaging/requirements.txt`, `packaging/llm/build_llm_linux.sh` and `llm_local.py`.

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
| NumPy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://numpy.org | Checked: package metadata |
| Beautiful Soup 4 | 4.15.0 | MIT | https://www.crummy.com/software/BeautifulSoup/ | Checked: package metadata |
| soupsieve (used by Beautiful Soup) | as resolved by pip | MIT | https://github.com/facelessuser/soupsieve | Checked: package metadata |
| llama.cpp and ggml, in `lastbook-llm` (optional local AI runner) | tag b12032 (dde879bc) | MIT (Copyright the ggml authors) | https://github.com/ggml-org/llama.cpp | Checked: `LICENSE`; it ships beside the runner as `LICENSE-llama.cpp.txt` |
| nlohmann/json, used by llama.cpp's `common/` | as vendored in b12032 | MIT | https://github.com/nlohmann/json | Checked: `licenses/LICENSE-jsonhpp` |

Build-only tools (CMake, Ninja, glslc/shaderc, Vulkan and SPIR-V headers, the compiler) are used to make the package, and
none of their code is bundled except as their licences allow.

## Used from the system, not bundled

Python 3, PyGObject, GTK 3, WebKitGTK 4.1, GStreamer and its plugins, the Vulkan loader and the graphics drivers come from
the system's own packages under their own licences.

## Downloaded by setup, never bundled

| Component | Version | Licence | Source | Checked |
|---|---|---|---|---|
| Gemma 4 26B A4B instruct, GGUF quantization by Unsloth (`gemma-4-26B-A4B-it-UD-Q4_K_M.gguf`) | revision pinned in `llm_local.py` | Apache-2.0, per the model card | https://huggingface.co/unsloth (Google's Gemma 4 release) | Upstream: check the model card and its licence file before release |

## Used through another program

Meaning-based search asks Recall for Omarchy, over a local socket, for EmbeddingGemma vectors. Recall and its model are
installed and licensed separately; Lastbook neither bundles nor downloads them.

## Library content

The manuals, videos, encyclopedias and map data in the library packs are not part of this repository. Each pack has a
Sources and permissions page with the publisher and terms of every item. The offline map uses OpenStreetMap data
(ODbL, © OpenStreetMap contributors) and US government data such as USGS, NOAA and USACE, as credited on the map.
