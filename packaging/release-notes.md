Lastbook for Omarchy v1.4.1 makes the map offer the streets it lacks, and rewrites the guides and messages for readers anywhere.

- "Nearest help" on a place no street-level map covers now offers to download that region's streets, right in the panel, with progress, pause and resume. The help list appears when the download finishes.
- The map opens on your newest street-level map rather than the whole world, and "Nearest help" on a whole-world view asks for a town instead of pinning the ocean.
- The guides read for an international audience: US agencies are explained, reading labels lead with what the page helps with, imperial measures have metric beside them, spelling is British, and search finds "power cut", "torch", "petrol" and "ambulance".
- The Guides screen's first section is "In an emergency", and four guides have clearer titles.
- Setup no longer says the Medium and All libraries include California or US maps, which this version does not install.
- Error messages are plainer, `lastbook read --pages` with too long a range says so, and the help lists `index --pause`.

Install or update (x86_64 and aarch64, including Apple silicon through Asahi):

```sh
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

Installed copies update through `omarchy update`.

Based on Lastbook (MIT); the window follows Strata (MIT). Map data © OpenStreetMap contributors (ODbL), via Protomaps; place names from GeoNames (CC BY 4.0); encyclopedias from Kiwix. Full notices in THIRD_PARTY_NOTICES.md.
