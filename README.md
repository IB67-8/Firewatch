# FireWatch

Real-time ground-temperature and wildfire monitoring from satellite thermal data.
**Tuned for Croatia**: it opens on the Croatian watch area, knows the coast and
the islands, and names the villages rather than the nearest big town.

Two pieces that work together, and each works alone:

| File | What it is | Needs |
|---|---|---|
| `firewatch.html` | The dashboard. One self-contained file — map, alerts, charts, table. | A browser. Nothing else. |
| `firewatch_server.py` | The data service. Polls NASA FIRMS + NOAA GOES, stores history, raises alerts. | Python 3.9+. Standard library only for the core. |

---

## Quick start

**Just the dashboard.** Open `firewatch.html`. It finds the best data source available and says which one it is in the header badge:

- **`live · VIIRS + MODIS`** — it reached the Esri Living Atlas mirror of NASA FIRMS. Real detections, no key needed, refreshed every few hours.
- **`SIMULATED DATA`** — nothing was reachable, so it runs a physically-plausible simulation. Every control still works. The badge is amber and a banner says so; it never pretends simulated data is real.

**Add the other feeds.** Put `firewatch_server.py` and `firewatch_effis.py` next to `firewatch.html` and run:

```bash
python3 firewatch_server.py --firms-key YOUR_KEY --effis
```

Then open <http://localhost:8000>. That adds NASA FIRMS and the whole EFFIS side — European hot spots, burnt-area perimeters and the Fire Weather Index. Add `--goes` too if you also want the Americas.

The watch area defaults to Croatia. `--bbox -180,-90,180,90` puts it back to the world.

A free FIRMS map key takes about a minute: <https://firms.modaps.eosdis.nasa.gov/api/map_key/>

---

## How fast is "as soon as possible"

This is the part that actually determines whether you hear about a fire in ten minutes or three hours.

| Source | Pixel size | How often it looks | Lag to availability | Coverage |
|---|---|---|---|---|
| **GOES ABI (FDC)** | 2 km | **every 5–10 min** | minutes | Americas only — *not Croatia* |
| VIIRS 375 m | 375 m | ~4 passes/day | ~3 h (NRT) | global |
| MODIS 1 km | 1 km | ~4 passes/day | ~3 h (NRT) | global |
| EFFIS / GWIS | 375 m – 1 km | follows the polar orbits | ~1–3 h | Europe, re-processed |

**For Croatia there is no five-minute channel.** GOES is geostationary over the
Americas and cannot see the Adriatic; Meteosat has a comparable fire product,
but no public keyless feed of it exists to pull from. So the honest ceiling
here is the polar orbiters: three VIIRS platforms plus two MODIS give roughly
six to ten looks a day, and a fire is typically visible one to three hours
after the overpass. EFFIS does not make that faster — it makes it *better*,
because it adds burnt-area perimeters and the fire-danger forecast that the
detections alone cannot give you.

The trade is resolution against revisit. GOES stares at the same hemisphere continuously, so it catches ignition fastest — but at 2 km a pixel has to get properly hot before it trips. VIIRS resolves fires roughly seven times smaller, but only when a satellite happens to fly over.

So: **GOES tells you first, VIIRS tells you truly.** Running both is the whole point of the server, and the dashboard's "Detection latency" tile always shows the fastest source currently contributing.

A browser page cannot call FIRMS or AWS directly — NASA sends no CORS header, and the GOES files are netCDF that needs decoding. That is the one thing the server exists to do.

---

## What it actually does

**Change detection, not a snapshot.** Every detection gets a stable ID and a `first_seen` timestamp in SQLite. "New this cycle" means genuinely new, and it survives a restart. The first cycle after startup is treated as a baseline and raises no alerts — otherwise you'd get a thousand notifications the moment you launch it.

**Clustering into incidents.** Three sensors at three resolutions see the same fire, so hot pixels are grouped geographically (single-linkage, 4.5 km) rather than by any one sensor's grid. Each cluster carries a power history, so the dashboard can tell you a fire is *intensifying* rather than merely *present*.

**Severity as a state, not a colour.** Every incident is Critical / Serious / Watch / Low based on total radiative power, pixel count and recency — and always ships with the word and an icon, never colour alone.

