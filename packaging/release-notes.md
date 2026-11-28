Lastbook for Omarchy v1.5.0 makes the library work better with your own AI agent, and offers the street map of where you are.

- The agent skill is half the size and tells an agent to answer as Lastbook's built-in assistant once did: search, read the page, cite it, and keep the library and its own knowledge apart.
- `lastbook search --read` returns the whole page behind the best four results, so one command is often enough to answer from. Search output no longer carries fields only the window uses, and says when it matched by words only.
- When you are online somewhere no installed street map covers, Lastbook offers that region's streets as it opens: one press downloads it. First-run setup and Library and downloads suggest the same regions.
- Lastbook reads where you are from Omarchy's weather location or time zone, or you can type your town; nothing is sent to find you. A switch under Map in Library and downloads turns the offer off.
- `lastbook place` and `lastbook status` tell an agent when no street map covers where the computer is, for it to pass on. No command downloads anything.
- The library's files are at `~/.local/share/Lastbook/library` after any `lastbook` command, not only after the window has opened.
- The README and docs are rewritten for new users, with how this fork differs from Lastbook.

Install or update (x86_64 and aarch64, including Apple silicon through Asahi):

```sh
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

Installed copies update through `omarchy update`.

Based on Lastbook (MIT); the window follows Strata (MIT). Map data © OpenStreetMap contributors (ODbL), via Protomaps; place names from GeoNames (CC BY 4.0); encyclopedias from Kiwix. Full notices in THIRD_PARTY_NOTICES.md.
