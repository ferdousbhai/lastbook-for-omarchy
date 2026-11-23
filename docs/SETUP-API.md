# Lastbook setup: the contract between the window and the engine

One package per computer (Arch/Omarchy). On first open, Lastbook finds no library and
opens **setup** in its own window. Setup recommends a library (Small, Medium or All) by the room on the disk, offers the
map of the world and street-level areas, then downloads, checks, unpacks and switches to it. No separate library
download, no manual unzip. Setup also opens later from inside the app (*Library and downloads*, in the sidebar and under
About) to upgrade, add map areas, or repair.

The screens are `native/src/setup.rs`; the engine side is `engine/setup/` ([its README](../engine/setup/README.md)).

## Rules this follows

- **The one way out.** Setup's downloader is the only code in Lastbook that may reach the internet, and only when the person
  starts or resumes a download. It fetches over HTTPS only from hosts in `ALLOWED_HOSTS` (below), with no proxy taken from
  the system, and follows a redirect only to another allowed host. Everything else stays as it is: the window blocks all web
  loads, and nothing is sent anywhere (no telemetry, no update check: updates come from pacman). This route covers
  library packs and the map, and nothing more.
- **A working library is never broken.** New data is downloaded and unpacked beside the old one, checked, then switched
  in one rename. A failure at any step leaves the old library as it was.

## The manifest (`manifest-v1.json`)