**Places at risk — the headline panel.** Every fire cluster is matched against an embedded gazetteer of 17,849 populated places, and the side panel lists the towns and cities with fire inside your danger radius: name, country, distance, which direction the fire lies, combined radiative power, how many separate fires, and roughly how many people live there. Ranked by how close the fire is, not by how big the town is. The radius control in the filter bar sets the threshold (10 / 25 / 50 / 100 km), and the risk wording follows distance modified by fire power — a 300 MW front 8 km out reads *Immediate*, a 4 MW hot spot at the same range does not.

Place names show up everywhere else too: fire clusters are titled "14 km NE of Novo Progresso, BR" instead of a coordinate pair, alerts name the nearest town, the map labels threatened places in red at every zoom and ordinary towns once you zoom in, the detections table gains a nearest-place column, and the CSV export carries it. Fires far from anyone are labelled honestly — "remote · 324 km from Aripuanã, BR" — because that is the useful fact about them.

The gazetteer is embedded, not fetched: no reverse-geocoding API to be rate-limited by, and it works with no connection at all. It covers places of 10,000 people and up, thinned so that only the anchor town of each cluster of municipalities is kept. Population figures are stored as a log step and accurate to about 6 %, which is why they always print as "about 25k" and never as a false-precision exact count.

**Built around Croatia.** The default watch area is Croatia, broken down the way the fire service thinks about it — Dalmatia and the islands, Split and the central coast, Zadar–Šibenik, Dubrovnik–Pelješac, Kvarner–Istria, Lika–Gorski kotar, Slavonia — plus an Adriatic-and-neighbours view, because smoke, wind and fire crews all cross these borders. The danger radius defaults to 15 km rather than 25, which is the right scale for a country this size.

The gazetteer carries **every settlement** the source has for Croatia, Slovenia, Bosnia and Herzegovina, Montenegro, Serbia and Hungary, at any population and barely thinned: 2,386 places, of which 623 are Croatian. The global floor of 10,000 people would have left Croatia with 42, and the villages that actually burn on the Dalmatian coast are a tenth of that size — "1.0 km from Unešić" is worth far more than "22 km from Šibenik". The whole supplement costs about 35 KB.

The Adriatic gets its own high-resolution basemap inset: 712 rings at ~150 m inside the box 13.2–19.6 E, 42.0–45.7 N, against 39 features and 687 vertices at 1:10m. The Dalmatian coast is one of the most indented in the world and the global layer flattens it — the Zadar and Šibenik archipelagos are simply not there. Inside its box the inset *replaces* the global geometry rather than drawing over it, because most of what it contributes is water: the channels between the islands. Thirty-two Croatian islands are named, Krk and Cres down to Susak and Olib.

Emergency numbers are in the footer where they belong: **193** for the fire brigade, **112** for the European emergency line, with links to HVZ, DHMZ and the EFFIS current-situation viewer. Times are stamped UTC with Zagreb local time alongside — the satellites work in one and the fire crews in the other, and showing only one of them guarantees a mistake eventually.

**Precision, where it changes the answer.** Three things are measured rather than approximated:

*Distance is measured to the fire's edge, not its middle.* Each cluster carries a convex hull, and the range quoted for a town is the distance to the nearest point of that hull. On a 30 km-long fire front a centroid reading is dangerously optimistic — the flames can be 3 km away while the centre sits 18 km out. Switching to hull distance roughly quadrupled the number of places that qualify as at risk in testing, which is the point.

*Detections are drawn as their real ground footprint.* FIRMS reports a per-detection `scan` and `track` size because a pixel grows off-nadir — a VIIRS cell is 375 m looking straight down and past 800 m at swath edge. Zoom in far enough and the map stops drawing symbols and starts drawing those actual cells, so you see which ground burned rather than where a dot was centred. The tooltip quotes the footprint for every detection.

*Area is a union, not a sum.* Each cluster reports actively-burning area in km², computed by rasterising the footprints onto a 250 m grid and counting occupied cells. Adding footprints up instead would report several times the truth, since the same fire is seen repeatedly by three sensors over many hours.

