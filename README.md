# Lastbook for Omarchy

An offline survival library for Omarchy (Arch Linux with Hyprland, on x86_64 and on aarch64 Apple silicon through Asahi). It puts
manuals, videos, encyclopedias, an offline map and short practical guides on one computer. After setup, it works offline. The
desktop sidebar opens Search, Guides, Library, Encyclopedias, Videos, Map, Saved and Library and downloads, with About in
its footer. Encyclopedias opens each encyclopedia at its front page and offers an article picked at random; search finds
encyclopedia articles too (it has an Encyclopedias filter). Lastbook has no AI of its own: an AI agent on the computer answers questions
from the library through the `lastbook` command line and its agent skill (below).

![The Guides screen: the sidebar of screens, and the guides for urgent needs](docs/screenshot.png)

A hard fork of [Lastbook](https://github.com/Immortal-Protocols/lastbook) (<https://lastbook.org>), made Omarchy-only, with one native window: the macOS,
Windows and iPad apps, the browser interface, the websites, the pack-building tools and the update checker are gone; meaning-based search uses
[Recall for Omarchy](https://github.com/ferdousbhai/recall-for-omarchy)'s embedding model; search reads the encyclopedias. The
download packs are Lastbook's own, pre-built, from `downloads.lastbook.org`; the map is made on your computer in setup.

> MIT License ([LICENSE](LICENSE)), as upstream. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Install

```bash
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

It adds Lastbook's signed package repository (both architectures) and installs `lastbook-for-omarchy`; re-running is safe.

Or build and install the package from this checkout (it compiles the window; it needs Rust):

```bash
cd packaging/arch && makepkg -si
```

The installer offers to add Recall for Omarchy for meaning-based search (or later: `curl -fsSL https://ferdousbhai.com/recall/install.sh | sudo bash`).
Without it everything works, and search matches words only.

Open Lastbook from the application menu. First-run setup downloads your chosen library and makes the map.
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
  Geofabrik's 554 regions. Places anywhere can be found offline (a GeoNames gazetteer of countries, regions, and towns and
  villages of 500 people or more). The person picks the areas in setup; Lastbook never detects location. Nothing is hosted by Lastbook: setup
  cuts the map on the person's computer from Protomaps' newest daily OpenStreetMap build (HTTPS range requests, only the
  tiles wanted) and builds place search from GeoNames (`engine/setup/maps.py`; the area list is
  `packaging/arch/map-regions.json`, made by `tools/make_region_list.py`). On the Map screen the help points are marks (a
  legend says what each means, a click names one and says how far it is); after a town or `LAT,LON` is found, or for the
  map centre on request, a Nearest help panel lists the closest of each kind, hospitals, clinics and doctors together
  under Medical help, and a row centres the map on it. `lastbook place --nearby` finds the nearest
  help points from the same street-level maps, around a town or a point given as `LAT,LON`, so it answers for the regions
  installed and says plainly when no installed map covers a place. Upstream's US map files are not unpacked, and are
  removed from a library that still has them ([docs/MAPS.md](docs/MAPS.md)).
- **For AI agents too.** The same library from the command line, as JSON, with a skill that tells an agent how to
  search it, read the pages and cite them (below). The agent can be any on the computer, with a local model as well
  (through Omarchy's `omarchy-local-ai`, say).
- **One-download setup.** On first run, setup defaults to Small.
  The person can choose Small, Medium or All, picks the world map and any street-level regions, and starts the checked
  downloads. A library already on the computer (a folder or a pack zip) can be used instead of downloading.

## Keyboard

| Keys | Where | Does |
|---|---|---|
| ?, F1 | Everywhere (`?` not while typing in a field) | Shows or hides every shortcut |
| Ctrl+K | Everywhere | Goes to Search, with the keyboard in its field |
| Ctrl+F | The reader, a web page or article | Finds in it, with the keyboard in the find bar |
| Ctrl+F | Any other screen | Goes to Search, as Ctrl+K does |
| Enter, Shift+Enter (also Ctrl+G, Ctrl+Shift+G) | Find in a document or page | The next match, the match before |
| Escape | Find in a document or page | Closes the find bar, and the page has the keyboard again |
| Ctrl+C | A web page or article | Copies the text selected in it |
| Ctrl+B | Everywhere | Shows or hides the sidebar |
| Alt+Left, Alt+Right | Everywhere | Back and forward through the screens visited |
| Ctrl+D | A page, article, video or guide | Saves it, or removes it from Saved (the note that appears offers Undo) |
| Ctrl+W, Ctrl+Q | Everywhere | Closes the window and quits (Lastbook has one window); while a download runs, asks first |
| Escape | A question | Answers with the choice that loses nothing |
| Up, Down | The sidebar | Moves to the screen above or below |
| Enter, Space | A card | Opens it |
| Left, Right | The reader | The page before, the page after |
| Home, End | The reader | The first page, the last page |
| Up, Down, K, J | The reader | Scrolls within the page |
| Space, Page Down | The reader | Down a screenful, and on to the next page from the foot of this one |
| Shift+Space, Backspace, Page Up | The reader | Up a screenful, and back to the page before from the top of this one |
| +, - (also with Ctrl) | The reader | Zooms in, zooms out |
| 0 | The reader | Fits the page to the window's width |
| +, - | The map | Zooms in, zooms out |
| Left, Right, Up, Down | The map | Moves the map |

The reader's and the map's single keys (K, J, 0, +, -, Space) are letters typed while a text field has the keyboard.

## Language

The window follows the language picked in Omarchy (Setup > Region > Language) as it runs, and reads right to left in
the languages that do. Its own words are in `native/src/i18n/en.json`, the list translators work from; English is the
only catalog so far. The library itself (manuals, videos, encyclopedias, and the curated guides, which cite English
sources page by page) stays in the language it was written in. The command line answers in English, for scripts.

## Command line and agent skill

`lastbook open` (the launcher's command), or `lastbook` alone, opens the window. With a command it answers from the same library as JSON on standard output, with no
window, for scripts and for AI agents working on the computer:

```bash
lastbook search "treat a burn" --limit 5          # passages by meaning and by words: doc_id, title, page, excerpt, trust
lastbook read med-443ae5cfaa6ecc68 --pages 37,38  # the text of PDF pages; of an encyclopedia article (zim:…); of a saved page
lastbook page med-443ae5cfaa6ecc68 38             # one PDF page as a PNG (in ~/.cache/Lastbook/pages, or --out FILE)
lastbook transcript VIDEO_ID --query "tinder"     # a video's timed lines
lastbook place "Denpasar" --nearby hospital       # a town in the offline gazetteer, or "LAT,LON", and help points near it (needs a street-level map of the area)
lastbook guides; lastbook guide hurt              # the situation guides and the pages they point to
lastbook status                                   # library folder and counts, meaning search, map files
lastbook open med-443ae5cfaa6ecc68 38             # a new window at that page; also routes: guide:hurt, search:burns
```

`lastbook --help` lists them. Exit codes: 0 success (no matches too), 1 any failure (invalid arguments, no library yet,
an unknown id); an error is one line on standard error, saying which. `--data FOLDER` names the library, as for the window.
The commands run beside an open window: they only read the library, and write nothing but the engine's own caches
(`lastbook index`, which the lastbook-index user service runs in the background, builds the meaning-search index). Only `open` starts a window.

The agent skill (`skills/lastbook/SKILL.md`) tells an agent when and how to use these: to answer a question it searches,
reads the pages the results point at, and cites document title and page. `lastbook skill` prints it;
`lastbook skill install` writes it to `~/.agents/skills/lastbook` and links it from `~/.claude/skills` and
`~/.codex/skills`. The installer does that for the desktop user, and the window keeps an installed skill current.

## Privacy and network use

- **No listening port.** The desktop app opens no network port. The window (`native/`) and the engine talk in JSON lines over
  the engine's standard input and output (`app/engine_pipe.py`, through the in-memory bridge `app/bridge.py`). Meaning-based search asks Recall for Omarchy's
  EmbeddingGemma over a local Unix socket in the user's runtime folder; nothing of it leaves the computer.
- **One use of the network, started by the person.** Setup's downloader (`engine/setup/`), when the person starts or
  resumes a download, or presses *Check for newer libraries* (one request for the published list). Opening setup reaches nothing. It goes through `engine/setup/download.py`, the only module that opens a connection: HTTPS on port
  443 only, to allow-listed hosts only (`downloads.lastbook.org` for upstream's packs and the manifest; Protomaps' build hosts for the map; `download.geonames.org` for place names), no proxy
  from the system, and a redirect is followed only to another allowed host.
- **Pinned trust.** The downloader trusts only the root certificates in `engine/setup/roots.pem` (6 roots from Mozilla's
  bundle), never the system's trust store. `python3 -I -B packaging/check_tls_roots.py` checks that those roots
  still cover the allowed hosts; it needs the network, so `bin/release` runs it and `bin/check` does not.
- **Checked files.** Every download is checked against the SHA-256 in the manifest before it is used. Zips are checked
  for unsafe paths, links and sizes before anything is written.
- **Nothing else.** The window blocks all web loads, there is no telemetry, no account, and nothing runs on a timer or at
  startup that reaches the network.
- **A working library is never broken.** New data is unpacked beside the old one, checked, and then switched in with a
  single rename. A failure at any step leaves the old library as it was.

The setup contract is in [docs/SETUP-API.md](docs/SETUP-API.md); the engine side is described in
[engine/setup/README.md](engine/setup/README.md).

## What's where

| Path | What it is |
|---|---|
| `native/` | The desktop window: a Rust crate (binary `lastbook`, which is also its app id, desktop entry and icon name) on GTK 4, WebKitGTK 6 and Poppler. It starts the engine as a child process. |
| `app/` | The engine's side of the app: the pipe the window talks to (`engine_pipe.py`), the command line (`cli.py`), library lookup and setup hooks (`main.py`), the no-port bridge (`bridge.py`), and tests (`app/tests/`). |
| `engine/` | Paths, the request handler's base and file rules (`library.py`), saved pages, the help points of `place --nearby` read from the street-level maps (`map_pois.py`) and setup (`engine/setup/`, which also makes the map). |
| `server.py`, `retrieval.py`, `encyclopedia.py` | The engine: request handler and the fused search (`server.hybrid`), meaning-based search (through Recall), and encyclopedia articles as passages. |
| `skills/` | The agent skill (`lastbook/SKILL.md`), built into the program and shipped in the package. |
| `content/` | The curated guides (`topics/*.json`) and what curators looked for and did not find (`gaps.json`). Format: `content/SCHEMA.md`. |
| `packaging/` | The Arch package (`arch/`: PKGBUILD, the manifest and map region list it ships, the package scan), the desktop entry (`linux/`), the icon (`icon/`), release manifest and TLS roots tools. |
| `tools/` | Curator helpers (`libq.py`, `validate_topics.py`), the map area list's maker (`make_region_list.py`), and the retrieval check (`eval/`). |
| `docs/` | How the window and the engine talk (`ENGINE.md`), the setup contract (`SETUP-API.md`) and how the offline map is put together (`MAPS.md`). |

## Run from source

You need Arch's Python 3.14 (the pinned libzim and pypdfium2 wheels are built for it), `python-numpy` and
`python-beautifulsoup4` from pacman, Rust, the GTK 4, WebKitGTK 6 and Poppler development files (the Arch packages in the
PKGBUILD's `depends`), and a **data folder**. A data folder holds `library-data/catalog.json` (the library) and gets a
`derived/` folder for the map, indexes, caches and logs. Get one by running the app once and letting setup download a
pack, or by unzipping a pack from lastbook.org.

```bash
cargo build --manifest-path native/Cargo.toml
LASTBOOK_DEV=1 PYTHONPATH=$(bin/test-deps) native/target/debug/lastbook --data /path/to/LastbookData
```

`bin/test-deps` unpacks the two wheels the PKGBUILD pins and prints their folder. `LASTBOOK_DEV=1` lets the engine
(`/usr/bin/python3 app/engine_pipe.py`, started by the window) take its dependencies from `PYTHONPATH`. Without `--data`, the engine checks `LASTBOOK_DATA`, the folder chosen last time, and its own data folder.
Existing libraries elsewhere are accessed when the person chooses them. If no library is available, it opens setup.
The full order is in `app/main.py`.

## How search finds passages

The window's Search and `lastbook search` both go through `server.hybrid()`, which fuses three ranked lists with
reciprocal-rank fusion and gives the best passages with their provenance (document, PDF page, video moment or article section):

1. **Keyword search**: the library's SQLite FTS5 index of every page (`search.sqlite`), BM25-ranked, with everyday words mapped
   to the words manuals use (`server.SYN`).
2. **Meaning-based search** (`retrieval.py`): the library cut into about 1,100-character passages, embedded once by Recall for
   Omarchy's EmbeddingGemma over its Unix socket and kept in `derived/index/` (passages in SQLite, vectors in one float16 file).
   A question is embedded and scored against every passage. The index records which model made it; another model empties and
   rebuilds it. Without Recall this list is empty.
3. **Encyclopedias** (`encyclopedia.py`): each ZIM file's own full-text and title indexes find candidate articles; with Recall,
   their openings and a few passages are ranked by meaning and weak ones dropped. Nothing is embedded ahead of time. The best
   article that clears the bar keeps the last place of a list of six or more, since library pages usually win on both
   other lists.

`tools/eval/run.py` measures
hits@8, MRR and timing on `tools/eval/questions.json` against a real data folder.

## Build

`packaging/arch/PKGBUILD` builds the package from the committed source (`git archive HEAD`): the window
(`cargo build --release --locked --manifest-path native/Cargo.toml`, installed as `/usr/bin/lastbook`) and the two Python packages Arch does not carry (libzim and pypdfium2, pinned wheels). Everything else comes from Arch. Check a
built package with `python3 packaging/arch/scan_package.py <package>`. Releases: `bin/release <version>` (with
`packaging/release-notes.md` written first) tags, builds both architectures natively in CI (`.github/workflows/packages.yml`,
`bin/build-packages`), signs the packages and repository databases locally and publishes them with `install.sh`.
Release day, in order: commit the release notes on main; publish this commit's `install.sh` at
`https://ferdousbhai.com/lastbook/install.sh` (verification compares the two); run `bin/release <version>` (a run
stopped part way is resumed by running it again); `verify-release.yml` then installs the release on both
architectures.

## Packs

- **Packs** are data folders: a library plus its catalog and search index (and upstream's US map data, which setup leaves out). They come pre-built from
  upstream Lastbook, on `downloads.lastbook.org`; nothing here builds them. Setup downloads the one the person picks and
  checks it against `setup/manifest-v1.json` (`packaging/arch/manifest-v1.json`; a newer copy is fetched from
  `downloads.lastbook.org` when setup opens and used only if it validates).
- **Meaning-based search** uses EmbeddingGemma through Recall for Omarchy, over a local socket. Without Recall running,
  search is keyword search.

## What is not in this repository, and why

- **The library** (`library-data/`): hundreds of gigabytes of third-party manuals, videos and encyclopedias, each under
  its own terms. It is distributed as packs, with a Sources and permissions page in each pack.
- **Maps**: large, and built from public data. Setup makes the map on the person's computer.
- **Generated data** (`derived/`, builds): indexes, caches and logs the engine makes as it runs, and build output.
- **Release and signing material**: signing keys, deploy settings and the release folders.
- **Private data**: a household's own home area, machine and user names, keys and internal security-audit notes. The
  guides here are in general wording and ship as they are. Contributors must not commit personal details; the package
  scan checks for home-folder paths.

## Tests

```bash
bin/test
```

It runs the window's tests (`cargo test`, then its screens driven offscreen by the scripts in `native/dev/qa`, where any
GLib warning fails the run; a script with a stand-in engine beside it shows what the window does while the engine is slow,
when it fails, and while a download runs) and the engine's (`python3 -m unittest`, with libzim and pypdfium2 from the
PKGBUILD's pinned wheels, `bin/test-deps`). `test_bridge` builds its own made-up library, which `test_cli` runs the commands against; `test_semantic` runs a stand-in
Recall; the others run without a library. `bin/check` adds the installer test.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Lastbook’s original code and
documentation use the [MIT License](LICENSE), permitting commercial use, modification and redistribution with the
copyright and license notice. Third-party software, media and library content retain their own licenses,
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and each library pack. Distributed packages include
GPL-covered components such as python-libzim and libzim and must also meet those components’ license obligations.

