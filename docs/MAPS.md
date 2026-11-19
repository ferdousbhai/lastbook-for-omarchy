# How the offline map is put together

A guide for anyone who wants to improve the map. It says which files the window draws, what makes them, and what the
engine still reads from the US map files that upstream Lastbook's packs carry.

The map follows the same rules as the rest of Lastbook (see [CONTRIBUTING.md](../CONTRIBUTING.md)). The window draws only
from files on the computer and never asks the network for a tile, a place or an address. Lastbook hosts no map: setup makes
the files on the person's computer, when the person asks for them.

## What the window draws

| File (in `derived/geo/`) | What it holds | Made by |
|---|---|---|
| `world.mbtiles` | The whole world to city level (zoom 0 to 8), about 0.6 GB | Setup, `engine/setup/maps.py` |
| `regions-<id>.mbtiles` | Street-level detail (zoom 9 to 15) for one region the person chose | Setup, the same way |
| `world-places.sqlite` | Place search: every country, first-level region, and town of 15,000 people or more, with alternate names | Setup, from GeoNames |

- The tiles are OpenStreetMap vector tiles in Protomaps' basemap schema, stored as MBTiles (SQLite). `native/src/map.rs`
  opens every `regions-*.mbtiles` and `world.mbtiles`, takes each tile from the most detailed file that has it, scales a
  coarser tile up where nothing finer exists, and draws with Cairo in the theme's colours (`native/src/mvt.rs` decodes the
  tiles).
- **Help points** come from the tiles' `pois` layer, from zoom 13: hospitals, clinics and doctors (H), pharmacies (+),
  police (P), fire stations (F), drinking water (W), shelters (S), toilets (T) and food shops (M). The list is `help_kind`
  in `map.rs`. Showing another kind is one line there; say in the pull request why it helps someone in an emergency.
- **Place search** asks the engine (`/api/places`, `places()` in `server.py`), which searches `world-places.sqlite` and,
  where a pack has them, the US files below.

## How setup makes the files

`engine/setup/maps.py` cuts the map out of the newest Protomaps daily build of OpenStreetMap, a PMTiles archive on
`build.protomaps.com`. It reads the archive's directories over HTTPS range requests and fetches only the tiles wanted:
all of them to zoom 8 for the world, and those inside a region's box for zoom 9 to 15. Place search is built from
GeoNames' public files (`download.geonames.org`). Every request goes through `engine/setup/download.py`, the one module
that may open a connection, to allow-listed hosts with the pinned roots.

- A file is built beside its final name and moved into place when complete, so a stopped download leaves nothing half
  made. There is no published checksum for a cut of a daily build; every tile directory and the gazetteer are parsed
  before anything is kept.
- The regions offered are Geofabrik's: countries, and parts of large countries, 554 in all, each with a bounding box.
  The list ships with the app (`packaging/arch/map-regions.json`); `tools/make_region_list.py` makes it from Geofabrik's
  index. Only names and boxes are taken from Geofabrik. A region across the antimeridian (Alaska, Fiji, New Zealand, the
  Russian Far East) is two boxes, one each side of ±180, and its street map is cut from their union.
- The person picks the areas in setup (`native/src/setup.rs`; `POST /api/setup/start {world, regions}` in
  [SETUP-API.md](SETUP-API.md)). Lastbook never detects location.
- These files belong to no pack. An upgrade to another pack keeps them (`maps.ours`).

Credits: map data © OpenStreetMap contributors (ODbL), tiles by Protomaps; place names from GeoNames (CC BY 4.0).

## US map files from upstream's packs

Upstream Lastbook's packs and state maps carry their own US map set, in SQLite files built by tools that are not in this
repository. Setup still installs, checks and repairs them where a pack or state map has them
([SETUP-API.md](SETUP-API.md), "State maps"). The window does not draw them. The engine reads them for two things:

| File | What the engine uses it for |
|---|---|
| `derived/geo/places.sqlite` | Place search in the US: towns, ZIP codes, counties, named places (USGS GNIS, Census gazetteers) |
| `derived/geo/addresses/<ST>.sqlite` | Street-address search for one state (`engine/address_search.py`; Overture Maps addresses) |
| `derived/geo/national.sqlite`, `map.sqlite` | Ranking same-named US towns, and the assistant's "nearest hospital, pharmacy, water…" answers (`near()`, table `poi`) |
| `derived/geo/water.sqlite`, `crops.sqlite`, `library-data/geography/resources/atlas.sqlite` | Named lakes, reservoirs and dams in place search, and nearby resource records for the assistant |

`terrain.sqlite` and `buildings.sqlite` in such a map set are no longer read by anything.

What is tied to the United States here: every source of these files is a US dataset; `server.py` treats a box around
Southern California as the area `map.sqlite` details (`socal_classes`, `in_socal`); and address search knows US state
names and street words. Outside the US, place search uses GeoNames and the assistant has no nearby-help lookup.

## Checking a map change

- A change to how the map draws is a change you can see: include screenshots in a light and a dark theme.
- `app.tests.test_maps` covers the PMTiles addressing and directory parsing behind the cut, and that an upgrade keeps the
  person's map files. `bin/test` runs it with the window's tests.
- Nothing a contributor adds may make the app open a connection outside setup's downloader.