**A basemap you can navigate at fire scale.** Natural Earth 1:10m: 165,000 vertices and **4,044 separate landmasses**, against 1,427 at 1:50m and a coarse 8,000-vertex outline before that. Nearly all of that difference is islands — the Aegean, the Antilles, Indonesia, the Baltic, the Pacific — and an island missing from a fire map is not a cosmetic problem when the fire is on it. Coastlines are simplified to about 1.6 km and internal borders to 4.8 km, because a border is a reference line while a coastline is the thing you are looking at. Features smaller than a few tolerances are exempt from simplification entirely, so no island can be smoothed out of existence.

**Lakes.** 823 of them, everything at least 30 km across, from a 1 km source rather than a coarse one — the Great Lakes, the Caspian, Baikal, Victoria, Ladoga, Titicaca all have real shorelines. Without them they are invisible holes in the land, and every border that runs through one appears to wander across dry ground.

**295 named islands and 48 named lakes.** Greenland down to Nauru; the Caspian down to Lake Okeechobee. Names appear when the feature is big enough on screen to be worth naming — Java at continental zoom, Naxos only once you are looking at the Aegean — and are set centred, without a marker dot, because an island is a region rather than a point. Water is italic, the usual cartographic convention, so an island and a lake side by side never read the same.

The names are not hand-placed. The seed list carries only a name and a rough interior point; the build resolves each one to the basemap polygon that contains it and takes the label position and the feature's size from that polygon. A name therefore cannot drift away from its island, the size that gates the label is the real one, and any landmass big enough to deserve a name but missing from the list is reported as a gap rather than quietly going unlabelled. That check currently reports zero gaps above 8,000 km² — the only unnamed landmass at that size is the piece of Eurasia east of the antimeridian, which is mainland and not an island.

Maximum zoom reaches roughly 12 km across the viewport, enough to inspect individual sensor pixels, with a scale bar that switches to metres. Despite carrying four times the geometry, it renders about as fast as the coarse map did: arcs, landmasses and lakes outside the viewport are skipped by bounding box, features too small to resolve to a pixel are dropped, and zoomed-out views walk the vertex list with a stride.

**Ground temperature.** Click any hot spot or anywhere on the map: soil temperature at 0 cm and air temperature at 2 m for the last 48 hours plus forecast, against a 7-day normal band built from that location's own history. Anomaly, 24-hour change, relative humidity, wind and vapour-pressure deficit sit underneath. VPD above ~3 kPa flags in red — that is the fire-weather number that matters most, and it is a *precursor*, not a detection.

**Knowing whether it is actually live.** Every feed reports its own freshness in a strip under the header: how many detections it contributed and how old its newest pixel is, with the chip going amber when a feed is past twice its expected cadence and red when it has failed. A single global "updated 2 seconds ago" hides the failure that matters most — one feed stalled hours ago while the others kept refreshing, so the page looks alive and the numbers are stale. The "Newest detection" tile shows the same thing as a headline.

This also answers a question the interface used to invite: *why isn't this changing?* For Croatia, usually because nothing new has been observed — the polar orbiters pass a few times a day, so a quiet hour is a quiet hour, not a broken page. The strip makes the difference legible.

**Sentinel-2 in one click.** Every fire cluster and every threatened place carries a **Sentinel-2** link that opens the Copernicus Browser at that spot, on the SWIR composite (B12/B8A/B04) — the band combination that makes a burn scar unmistakable where true colour shows only smoke. The window runs from twelve days before the detection to two days after, so there is a pre-fire scene to compare against and the next clear overpass is already in range. No API key: it hands you off to Copernicus with the view already set up.

**Burnt-area perimeters.** With `--effis`, the map draws EFFIS burnt-area polygons as hatched outlines. This is a different kind of fact from a hot spot: the hot pixels say a fire is burning, the perimeter says what it has already taken.

**Fire danger.** With `--effis`, selecting a point fetches the EFFIS Fire Weather Index for it and shows the value and its danger class beside the ground-temperature readings. It is the number European fire services actually plan around, and the only forecast in the interface — everything else here is observation.

**Alerts.** Press **Arm alerts**. You get a tone (rising pitch and repeat count scale with severity) plus a desktop notification, and the event lands in the feed regardless. Threshold is configurable under ⚙ — anything new, nominal-and-above, high-confidence only, or high-confidence ≥ 25 MW.

---