The package carries a copy (`packaging/arch/manifest-v1.json`, installed as `setup/manifest-v1.json`;
`packaging/release_manifest.py` rewrites and checks it). The packs it lists are upstream Lastbook's, pre-built. Setup may
also fetch `https://downloads.lastbook.org/manifest-v1.json` (one GET, only when the person presses *Check for newer libraries* in setup) to learn of
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
  ]
}
```

`installed_bytes` is the unpacked size, measured (upstream's map data in it, below, is not unpacked). A pack with `"available": false` shows as *coming later* and can't be chosen.
Packs may be split into several files later; setup downloads all `files` of a pack, in order. Anything else in a
manifest is ignored.

`ALLOWED_HOSTS`: `downloads.lastbook.org` (packs, the manifest), `build-metadata.protomaps.dev` and
`build.protomaps.com` (the map), and `download.geonames.org` (place names). R2 ignores If-Range, so the downloader compares
the ETag itself. The exact list is a constant in
`engine/setup/download.py`, and the package scan checks that it is the only module that opens connections.

## Where things go

- The library folder stays **LastbookData** (layout unchanged: `library-data/`, `derived/`). Default place:
  `$XDG_DATA_HOME/Lastbook/LastbookData` (`~/.local/share/Lastbook/LastbookData`), or a folder they choose, on a disk
  with room. `settings.json` remembers it as before (`app/main.py`).
- Work in progress: `<parent>/LastbookData.setup/`, holding `downloads/` (partial files with a `.part` suffix and a
  `.json` beside each: url, size, sha256, bytes done, ETag) and `unpack/` (the new LastbookData being built).
- Upstream's packs ship US-only map data: `derived/geo/` `national.sqlite`, `national-status.json`, `map.sqlite`,
  `places.sqlite`, `water.sqlite`, `terrain.sqlite`, `crops.sqlite`, `socal-cells.json` and `addresses/`, plus
  `library-data/geography/resources/atlas.sqlite` and a state map's `map-set.json` (`install.DROPPED`). Nothing in this
  fork reads them, so setup does not unpack them (about 1.7 GB less on Small), an upgrade does not carry them, and repair
  treats them as meant to be absent. `install.drop_upstream_maps(library)` removes them from a library installed before.
- An upgrade carries only the maps made on this computer (`world.mbtiles`, `world-places.sqlite`, `regions-*.mbtiles`).
  An old library holding a folder no pack ships (an upstream version's `models/`, say) is not deleted: the new library
  opens, and the start note says the old one was kept and why.
- Space needed = download size + unpacked size + 2 GB, on the disk that holds the target. A zip is deleted once it is
  unpacked and checked. Setup refuses to start, with the numbers, when there is not enough room.
- Files already here are recognized: a `Lastbook-*.zip` in the app's own data folder, in the open
  library, or one the person chose (`use`), and an existing LastbookData where `app/main.py` looks
  (the remembered one and the app's own). A file is used only after its size and SHA-256 match the manifest.

## Engine interface: `/api/setup/*`

JSON over the engine's pipe and bridge, so no port. In setup mode (no library yet) these
are served by `engine/setup/api.py` alone. In normal mode `server.py` hands `/api/setup/` to the same module.

| Request | Answer |
|---|---|
| `GET /api/setup/state` | `{mode: "first-run" or "manage", hardware, manifest: {source: "bundled" or "fetched", published, packs}, current: {folder, pack, content_revision, osm} or null, found: [...], recommend: {pack, why}, job, map_regions, world_bytes}` |
| `POST /api/setup/start` `{pack, folder?, world?, regions?}` | the new `job`, or `{error, need_bytes, free_bytes}` |
| `GET /api/setup/job` | `job` |
| `POST /api/setup/check` | the one GET of the published manifest (waited for up to 10 s), then the same answer as `state`. Only when the person presses *Check for newer libraries*: `state` itself reaches nothing |
| `POST /api/setup/pause`, `/resume`, `/cancel`, `/retry` | `job` |
| `POST /api/setup/use` `{path}` | uses a library or file already on this computer: `job` (verify, then unpack or activate) |
| `POST /api/setup/finish` | `{ok: true, folder}`; the window then starts the engine again on that library (see below) |
| `POST /api/setup/start` `{repair: true}` | manage mode: re-checks every file against the library's `pack-manifest.tsv` (item `verifying`), except upstream's map data, which is meant to be absent; if any is wrong the pack is downloaded and switched to like an upgrade. All well: `done`, nothing downloaded |

Also: `recommend.folder` (where it would go) and `recommend.need_bytes` (`{pack id: bytes}` per pack); each `found` entry is
`{kind: "library" or "pack", path, name, id, size, size_ok}`: a pack zip only under its published name. An error answer is `{error, code}` (with HTTP 400,
409 for `busy`/`not_done`, 507 for `no_space`). An upgrade or a new library is switched to when the app restarts
after `finish` (the job ends `done` with the switch staged), so nothing is renamed while it is open.

**The map.** `world: true` asks for the world map and place search; `regions` is a list of ids from `map_regions`
(`[{id, name, parent, bbox}]`, Geofabrik's regions as bundled; `bbox` is [w, s, e, n], with w > e for a region across the
antimeridian, which also has `bboxes`, its box each side of ±180). Each becomes a job item of `kind: "osm"` (ids `world`,
`places`, or the region id), made on this computer by `engine/setup/maps.py`; a region's size is known only once its turn
comes and its tiles are counted. `world_bytes` is the estimate shown before that. `current.osm` =
`{world, places, regions: [{id, name, bytes}]}` says what the open library already has; `places` is false for a place
search made by an earlier version (towns of 15,000+ only, `maps.places_current`), so `{world: true}` makes it again, and
`world_bytes` is then the estimate for place search alone. With a pack in the same `start`
the files go into the staged library; without one they go straight into the open library.

`hardware` = `{disks: [{path, free_bytes, total_bytes}]}`: the app's own data folder and the open library's disk,
nothing else. The recommendation is made from it.

`job` = `{id, state, items: [{id, kind: "pack" or "osm", title, state, bytes_done, bytes_total, rate_bps, eta_s,
error}], error, started, folder}`. Each item also has `source`: `"found"` when its file is already on this computer
(it is checked, not downloaded), else `"download"` (it changes to `"download"` when a found file is rejected). After a restart, an
item that stopped mid-run has `paused_from` (`downloading`, `verifying`, `unpacking` or `activating`), dropped on resume, and its
`bytes_done`/`bytes_total` describe that step: unpacked bytes of the installed size for `unpacking`, the whole file for a found item. States in order: `waiting`, `downloading`, `paused`, `verifying`, `unpacking`,
`activating`, `done`, or `failed` (with `error`: a plain sentence and a `code`: `no_space`, `network`, `disk` (this computer's disk could not be written: not retried by itself), `checksum`, `unpack`,
`activate`, `cancelled`, and `found_mismatch`: a file already on this computer (`use`) did not match its published SHA-256; the error carries `file`, the file is left untouched and no longer offered, and `retry` downloads it instead). The job lives in the app's own settings folder (`<settings>/setup/job.json`, with a staged switch in `<settings>/setup/pending.json`; never in `LastbookData.setup`, whose contents are not obeyed, because that folder can arrive with a copied library or a drive), so a restart picks it up as `paused` and resumes
with HTTP Range from the bytes already on disk. A map (`osm`) item carries on from its part file when the same daily map build is still the newest, and starts over otherwise. A corrupt download (wrong SHA-256) is deleted and downloaded once more
automatically, then fails with `checksum`.

## Switching to the library

Each time the job is saved, which is each time its state changes, the engine sends the window `{"job": state}` unasked
(`on_job`), and the window reads `job` again. Closing the window while a job runs here asks first: finish in the
background (the window is put away, the engine carries on, and the window comes back when the job stops), stop (the job
is paused, then the program leaves) or keep the window open, which is also what Escape answers.

`finish` answers `{ok, folder}`. `app/engine_pipe.py` then remembers the folder in `settings.json` and sends the window
`{"restart": folder}`; the window starts the engine again, and `api.startup()` makes the staged switch before the new
engine opens anything. An upgrade (for example Small to Medium) renames the old LastbookData to
`LastbookData.old-<date>` only once the new one is complete. If the new one does not open, the old one is put back. Once
the new one has opened, the old one is deleted without asking (`api.opened`, in its own thread): only a real folder beside the
open library, named `<library>.old-<8 digits>` (or with `-<n>` after it), that setup itself moved aside. Until it is gone it
is listed under `old` in the app's `setup.json`, so a delete cut short carries on at the next start. There is no
`delete-old` request and no `current.old` any more.
