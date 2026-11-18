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

Build and install the package from this checkout (both architectures; it compiles the local AI's runner, which takes a while):

```bash
cd packaging/arch && makepkg -si
```

For meaning-based search, install Recall for Omarchy as well (`curl -fsSL https://ferdousbhai.com/recall/install.sh | sudo bash`).
Without it everything works, and search matches words only.

Open Lastbook from the application menu. First-run setup downloads your chosen library, state map and optional local AI.
The app uses the system's GTK, WebKitGTK and GStreamer codecs. Your library and settings stay in your home folder
(`~/.local/share/Lastbook`, `~/.config/Lastbook`, `~/.cache/Lastbook`). Updates come from pacman (`omarchy update`).

## What it does

- **Guides first.** Short, curated guides for urgent needs (`content/topics/`), each pointing to real pages in the library
  with page numbers, plus what curators looked for and did not find (`content/gaps.json`).
- **The full library.** Manuals and PDFs (with a whole-PDF view), videos, and offline encyclopedias (ZIM files), with
  keyword search everywhere and meaning-based search through Recall for Omarchy when it is running.
- **Offline map.** A base map of the world and the US, plus a detailed map for the state selected during setup: local streets,
  buildings, terrain, crops, and water, help and resource points. Addresses in that state can be found offline. The person
  picks the state; Lastbook never detects location.
- **Optional local AI.** Ask questions in plain words and get answers that cite library pages, from a model that runs on
  the computer itself (see "The local AI" below). Without it, Chat still finds the right pages.
- **One-download setup.** On first run, setup defaults to Small and selects local AI when the computer can run it.
  The person can choose Small, Medium or All, selects a state map, and starts the checked downloads. A library already on the
  computer (a folder or a pack zip) can be used instead of downloading.

## Privacy and network use

- **No listening port.** The desktop app opens no network port. The window and the engine talk through an in-memory bridge
  (`app/bridge.py`) served through WebKitGTK's `URISchemeRequest` (`app/shell_linux.py`). The local AI runner speaks JSON
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
| `app/` | The desktop app: entry point (`main.py`), the no-port bridge, the GTK/WebKitGTK window (`shell_linux.py`), and tests (`app/tests/`). |
| `engine/` | Paths and data-folder lookup, library access, saved pages, offline address search (`address_search.py`) and first-run setup (`engine/setup/`). |
| `server.py`, `assistant.py`, `retrieval.py`, `encyclopedia.py`, `llm_local.py` | The engine: request handler, the assistant and its citation checks, keyword plus meaning-based search (through Recall), encyclopedia articles as passages, and the local AI (model pins, fit check, runner control). |
| `web/` | The interface, in plain HTML, CSS and JavaScript with no build step and nothing loaded from the web. `setup.*` is the setup screen. |
| `content/` | The curated guides (`topics/*.json`) and what curators looked for and did not find (`gaps.json`). Format: `content/SCHEMA.md`. |
| `packaging/` | The Arch package (`arch/`: PKGBUILD, the pack manifest it ships, the package scan), the launcher, desktop entry and video source (`linux/`), icons (`icon/`), pack and state-map builders, release manifest, TLS roots tools, and `packaging/llm/` (the local AI runner). |
| `derived/packs/pack-spec.json` | What goes into each download pack (Small, Medium, All and add-ons) and each state map. Everything else under `derived/` is generated and not kept here. |
| `tools/` | Builders for the offline map, addresses and place finder, curator helpers (`libq.py`, `validate_topics.py`), and the retrieval check (`eval/`). |
| `docs/` | The setup contract (`SETUP-API.md`), how the offline map is put together (`MAPS.md`) and notes on the library server version (`LIBRARY-SERVER.md`). |

## Run from source

You need Python 3.11 or later and a **data folder**. A data folder holds `library-data/catalog.json` (the library) and gets
a `derived/` folder for indexes, caches and logs. Get one by running the app once and letting setup download a pack, or by
unzipping a pack from lastbook.org.

```bash
python3 -m venv --system-site-packages .venv     # the system's PyGObject (python-gobject) gives GTK and WebKitGTK
.venv/bin/pip install -r packaging/requirements.txt
.venv/bin/python app/main.py --data /path/to/LastbookData
```

Without `--data`, the app checks `LASTBOOK_DATA`, the folder chosen last time, and its own data folder.
Existing libraries elsewhere are accessed when the person chooses them. If no library is available, it opens setup.
The full order is in `app/main.py`.

The **library server** version runs the same engine in a browser and can share with phones on the same Wi-Fi:

```bash
LASTBOOK_DATA=/path/to/LastbookData python3 server.py          # then open http://127.0.0.1:8890/
```

See [docs/LIBRARY-SERVER.md](docs/LIBRARY-SERVER.md). It was written for the household library this project grew out of,
so some of its paths and ports describe that setup. The desktop app does not use this port.

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

`packaging/arch/PKGBUILD` builds the package from the committed source (`git archive HEAD`): the local AI's runner
(`packaging/llm/build_llm_linux.sh`, llama.cpp at a pinned tag with Vulkan and every CPU variant), the GStreamer video source,
and the two Python packages Arch does not carry (libzim and pypdfium2, pinned wheels). Everything else comes from Arch. Check a
built package with `python3 packaging/arch/scan_package.py <package>`.

## Packs, state maps and the local AI

- **Packs** are data folders: a library plus its catalog, search index and map. `packaging/build_packs.py` builds them
  from a frozen snapshot of a full library according to `derived/packs/pack-spec.json`, and `packaging/verify_packs.py` and
  `packaging/scan_pack.py` check them. The published packs are on `downloads.lastbook.org`. Setup downloads the one the
  person picks and checks it against `setup/manifest-v1.json`, which `packaging/release_manifest.py` writes at build time
  (a newer copy is fetched from `downloads.lastbook.org` when setup opens and used only if it validates).
- **State maps** are one US state's detailed map set, built by `packaging/build_packs.py map <ST>` and published by
  `packaging/state_maps.py`, with per-state address indexes from `packaging/address_indexes.py`. A state map replaces the
  library's map set; it never merges with it. Medium already holds California and All the whole US.
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
- **Generated data** (`derived/`, builds, installers): everything there can be rebuilt with the scripts here.
- **Release and signing material**: signing keys, deploy settings and the release folders.
- **Private data**: the household's own home area (`web/home-area.json`), the private word lists the scans use, machine
  and user names, keys and internal security-audit notes. The guides here are already in their shipped, general wording.
  To keep your own home-area words out of a build, put them in a local copy of `packaging/ship_edits.json` (`forbidden`)
  and never commit it.

## Tests

```bash
python3 -B -m unittest app.tests.test_bridge app.tests.test_setup app.tests.test_data_folder app.tests.test_what_ships app.tests.test_address_search app.tests.test_topic_search app.tests.test_validate_topics app.tests.test_libq app.tests.test_semantic
```

`test_bridge` builds its own made-up library; `test_semantic` runs a stand-in Recall; the others run without a library. `packaging/test_pack_tools.py SAMPLE
WORK` checks the pack tools against a pack sample (`build_packs.py build <pack> --sample N`). The `linux_*` helpers in
`app/tests/` drive a running window through AT-SPI.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Lastbook’s original code and
documentation use the [MIT License](LICENSE), permitting commercial use, modification and redistribution with the
copyright and license notice. Third-party software, models, media and library content retain their own licenses,
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and each library pack. Distributed packages include
GPL-covered components such as python-libzim and libzim and must also meet those components’ license obligations.