## Dashboard controls

| | |
|---|---|
| **Pan / zoom** | drag, scroll, double-click. `+ − ⤢ ⌂` at top right. |
| **Custom watch box** | shift-drag on the map. Sets the region filter to that box. |
| **Select a point** | click a hot spot or empty map — loads ground temperature there. |
| **Find place** | type a place name — searched offline against the gazetteer, accents optional ("malaga" finds Málaga) — or `38.0 23.7` for raw coordinates. |
| **Marker shape** | ▲ GOES · ● VIIRS · ■ MODIS. Colour is reserved for radiative power. |
| **Red pulsing ring** | new since the last poll. |
| **Dashed outline** | a fire cluster's extent, labelled with burning area and power. |
| **Rectangles at deep zoom** | the sensor's true ground footprint for that detection. |
| **Danger radius** | how close a fire has to be for a town to count as at risk. |
| **Table view** | sortable, with CSV export of exactly what's filtered. |
| **◐** | light / dark. Both are separately designed, not an inverted flip. |

The map is plate carrée rather than Web Mercator on purpose: boreal fires in Siberia, Alaska and the Canadian shield are a large share of global burned area, and Mercator inflates those latitudes beyond recognition.

---

## Server options

```bash
python3 firewatch_server.py --help
```

```bash
# Global watch, FIRMS only, every 5 minutes
python3 firewatch_server.py --firms-key YOUR_KEY

# California + Great Basin, GOES rapid scan on, Slack webhook
python3 firewatch_server.py --firms-key YOUR_KEY \
    --bbox -125,32,-110,43 --goes \
    --webhook https://hooks.slack.com/services/...

# One poll, print a ranked summary, exit — good for cron
python3 firewatch_server.py --firms-key YOUR_KEY --once

# Verify everything offline. No network, no key.
python3 firewatch_server.py --selftest
```

Useful flags:

- `--bbox west,south,east,north` — narrow the watch area. Smaller box, faster polls, fewer false positives.
- `--goes` / `--goes-product ABI-L2-FDCF` — FDCC is CONUS every 5 min; FDCF is the full disk every 10 min and reaches South America.
- `--goes-strict` — only *processed* and *saturated* fire pixels (mask 10/11/30/31). Fewest false alarms.
- `--effis` — EFFIS hot spots, burnt-area perimeters and fire danger. No key needed.
- `--effis-probe` — print every layer EFFIS currently advertises and exit. EFFIS has renamed its layers more than once, and the client discovers names from GetCapabilities rather than hard-coding them; this is how you see what it found.
- `--min-frp`, `--min-conf`, `--min-sev` — alert thresholds.
- `--interval`, `--keep-days`, `--port`, `--host`, `--db`.

**GOES needs two extra packages** (FIRMS does not):

```bash
pip install numpy h5netcdf     # or: pip install numpy netCDF4
```

Without them the server logs one line explaining why GOES is off and carries on with FIRMS.

### API

`GET /api/health` · `GET /api/detections?bbox=w,s,e,n&hours=24` · `GET /api/incidents` · `GET /api/events`

---

## Verifying it

```bash
python3 firewatch_server.py --selftest   # 30 offline checks
python3 test/integration.py              # seeds a store, runs the real HTTP server
node test/render.mjs                     # headless screenshots + console-error check
node test/alerts.mjs                     # injects a new fire, asserts the alert fires
node test/boot.mjs                       # blank-screen guards: blocked scripts, thrown errors
node test/perf.mjs                       # per-stage timings at normal load
node test/smooth.mjs                     # drag/zoom frame rate, poll cost, region switch
node test/layers.mjs                     # cold vs warm redraw, i.e. what a poll pays
node test/antimeridian.mjs               # Wrangel and Fiji, drawn from either side of 180°
node test/stress.mjs                     # 5k / 20k / 50k detections
node test/layout.mjs                     # canvas size and overflow across four viewports
node test/cities.mjs                     # gazetteer accuracy against known coordinates
node test/riskui.mjs                     # risk panel, radius control, tabs, offline search
node test/zoom.mjs                       # four zoom levels, from continent to single pixel
node test/loadtime.mjs                   # time to interactive on a phone profile
node test/mapquality.mjs                 # island and lake counts over five regions
node test/mapcost.mjs                    # basemap draw cost by zoom level
node test/namedplaces.mjs                # island and lake labels across seven regions
node test/croatia.mjs                    # Croatian defaults, gazetteer depth, Sentinel links
python3 firewatch_server.py --effis-probe   # what EFFIS currently advertises
python3 tools/list_landmasses.py 40      # rank landmasses by area
node build_placedata.js                  # re-resolve names; reports gaps and bad seeds
```

