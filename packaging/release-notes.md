Lastbook for Omarchy v1.4.0 makes the library international and puts everything it holds within reach of the window.

- The US-only parts are gone: state maps, US address search and the Southern California content. Setup no longer unpacks upstream's US map data, and the engine removes it from a library that has it (about 2 GB).
- Place search knows far more of the world: about 240,000 places of 500 people or more, in place of 37,000. A library made by an earlier version keeps its list until you tick "World map and place search" in Library and downloads.
- The map shows where help is: hospitals, clinics, pharmacies, water, shelter and more are marked, with a legend, and "Nearest help" lists them by distance around the place you found. Where no street-level map covers a place it says so. `lastbook place` takes coordinates too, and answers the same way.
- Encyclopedias have a screen of their own: front pages, a random article, something to read. Library and downloads is in the sidebar.
- Reading: Ctrl+F finds in the page or document (Ctrl+K is Search), a button copies a page's text, and "Related" lists other documents on the same subject.
- Meaning search no longer needs the window open. A background service (`lastbook index`) prepares it whenever Recall for Omarchy is there; the Search screen and `lastbook status` say how far it is. Recall stays optional: without it search is by words.
- Search gives fewer off-topic results, ranks governments and UN bodies of every country as trusted sources, and no longer stalls on thumbnails.
- Setup is steadier: stalled downloads are noticed and resumed, a map cut carries on where it stopped, two windows cannot run setup at once, and "Check for newer libraries" is a button rather than something done at every start.
- The agent skill installs itself the first time `lastbook` is used, and says first what can be read straight from the library's files (`~/.local/share/Lastbook/library`).
- The window speaks the language of the desktop where a translation exists, and every command exits 1 on any failure, as Omarchy's do.

Install or update (x86_64 and aarch64, including Apple silicon through Asahi):

```sh
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

Installed copies update through `omarchy update`.

Based on Lastbook (MIT); the window follows Strata (MIT). Map data © OpenStreetMap contributors (ODbL), via Protomaps; place names from GeoNames (CC BY 4.0); encyclopedias from Kiwix. Full notices in THIRD_PARTY_NOTICES.md.
