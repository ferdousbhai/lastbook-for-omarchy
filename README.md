# Lastbook for Omarchy

An offline survival library for Omarchy (Arch Linux with Hyprland, on x86_64 and on aarch64 Apple silicon through Asahi). It puts
manuals, videos, encyclopedias, an offline map and short practical guides on one computer. After setup, it works offline. The
desktop sidebar opens Videos, Map and Chat. Chat answers questions from the library, encyclopedias included, and cites its sources.

A hard fork of [Lastbook](https://github.com/Immortal-Protocols/lastbook) (<https://lastbook.org>), made Omarchy-only: the macOS,
Windows and iPad apps, the websites and the update checker are gone; meaning-based search uses
[Recall for Omarchy](https://github.com/ferdousbhai/recall-for-omarchy)'s embedding model; Chat reads the encyclopedias. The
download packs are Lastbook's own, from `downloads.lastbook.org`.

> MIT License ([LICENSE](LICENSE)), as upstream. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Install

```bash
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

It adds Lastbook's signed package repository (both architectures; with `vulkan-asahi` on Apple silicon) and installs
`lastbook-for-omarchy`; re-running is safe.

Or build and install the package from this checkout (it compiles the window and the local AI's runner, which takes a while;
it needs Rust):

```bash
cd packaging/arch && makepkg -si
```

For meaning-based search, install Recall for Omarchy as well (`curl -fsSL https://ferdousbhai.com/recall/install.sh | sudo bash`).
Without it everything works, and search matches words only.

Open Lastbook from the application menu. First-run setup downloads your chosen library, state map and optional local AI.
The window is a native GTK 4 program (`lastbook`); it uses the system's WebKitGTK, Poppler and GStreamer codecs, and runs
the Python engine as its own child process. Your library and settings stay in your home folder
(`~/.local/share/Lastbook`, `~/.config/Lastbook`, `~/.cache/Lastbook`). Updates come from pacman (`omarchy update`).

## What it does

- **Guides first.** Short, curated guides for urgent needs (`content/topics/`), each pointing to real pages in the library
  with page numbers, plus what curators looked for and did not find (`content/gaps.json`).
- **The full library.** Manuals and PDFs (with a whole-PDF view), videos, and offline encyclopedias (ZIM files), with
  keyword search everywhere and meaning-based search through Recall for Omarchy when it is running.
- **Offline map of the world.** OpenStreetMap vector tiles drawn natively in the theme's colours: the whole world to city
  level (`derived/geo/world.mbtiles`, about 0.6 GB), plus street-level detail (roads, buildings, water, and help points:
  hospitals, pharmacies, police, fire stations, drinking water, shelters) for the region chosen in setup, any of
  Geofabrik's 554 regions. Places anywhere can be found offline (a GeoNames gazetteer of countries, regions and towns of
  15,000+). The person picks the areas in setup; Lastbook never detects location. Nothing is hosted by Lastbook: setup
  cuts the map on the person's computer from Protomaps' newest daily OpenStreetMap build (HTTPS range requests, only the
  tiles wanted) and builds place search from GeoNames (`engine/setup/maps.py`; the area list is
  `packaging/arch/map-regions.json`, made by `tools/make_region_list.py`). The US library maps (state maps, addresses,
  terrain) still work where installed.
- **Optional local AI.** Ask questions in plain words and get answers that cite library pages, from a model that runs on
  the computer itself (see "The local AI" below). Without it, Chat still finds the right pages.
- **One-download setup.** On first run, setup defaults to Small and selects local AI when the computer can run it.
  The person can choose Small, Medium or All, selects a state map, and starts the checked downloads. A library already on the
  computer (a folder or a pack zip) can be used instead of downloading.

## Privacy and network use

- **No listening port.** The desktop app opens no network port. The window (`native/`) and the engine talk in JSON lines over
  the engine's standard input and output (`app/engine_pipe.py`, through the in-memory bridge `app/bridge.py`). The local AI runner speaks JSON
  lines on standard input and output and opens no socket either. Meaning-based search asks Recall for Omarchy's
  EmbeddingGemma over a local Unix socket in the user's runtime folder; nothing of it leaves the computer.
- **One use of the network, started by the person.** Setup's downloader (`engine/setup/`), when the person starts or
  resumes a download. It goes through `engine/setup/download.py`, the only module that opens a connection: HTTPS on port
  443 only, to allow-listed hosts only (`downloads.lastbook.org` for packs, maps and the manifest; Hugging Face's download
  hosts for the AI model), no proxy from the system, and a redirect is followed only to another allowed host.
- **Pinned trust.** The downloader trusts only the root certificates in `engine/setup/roots.pem` (11 roots from Mozilla's
  bundle), never the system's trust store. The builds refuse to finish if those roots no longer cover the allowed hosts
  (`packaging/check_tls_roots.py`).
- **Checked files.** Every download is checked against the SHA-256 in the manifest before it is used. Zips are checked
  for unsafe paths, links and sizes before anything is written.
- **Nothing else.** The window blocks all web loads, there is no telemetry, no account, and nothing runs on a timer or at
  startup that reaches the network.
- **A working library is never broken.** New data is unpacked beside the old one, checked, and then switched in with a
  single rename. A failure at any step leaves the old library as it was.

The setup contract, including state maps, is in [docs/SETUP-API.md](docs/SETUP-API.md); the engine side is described in
[engine/setup/README.md](engine/setup/README.md).

## What's where

| Path | What it is |
|---|---|
| `native/` | The desktop window: a Rust crate (binary `lastbook`, app id `io.github.ferdousbhai.Lastbook`) on GTK 4, WebKitGTK 6 and Poppler. It starts the engine as a child process. |
| `app/` | The engine's side of the app: the pipe the window talks to (`engine_pipe.py`), library lookup and setup hooks (`main.py`), the no-port bridge (`bridge.py`), and tests (`app/tests/`). |
| `engine/` | Paths and data-folder lookup, library access, saved pages, offline address search (`address_search.py`) and first-run setup (`engine/setup/`). |
| `server.py`, `assistant.py`, `retrieval.py`, `encyclopedia.py`, `llm_local.py` | The engine: request handler, the assistant and its citation checks, keyword plus meaning-based search (through Recall), encyclopedia articles as passages, and the local AI (model pins, fit check, runner control). |
| `web/` | The browser interface of the library server (`server.py`), in plain HTML, CSS and JavaScript with no build step and nothing loaded from the web. The desktop app does not use or ship it. |
| `content/` | The curated guides (`topics/*.json`) and what curators looked for and did not find (`gaps.json`). Format: `content/SCHEMA.md`. |
| `packaging/` | The Arch package (`arch/`: PKGBUILD, the pack manifest it ships, the package scan), the desktop entry (`linux/`), icons (`icon/`), release manifest, TLS roots tools, and `packaging/llm/` (the local AI runner). |
| `tools/` | Curator helpers (`libq.py`, `validate_topics.py`), the map area list's maker (`make_region_list.py`), and the retrieval check (`eval/`). |
| `docs/` | The setup contract (`SETUP-API.md`), how the offline map is put together (`MAPS.md`) and notes on the library server version (`LIBRARY-SERVER.md`). |

## Run from source

You need Python 3.11 or later, Rust, the GTK 4, WebKitGTK 6 and Poppler development files (the Arch packages in the
PKGBUILD's `depends`), and a **data folder**. A data folder holds `library-data/catalog.json` (the library) and gets a
`derived/` folder for indexes, caches and logs. Get one by running the app once and letting setup download a pack, or by
unzipping a pack from lastbook.org.

```bash
python3 -m pip install --target .deps -r packaging/requirements.txt     # for /usr/bin/python3, the Python the window starts
cargo build --manifest-path native/Cargo.toml
LASTBOOK_DEV=1 PYTHONPATH=$PWD/.deps native/target/debug/lastbook --data /path/to/LastbookData
```

`LASTBOOK_DEV=1` lets the engine (`/usr/bin/python3 app/engine_pipe.py`, started by the window) take its dependencies from
`PYTHONPATH`. Without `--data`, the engine checks `LASTBOOK_DATA`, the folder chosen last time, and its own data folder.
Existing libraries elsewhere are accessed when the person chooses them. If no library is available, it opens setup.
The full order is in `app/main.py`.

The **library server** version runs the same engine in a browser and can share with phones on the same Wi-Fi:

```bash
LASTBOOK_DATA=/path/to/LastbookData python3 server.py          # then open http://127.0.0.1:8890/
```

See [docs/LIBRARY-SERVER.md](docs/LIBRARY-SERVER.md). It was written for the household library this project grew out of,
so some of its paths and ports describe that setup. The desktop app does not use this port; `web/` is there for this mode only.

## How Chat finds what it cites

Every question first goes through `assistant.hybrid()`, which fuses three ranked lists with reciprocal-rank fusion and hands the
best eight passages to the model, with their provenance (document, PDF page, video moment or article section):

1. **Keyword search**: the library's SQLite FTS5 index of every page (`search.sqlite`), BM25-ranked, with everyday words mapped
   to the words manuals use (`server.SYN`).
2. **Meaning-based search** (`retrieval.py`): the library cut into about 1,100-character passages, embedded once by Recall for
   Omarchy's EmbeddingGemma over its Unix socket and kept in `derived/index/` (passages in SQLite, vectors in one float16 file).
   A question is embedded and scored against every passage. The index records which model made it; another model empties and
   rebuilds it. Without Recall this list is empty.
3. **Encyclopedias** (`encyclopedia.py`): each ZIM file's own full-text and title indexes find candidate articles; with Recall,
   their openings and a few passages are ranked by meaning and weak ones dropped. Nothing is embedded ahead of time. The best
   article that clears the bar keeps the last of the eight places, since library pages usually win on both other lists.

The model can then search again, read whole pages (`read_pages`) or articles (`read_article`), transcripts, and the offline
gazetteer. Every `[S#]` in its answer is checked against what the tools returned in that turn. `tools/eval/run.py` measures
hits@8, MRR and timing on `tools/eval/questions.json` against a real data folder.

## Build

`packaging/arch/PKGBUILD` builds the package from the committed source (`git archive HEAD`): the window
(`cargo build --release --locked --manifest-path native/Cargo.toml`, installed as `/usr/bin/lastbook`), the local AI's runner
(`packaging/llm/build_llm_linux.sh`, llama.cpp at a pinned tag with Vulkan and every CPU variant), and the two Python packages Arch does not carry (libzim and pypdfium2, pinned wheels). Everything else comes from Arch. Check a
built package with `python3 packaging/arch/scan_package.py <package>`. Releases: `bin/release <version>` (with
`packaging/release-notes.md` written first) tags, builds both architectures natively in CI (`.github/workflows/packages.yml`,
`bin/build-packages`), signs the packages and repository databases locally and publishes them with `install.sh`.

## Packs, state maps and the local AI

- **Packs** are data folders: a library plus its catalog, search index and US map data. They come pre-built from
  upstream Lastbook, on `downloads.lastbook.org`; nothing here builds them. Setup downloads the one the person picks and
  checks it against `setup/manifest-v1.json` (`packaging/arch/manifest-v1.json`; a newer copy is fetched from
  `downloads.lastbook.org` when setup opens and used only if it validates).
- **State maps** are upstream's detailed map set and address index for one US state. Setup still installs and repairs
  them where a library has one, and address search uses the index; the window draws its own map of the world instead.
- **The local AI** is optional: Gemma 4 26B A4B (Unsloth's `gemma-4-26B-A4B-it-UD-Q4_K_M.gguf`, about 17 GB). The file
  name, size, SHA-256 and Hugging Face revision are pinned in `llm_local.py`. Setup offers it only where the measured fit
  says it fits (about 18 GB of memory for model and context, plus a reserve) and says why in plain words when it does not.
  Setup downloads it directly from Hugging Face. The model is never part of the app or of this repository. The runner,
  `lastbook-llm`, is our own small program (`packaging/llm/lastbook_llm.cpp`) on llama.cpp tag b12032, with
  Vulkan and CPU fallbacks.
- **Meaning-based search** uses EmbeddingGemma through Recall for Omarchy, over a local socket. Without Recall running,
  search is keyword search.

## What is not in this repository, and why

- **The library** (`library-data/`): hundreds of gigabytes of third-party manuals, videos and encyclopedias, each under
  its own terms. It is distributed as packs, with a Sources and permissions page in each pack.
- **Models and maps** (GGUF, map data): large, and published by their makers or built from public data. Setup
  fetches them, and the checksums and pins are here.
- **Generated data** (`derived/`, builds): indexes, caches and logs the engine makes as it runs, and build output.
- **Release and signing material**: signing keys, deploy settings and the release folders.
- **Private data**: the household's own home area (`web/home-area.json`), the private word lists the scans use, machine
  and user names, keys and internal security-audit notes. The guides here are already in their shipped, general wording.
  To keep your own home-area words out of a build, put them in a local copy of `packaging/ship_edits.json` (`forbidden`)
  and never commit it.

## Tests

```bash
python3 -B -m unittest app.tests.test_bridge app.tests.test_setup app.tests.test_data_folder app.tests.test_what_ships app.tests.test_address_search app.tests.test_topic_search app.tests.test_validate_topics app.tests.test_libq app.tests.test_semantic
```

`test_bridge` builds its own made-up library; `test_semantic` runs a stand-in Recall; the others run without a library. The window's own tests run with
`cargo test --manifest-path native/Cargo.toml`. `bin/test` runs both (libzim and pypdfium2 come from the PKGBUILD's pinned
wheels, `bin/test-deps`); `bin/check` adds the installer test. The `linux_*` helpers in `app/tests/` drive a running window through AT-SPI.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Lastbook’s original code and
documentation use the [MIT License](LICENSE), permitting commercial use, modification and redistribution with the
copyright and license notice. Third-party software, models, media and library content retain their own licenses,
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and each library pack. Distributed packages include
GPL-covered components such as python-libzim and libzim and must also meet those components’ license obligations.

