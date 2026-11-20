Lastbook for Omarchy v1.3.0 takes the built-in AI out. Lastbook is the library; the AI agent you already use on the computer reads it through Lastbook's command line and skill.

- Chat and `lastbook ask` are gone, and with them the 17 GB model download, the bundled llama.cpp and the Vulkan requirements. The package is about half a megabyte, and setup downloads only the library and the map.
- Agents answer from the library instead. `lastbook skill install` (the installer runs it) teaches Claude Code, Codex, pi and the other agents Omarchy sets up to search the library, read the pages and cite them by title, publisher and page number. On Omarchy 4.5 that includes the local models of `omarchy agent local`.
- Search, Guides, Library, Videos, the encyclopedias, the map and Saved are unchanged. `lastbook search`, `read`, `page`, `transcript`, `place`, `guides`, `status` and `open` still answer as JSON with no window.
- Upgrading from v1.2.0 keeps your library. A model that v1.2.0 downloaded is not removed: delete `~/.cache/huggingface/hub/models--unsloth--gemma-4-26B-A4B-it-GGUF` to get its 17 GB back if nothing else uses it.
- The window's keyboard paths were tidied: in a single pane the arrows walk the sidebar and Enter chooses.

Install or update (x86_64 and aarch64, including Apple silicon through Asahi):

```sh
curl -fsSL https://ferdousbhai.com/lastbook/install.sh | sudo bash
```

Installed copies update through `omarchy update`.

Based on Lastbook (MIT); the window follows Strata (MIT). Map data © OpenStreetMap contributors (ODbL), via Protomaps; place names from GeoNames (CC BY 4.0); encyclopedias from Kiwix. Full notices in THIRD_PARTY_NOTICES.md.
