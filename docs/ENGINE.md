# The engine

The window (`native/`, Rust and GTK 4) starts the engine as a child process and talks to it over the child's standard
input and output. The engine is Python. It opens no port and no socket of its own.

## How a request travels

1. `native/src/engine.rs` writes one JSON line: `{"id": 7, "method": "GET", "path": "/api/find?q=water"}`.
2. `app/engine_pipe.py` reads it and hands it to the bridge (`app/bridge.py`), which runs the engine's request handler
   (`server.Handler`, on `engine/library.py`'s base) on a worker thread over in-memory streams. Small requests, PDF page
   images, chat and large files each have their own workers, so a flood of one never holds up another.
3. The answer comes back as lines with the same id: one `head` (status and type), any number of `chunk` (text) or `b64`
   (anything else) lines as the handler writes them, and one `end`. `{"cancel": 7}` stops a request.

The first line the engine writes is `{"ready": {...}}` with the data and library folders. The window reads thumbnails,
PDFs and videos from those folders directly; only answers that need the engine come through the pipe. With no library,
the engine starts in setup and answers `/api/setup/*` only ([SETUP-API.md](SETUP-API.md)).

## What the window asks for

| Path | Answer |
|---|---|
| `/api/topics` | The curated guides, with each cited source resolved from the catalog; items this library does not hold are left out |
| `/api/find?q=&kind=` | Plain-language keyword search, grouped by document |
| `/api/retrieve?q=&k=&kind=` | The passages Chat would cite, with where each came from |
| `/api/doc/<id>` | One catalog record |
| `/api/transcript/<video id>` | Timed transcript cues |
| `/api/videos`, `/api/videos/<row>?q=` | The Videos screen's rows; one row, or a search of all |
| `/api/shelves`, `/api/shelf/<id>` | The library by plain-language shelf |
| `/api/collection-search?q=` | Encyclopedia articles, from each ZIM file's own full-text index |
| `/api/places?q=` | Place search: addresses, ZIP codes, towns and named places (US data from an upstream pack), and the world's countries, regions and towns (GeoNames, built in setup) |
| `/api/library`, `/api/assistant/status` | What this library holds; the state of the model and of the search index |
| `POST /api/chat` | One assistant turn, streamed as server-sent events |
| `POST /api/assistant/local` | The local AI: `rescan`, `on`, `off`, `stop`, `warm` |
| `/api/setup/*` | Library and local AI ([SETUP-API.md](SETUP-API.md)) |
| `/zim/<file>/<article>` | An encyclopedia article, its scripts and web links taken out (`engine/saved_pages.py`) |
| `/documents/…`, `/pages/…`, `/videos/…` and the other library folders | A file of the library. A saved web page is rewritten to run no code; a PDF comes as a page of pictures PDFium has drawn, never as the file; a video comes in pieces of at most 8 MiB |

Only the library's served folders are readable, judged by where a path really leads (`engine/library.py`, `file_ok`): no
catalog, index, log, hidden file or link out of the library. Article and saved pages are shown in a WebKit view with
JavaScript off and every address but `lastbook:` blocked (`native/src/web.rs`).

## Search and the assistant

- **Keyword search** is the library's own FTS5/BM25 index, `library-data/search.sqlite`, read only. It comes with the pack.
- **Meaning-based search** (`retrieval.py`) asks Recall for Omarchy for EmbeddingGemma vectors over its local socket. The
  index is `derived/index/semantic.sqlite` plus `vectors.f16` (append-only). It updates incrementally: the engine
  re-checks the catalog every 5 minutes and embeds only new or changed documents and transcripts (one writer at a time,
  by file lock). Without Recall, search is keyword search.
- **The assistant** (`assistant.py`) searches first, then gives the model read-only tools that accept catalog ids only:
  `search_library`, `read_pages`, `read_article`, `get_timed_transcript`, `lookup_place` (and `view_page`, a page image,
  for a model that reads images). Source text is wrapped as untrusted reference data. Every `[S3]` in an answer is checked
  against what the tools returned in that turn; unknown keys are removed and reported.
- **The local AI** (`llm_local.py`) is Gemma 4 26B A4B run by `lastbook-llm` (`packaging/llm/lastbook_llm.cpp`, on a pinned
  llama.cpp), which speaks JSON lines on its standard input and output and has no network code. The engine finds the model
  file in `LastbookData/models/` (setup puts it there), checks it once (size, then SHA-256), and asks
  `lastbook-llm --measure` whether it fits this computer's memory. It starts when someone asks, and stops after 15
  minutes unused, when turned off, and when the app quits. It is text only: it is not given page images.
- What the assistant cannot read is reported, not hidden (`/api/assistant/status`, shown under Library → About): videos
  without captions, scanned PDFs without text.

For development, `LASTBOOK_LOCAL_AI=0` makes the assistant call an OpenAI-compatible model server instead, at
`SURVIVAL_MODEL_BASE` (default `http://127.0.0.1:19443`), which must be a loopback address.

## What stays correct as a library changes

- Nothing in `library-data/` is written. Source details are read from `catalog.json`, reloaded when the file changes.
- Curated guides cite catalog ids. Run `tools/validate_topics.py` against a library after changing a guide.
- Everything the engine makes goes under `derived/` in the data folder: `index/` (meaning-based search), `cache/`
  (rendered PDF pages), `llm/` and `logs/`.
