# Lastbook setup: the contract between the window and the engine

One package per computer (Arch/Omarchy). On first open, Lastbook finds no library and
opens **setup** in its own window. Setup shows what this computer has, recommends a library (Small, Medium or All) and whether
the optional local AI fits, offers the map of the world and street-level areas, then downloads, checks, unpacks and switches
to it. No separate library or model download, no manual unzip. Setup also opens later from inside the app (Library →
*Library and local AI*) to upgrade, add the AI or map areas, or repair.

The screens are `native/src/setup.rs`; the engine side is `engine/setup/` ([its README](../engine/setup/README.md)).

## Rules this follows

- **The one way out.** Setup's downloader is the only code in Lastbook that may reach the internet, and only when the person
  starts or resumes a download. It fetches over HTTPS only from hosts in `ALLOWED_HOSTS` (below), with no proxy taken from
  the system, and follows a redirect only to another allowed host. Everything else stays as it is: the window blocks all web
  loads, the assistant talks to loopback or pipes only, and nothing is sent anywhere (no telemetry, no update check:
  updates come from pacman). This route covers library packs, the model and the map, and nothing more.
- **No blanket memory floor.** The local AI is offered when the measured fit says it fits (`llm_local.judge`, about 18 GB
  of memory for model and context, plus a 2.5 GiB reserve). It is not offered below that, and the screen says why in plain words.
- **A working library is never broken.** New data is downloaded and unpacked beside the old one, checked, then switched
  in one rename. A failure at any step leaves the old library as it was.

## The manifest (`manifest-v1.json`)

The package carries a copy (`packaging/arch/manifest-v1.json`, installed as `setup/manifest-v1.json`;
`packaging/release_manifest.py` rewrites it so the model entry matches `llm_local.py`). The packs it lists are upstream
Lastbook's, pre-built. Setup may
also fetch `https://downloads.lastbook.org/manifest-v1.json` (one GET, only when the person opens setup online) to learn of
packs published after the app was built. A fetched manifest is used only if it parses, its `version` is 1, and every URL is
on an allowed host.

```json
{
  "version": 1,
  "published": "2026-11-12",
  "packs": [
    {"id": "small", "title": "Small", "summary": "…one plain sentence…", "records": 315,
     "installed_bytes": 8236931672, "available": true,
     "files": [{"name": "Lastbook-Small.zip", "url": "https://downloads.lastbook.org/1.0.0/Lastbook-Small.zip",
                "size": 8236931672, "sha256": "…", "kind": "zip"}]}
  ],
  "models": [
    {"id": "gemma-4-26b-a4b", "title": "Gemma 4 26B A4B", "file": "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
     "url": "https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF/resolve/c099eb48e663fd284577b04978a94ffccb261841/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf?download=true",
     "size": 16947541728, "sha256": "f2c28b3dc4776931ac6f879e11f203dec637ea0f14267a86ec8f6165f63f293f",
     "memory_bytes": 19027384256, "available": true}
  ]
}
```

`installed_bytes` is the unpacked size, measured. A pack with `"available": false` shows as *coming later* and can't be chosen.
Packs may be split into several files later; setup downloads all `files` of a pack, in order.

`ALLOWED_HOSTS`: `downloads.lastbook.org` (packs, state maps, the manifest), `huggingface.co` and the hosts Hugging Face
redirects model downloads to (`cas-bridge.xethub.hf.co`, `*.hf.co`), `build-metadata.protomaps.dev` and
`build.protomaps.com` (the map), and `download.geonames.org` (place names). R2 ignores If-Range, so the downloader compares
the ETag itself. The exact list is a constant in
`engine/setup/download.py`, and the package scan checks that it is the only module that opens connections.

## Where things go

- The library folder stays **LastbookData** (layout unchanged: `library-data/`, `derived/`). Default place:
  `$XDG_DATA_HOME/Lastbook/LastbookData` (`~/.local/share/Lastbook/LastbookData`), or a folder they choose, on a disk
  with room. `settings.json` remembers it as before (`app/main.py`).
- Work in progress: `<parent>/LastbookData.setup/`, holding `downloads/` (partial files with a `.part` suffix and a
  `.json` beside each: url, size, sha256, bytes done, ETag) and `unpack/` (the new LastbookData being built).
- The model goes to `LastbookData/models/<file>`. After its SHA-256 matches, setup calls `llm_local.mark_checked(path)`, so
  the AI does not read 17 GB a second time.
- Space needed = download size + unpacked size + 2 GB, on the disk that holds the target. A zip is deleted once it is
  unpacked and checked. Setup refuses to start, with the numbers, when there is not enough room.
