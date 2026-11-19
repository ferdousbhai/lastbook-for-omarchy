# engine/setup: first-run setup, engine side

The contract is `docs/SETUP-API.md`. This package fills it in.

| File | What it does |
|---|---|
| `download.py` | The only module in Lastbook that opens a network connection. It uses HTTPS on port 443 to `ALLOWED_HOSTS` (and `*.hf.co`) and takes no proxy: it uses `http.client`, which never reads proxy settings. A redirect is followed only to an allowed address. A download resumes with Range. The ETag is compared here, because R2 ignores If-Range. The SHA-256 is computed while the file streams in. |
| `roots.pem` | The only root certificates the downloader trusts: 11 roots from Mozilla's bundle, those the allowed hosts' chains end in, plus their siblings. The system's trust store is never used. Rebuild with `packaging/setup_roots.py <certifi cacert.pem>`. The package scans require it. |
| `manifest.py` | Loads the bundled `setup/manifest-v1.json`, plus one fetch of `https://downloads.lastbook.org/manifest-v1.json` when setup opens. The fetched copy is used only if it validates and every URL is allowed. |
| `hardware.py` | Reads the OS, processor type, RAM, unified memory, GPUs and VRAM (from `/proc` and `/sys`) and free space per disk. It estimates whether the AI fits, or measures it with `lastbook-llm --measure` and `llm_local.judge`. |
| `install.py` | Checks a zip before writing anything: zip-slip, absolute paths, links, case clashes, encryption and the size against `installed_bytes`. Unpacks so that a stopped unpack can resume. Does the one-rename switch, keeps the old library, and rolls back if the new one does not open. |
| `api.py` | Handles `/api/setup/*`, runs the job (`job.json`, pause, resume, cancel and retry), recognizes files already on the computer, makes the recommendation, and provides setup mode's `SetupServer` for the bridge. |

## How it runs

- **Setup mode.** `app/main.py` finds no library. It opens the window on `SetupServer` through the bridge, with no socket. The page is `web/setup.html` at `/`. The job downloads into `<parent>/LastbookData.setup/downloads/` and unpacks into `.../unpack/LastbookData`. Then it writes `pending.json`. `finish` remembers the target in `settings.json` and relaunches with `execv`. On the next start, `api.startup()` makes the switch before the engine opens anything. After the engine starts, `api.opened()` marks the model checked (`llm_local.mark_checked`), records the old library and removes the work folder.
- **Normal mode.** `server.py` hands `/api/setup/` to `api.handle()`. `main.py` configures a manage-mode `Setup` for that. The setup page is at `/ui/setup.html`. An upgrade is staged the same way and switches on relaunch, so nothing is renamed while files are open. Adding only the AI moves the model into the open library and marks it checked at once.
- Only this computer gets answers: the bridge, or loopback when running as `server.py`.

## Routes added beyond the contract table

- `POST /api/setup/pick` returns `{path, free_bytes, disk}` from the system's folder picker, or `{path: null, cancelled: true}`. A chosen folder can be the install place (`start {folder}`) or something to `use`: a LastbookData folder, a folder holding one, or a folder holding a pack zip.
- `POST /api/setup/remove-ai` deletes the model file from the open library.
- `POST /api/setup/start {repair: true, ai}` re-checks every file against `pack-manifest.tsv` and the model's SHA-256, and fetches again only what is wrong (the whole pack zip, then switches as an upgrade does).
- `POST /api/setup/delete-old {path}` deletes a `LastbookData.old-*` that setup itself moved aside. These are listed in `state.current.old`.
- `state.recommend` also carries `folder` and `need_bytes` (per pack). Each `found` entry is `{kind: library|pack|model, path, name, id, size, size_ok}`.

## Tests

The tests use a local HTTPS test server, which exists only in the test file. It can cut the connection, send corrupt bytes, change the ETag, ignore Range and redirect to bad hosts. The tests also cover kill and restart mid-download and mid-unpack, unpack guards, switching, rollback, upgrades, recommendations, and the bridge:

```bash
python3 -W ignore -B app/tests/test_setup.py
```

The bundled manifest is built from `derived/release/manifest-v1.json` (publish-packs), plus the model constants in `llm_local.py`:

```bash
python3 -I -B packaging/release_manifest.py --small-from derived/release/1.0.0-20261104-1507
```
