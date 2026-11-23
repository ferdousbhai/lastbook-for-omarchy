# How the offline map is put together

A guide for anyone who wants to improve the map. It says which files the window draws, what makes them, and where place
search and `lastbook place --nearby` find their answers. All of it is made on the person's computer; upstream Lastbook's
US map files, where an old library still has them, are read by nothing.

The map follows the same rules as the rest of Lastbook (see [CONTRIBUTING.md](../CONTRIBUTING.md)). The window draws only
from files on the computer and never asks the network for a tile or a place. Lastbook hosts no map: setup makes
the files on the person's computer, when the person asks for them.

## What the window draws

| File (in `derived/geo/`) | What it holds | Made by |
|---|---|---|
| `world.mbtiles` | The whole world to city level (zoom 0 to 8), about 0.6 GB | Setup, `engine/setup/maps.py` |
| `regions-<id>.mbtiles` | Street-level detail (zoom 9 to 15) for one region the person chose | Setup, the same way |
| `world-places.sqlite` | Place search: every country, first-level region, and place of 500 people or more (about 240,000; the seats of local governments too), those of 15,000+ with alternate names; about 40 MB | Setup, from GeoNames |

- The tiles are OpenStreetMap vector tiles in Protomaps' basemap schema, stored as MBTiles (SQLite). `native/src/map.rs`
  opens every `regions-*.mbtiles` and `world.mbtiles`, takes each tile from the most detailed file that has it, scales a
  coarser tile up where nothing finer exists, and draws with Cairo in the theme's colours (`native/src/mvt.rs` decodes the
  tiles).
