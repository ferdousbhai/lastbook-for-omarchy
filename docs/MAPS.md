# How the offline map is put together

A guide for anyone who wants to improve the map: add a kind of place, show one that is already in the data, or extend
coverage. It says which files the app reads, what builds each one, and where the gaps are. Everything here was read from
the code and from one installed state map, without running the app; where something could not be found in this
repository, it says so.

The map follows the same rules as the rest of Lastbook (see [CONTRIBUTING.md](../CONTRIBUTING.md)). The app draws only
from files on the computer and never asks the network for a tile, a place or an address. The build tools in `tools/` are
different: they run on a build machine, and several of them download public data. They are never part of the app.

## What the app reads

A map set lives in the data folder. `map-set.json` at its top lists the files and their sizes.

| File | Tables | What it holds | Read by |
|---|---|---|---|
| `derived/geo/national.sqlite` | `feat`, `feat_idx`, `poi`, `meta` | Roads, water and land shapes, and the place pins | `server.py` (`national_mapdb`) |
| `derived/geo/map.sqlite` | the same four | The original detailed street map of Southern California; it had no rows in the state map checked | `server.py` (`mapdb`) |
| `derived/geo/water.sqlite` | `resource`, `spatial`, `feat`, `feat_idx` | Dams, reservoirs, lakes and ponds | `server.py` |
| `derived/geo/crops.sqlite` | `resource`, `spatial`, `tile` | Orchards, vineyards and berries | `server.py` |
| `derived/geo/terrain.sqlite` | `tile` | Elevation tiles for relief, contours and the 3D view | `server.py` |
| `derived/geo/buildings.sqlite` | `bldg` | Building outlines | `server.py` |
| `derived/geo/places.sqlite` | `place`, `place_fts` | The place finder: towns, ZIP codes, counties, named places | `server.py` (`/api/places`) |
| `library-data/geography/resources/atlas.sqlite` | `resource`, `spatial` | Natural resource records. The state map checked held minerals only; the fish, boating and well layers are labelled California in `web/map.js` | `server.py` |
| `derived/geo/addresses/` | one index per state | Street addresses for address search | `engine/address_search.py` |

Two kinds of table carry the things a person can tap:

- `poi(id, cls, name, lon, lat)`: a point with a class, such as `hospital` or `fuel`.
- `resource(id, source_id, kind, name, lat, lon, detail, source, origin)` with its `spatial` index: a record with a kind,
  a line of detail and the name of its source.

## How a place gets onto the map

1. A row with a class (`poi.cls`) or a kind (`resource.kind`) is in one of the files above.
2. The class is named in a layer in `web/map.js` (`LAYERS`: id, label, symbol, colour, classes, group).
3. The engine reports which classes a map set really holds (`map_contents` in `server.py`), and the app offers only the
   layers that have something to show.

A class that is in the data but in no layer has no entry in the Layers panel, so it cannot be switched on. The engine
already has a plain name for several of them (`HELP` in `server.py`: School, Dentist, Vet, Care home, Post office).

### Places that are in the data and not shown

The data holds more classes than `web/map.js` has layers for . In one state map checked on 15 November 2026 (77,356 pins), these classes had rows and no layer:

| Class | Rows |
|---|---|
| `school` | 13,862 |
| `fountain` | 4,971 |
| `post_office` | 2,224 |
| `car_repair` | 1,444 |
| `park` | 1,102 |
| `veterinary` | 1,102 |
| `college` | 468 |
| `university` | 362 |
| `garden_centre` | 176 |
| `nursing_home` | 49 |
| `water_works` | 3 |

Showing one of these needs no new data: add a layer line in `web/map.js` and say in the pull request why the kind helps
someone in an emergency. The comment in `map.js` about retired layers shows that some kinds were left out on purpose,
so ask before adding many.

## What builds each file

| File | Built by | From |
|---|---|---|
| `map.sqlite` | Upstream Lastbook | An OpenStreetMap regional extract (ODbL) |
| `water.sqlite` | Upstream Lastbook | USACE National Inventory of Dams, USGS National Hydrography Dataset |
| `crops.sqlite` | Upstream Lastbook | USDA Cropland Data Layer |
| `terrain.sqlite` | Upstream Lastbook | USGS 3DEP elevation |
| `buildings.sqlite` | Upstream Lastbook | Microsoft building footprints (ODbL), OpenStreetMap heights |
| `places.sqlite` | Upstream Lastbook | USGS GNIS, Census gazetteers, named OpenStreetMap places |
| address indexes | Upstream Lastbook | Overture Maps addresses |

These files arrive inside upstream's packs and state maps. The tools that built them are not in this repository.

## What is tied to the United States

- Every detailed source above is a US dataset, or a worldwide one filtered to the US.
- `server.py` treats a box around Southern California as the area `map.sqlite` covers (`_poi_sources`, `socal_classes`).
- Detailed maps are cut and installed one US state at a time (`docs/SETUP-API.md`, "State maps").
- Address search knows US state names and US street words (`engine/address_search.py`).
- The base map of the world is drawn from `web/world-context.json` and `web/geographic-context.json`.

Coverage outside the US needs a decision on each of these first: which open source replaces each federal list, under
what licence, what a "state" is elsewhere, and how addresses are written. That is a design discussion to have in an
issue before any code.

[REGIONS.md](REGIONS.md) proposes answers for Ontario, British Columbia and Guyana, as a starting point for that issue.

## Checking a map change

- A change to a layer in `web/map.js` is a change you can see: include screenshots in light and dark mode.
- Nothing a contributor adds may make the app open a connection. New data comes in through a build tool and ships as a
  file.