- Files already here are recognized: a `Lastbook-*.zip` or the model file in the app's own data folder, in the open
  library or its `models/` folder, or one the person chose (`use`), and an existing LastbookData where `app/main.py` looks
  (the remembered one and the app's own). A file is used only after its size and SHA-256 match the manifest.

## Engine interface: `/api/setup/*`

JSON over the engine's pipe and bridge, so no port. In setup mode (no library yet) these
are served by `engine/setup/api.py` alone. In normal mode `server.py` hands `/api/setup/` to the same module.

| Request | Answer |
|---|---|
| `GET /api/setup/state` | `{mode: "first-run" or "manage", hardware, manifest: {source: "bundled" or "fetched", packs, models, maps, address_indexes}, current: {folder, pack, model, map, address_indexes, osm} or null, found: [...], recommend: {pack, ai, why}, job, map_regions, world_bytes}` |
| `POST /api/setup/start` `{pack, ai: true or false, folder?, world?, regions?, map?, address_id?}` | the new `job`, or `{error, need_bytes, free_bytes}` |
| `GET /api/setup/job` | `job` |
| `POST /api/setup/pause`, `/resume`, `/cancel`, `/retry` | `job` |
| `POST /api/setup/use` `{path}` | uses a library or file already on this computer: `job` (verify, then unpack or activate) |
| `POST /api/setup/finish` | `{ok: true, folder}`; the window then starts the engine again on that library (see below) |
| `POST /api/setup/remove-ai` | manage mode: deletes `LastbookData/models/<file>` (the library stays): `{ok, freed_bytes}` |
| `POST /api/setup/start` `{repair: true, ai}` | manage mode: re-checks every file against the library's `pack-manifest.tsv` and the model's SHA-256 (items `verifying`); fetches again only what is wrong (the pack: downloaded and switched to like an upgrade; the model: replaced in place). All well: `done`, nothing downloaded |

Also: `recommend.folder` (where it would go) and `recommend.need_bytes` (`{pack id: bytes}` per pack); each `found` entry is
`{kind: "library" or "pack" or "model", path, name, id, size, size_ok}`. An error answer is `{error, code}` (with HTTP 400,
409 for `busy`/`not_done`, 507 for `no_space`). An upgrade or a new library is switched to when the app restarts
after `finish` (the job ends `done` with the switch staged), so nothing is renamed while it is open.

**The map.** `world: true` asks for the world map and place search; `regions` is a list of ids from `map_regions`
(`[{id, name, parent, bbox}]`, Geofabrik's regions as bundled). Each becomes a job item of `kind: "osm"` (ids `world`,
`places`, or the region id), made on this computer by `engine/setup/maps.py`; a region's size is known only once its turn
comes and its tiles are counted. `world_bytes` is the estimate shown before that. `current.osm` =
`{world, places, regions: [{id, name, bytes}]}` says what the open library already has. With a pack in the same `start`
the files go into the staged library; without one they go straight into the open library.

`hardware` = `{ram_bytes, unified_memory: true or false, gpus: [{name,
vram_bytes}], disks: [{path, free_bytes, total_bytes}], ai: {fits, needs_bytes, why}}`. `disks` covers the app's own
data folder and any folder the person chose, nothing else.
`ai.fits` comes from `llm_local.judge` of a real `lastbook-llm --measure` once the model file is present. Before that, it is
an estimate from `memory_bytes` and RAM (plus a graphics card's own memory, read from `/sys` and the NVIDIA driver's files;
Apple silicon under Asahi counts as one unified memory), marked `"estimate": true`.

`job` = `{id, state, items: [{id, kind: "pack", "map", "addresses", "osm" or "model", title, state, bytes_done, bytes_total, rate_bps, eta_s,
error}], error, started, folder}`. Each item also has `source`: `"found"` when its file is already on this computer
(it is checked, not downloaded), else `"download"` (it changes to `"download"` when a found file is rejected). After a restart, an
item that stopped mid-run has `paused_from` (`downloading`, `verifying`, `unpacking` or `activating`), dropped on resume, and its
`bytes_done`/`bytes_total` describe that step: unpacked bytes of the installed size for `unpacking`, the whole file for a found item. States in order: `waiting`, `downloading`, `paused`, `verifying`, `unpacking`,
`activating`, `done`, or `failed` (with `error`: a plain sentence and a `code`: `no_space`, `network`, `checksum`, `unpack`,
`activate`, `cancelled`, and `found_mismatch`: a file already on this computer (`use`) did not match its published SHA-256; the error carries `file`, the file is left untouched and no longer offered, and `retry` downloads it instead). The job lives in the app's own settings folder (`<settings>/setup/job.json`, with a staged switch in `<settings>/setup/pending.json`; never in `LastbookData.setup`, whose contents are not obeyed: audit E3), so a restart picks it up as `paused` and resumes
with HTTP Range from the bytes already on disk. A corrupt download (wrong SHA-256) is deleted and downloaded once more
automatically, then fails with `checksum`.

## Switching to the library

`finish` answers `{ok, folder}`. `app/engine_pipe.py` then remembers the folder in `settings.json` and sends the window
`{"restart": folder}`; the window starts the engine again, and `api.startup()` makes the staged switch before the new
engine opens anything. An upgrade (for example Small to Medium) renames the old LastbookData to
`LastbookData.old-<date>` only once the new one is complete. If the new one does not open, the old one is put back. Once
the new one has opened, the old one is deleted without asking (`api.opened`, in its own thread): only a real folder beside the
open library, named `<library>.old-<8 digits>` (or with `-<n>` after it), that setup itself moved aside. Until it is gone it
is listed under `old` in the app's `setup.json`, so a delete cut short carries on at the next start. There is no
`delete-old` request and no `current.old` any more.

## State maps

**Engine and upstream packs only: the native window does not offer state maps and does not draw them.** The engine still
installs, checks and repairs one where a request or an existing library has it, and place and address search read its
files ([MAPS.md](MAPS.md), "US map files from upstream's packs").

A state map is upstream Lastbook's detailed map set for one US state: local streets, buildings, terrain, crops and the
resource layers (water points, help points, atlas). It is self-contained: the base map plus every row inside the state's
box, so it **replaces** the library's map set and never merges with it. One state per library; choosing another replaces
the first. `address_indexes` in the manifest are per-state street-address indexes, installed the same way
(`start {address_id}`, item `kind: "addresses"`).

Manifest: a new top-level list, alongside `packs` and `models`. A fetched manifest merges by `id`, as packs do.

```json
"maps": [
  {"id": "CA", "title": "California", "kind": "state", "installed_bytes": 5000000000, "available": true,
   "includes": "Local streets, buildings, terrain and crops, plus water, help and resource points",
   "files": [{"name": "Lastbook-Map-CA.zip", "url": "https://downloads.lastbook.org/maps/Lastbook-Map-CA.zip",
              "size": 0, "sha256": "…", "kind": "zip"}]}
]
```

The zip unpacks to the same layout as a pack, holding only the map set's files: `derived/geo/*.sqlite`,
`derived/geo/national-status.json`, `derived/geo/socal-cells.json` and `library-data/geography/resources/atlas.sqlite`,
plus `map-set.json` naming the state. Nothing else is allowed in it. Unpacking guards are the same as for packs.

Setup: `POST /api/setup/start {pack, ai, map: "CA" | null}`. A map is a third job item, `kind: "map"`. The space
needed counts the map's download plus installed bytes. Installing it builds the new map set in
`LastbookData.setup/unpack/` and switches the listed files in one step, keeping the old map set until the library
opens with the new one. `state.current.map` reports the installed set (`base`, `us` or a state code, from
`map-set.json`). A pack that already contains a detailed map (Medium has California; All has the whole US) says so in
its `map`, and choosing a state on Medium replaces its California map. All needs no state map.

In detail:
- Job item `kind: "map"` comes after the pack and before the model: `waiting`, `downloading`, `unpacking` (bytes of
  `installed_bytes`), `done`. With a pack, the state's files replace the pack's own map set inside the staged library.
  On its own (manage mode, `start {map: "CA"}` with no `pack`), the job ends `done` with the swap staged. The swap happens
  when the app restarts after `finish`: the old map set's files, with their SQLite `-wal`/`-shm` files, move to
  `LastbookData.setup/map-old/`, the new ones move in, and if the result does not check out (every file `map-set.json`
  lists, at its size) the old set is put back. `map-old/` is deleted once the library has opened.
- A map zip may hold only the map-set files listed above (it fails with `unpack` otherwise), and `map-set.json`'s `name`
  must equal the entry's `id`. Its files may sit under a top `LastbookData/` folder or at the zip's top (as the published
  state zips do); a pack zip still needs the folder.
- `state.current.map` is `map-set.json`'s `name`, else `pack.json`'s `map`, else `"base"`. Asking for the state already
  there answers `{code: "nothing"}`. In first-run setup, a map needs a pack in the same `start`.
- `state.recommend.map_need_bytes` = `{map id: download + installed bytes}`; `start`'s space check adds the map.
  `state.manifest.maps` lists the entries (without URLs).
- `found[]` also lists a `Lastbook-Map-*.zip` already on this computer (`kind: "map"`); `use` on it starts the map job
  in manage mode, or is used by the next `start` in first-run setup.
- Repair skips the map files when a `map-set.json` is present; those are checked against it instead.