- **Help points** come from the tiles' `pois` layer, from zoom 13: hospitals, clinics and doctors (H), pharmacies (+),
  police (P), fire stations (F), drinking water (W), shelters (S), toilets (T) and food shops (M). The list is `HELP`
  in `map.rs`, which the legend (the map bar's info button) and the nearest-help list read too. Showing another kind is
  one line there and its word in `MAP_HELP` in `server.py`; say in the pull request why it helps someone in an
  emergency. A click on a mark names it, with its kind and its distance from the place found (or the map centre).
  `lastbook place --nearby` reads the same layer (below).
- **Nearest help** (the map bar's locate button, and on its own after a place is found) lists the nearest points of each
  kind around the place, under one heading per mark: hospitals, clinics and doctors together as Medical help, since in
  much of the world a small place has only a clinic or a doctor. A row centres the map on its point. The window asks the
  engine (`/api/near?lat=&lon=`, `near_help()` in `server.py`), the same `near()` as `--nearby` (below), and measures no
  distance itself. Where no street-level map covers the place it lists no points from a map of somewhere else: it names
  the regions whose streets would cover the place (up to three, smallest first, which the engine picks from
  `map-regions.json`: `covering()` in `engine/setup/maps.py`) with a button to download each. A button starts the same
  setup job Library and downloads would (`/api/setup/start` with that region; the world map too if it is missing),
  nothing reaches the network before it is pressed, and the panel shows the job as it runs (`item_row` in `setup.rs`):
  how far it has got, Pause and Resume, a failure in the engine's words with Try again, a disk too full, or a download
  another window runs. Once it is done the map opens the new file and the panel lists the help around the place.
  Library and downloads stays as a quieter button for other choices. A point in no region (out at sea) is said to have
  nothing to download, and the person is asked to find a town.
- **Which regions cover a place.** Only each region's box is known, and neighbours' boxes overlap (Paris lies in
  Picardie's and Centre's too), so a region is offered where the point lies well inside its box (no nearer its edge than
  15% of the way from the middle), or where the nearest town's region and country in place search name it; once a
  country is named, regions of other countries are left out (Kathmandu: Nepal, not India or China).
- **No place, no pin.** Asked for the help around the map centre while the map shows more than about 50 km across (below
  zoom 11, `PLACE_ZOOM`), the panel pins nothing: it asks for a town, with the keyboard in the find field, and lists the
  street-level maps installed, each a row that shows its area.
- **Where the map opens.** Shown for the first time with no place asked for, the map shows the street-level map made
  last (its `bounds`), not the whole world. Asked to show a point (`Route::MapAt`, `map:LAT,LON`), it opens there.
- **Place search** asks the engine (`/api/places`, `places()` in `server.py`), which searches `world-places.sqlite`, or
  takes a point as `LAT,LON` in decimal degrees. Each place says how it `match`es the query: `exact` (its name, maybe
  with its region or country), `alternate name` (one of the other names GeoNames knows it by), `partial` (a word left
  out, or only the start of a longer name) or `coordinates`; and `onMap`, whether an installed street-level map covers
  it. Places are listed best first: those the query names (or, for the largest, names by an old or other name: "Bombay"
  is Mumbai before a village called Bombay), then the larger before the smaller (`rank`: countries 0; regions, capitals
  and cities over a million 1; then over 100,000, 15,000 and 1,000; the rest 5). Hamlets of fewer than 500, and places
  GeoNames gives no population and no local government (some Balinese villages, such as Amed and Pemuteran), are not in
  it: give their coordinates.

## How setup makes the files

`engine/setup/maps.py` cuts the map out of the newest Protomaps daily build of OpenStreetMap, a PMTiles archive on
`build.protomaps.com`. It reads the archive's directories over HTTPS range requests and fetches only the tiles wanted:
all of them to zoom 8 for the world, and those inside a region's box for zoom 9 to 15. Place search is built from
GeoNames' public files (`download.geonames.org`: `cities500.zip`, about 14 MB, with `countryInfo.txt` and
`admin1CodesASCII.txt`). Every request goes through `engine/setup/download.py`, the one module
that may open a connection, to allow-listed hosts with the pinned roots.

- A file is built beside its final name (`<name>.part`) and moved into place when complete. A paused cut, or one the
  network failed, keeps its part file and carries on from it while the same daily map build is still the newest; Cancel
  removes it; what a killed process leaves behind is removed at the next map job. There is no published checksum for a cut of a
  daily build; every tile directory and the gazetteer are parsed before anything is kept.
- The regions offered are Geofabrik's: countries, and parts of large countries, 554 in all, each with a bounding box.
  The list ships with the app (`packaging/arch/map-regions.json`); `tools/make_region_list.py` makes it from Geofabrik's
  index. Only names and boxes are taken from Geofabrik. A region across the antimeridian (Alaska, Fiji, New Zealand, the
  Russian Far East) is two boxes, one each side of ±180, and its street map is cut from their union.
- The person picks the areas in setup (`native/src/setup.rs`; `POST /api/setup/start {world, regions}` in
  [SETUP-API.md](SETUP-API.md)). Lastbook never detects location.
- These files belong to no pack. An upgrade to another pack keeps them (`maps.ours`).
- A library set up before place search held villages has a gazetteer of towns of 15,000+ only. It still works, but
  `world-places.sqlite` says which kind it is (SQLite's `user_version`, `maps.PLACES_VERSION`), and setup counts the
  older one as missing (`maps.places_current`): Library and downloads then offers "World map and place search" again
  (about 55 MB, since the world map is there), and adding a street-level area makes it again too. Nothing is fetched
  until the person starts that download.

Credits: map data © OpenStreetMap contributors (ODbL), tiles by Protomaps; place names from GeoNames (CC BY 4.0).

## Nearby help from the command line

`lastbook place QUERY --nearby hospital,pharmacy,police` lists the nearest help points around the first place found
(`near()` in `server.py`), or around a point: `lastbook place "-8.5069,115.2625" --nearby hospital`. They come from the
`pois` layer of the installed street-level maps, which `engine/map_pois.py` reads: it opens every `regions-*.mbtiles`,
unpacks the tiles around the place (gzip) and decodes that one layer by hand, as `mvt.rs` does for the window, so the
engine needs no protocol buffer library.

- **Only around a sure place, only where there is a map.** Help points are looked up only when the first place is an
  `exact`, `alternate name` or `coordinates` match, and only when a street-level map covers it (`covered`). Otherwise
  `nearby` is empty and `nearby_note` says why in a sentence; for a place no map covers, `regions_installed` lists the
  regions there are. So an empty `nearby` with `covered: true` means the map has no such point within reach.
- **How far it looks.** The search reaches out from about 2 km each way, twice as far each step, as far as about 140 km
  (`NEAR_KM`), and stops once five points of a kind lie within the distance read, so none nearer is missed. Distances
  are on the ground (great circle) and a box is as wide in km at 60 N as at the equator; it goes on across the
  antimeridian. Out to about 35 km (`FULL_KM`) it reads zoom 15, which holds every point. Each doubling beyond that
  reads one zoom coarser (14, then 13), where the map keeps the larger points: the main hospitals, but few or no
  pharmacies, clinics or police stations. So a pharmacy more than about 35 km away is not found, and a far hospital may
  not be the nearest one. A lookup takes well under a second.
- **Kinds.** A kind is OpenStreetMap's tag value as the tiles carry it (`hospital`, `clinic`, `doctors`, `pharmacy`,
  `police`, `fire_station`, `supermarket`, `fuel`…). Where `--nearby`'s word differs, `KINDS` in `map_pois.py` says
  so: `drinking_water` also takes `water_point`, `toilet` is `toilets`, `town_hall` is `townhall`, `bicycle_shop` is
  `bicycle`. The words `--nearby` accepts are `HELP` in `server.py`, and only kinds the street maps carry belong there.
  `shelter` is OpenStreetMap's `amenity=shelter`, a roofed structure (bus or rain shelter, hut, guard post), not an
  emergency or evacuation shelter, and its label says so; `chemist` is a drugstore that may sell no medicines.
- The same name within about 80 m is one point (a pharmacy mapped as a node and as a building).
- The window's nearest-help list is the same lookup, for the kinds the map marks (`MAP_HELP`, five of each).

## Checking a map change

- A change to how the map draws is a change you can see: include screenshots in a light and a dark theme.
- `app.tests.test_maps` covers the PMTiles addressing and directory parsing behind the cut, that an upgrade keeps the
  person's map files, and the help points read back out of a made-up street map (across the antimeridian, at 60 N, and
  from damaged tiles); `app.tests.test_cli` asks `place` and `place --nearby` of one, by name and by coordinates, and
  `app.tests.test_map_near` asks `/api/near` through the bridge, and the regions offered for a point. `native/dev/qa/map.jsonl`
  drives the Map screen over the made-up town of `native/dev/seed.py` (Mapton, with a street map) and one without
  (Testville); `map-download.jsonl` downloads Testville's streets from the panel over a stand-in engine that makes the
  download up. `bin/test` runs them with the window's tests.
- Nothing a contributor adds may make the app open a connection outside setup's downloader.