### Rendering notes

The map is three canvas layers, split along what actually invalidates them.

**Layer 0, the basemap** — coastlines, borders, lakes, the Croatian inset and
the graticule. These depend only on the view and the theme, so a 30-second poll
has no business re-projecting them. Redrawn only when the view or the palette
changes; otherwise blitted in one copy.

**Layer 1, the data** — the region outline, the hot pixels, EFFIS perimeters,
fire outlines and place labels. This is what a poll or a filter change repays.

**Layer 2, the visible canvas** — one `drawImage` plus the handful of animated
rings. Mid-gesture nothing is re-rendered at all: plate carrée is an affine
projection, so the previous buffer is translated and scaled into place and the
real redraw happens once the gesture settles.

Splitting the basemap out is the single biggest win in the app. A poll used to
cost a full geometry redraw; measured on a 1440×950 desktop viewport at device
ratio 2, a data refresh went from **31 ms to 2–6 ms**, and the whole poll cycle
from **56 ms to 31 ms** (40 ms to 12 ms on a phone profile).

#### What the rasteriser actually charges for

Instrumenting `paintBase` stage by stage produced a result that looked absurd —
the timed stages summed to 1.3 ms against a 50 ms total — because canvas work is
deferred: the JS returns long before anything is rasterised. Forcing a flush
after each stage put the cost where it belonged, and the shape of it was
consistent across every zoom level: **about 20 µs per subpath, and almost
nothing per vertex.**

That one number explains the rest of the design:

- **Marks are sprites, not paths.** Every `moveTo` starts a subpath, so 320 hot
  pixels cost more to draw than every coastline on screen. Each distinct
  (shape, power bucket, confidence, radius) combination is rasterised once into
  a small canvas and blitted, snapped to whole device pixels so it stays crisp.
  Mark drawing fell from **10–13 ms to 2.4 ms**. Place-label dots go through the
  same cache.
- **Islets are dots.** A landmass under about three pixels cannot be drawn as a
  shape, but on the Dalmatian coast the islets *are* the geography — so instead
  of dropping them they are blitted at their own size. 259 of them were costing
  more than the Croatian mainland.
- **Off-screen runs collapse.** Bounding-box culling cannot help with a feature
  that is both on screen and enormous: Eurasia is one ring of tens of thousands
  of vertices, nearly all of them outside a regional viewport. A run of vertices
  that all sit beyond the same edge is replaced by a single chord. This is exact
  rather than approximate — a half-plane is convex so the chord can never be
  seen, and the parity of a polyline's crossings of any horizontal line depends
  only on its endpoints, so the even-odd fill is unchanged. Verified by
  pixel-diffing a full world render against the unoptimised path: zero
  differing pixels.
- **Round joins are not free.** The stroker emits an arc at every vertex, and
  there are 166 000 of them. Bevel is indistinguishable at hairline widths and
  is used below a regional zoom.

Longitude is unwrapped as each ring is walked. Wrangel Island and the Fiji group
straddle the antimeridian, so their vertex lists step from +179 to −179; taken
literally that is a segment right across the Pacific, and it drew as a bright
band across the whole map at 71° N and 17° S. `node test/antimeridian.mjs`
renders both from either side of the seam.

Above the renderer sits a spatial index rebuilt once per poll: 5° buckets so a
zoomed-in view never walks the global set, and a pre-thinned overview list for
zoomed-out views where a 0.2° cell is narrower than a pixel. On top of that,
screen-space thinning keeps only the strongest detection per 3-pixel cell.

#### Off the canvas

The same "what actually changed" question applies to the DOM.

- Chart widths come from a `ResizeObserver`, not from `getBoundingClientRect`
  mid-render. Measuring on the spot forces the browser to flush style and layout
  for every change the earlier renderers just made — the rate chart was being
  billed **14 ms** for the risk list's relayout.
