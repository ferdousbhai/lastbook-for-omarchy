# Curated topic files (`content/topics/<id>.json`)

Each file is one "need" in the app. The app shows, in order:
1. the title and a one-sentence intro,
2. **choices** ("What's happening?") — the person taps one situation and sees 1–4 recommended readings for it,
3. **limits** — when the source itself says to stop, get help, or that a professional/trained person is needed,
4. **prepare** — readings for getting ready before it happens,
5. **learn** — full manuals, videos, encyclopedia articles for deeper study,
6. for learning tracks, an ordered **route** of beginner-to-advanced steps,
7. **what this guide does not cover** — from `content/gaps.json` (below).

Source facts (publisher, edition, date, file type, page count, video length, transcript) are filled in
automatically from `catalog.json` — do not copy them into the topic file.

```json
{
  "id": "water",
  "mode": "now",                     // "now" = urgent need, "learn" = longer-term learning track
  "title": "Get safe drinking water",
  "short": "Treat, store and find water",
  "intro": "One or two plain sentences in your own words. Describe what this section helps with; do not give procedures.",
  "keywords": ["water", "boil", "bleach", "purify", "filter", "thirsty", "drinking"],
  "choices": [
    {
      "id": "unsafe",
      "label": "Tap water is off or might be unsafe",
      "hint": "Boil-water notice, earthquake, flood, broken main",
      "read": [
        {"doc": "<catalog id>", "page": 9,
         "label": "Short action-style headline of what the page helps with",
         "note": "What the reader will find on that page, <= 30 words, describing (not prescribing)."}
      ]
    }
  ],
  "limits": [
    {"text": "Faithful short paraphrase of a limit/warning/when-to-get-help statement from the source.",
     "doc": "<catalog id>", "page": 12}
  ],
  "prepare": [ {"doc": "...", "page": 21, "label": "...", "note": "..."} ],
  "learn":   [ {"doc": "...", "label": "...", "note": "..."},
               {"zim": "zim/<file>.zim/<Article_path>", "title": "Article title", "label": "...", "note": "..."} ],
  "route": [
    {"title": "Step 1: ...", "goal": "What the step is for, in plain words",
     "read": [ {"doc": "...", "page": 3, "label": "...", "note": "..."} ]}
  ],
  "related": ["sanitation", "food"]
}
```

Rules
- Every `doc` must be a real catalog id (not a `metadata-video-*` id). `page` is the PDF *file* page (1-based) as used by
  `libq.py page`; omit it for html/video/whole-document references. Verify each page by reading it.
- Use a video's own id (`video-...`) for videos. Prefer videos with captions/transcripts and reputable publishers.
- `label`/`note`/`intro`/`hint` are plain-language descriptions written for a stressed non-expert. They may describe what a
  page contains, but must not add steps, doses, quantities or advice that the page does not say.
- `limits` must be traceable to the cited page. Paraphrase faithfully; keep any quote under 15 words.
- Medical, chemical (bleach, fuels, pesticides), electrical and gas items: prefer current WHO/CDC/EPA/FEMA/Red Cross/
  Hesperian/MSF/USDA/extension sources; mention in `note` if a source is old (e.g. 1980s civil defense) or written for
  health workers or trained responders.
- Favour fewer, better items: 1–4 readings per choice, 3–8 in prepare, 4–10 in learn.
- A choice that belongs to another guide has `"goto": "<guide id>/<choice id>"` in place of `read`: the app links to that guide.

# What a guide does not cover (`content/gaps.json`)

One entry per guide id, shown at the end of the guide. `missing` lists what curators looked for and did not find;
`disagree` lists where the sources give different answers. Both are plain sentences.

```json
{"water": {"missing": ["No current CDC or EPA page on disinfecting drinking water."],
           "disagree": ["Sources give different bleach drop counts."]}}
```
