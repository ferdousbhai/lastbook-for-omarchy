Lastbook for Omarchy v1.2.0 is the first release: an offline survival library (manuals, videos, encyclopedias, a world map and a local AI that cites its sources) as a native Omarchy app.

- A hard fork of [Lastbook](https://github.com/Immortal-Protocols/lastbook), made native for Omarchy: a Rust and GTK 4 window (`lastbook`) in the Omarchy theme, following [Strata](https://github.com/lgse/strata)'s look, with the Python engine as its child process.
- Chat answers from the library with a local AI that runs on the computer, and every `[S#]` it cites is checked against what it read. It reads the encyclopedias too.
- Meaning-based search uses [Recall for Omarchy](https://github.com/ferdousbhai/recall-for-omarchy) when it is installed; without it, search matches words.
- An offline world map drawn in the theme's colours, with place search. The world map and the street-level areas the person picks are made during setup from OpenStreetMap, on their own computer; nothing is hosted.
- Guides for urgent needs, videos, manuals and PDFs, and the library's encyclopedias, all offline after setup.
- A command line and an agent skill: `lastbook search`, `read`, `page`, `transcript`, `place`, `guides`, `ask`, `status` and `open` answer from the library as JSON with no window, and `lastbook skill install` teaches AI agents on the computer to use them.

Install (x86_64 and aarch64, including Apple silicon through Asahi):

```sh
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

Open Lastbook from the application menu; its first launch sets up the library, the map and the optional local AI. Updates arrive through `omarchy update`.

Based on Lastbook (MIT); the window follows Strata (MIT). Map data © OpenStreetMap contributors (ODbL), via Protomaps; place names from GeoNames (CC BY 4.0); encyclopedias from Kiwix. Full notices in THIRD_PARTY_NOTICES.md.
