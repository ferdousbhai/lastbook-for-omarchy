# The engine

The window (`native/`, Rust and GTK 4) starts the engine as a child process and talks to it over the child's standard
input and output. The engine is Python. It opens no port and no socket of its own.

## How a request travels

1. `native/src/engine.rs` writes one JSON line: `{"id": 7, "method": "GET", "path": "/api/find?q=water"}` (a POST
   adds `"body"`). Any other line (not a JSON object, or with no number or string as its id) is passed over.
2. `app/engine_pipe.py` reads it and hands it to the bridge (`app/bridge.py`), which runs the engine's request handler
   (`server.Handler`, on `engine/library.py`'s base) on a worker thread over in-memory streams. Small requests, PDF page
   images and large files each have their own workers, so a flood of one never takes a worker from another; the small
   requests' lane has many (48), so a slow search does not hold up a quick request behind it.
3. The answer comes back as lines with the same id: one `head` (status and type), any number of pieces as the handler
   writes them, and one `end`. A JSON answer's pieces are `chunk` (text); anything else comes as `b64`, byte for byte.
   `"end": false` means the answer stopped short, and the window treats it as no answer. An answer over 64 MiB is
   refused (403) before any of it moves, since the window holds each answer whole.
4. A line with no id is something the engine says unasked: `{"restart": folder}` when setup has finished, and
   `{"job": state}` each time a setup job's state changes. The window reads the job then, so it asks on a timer only
   for a running job's progress, and only while its setup page is on screen in the window in use.

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
| `/api/text/<id>` | A document's extracted text as `{pages: [string, …]}`, one string a page (a PDF's file pages, form feeds in `text/` split): the reader's find in the document and "Copy this page's text"; 404 for what is not a PDF, saved page or slides |
| `/api/related?doc=<id>` or `?path=<address>` | `{results: [...]}`: up to five other documents on the same subject, shaped as `/api/retrieve`'s passages with no page, time or excerpt (each opens at its start). `path` is the web view's address (`/pages/...`, `/zim/<file>/<article>`). By meaning when the index holds the document: the documents whose passages lie nearest the average of its own vectors (`retrieval.Index.centroid`; no Recall needed); otherwise, as for an encyclopedia article, `server.hybrid` on its title |
| `/api/search-status` | Where meaning-based search stands, as `lastbook status` reports it under `meaning_search` (`app/cli.py` `meaning`): `ready`, `complete`, `coverage`, `state`, `recall: {installed, running, …}`, counts. The Search screen's line |
| `/api/transcript/<video id>` | Timed transcript cues |
| `/api/videos`, `/api/videos/all?q=` | The Videos screen's rows; a search of every video |
| `/api/shelves`, `/api/shelf/<id>` | The library by plain-language shelf |
| `/api/places?q=` | Place search: the world's countries, regions and towns (GeoNames, built in setup), or a point given as LAT,LON; each with `match` and `onMap` |
| `/api/near?lat=&lon=` | The Map screen's nearest help around a point: `{covered, points}`, each point `{cls, label, name, lat, lon, km, dir}`, nearest first, up to five of each kind the map marks; no points where no street-level map covers the point. With `&to=LAT,LON`: `{km, dir}` from the point to that one. 400 for a point that is none |
| `/api/encyclopedias` | The Encyclopedias screen: each ZIM file in `collections/` as `{file, title, description, articles, main}`, `main` its front page as a `/zim/` path (or null) |
| `/api/encyclopedia/random?file=` | A random article with text (redirects followed, empty pages skipped) of that encyclopedia, or of any when `file` is left out: `{file, title, wiki, snippet}`, `snippet` its first sentences; 404 if none |
| `/api/library` | What this library holds, and the state of the meaning-based search index (`index`) |
| `/api/setup/*` | Library and downloads ([SETUP-API.md](SETUP-API.md)) |
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
