# engine/setup: first-run setup, engine side

The contract is `docs/SETUP-API.md`. This package fills it in.

| File | What it does |
|---|---|
| `download.py` | The only module in Lastbook that opens a network connection. It uses HTTPS on port 443 to `ALLOWED_HOSTS` (and `*.hf.co`) and takes no proxy: it uses `http.client`, which never reads proxy settings. A redirect is followed only to an allowed address. A download resumes with Range. The ETag is compared here, because R2 ignores If-Range. The SHA-256 is computed while the file streams in. |
| `roots.pem` | The only root certificates the downloader trusts: 11 roots from Mozilla's bundle, those the allowed hosts' chains end in, plus their siblings. The system's trust store is never used. Rebuild with `packaging/setup_roots.py <certifi cacert.pem>`. The package scans require it. |
| `manifest.py` | Loads the bundled `setup/manifest-v1.json`, plus one fetch of `https://downloads.lastbook.org/manifest-v1.json` when setup opens. The fetched copy is used only if it validates and every URL is allowed. |
| `hardware.py` | Reads RAM, unified memory, GPUs and VRAM (from `/proc` and `/sys`) and free space per disk. It estimates whether the AI fits, or measures it with `lastbook-llm --measure` and `llm_local.judge`. |
| `install.py` | Checks a zip before writing anything: zip-slip, absolute paths, links, case clashes, encryption and the size against `installed_bytes`. Unpacks so that a stopped unpack can resume. Does the one-rename switch, keeps the old library, and rolls back if the new one does not open. |
| `maps.py` | Makes the map on the person's computer: cuts `world.mbtiles` and `regions-<id>.mbtiles` out of Protomaps' newest daily OpenStreetMap build (PMTiles, range requests through `download.py`) and builds `world-places.sqlite` from GeoNames. The region list is the bundled `setup/map-regions.json`. |
| `api.py` | Handles `/api/setup/*`, runs the job (`job.json`, pause, resume, cancel and retry), recognizes files already on the computer, makes the recommendation, and provides setup mode's `SetupServer` for the bridge. |

## How it runs

- **Setup mode.** `app/main.py` finds no library. `app/engine_pipe.py` puts `SetupServer` behind the bridge, with no socket: it answers `/api/setup/*` only. The job downloads into `<parent>/LastbookData.setup/downloads/` and unpacks into `.../unpack/LastbookData`. Then it writes `pending.json`. `finish` answers `{ok, folder}`; `engine_pipe.py` remembers the target in `settings.json` and sends the window `{"restart": folder}`, which starts the engine again. On the next start, `api.startup()` makes the switch before the engine opens anything. After the engine starts, `api.opened()` marks the model checked (`llm_local.mark_checked`), removes the work folder and deletes the old library (below).
- **Normal mode.** `app/engine_pipe.py` configures a manage-mode `Setup` for the open library, and `server.py` hands `/api/setup/` to `api.handle()`. The screens are the window's own (`native/src/setup.rs`, under About → Library and downloads). An upgrade is staged the same way and switches when the engine restarts, so nothing is renamed while files are open. Adding only the AI moves the model into the open library and marks it checked at once; map files made without a pack go straight into the open library.
- Requests arrive only through the pipe and the bridge. Nothing listens.
- **The old library after an upgrade.** The switch renames it to `LastbookData.old-<date>` and `pending.json` remembers it, so `apply_pending` can put it back if the new library does not open. `api.opened()` runs only after the engine has opened the new library: it removes `pending.json`, then deletes the old folder in a background thread. `install.is_old_of` decides what may be deleted: a real folder (never a link) directly beside the open library, named `<library>.old-<8 digits>` with an optional `-<n>`, and listed by setup itself (this switch, or `old` in `setup.json` from an earlier one). A folder stays in that list until it is gone, so a delete cut short carries on at the next start.

## Routes added beyond the contract table

- `POST /api/setup/start {repair: true, ai}` re-checks every file against `pack-manifest.tsv` and the model's SHA-256, and fetches again only what is wrong (the whole pack zip, then switches as an upgrade does).
- `state.recommend` also carries `folder` and `need_bytes` (per pack). Each `found` entry is `{kind: library|pack|model, path, name, id, size, size_ok}`.

## Tests

The tests use a local HTTPS test server, which exists only in the test file. It can cut the connection, send corrupt bytes, change the ETag, ignore Range and redirect to bad hosts. The tests also cover kill and restart mid-download and mid-unpack, unpack guards, switching, rollback, upgrades, recommendations, and the bridge:

```bash
PYTHONPATH=$(bin/test-deps) python3 -W ignore -B -m unittest app.tests.test_setup app.tests.test_maps
```

`bin/test` runs these with the rest. The manifest the package ships is `packaging/arch/manifest-v1.json`; its packs are upstream's, and `packaging/release_manifest.py` rewrites it so the model entry matches the constants in `llm_local.py`:

```bash
python3 -I -B packaging/release_manifest.py
```