- The risk list, the incident list and the rate chart are keyed on what they
  display rather than on the poll counter, so a cycle where nothing moved costs
  a string compare instead of nine hundred DOM nodes. `render()` fell from
  **30 ms to 3 ms** on an unchanged cycle.
- Those lists use one delegated click handler each instead of sixty closures
  rebuilt per render.
- The detection table renders its first screenful synchronously and the tail on
  the next frame, and the nearest-place lookup is memoised on the detection.
  Opening the table went from **53 ms to 8.6 ms**.
- Clustering lifts coordinates into typed arrays and tests distance
  equirectangularly — at a 4.5 km link threshold the great-circle correction is
  under a millimetre, and it is a threshold test, not a reported figure. Fifty
  thousand detections cluster in **87 ms**, down from 148 ms.

The payloads are written as single backslash-continued string literals rather
than arrays of chunks joined at runtime. The two look equivalent; the array
form costs the JS parser thousands of tokens plus an array build, and switching
away from it cut half a second off load on a phone profile.

The net effect is that redraw cost is governed by screen area rather than
detection count: the settling redraw measures about the same at 50 000
detections as at 5 000, panning and pinching hold 60 fps, and the canvas backing
store is hard-capped at ~4.2 megapixels, stepping the pixel ratio down rather
than handing a phone a canvas it cannot composite in a frame.

`node test/smooth.mjs` reports all of this — frames presented during a drag and
a zoom, the settling redraw, the poll cycle broken into parts, and a region
switch — on a desktop and a phone viewport.

The self-test covers FIRMS CSV parsing, confidence normalisation across all three schemes, antimeridian-crossing bounding boxes, clustering, severity, SQLite change detection, and the GOES geostationary-projection inverse (checked against the nadir invariant: a pixel at scan angle zero must map to 0° N at the sub-satellite longitude).

---

## Limits — please read

Satellite hot spots are **candidate thermal anomalies, not confirmed fires.** Gas flares, volcanoes, steel mills, and sun glint all trigger them. Cloud and thick smoke can hide a real fire completely, and a fire that starts just after an overpass may not be seen for hours by the polar orbiters.

GOES cannot see fires below roughly 10 MW reliably at 2 km, and its coverage degrades toward the limb — western Europe, Africa and Asia get nothing from it.

This is a situational-awareness tool, not a life-safety system. For anything urgent, call your local emergency number and follow official agency guidance.

## Sources

- [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) — VIIRS 375 m (S-NPP, NOAA-20, NOAA-21) and MODIS 1 km active fire, NRT
- [NOAA GOES-R ABI L2 Fire/Hot-Spot Characterization](https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc%3AC01520) via the [AWS Open Data](https://registry.opendata.aws/noaa-goes/) mirror
- [Esri Living Atlas](https://www.arcgis.com/home/item.html?id=dece90af1a0242dcbf0ca36d30276aa3) — CORS-accessible mirror of FIRMS, used when there is no local server
- [EFFIS / GWIS](https://forest-fire.emergency.copernicus.eu/) (Copernicus Emergency Management Service) — European hot spots, burnt-area perimeters, Fire Weather Index
- [Copernicus Browser](https://browser.dataspace.copernicus.eu/) — Sentinel-2 L2A, linked per fire on the SWIR burn-scar view
- [Open-Meteo](https://open-meteo.com/) — soil and air temperature, humidity, wind, VPD
- Coastlines, islands and borders from [Natural Earth 1:10m](https://www.naturalearthdata.com/) via world-atlas, embedded in the HTML
- Lakes from [@geo-maps/earth-lakes-1km](https://www.npmjs.com/package/@geo-maps/earth-lakes-1km) (ODbL, derived from OpenStreetMap), filtered to those 30 km and larger
- Island and lake names resolved against that geometry from the seed list in `tools/place_seeds.py`
- Populated places from [GeoNames](https://www.geonames.org/) (CC BY 4.0) via `all-the-cities`, embedded in the HTML

One thing the gazetteer does *not* reach: `firewatch_server.py` still writes coordinates in its console log and webhook payloads, because the place database lives in the dashboard. If you want named places in Slack alerts, that is the next thing to wire up.
