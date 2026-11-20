# The engine

The window (`native/`, Rust and GTK 4) starts the engine as a child process and talks to it over the child's standard
input and output. The engine is Python. It opens no port and no socket of its own.

## How a request travels

1. `native/src/engine.rs` writes one JSON line: `{"id": 7, "method": "GET", "path": "/api/find?q=water"}`.
2. `app/engine_pipe.py` reads it and hands it to the bridge (`app/bridge.py`), which runs the engine's request handler
   (`server.Handler`, on `engine/library.py`'s base) on a worker thread over in-memory streams. Small requests, PDF page
   images and large files each have their own workers, so a flood of one never holds up another.
3. The answer comes back as lines with the same id: one `head` (status and type), any number of `chunk` (text) or `b64`
   (anything else) lines as the handler writes them, and one `end`. `{"cancel": 7}` stops a request.

The first line the engine writes is `{"ready": {...}}` with the data and library folders (and `notice`, a sentence, when
a library switch failed or was undone at this start). The window reads thumbnails,
PDFs and videos from those folders directly; only answers that need the engine come through the pipe. With no library,
the engine starts in setup and answers `/api/setup/*` only ([SETUP-API.md](SETUP-API.md)).

## What the window asks for

| Path | Answer |
|---|---|
| `/api/topics` | The curated guides, with each cited source resolved from the catalog; items this library does not hold are left out |
| `/api/find?q=&kind=` | Plain-language keyword search, grouped by document |
| `/api/retrieve?q=&k=&kind=` | The best passages (keyword, meaning-based and encyclopedia search, fused), with where each came from |
| `/api/doc/<id>` | One catalog record |
| `/api/transcript/<video id>` | Timed transcript cues |
| `/api/videos`, `/api/videos/all?q=` | The Videos screen's rows; a search of every video |
| `/api/shelves`, `/api/shelf/<id>` | The library by plain-language shelf |
| `/api/places?q=` | Place search: addresses, ZIP codes, towns and named places (US data from an upstream pack), and the world's countries, regions and towns (GeoNames, built in setup) |
| `/api/library` | What this library holds, and the state of the meaning-based search index (`index`) |
| `/api/setup/*` | About → Library and downloads ([SETUP-API.md](SETUP-API.md)) |
| `/zim/<file>/<article>` | An encyclopedia article, its scripts and web links taken out (`engine/saved_pages.py`) |
| `/documents/…`, `/pages/…`, `/videos/…` and the other library folders | A file of the library. A saved web page is rewritten to run no code; a PDF comes as a page of pictures PDFium has drawn, never as the file; of a video only the first 8 MiB come (the window plays the library's videos from disk, not through here) |

Only the library's served folders are readable, judged by where a path really leads (`engine/library.py`, `file_ok`): no
catalog, index, log, hidden file or link out of the library. Article and saved pages are shown in a WebKit view with
JavaScript off and every address but `lastbook:` blocked (`native/src/web.rs`).

## Search

- **Keyword search** is the library's own FTS5/BM25 index, `library-data/search.sqlite`, read only. It comes with the pack.
- **Meaning-based search** (`retrieval.py`) asks Recall for Omarchy for EmbeddingGemma vectors over its local socket. The
  index is `derived/index/semantic.sqlite` plus `vectors.f16` (append-only). It updates incrementally: the engine
  re-checks the catalog every 5 minutes and embeds only new or changed documents and transcripts (one writer at a time,
  by file lock). Without Recall, search is keyword search.
- **The two together** (`server.hybrid`): keyword, meaning-based and encyclopedia (`encyclopedia.py`) results fused into
  one list of passages, each with its document and page, video moment or article section. The window's Search shows
  them, and `lastbook search` prints them.

Lastbook runs no model of its own and has no assistant. An AI agent on the computer answers questions from the library through
the command line (`app/cli.py`: `search`, `read`, `page`, `transcript`, `place`, `guides`) and the agent skill
(`skills/lastbook/SKILL.md`), which tells it to search, read the pages and cite document title and page.

## What stays correct as a library changes

- Nothing in `library-data/` is written. Source details are read from `catalog.json`, reloaded when the file changes.
- Curated guides cite catalog ids. Run `tools/validate_topics.py` against a library after changing a guide.
- Everything the engine makes goes under `derived/` in the data folder: `index/` (meaning-based search), `cache/`
  (rendered PDF pages) and `logs/`.
