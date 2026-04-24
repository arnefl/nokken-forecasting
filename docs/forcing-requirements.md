# Forcing-requirements specification — Phase 2

Demand document from `nokken-forecasting` to `nokken-data`:
the forcing variables, cadence, and spatial shape the
forecasting layer requires for Phase 3 baselines and Phase 4
framework evaluation. Written during Phase 2 as the reality-check
pass against MET Norway's published products; the next PR in
`nokken-data` implements against it.

Framework choice remains deferred — open decision 6 in
[`docs/scoping-genesis.md` §8](./scoping-genesis.md#8-open-decisions)
revisits at end of Phase 3. The five-variable floor below is "keep
Shyft viable as a Phase 4 candidate," not "we are building for
Shyft." The floor itself is taken from
[`docs/shyft-investigation.md` §4](./shyft-investigation.md#4-forcing-input-requirements)
and [§13](./shyft-investigation.md#13-implications-for-phase-2) as
load-bearing finding of that investigation; §3 reproduces it
concisely and does not re-derive it.

MET product identification was done against
[MET Norway NWPdocs wiki](https://github.com/metno/NWPdocs/wiki),
[thredds.met.no](https://thredds.met.no/thredds/metno.html), and
[api.met.no](https://api.met.no) on 2026-04-24. Every factual claim
about MET products carries a permalink to MET's own documentation
or thredds catalogue; where MET's docs are silent we flag the
uncertainty rather than guess.

## Decisions (final)

Compact reference for the nokken-data fetcher author and the Phase 3
modeller — skip the rest of this doc if you are orienting only.

- **Five-variable floor.** Every Shyft stack in the default PTGSK
  family consumes the same five forcings per cell per hour:
  temperature, precipitation, downwelling shortwave, relative
  humidity, wind speed ([Shyft §4.1](./shyft-investigation.md#41-variables-units-and-cadence)).
  Any variable dropped from persistence is a Shyft-out-of-scope
  decision that Phase 4 cannot recover from without re-backfill.
  All five are in scope for Phase 2 ingest. (§3)
- **Historical observations product** — **MET Nordic Rerun
  Archive v4** (`metpparchivev4/`) for 2012-09-01 → 2025-10-31,
  stitched with **MET Nordic Operational Archive**
  (`metpparchive/`) from 2025-11 onward. 1 km Lambert conformal
  conic grid over Fennoscandia, hourly, NetCDF via thredds
  ([MET Nordic dataset wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset)).
  All five variables present. Known pre-2016-11-08 precipitation
  quality step-change — material for hindcast weighting.
  `source = "met_nordic_analysis_v4"` for v4 rows;
  `source = "met_nordic_analysis_operational"` for the 2025-11+
  stitch. (§4.1)
- **Live forecast product — primary** —
  **locationforecast 2.0 `/complete`**
  ([datamodel](https://docs.api.met.no/doc/locationforecast/datamodel.html)).
  Point-JSON, 9-day horizon, hourly steps to ~66 h then 6-hourly to
  +228 h. Exposes four of five variables:
  `air_temperature`, `precipitation_amount`, `relative_humidity`,
  `wind_speed`. `source = "met_locationforecast_2_complete"`. (§4.2)
- **Live forecast product — shortwave** —
  **MET Nordic Forecast 1 km** on thredds
  (`metpplatest/metpparchive`,
  [wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset))
  for
  `integral_of_surface_downwelling_shortwave_flux_in_air_wrt_time`
  over its 58–64 h lead window. Beyond +66 h the shortwave forecast
  is a gap (§5); mitigation deferred to Phase 3 when hindcast
  numbers tell us what skill is at stake.
  `source = "met_nordic_forecast_1km"`. (§4.2, §5)
- **Spatial aggregation** — basin-mean per gauge per hour. One row
  per (gauge, timestep, variable, source) in nokken-web's
  `weather_observations` / `weather_forecasts` tables. Rationale:
  baselines consume basin-mean directly, and per-grid-point
  persistence is infeasible at 1 km × Sjoa × 13 yrs × 5 vars (§6).
- **Phase 4 deferrals** — named in §7 so they are not lost:
  (a) basin-mean concession against Shyft's per-cell design;
  (b) Shyft-viability-if-a-variable-drops risk; (c) shortwave
  horizon gap past +66 h. Revisit at Phase 4 start.

Per-variable summary table — MET's names, units on disk, conversion:

| Our name | Obs (`metpparchivev4`) | Fcst point (locationforecast `/complete`) | Fcst grid (`metpplatest`) | Persist? |
|---|---|---|---|---|
| temperature | `air_temperature_2m` (K → −273.15) | `air_temperature` °C `instant.details` | `air_temperature_2m` (K) | yes |
| precipitation | `precipitation_amount` (mm, hourly-backward-accumulated; already rate) | `precipitation_amount` mm `next_1_hours.details` (short) / `next_6_hours.details` (medium) | `precipitation_amount` (mm, deaccumulated hourly) | yes |
| shortwave | `integral_of_surface_downwelling_shortwave_flux_in_air_wrt_time` (W·s·m⁻², ÷3600 → W·m⁻²) | **absent** | `integral_of_surface_downwelling_shortwave_flux_in_air_wrt_time` (W·s·m⁻²) | yes — obs always, fcst from `metpplatest` to +66 h, gap beyond (§5) |
| relative humidity | `relative_humidity_2m` (fraction 0..1) | `relative_humidity` % `instant.details` | `relative_humidity_2m` (fraction 0..1) | yes |
| wind speed | `wind_speed_10m` (m·s⁻¹) | `wind_speed` m·s⁻¹ `instant.details` | `wind_speed_10m` (m·s⁻¹) | yes |

## 3. The five-variable floor

Shyft-os's PTGSK (and every stack in the default family via the
Priestley-Taylor ET dependency) consumes five forcings per cell per
hour: **temperature, precipitation, downwelling shortwave,
relative humidity, wind speed**
([Shyft §4.1](./shyft-investigation.md#41-variables-units-and-cadence);
[Shyft §4.2](./shyft-investigation.md#42-minimum-vs-perform-well)).
Minimum to run at all = all five; a NaN in any slot is a runtime
fatal. There is no "PTGSK-light" with fewer variables. Swapping
Gamma snow for Skaugen or HBV snow discards radiation and wind
inside the *snow routine*, but PT evap still needs them, so the
five-variable ingest surface is invariant across every default-family
stack.

The load-bearing Phase-2 implication
([Shyft §13 part A](./shyft-investigation.md#13-implications-for-phase-2))
is that **any forcing ingest persisting fewer than five variables
locks Shyft out of Phase 4 without a re-backfill**. This is why
Phase 2 carries all five even though the initial Phase-3 baselines
(persistence, recession curve, linear regression, gradient-boosted
trees — [`docs/scoping-genesis.md` §5](./scoping-genesis.md#5-baseline-stack-survey--fair-comparison-depth))
will use a much smaller subset.

The decision here is not "Shyft wins" — that decision is deferred.
The decision is "keep Shyft viable." A literature-level debate about
whether the Norwegian operational consensus on five forcings is
right or wrong would be a Phase-4 question at best; it is not a
Phase-2 question, because a re-backfill is always cheaper than
re-running an already-built v1 model on richer data.

## 4. MET reality check

### 4.1 Historical observations — MET Nordic Rerun Archive v4

**Product identity.** "MET Nordic" is the umbrella dataset; it
includes a past-time **MET Nordic Analysis** ("Rerun Archive" for
reprocessed, methodology-stable historical runs) and a future-time
**MET Nordic Forecast** (§4.2 Product B). Versions 1, 2, and 3 of
the rerun archive **became inaccessible in January 2026**, and v3 is
scheduled for deletion on 2026-05-01
([v3 wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset-v3)).
**v4** is the currently supported release and covers
**2012-09-01T03Z → 2025-10-31T23Z** hourly
([MET Nordic dataset wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset)).
For 2025-11-01 onward, the **Operational Archive** (`metpparchive/`,
2018-03-01 → present) uses the same grid, variable set, and file
layout ([wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset))
and stitches directly onto the rerun archive.

**Access.** Thredds catalogue — no API key, anonymous HTTPS:

- Rerun v4:
  [`/thredds/catalog/metpparchivev4/catalog.html`](https://thredds.met.no/thredds/catalog/metpparchivev4/catalog.html)
- Operational archive:
  [`/thredds/catalog/metpparchive/catalog.html`](https://thredds.met.no/thredds/catalog/metpparchive/catalog.html)

Three access modes: direct HTTPS download (`/thredds/fileServer/…`),
OPeNDAP (`/thredds/dodsC/…`), and NcML subsetting. MET's own notice
at [thredds root](https://thredds.met.no/thredds/metno.html)
explicitly warns: **"Don't spawn multiple parallel opendap sessions
or file downloads."** A polite serial fetcher is mandatory.

**Grid and CRS.** 1 km Lambert conformal conic; proj4
`+proj=lcc +lat_0=63 +lon_0=15 +lat_1=63 +lat_2=63 +no_defs +R=6.371e+06`
([wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset)).
No EPSG is published — the grid is a custom LCC on a **spherical
earth** (R = 6 371 000 m); when reprojecting to EPSG:25833 for
basin aggregation, spherical-vs-WGS84 handling can introduce
sub-pixel mis-registration. Flagged for operator verification when
the aggregation raster code lands in nokken-data. Coverage:
Norway + Sweden + Finland + Denmark + Baltics — fully encloses
Sjoa.

**Temporal.** Hourly, timestamp = end of hour, UTC. No sub-daily or
daily rollups published — aggregate ourselves if needed.

**File format and chunking.** One NetCDF-4 (CF-compliant) per hour,
path `metpparchivev4/YYYY/MM/DD/met_analysis_1_0km_nordic_YYYYMMDDThhZ.nc`.
File size is not stated on the wiki; a rerun over 2012–present is
~120 000 files. Flagged for operator to measure at first-fetch time
so disk budget is real.

**Licensing.** **NLOD / CC BY 4.0**. Attribution text: "Data from
The Norwegian Meteorological Institute" or "Based on data from MET
Norway"
([MET licensing page](https://www.met.no/en/free-meteorological-data/Licensing-and-crediting)).
The page does not explicitly scope historical gridded products;
flagged for operator verification, but CC BY 4.0 is the default for
MET's free datasets.

**Variables (all five confirmed).** Names per the
[v4 wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset)
(the v3 wiki lists the same variable set; v4 carries it forward):

| Our need | NetCDF short_name | Unit on disk | Conversion |
|---|---|---|---|
| temp 2m | `air_temperature_2m` | K | K → °C (subtract 273.15) |
| precip | `precipitation_amount` | mm, hour-accumulated backward | already hourly rate; keep as mm·h⁻¹ |
| shortwave | `integral_of_surface_downwelling_shortwave_flux_in_air_wrt_time` | W·s·m⁻² (time-integrated over the hour) | divide by 3600 → W·m⁻² |
| RH 2m | `relative_humidity_2m` | 1 (fraction) | ×100 if %-needed downstream |
| wind 10m | `wind_speed_10m` | m·s⁻¹ | none |

**Known caveats on our five variables** — material for hindcast
weighting, not blockers:

- **Precipitation** — quality step-change at 2016-11-08 when the
  background NWP moved from deterministic to ensemble; pre-step
  precip is systematically lower quality
  ([wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset)).
  Sjoa hindcast 2012–2016 will be degraded on precipitation.
- **Precipitation** — bias-corrected against Netatmo (crowdsourced),
  WMO stations, and radar
  ([v3 wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset-v3));
  Netatmo coverage is regional and introduces quality variability.
- **Shortwave** — plain nearest-neighbour from the 2.5 km NWP
  parent, **no bias correction against observations**. The least
  tuned of our five — note this when reading Phase-3 residuals.
- **Temperature / RH** — downscaled (elevation-gradient / bilinear)
  from 2.5 km MEPS plus observation bias correction. Not a
  direct-observation grid.
- **Wind** — elevation-gradient downscaling only; valley-channeling
  in the Sjoa gorge will not be resolved at 1 km.
- **Known data gap** — 2019-03-03T13Z → 2019-03-04T02Z filled from
  ECMWF in v3
  ([v3 wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset-v3));
  v4 wiki does not re-confirm. Worth stamping as "gap-filled" if
  the fetcher encounters it.

**Sibling products — named, not chosen.** MET publishes a
long-term-consistent variant
([`metppltcarchivev1/`, 1958 → 2025](https://thredds.met.no/thredds/catalog/metppltcarchivev1/catalog.html))
useful only if pre-2012 training data ever surfaces. seNorge2018 is
daily temp+precip only (too coarse temporally). NORA3 and ERA5 /
CARRA are reanalyses at 3 km / 31 km / 2.5 km; CARRA would be a
reasonable fallback if MET Nordic v4 were ever pulled, but at 1 km
with obs-bias-correction the rerun archive is the better fit for
Sjoa.

### 4.2 Live forecast — locationforecast 2.0 `/complete` + shortwave fallback

There are two products in scope — locationforecast for T / P / RH /
wind (the point-JSON path most of our forecasting does) and MET
Nordic Forecast 1 km on thredds for the shortwave variable that
locationforecast does not expose. The structural gap was the
load-bearing sub-question of this investigation; §4.2.2 documents it
and §5 carries the mitigation choice forward.

#### 4.2.1 Product A — locationforecast 2.0 `/complete`

**Variant.** Locationforecast 2.0 publishes three methods:
`/complete`, `/compact`, and `/classic` (legacy XML, frozen). The
`/complete` method is a strict superset of `/compact` and adds fog
fraction, dewpoint, cloud layers, wind-gust, temperature /
wind-speed 10th and 90th percentiles, UV index, and period-aggregate
probabilities
([datamodel variables](https://docs.api.met.no/doc/locationforecast/datamodel.html#variables)).
We use **`/complete`**: our four locationforecast-served variables
are already in `/compact`, but the extra percentiles give us a free
quantile channel that Phase-3 pinball loss will consume.

**Endpoint URL.**
`https://api.met.no/weatherapi/locationforecast/2.0/complete?lat={lat}&lon={lon}&altitude={m}`
— altitude is optional but
[recommended for temperature accuracy](https://api.met.no/weatherapi/locationforecast/2.0/documentation#Parameters).
Lat/lon must be truncated to ≤ 4 decimal places or the server
returns 403
([TOS §5 Traffic](https://api.met.no/doc/TermsOfService)).

**Horizon.** 9 days total. The
[datamodel availability chart](https://docs.api.met.no/doc/locationforecast/datamodel.html#availability)
shows **short-range hourly steps to ~66 h in the Nordic domain, then
medium-range 6-hourly to +228 h**. Instant variables (temperature,
wind speed, relative humidity) are present at every timestep;
period variables (precipitation) live in `next_1_hours.details`
over the short range and `next_6_hours.details` over the medium
range.

**Spatial semantics.** Point query. Under the hood the Nordic
domain is served from **MEPS** (2.5 km, hourly update), Arctic from
**AROME-Arctic** (2.5 km, 4×/day), and the rest of the world from
**ECMWF HRES** (~9 km, 4×/day)
([datamodel data sources](https://docs.api.met.no/doc/locationforecast/datamodel.html#data-sources)).
Coverage polygons are published at
[`/locations.json`](https://api.met.no/weatherapi/locationforecast/2.0/locations.json);
the MEPS polygon covers mainland Norway plus surrounding seas and
encloses Sjoa. No land/sea mask — point queries over water still
return forecasts.

**Response structure.** GeoJSON Feature,
`properties.meta.{updated_at, units}` +
`properties.timeseries[].{time, data.{instant, next_1_hours,
next_6_hours, next_12_hours}}`
([swagger](https://api.met.no/weatherapi/locationforecast/2.0/swagger)).
Period blocks carry
`summary.symbol_code` and `details.{…}`; instant blocks carry the
three always-present continuous variables we need.

**Our five — keys, units, presence:**

| Our need | Key | Block | Unit on wire |
|---|---|---|---|
| temperature | `air_temperature` | `instant.details` | °C |
| precipitation | `precipitation_amount` | `next_1_hours.details` (short range) / `next_6_hours.details` (medium range) | mm (accumulated over the block) |
| **shortwave** | — | — | **absent** (see §4.2.2) |
| relative humidity | `relative_humidity` | `instant.details` | % |
| wind speed (10 m, 10-min avg) | `wind_speed` | `instant.details` | m·s⁻¹ |

**Shortwave absence, specifically.** Exhaustive read of the
[datamodel variables table](https://docs.api.met.no/doc/locationforecast/datamodel.html#variables)
and the
[OpenAPI schema](https://api.met.no/weatherapi/locationforecast/2.0/swagger)
shows no radiation-flux field. The only radiation-adjacent fields
are `ultraviolet_index_clear_sky` (instant) and the period
aggregates `ultraviolet_index_clear_sky_max` — UV index, not
W·m⁻². **Confirmed absent.** MET's
[NWPdocs wiki](https://github.com/metno/NWPdocs/wiki) routes users
who need shortwave to the gridded products on thredds. §4.2.2 picks
that path up.

**Rate limits and polite-client.** No anonymous / registered tier
split — one tier. Bandwidth cap:
**> 20 req/s per application requires special agreement**
([TOS Bandwidth](https://api.met.no/doc/TermsOfService#bandwidth));
above that MET returns 429 and risks IP blocking. Required headers:

- `User-Agent` of form `"appname/version contact"`
  (e.g. `"nokken.net/1.0 arnelyshol@gmail.com"`); missing or generic
  UAs (`okhttp`, `Java`, `Dalvik`, `fhttp`) are rejected with 403
  ([AUTHENTICATION](https://api.met.no/weatherapi/locationforecast/2.0/documentation#AUTHENTICATION),
  [TOS §5 Identification](https://api.met.no/doc/TermsOfService#identification)).
- `If-Modified-Since` **equal to the previously received
  `Last-Modified`** (not an arbitrary prior date) — expect 304 if
  nothing has changed
  ([TOS §5 Traffic](https://api.met.no/doc/TermsOfService#traffic)).
- Honour `Expires` — don't retry until that time.
- Support gzip. Add jitter to scheduled requests.

**NWP issue cycles.** The endpoint itself caches for **30 minutes**
([update frequencies](https://docs.api.met.no/doc/updates.html)) and
the Nordic source model (MEPS) runs lagged-ensemble hourly updates
with **full 6-hour cycles at 00 / 06 / 12 / 18 UTC**
([datamodel Nordic](https://docs.api.met.no/doc/locationforecast/datamodel.html#nordic-area);
[NWPdocs MEPS](https://github.com/metno/NWPdocs/wiki/MEPS-dataset)).
Medium-range ECMWF updates twice per day (00 / 12 UTC). The seed's
working assumption of 00 / 06 / 12 / 18 UTC is **confirmed for the
short-range Nordic window we actually care about**; the scheduled
fetcher can run after each of those cycles with safe margin.

**Licensing.** CC BY 4.0
([swagger info.license](https://api.met.no/weatherapi/locationforecast/2.0/swagger);
[License page](https://api.met.no/doc/License)). No use of "Yr" /
MET / NRK marks
([TOS Trademarks](https://api.met.no/doc/TermsOfService#trademarksandnamingrestrictionsnew)).

#### 4.2.2 Product B — MET Nordic Forecast 1 km on thredds (shortwave only, for now)

Because shortwave is absent from locationforecast, one thredds path
is mandatory regardless of what we pick for the other four. The
**MET Nordic Forecast 1 km** post-processed product on thredds
(files named `met_forecast_1_0km_nordic_<YYYYMMDD>T<HH>Z.nc`) is
the canonical source.

**Access.** Same thredds infrastructure as §4.1:

- Real-time:
  [`/thredds/catalog/metpplatest/catalog.html`](https://thredds.met.no/thredds/catalog/metpplatest/catalog.html)
- Operational archive:
  [`/thredds/catalog/metpparchive/catalog.html`](https://thredds.met.no/thredds/catalog/metpparchive/catalog.html)
  — the same archive the observations stitch uses past +1 h, see §4.1.
- OPeNDAP endpoint shape:
  `https://thredds.met.no/thredds/dodsC/metpplatest/met_forecast_1_0km_nordic_<cycle>.nc`

**What it is.** Post-processed, statistically bias-corrected blend
of MEPS + observations (including crowdsourced) — MET's "best
forecast estimate" and the dataset behind Yr
([MET Nordic wiki](https://github.com/metno/NWPdocs/wiki/MET-Nordic-dataset)).
Same grid, CRS, and per-variable NetCDF short-names as §4.1. Hourly.

**Lead times and cycle.** Forecasts run to **58–64 h** depending on
input availability. Archive retains analyses hourly and full
forecasts at 00 / 06 / 12 / 18 UTC. This means **the shortwave
forecast does not cover the full 7-day horizon** — the mitigation is
in §5.

**Variables of interest.** All five present (cited per-variable in
the §2 summary table). File size per cycle is ~4.5 GB whole-grid
(NetCDF-4); OPeNDAP subsetting pulls only the N basin-sample points
× 5 variables × 66 hours — ~tens of KB of actual payload per cycle.

**Auth / rate.** Anonymous HTTPS. Same "don't spawn parallel
sessions" convention as the archive; contact `thredds@met.no` for
priority access only if we discover we need it. Same CC BY 4.0.

**Raw MEPS fallback (not chosen, named for §7).**
[`meps_det_2_5km_*.nc`](https://thredds.met.no/thredds/catalog/meps25epsarchive/catalog.html)
is the raw 2.5 km deterministic MEPS run, with all five variables
present (precipitation as cumulative `precipitation_amount_acc` —
needs differencing; wind as `x_wind_10m`/`y_wind_10m` — needs
magnitude). 30-member ensemble to +61 h, control to +66 h. File
size ~78 GB per cycle — OPeNDAP subsetting is mandatory. We do not
use it in v1; we prefer the post-processed 1 km product because it
is the forecast MET operates production forecasts on and the grid
and downscaling match the historical archive.

**Engineering comparison — point-JSON vs NetCDF.**

| Dimension | locationforecast | thredds |
|---|---|---|
| Shape | 1 GET per point per refresh, ~50 KB JSON | 1 OPeNDAP request per cycle spans N points × 5 vars × 66 h |
| Pipeline fragility | Rate-limit-bounded (20 req/s); polite-client required | Sequential OPeNDAP requests; MET warns against parallelism |
| Forecast quality | Point-interpolated from MEPS / ECMWF; includes post-processed bias correction | Fully gridded NWP + post-processing; consistent with the historical archive |
| Variable coverage | 4 of 5 | 5 of 5 |
| Horizon | 9 days (hourly to +66 h, 6-hourly beyond) | 58–64 h, hourly |

The split — locationforecast for four variables to 9 days, thredds
for shortwave to +66 h — is the v1 shape. §5 records the
shortwave-horizon gap and the mitigation choice.

## 5. Gap register

### 5.1 Locationforecast shortwave gap (confirmed)

Locationforecast does not expose downwelling shortwave (§4.2.1).
This is the training-vs-inference asymmetry the Shyft investigation
flagged: we will train against obs-with-shortwave but the live
forecast path can go only 58–64 h with shortwave before hitting the
MET Nordic Forecast horizon. Four mitigations, one recommendation.

**Option A — derive from cloud cover + solar geometry at query
time.** Locationforecast `/complete` exposes
`cloud_area_fraction` (and low / medium / high cloud layer
fractions) at every instant. A simple clear-sky shortwave + cloud
attenuation (e.g. Kasten-Czeplak or an Angstrom-style parameterised
fit) gives a synthetic shortwave from the same point-JSON we
already fetch. Clearly approximate — cloud optical depth varies
across the same cover fraction; latent in winter polar conditions —
but has the enormous merit of being the same wire call and the
same rate budget as the four native variables.

**Option B — use MET Nordic Forecast 1 km (§4.2.2) beyond +66 h.**
Not possible. The grid forecast runs out at the same horizon
regardless. So Option B is "use thredds to +66 h, accept that
shortwave forecasts are unavailable past +66 h and fall back to
Option A (derivation) or Option C (climatology) for +66 h to +168 h."
This is the v1 path modelled against.

**Option C — accept the gap, train with shortwave, run live
without it beyond +66 h (substitute shortwave climatology or a
seasonal-monthly mean).** Degraded skill past +66 h on any feature
that depends on shortwave. Honest about the mismatch;
Phase 3 will tell us how much skill this costs.

**Option D — ditch shortwave from live-forecast features past +66 h
entirely, using only a "short-range horizon" model below +66 h and a
"long-range" model above +66 h trained without the shortwave
feature.** Two-model complexity. Revisit at Phase 3 if Options A–C
all fail.

**Recommendation — decide at Phase 3 when we have hindcast numbers
to compare against.** Defer the choice, build the scaffolding to
support A / B / C in parallel: persist shortwave from the historical
archive unconditionally, persist shortwave from MET Nordic Forecast
over its 0–66 h window unconditionally, leave +66 h–+168 h
shortwave unpopulated for now. Phase 3 runs baselines with three
shortwave-treatment variants and tells us what skill is at stake.
Deferring with reasons is a valid decision per
[`docs/scoping-genesis.md` §8](./scoping-genesis.md#8-open-decisions)
convention.

### 5.2 Historical precipitation quality step-change (2016-11-08)

Covered in §4.1 — pre-2016-11-08 precipitation is systematically
lower quality. Mitigation: flag the cut-off in hindcast reports; if
the 2012-01 → 2016-11 window degrades skill materially, consider
training on 2017-01 → 2019-12 only and hindcasting 2020-01 → 2024-12
unchanged. The training-window decision is a Phase 3 call against
actual numbers and is not re-opened here.

### 5.3 MET Nordic Analysis v3 deprecation

The v3 wiki states access ends 2026-05-01 — eight days after this
doc lands. v4 covers the same variables with the same grid and
naming. Any tooling elsewhere in our ecosystem that references v3
needs to move to v4 before the deadline; this is a
nokken-data-fetcher implementation note, not a forecasting gap.

### 5.4 Custom LCC spherical earth

The MET Nordic grid uses a spherical earth (R = 6 371 000 m) for
its Lambert conformal conic. Reprojection to EPSG:25833 for basin
clipping needs the same proj4 string MET publishes — substituting a
WGS84 ellipsoid silently introduces sub-pixel mis-registration. Flag
for the fetcher author to assert the proj4 round-trip at ingest
time.

## 6. Spatial aggregation decision

**Decision — basin-mean per gauge per hour.** One row per
`(gauge_id, time, variable, source)` in nokken-web's
`weather_observations` / `weather_forecasts` tables (the re-keyed
schema from nokken-web migration 008 — see
[`SCHEMA_COMPAT.md`](../SCHEMA_COMPAT.md)). No per-grid-point
persistence in v1.

**Rationale — the per-point persistence arithmetic.** MET Nordic at
1 km × Sjoa basin (~3 700 km² after the basin polygon clips) is
~3 700 grid cells. Over 2012-09 → present that is
~120 000 hours × 3 700 cells × 5 variables = **~2.2 × 10⁹
per-grid-point rows for observations alone**, and doubles if
forecasts land the same way. Timescale can physically hold it, but
baseline models in Phase 3 consume basin-mean anyway — per-cell
storage is pure overhead for v1.

**Phase-4 concession against Shyft's per-cell design.** Shyft's
region model interpolates per-point `geo_point_source` series to
cells via BTK / IDW
([Shyft §4.3](./shyft-investigation.md#43-temporal--spatial-resolution)).
Feeding it basin-mean makes every cell in the catchment get the
same value, bypassing Shyft's spatial-resolution machinery entirely.
This is a concession, not a blocker: Phase 4 can escape in three
ways, all deferred:

- **Re-fetch per-point sources on demand** for Shyft experiments,
  treating MET Nordic as the source of truth and the basin-mean
  table as cache only.
- **Augment persistence with elevation-band-stratified forcing**
  (bins of 100 / 200 m elevation) — a middle-ground richer than
  basin-mean but cheaper than per-cell.
- **Accept the limitation** — Shyft becomes a Phase-4 candidate
  whose spatial distribution is effectively uniform. For a ~3 700 km²
  basin this may still be competitive with a lumped baseline.

The Phase-2 commitment is basin-mean. The Phase-4 escape hatches are
recorded here so the decision tree is visible; §7 repeats the
pointer so it is not lost.

## 7. Phase 4 deferrals

Named explicitly so the Phase-4 kick-off picks them up.

1. **Spatial aggregation revisit (§6).** Basin-mean today,
   per-point / per-elevation-band / accept-the-limitation tomorrow.
   Not pre-committed.
2. **Shyft-viability risk if any variable drops from persistence
   (§3).** Phase-2 ingest carries all five. If a later refactor
   considers dropping one, re-read §3 first.
3. **Shortwave horizon gap +66 h → +168 h (§5.1).** Phase-3 hindcast
   numbers choose between derivation / climatology / two-model
   split. v1 leaves the rows unpopulated.
4. **Raw MEPS vs MET Nordic Forecast (§4.2.2).** We chose the
   post-processed 1 km product for v1. If Phase 4 wants raw NWP (e.g.
   for a Shyft experiment that prefers un-post-processed forcings)
   the raw MEPS archive is cited and documented.

## 8. Ask to nokken-data

What to build next, specified from the forecasting side.

### 8.1 Per-variable fetch contract

Five variables from three sources into two sink tables — one
historical row-set from `metpparchivev4` + `metpparchive`, one live
row-set from `locationforecast` + `metpplatest`:

| Variable | Obs source string | Fcst source string(s) |
|---|---|---|
| temperature | `met_nordic_analysis_v4` (2012-09 → 2025-10), `met_nordic_analysis_operational` (2025-11+) | `met_locationforecast_2_complete` |
| precipitation | same | `met_locationforecast_2_complete` |
| shortwave | same | `met_nordic_forecast_1km` (0 → +66 h only; +66 h → +168 h left unpopulated in v1) |
| relative humidity | same | `met_locationforecast_2_complete` |
| wind speed | same | `met_locationforecast_2_complete` |

Source strings are chosen to match MET's own product names so that
future debugging against an MET service rename is tractable. Write
them verbatim to the `source` column; do not collapse variants.

### 8.2 Aggregation shape

Basin-mean per gauge per hour. One row per
`(gauge_id, time, variable, source)` in `weather_observations` /
`weather_forecasts`. Stamp `basin_version` from `basins_current` at
aggregation time — per migration 008 the schema is gauge-keyed, not
basin-keyed, and the version pin lives on the row. Compute the mean
once per `(gauge, timestep)`; write gauge-keyed rows.

Historical obs: raster aggregation from the 1 km NetCDF grid clipped
to the basin polygon, area-weighted.

Live forecast from locationforecast: **point-sampling at ~2.5 km
spacing on a regular grid inside the basin polygon, equal-weight
average across points**. For Sjoa (~3 700 km²) that is ~592 points.
Rate budget: at 5 req/s (well under the 20 req/s special-agreement
ceiling, §4.2.1), ~120 s per refresh × 4 cycles/day = ~8 min/day
per gauge — comfortable. Density rationale: 2.5 km matches MEPS's
native grid spacing, so we sample the forecast at its native
resolution — neither coarser (loses information) nor finer (no new
information, wastes budget). The intentional v1 inconsistency with
the historical area-weighted raster aggregation must not be
reconciled: the two products have different native shapes and
each is sampled at its own native resolution.

Live forecast from `metpplatest` shortwave: grid aggregation like
the historical path, area-weighted over the same basin polygon.

### 8.3 Operational requirements

- **Scope.** Faukstad only (`gauge_id = 12`, `sourcing_key = 2.595.0`)
  for first integration. Add a `--gauge-id` flag mirroring the
  existing NVE basin fetcher so the pipeline generalises.
- **Resumability.** Both historical and live fetchers resumable
  across failures — no all-or-nothing runs. Historical is naturally
  chunkable by hour; live is naturally chunkable by cycle.
- **Polite-client (locationforecast).** Project-identifying
  `User-Agent`, overridable via environment variable. Honour
  `If-Modified-Since` / `ETag`; treat 304 as no-op. Honour `Expires`
  and add jitter.
- **Polite-client (thredds).** Sequential requests only — MET's
  "no parallel OPeNDAP sessions" warning
  ([thredds root](https://thredds.met.no/thredds/metno.html))
  applies.
- **Schema pin.** `SCHEMA_COMPAT.md` already pins nokken-web at
  migration 008; no new bump needed for this work.

No code changes land in this PR — this is specification only. The
fetcher PR in nokken-data implements against the above.
