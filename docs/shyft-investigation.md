# Shyft-os — hydrological-forecasting investigation

Reference survey of [Shyft-os](https://gitlab.com/shyft-os/shyft) as
a candidate hydrological-forecasting framework for
`nokken-forecasting`. Written during Phase 2 as one leg of the
forcing-variable triangulation (Shyft, hydrological literature, MET
Nordic availability). This is investigation, **not adoption** —
it characterises the framework so later decisions are informed.

Primary reading was done against a shallow clone at SHA
`8c038f0068f3d3180072554fbdfc387bb3778a01` on branch `master`
(2026-04-06). Citations below link to that SHA, keeping the
references reproducible even as the upstream repo keeps moving (the
project tagged `35.1.0` two weeks after the clone — see §12).
Tutorial citations use `shyft.readthedocs.io/en/latest/…` because the
rendered pages are authoritative for prose.

## 1. Scope and framing

This doc characterises Shyft-os for future decisions; it does not
commit the project to it. Open decisions 6 (framework choice) and 9
(Shyft-os local-dev path) in
[`docs/scoping-genesis.md` §8](./scoping-genesis.md#8-open-decisions)
remain deferred to Phase 4 per
[`ROADMAP.md`](../ROADMAP.md); the investigation feeds Phase 4's
head-to-head against the Phase-3 best baseline, and it feeds the
forthcoming variable decision (the Phase 2b-ii fetcher PR in
`nokken-data`).

Scope is **hydrological forecasting only**. Shyft-os is a larger
toolbox that also covers:

- **Energy-market modelling** (`shyft.energy_market`, STM / DSTM
  services, Nordic / European market ops).
- **ENKI-style optimisation** surfaces.
- **Dashboard** widgetry (`shyft.dashboard`).
- The **Distributed Time-Series System (DTSS)** used for market-ops
  storage (we may touch it incidentally where it backs hydrology
  services, but we do not evaluate it as storage).

Those subsystems exist, they ship in the same repo, and they leak
into some orchestration paths (notably
[`examples/hydrology/task/calibrate.py`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/examples/hydrology/task/calibrate.py)
imports from `shyft.energy_market.stm`), but they are out of scope
for this survey.

## 2. TL;DR

Shyft-os is an actively maintained (release every 1–2 weeks,
current tag `35.1.0`, 2026-04-20, per
[`gitlab.com/shyft-os/shyft/-/tags`](https://gitlab.com/shyft-os/shyft/-/tags))
distributed conceptual hydrology framework built in C++23 with a
pybind11 Python API, developed at Statkraft and the University of
Oslo and used in Statkraft's 24×7 operational inflow forecasting.

**Default stack for Norwegian catchments.** PTGSK —
Priestley-Taylor evapotranspiration, Gamma-snow (energy-balance
snow with shape-distributed snow-covered area), Kirchner (2009)
catchment response — is the de-facto default. Every Nea-Nidelva
tutorial and both Statkraft-originating API walkthroughs use it
([`run_nea_configured_simulations.rst` L93–101](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_configured_simulations/run_nea_configured_simulations.rst),
[`shyft_intro.rst` L136–150](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst)).
Ten sibling stacks exist (PTSSK, PTHSK, PTHPSK, PTFSM2K, PTSTK,
PTSTHBV, R-PTGSK, R-PMGSK, R-PMSTK, R-PMVSTK — enumerated in
[`cpp/shyft/hydrology/stacks/cell_model_all.h`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/stacks/cell_model_all.h));
the `r_*` family adds an explicit radiation pre-processor, the `pm*`
family swaps Priestley-Taylor for Penman-Monteith.

**Forcing variables — minimum.** PTGSK takes **five** per-cell
time series: 2-m air temperature [°C], precipitation [mm/h],
downwelling shortwave radiation [W/m²], relative humidity [0..1],
wind speed [m/s]
([`cpp/shyft/hydrology/cell/model.h` L32–89](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/cell/model.h#L32)).
NaN in any of them is a fatal runtime condition, not a silent skip
— there is no documented shorter subset. Gamma-snow itself
consumes all five in an energy-balance formulation
([`methods/snow/gamma/calculator.h` L197–210](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/methods/snow/gamma/calculator.h)),
so substituting a degree-day snow (Skaugen, HBV-snow) still keeps
the five-variable surface because Priestley-Taylor independently
needs radiation and humidity.

**Forcing variables — perform-well.** Same five, with hourly
cadence, elevation-stratified station input or gridded NWP
expressed as geo-point sources. Longwave (`lw_in`) and split
direct/diffuse shortwave (`sw_dir`, `sw_diffuse`) are optional
`environment` slots consumed only by `r_*` stacks
([`model.h` L38–44](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/cell/model.h#L38));
PTGSK derives clear-sky longwave internally from T + RH. Station
→ cell distribution runs inside the engine via Bayesian temperature
kriging for T (with elevation as covariate) and IDW for the other
four
([`region/interpolation_parameter.h` L20–47](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/region/interpolation_parameter.h#L20),
[`region/model.ipp` L85–200](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/region/model.ipp#L85)).

**Geometry inputs.** A pre-built **cell vector**: one row per cell
with `(x, y, z, area, catchment_id, radiation_slope_factor,
glacier, lake, reservoir, forest, unspecified)`
([`cf_region_model_repository.py` L190–194](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/netcdf/cf_region_model_repository.py#L190)).
The canonical Nea-Nidelva cell grid is 1 km × 1 km, EPSG:32633
([`neanidelva_region.yaml` L7–14](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/test_suites/hydrology/netcdf/neanidelva_region.yaml#L7)).
**Shyft does not ship a polygon → cell rasteriser** — intersecting
NVE catchment polygons with a DEM + a land-cover raster to produce
the cell NetCDF is a consumer-side offline step.

**Installability.** PyPI is effectively abandoned as a
cross-platform channel: `shyft 26.0.0.post1` (2025-02-26,
[`pypi.org/pypi/shyft/json`](https://pypi.org/pypi/shyft/json))
ships only a `win_amd64 cp311` wheel, no sdist, no macOS, no
arm64. The `sigbjorn/shyft` Anaconda channel is Windows-only and
last updated 2023-09-24
([`anaconda.org/sigbjorn/shyft`](https://anaconda.org/sigbjorn/shyft)).
Official release artifacts today are **signed RPMs** from the
GitLab package registry, built on Fedora and Arch
([`README.md` L149–162](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/README.md#L149),
[`.gitlab-ci.yml`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/.gitlab-ci.yml)).
**macOS is not a supported target**: the only macOS / Darwin
reference in the repo is a host-ID stub
([`shyft_prologue.cmake` L94–95](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/cmake/shyft_prologue.cmake#L94)),
CMakePresets has no Darwin preset, CI has no macOS runner, no
package recipe ships for it. From-source requires GCC ≥ 13 /
Clang ≥ 16, CMake 3.27–3.31, Boost ≥ 1.83, RocksDB, dlib,
Armadillo, and a BLAS/LAPACK stack
([`shyft_find_packages.cmake`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/cmake/shyft_find_packages.cmake),
[`CMakeLists.txt` L1](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/CMakeLists.txt#L1)).
Realistic local-dev path on the operator's Apple-Silicon macOS is
an upstream Linux container via Podman / Docker under Rosetta
emulation — arm64 images are not published. Open decision 9 was
right to flag this as a blocker.

**Clearest runnable example.** The Nea-Nidelva PTGSK trio:
[`run_nea_configured_simulations.rst`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_configured_simulations/run_nea_configured_simulations.rst)
(YAML-driven), with
[`run_nea_nidelva_part2.rst`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_nidelva_part2/run_nea_nidelva_part2.rst)
showing the low-level API assembly and
[`run_nea_calibration.rst`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_calibration/run_nea_calibration.rst)
showing how to calibrate it. One hourly year (8 759 steps), 27
sub-catchments, ~3 874 cells, PTGSK, five-variable forcing —
exactly the template a Phase 4 head-to-head would follow. All three
need the separate `shyft-data` companion repo checked out.

**Implications for Phase 2.** Any MET Nordic / locationforecast
ingest that stops at temperature + precipitation locks Shyft out
without a re-backfill. To keep Shyft viable as the Phase 4
framework, the forcing ingest has to persist the **five-variable
bundle per gauge-basin and per hour**: T, P, shortwave, RH, wind.
See §13 for detail.

## 3. Architecture overview

Shyft decomposes a catchment into **cells**. Each cell owns
geometry (mid-point `GeoPoint(x, y, z)`, area, `catchment_id`, a
`radiation_slope_factor`, and `LandTypeFractions(glacier, lake,
reservoir, forest, unspecified)` summing to 1.0) and runs a
**method stack** independently
([`doc/sphinx/content/hydrology/concepts.rst`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/concepts.rst)).
A **region model** owns a typed `cell_vector`, the stack's
parameters (region-wide plus optional per-catchment overrides),
interpolation parameters, and a `region_environment` holding
geo-located forcing `SourceVector`s — one per variable.

The run loop is:

1. Build cell vector (offline; see §5).
2. Construct region model with stack-specific `parameter_t`.
3. Populate `region_env` with
   `TemperatureSourceVector` / `PrecipitationSourceVector` / … from
   a `GeoTsRepository` or directly from code.
4. Set initial state (per-cell snapshot) via a `StateRepository`.
5. `region_model.initialize_cell_environment(time_axis)` — allocate
   per-cell forcing storage.
6. `region_model.interpolate(ip, region_env)` — project sources
   onto cells (BTK or IDW per variable; see §4.3).
7. `region_model.run_cells()` — advance every cell forward in time
   in parallel.
8. Extract output — discharge, state, response — via statistics
   and collector objects (§6).

Cells with the same `catchment_id` are aggregated for statistics.
Optionally cells are wired into a **river network**
([`cpp/shyft/hydrology/routing/`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/routing/))
with per-cell unit-hydrograph shaping and Muskingum-style routing,
producing discharge at arbitrary interior river nodes — useful for
our multi-section case (§6.5).

## 4. Forcing input requirements

### 4.1 Variables, units, and cadence

PTGSK (canonical Norway stack) reads the five slots on the cell
`environment`
([`cell/model.h` L32–89](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/cell/model.h#L32)):

| Slot | Units | Notes |
|---|---|---|
| `temperature` | °C @ 2 m | Bayesian-kriged with elevation covariate |
| `precipitation` | mm / h | IDW; scaled per-cell by `precipitation_correction.scale_factor` (headline calibration knob) |
| `sw_in` | W / m² | downwelling shortwave; **required** for PT evap + gamma-snow energy balance |
| `rel_hum` | 0..1 | fraction; used in PT longwave and gamma-snow vapour pressure |
| `wind_speed` | m / s | gamma-snow turbulent flux |

Optional extra slots `sw_dir`, `sw_diffuse`, `lw_in`, `snow_fall`,
`p_atm` exist for `r_*` and Penman-Monteith stacks but are not
consumed by PTGSK
([`model.h` L38–44](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/cell/model.h#L38)).

### 4.2 Minimum vs. perform-well

Minimum to **run at all** = all five. NaN in any of them is fatal
(`has_nan_values` is a runtime check,
[`model.h` L76–88](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/cell/model.h#L76));
there is no "PTGSK-light" with fewer variables. Swapping Gamma for
a degree-day snow (Skaugen, HBV snow) discards `rad` and
`wind_speed` inside the snow routine
([`methods/snow/skaugen/calculator.h` L92–100](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/methods/snow/skaugen/calculator.h#L92)),
but PT evap still needs radiation + humidity, so the **five-variable
ingest surface is invariant across every stack in the default
family**.

To **perform well** on a Norwegian melt-fed catchment the same
variables at hourly cadence are preferred; precipitation is the
dominant control, and the calibration knobs the Nea-Nidelva tutorial
exposes (`gs.tx`, `gs.wind_scale`, `gs.wind_const`, `gm.dtf`,
`p_corr.scale_factor`, `kirchner.c1/c2/c3`) are all either snow- or
precipitation-related
([`shyft_intro.rst` L540–543](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst)).
Radiation mattering as much as it does — for both gamma-snow
energy balance and PT potential ET — was surprising to our reading;
§13 calls out the Phase-2 implication.

### 4.3 Temporal & spatial resolution

Temporal resolution is user-chosen via a `TimeAxis(start, delta,
n)`. Hourly is the operational cadence
(`neanidelva_simulation.yaml` uses `run_time_step=3600,
number_of_steps=8759` per
[`run_nea_configured_simulations.rst` L155](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_configured_simulations/run_nea_configured_simulations.rst)).
Daily is fine for development / CAMELS examples
([`shyft_intro.rst` L417](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst)).
Sub-hourly is supported by construction (the ODE solver in Kirchner
adapts internally); the radiation tutorial exercises 1 h / 3 h /
24 h with the same model
([`radiation_camels_data.rst`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/radiation_camels_data/radiation_camels_data.rst)).
Our reading suggests no hard lower bound; gamma-snow is typically
validated hourly–daily.

Spatially, **Shyft is station-oriented by design** — cells pull
from nearby `geo_point_source` series. Interpolation lives inside
the region model
([`region/model.ipp` L85–200](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/region/model.ipp#L85)):

- **Bayesian temperature kriging (BTK)** for temperature, using
  elevation as secondary variable; requires ≥ 2 sources at different
  heights ([`spatial/bayesian_kriging.h` L253–256](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/spatial/bayesian_kriging.h#L253)).
  Overridable to IDW via `use_idw_for_temperature`.
- **Inverse-distance weighting** for precipitation, wind, radiation,
  humidity — always.
- **Ordinary kriging** is implemented
  ([`spatial/kriging.h` L69](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/spatial/kriging.h#L69))
  but not wired into the default region-model driver.
- **Kalman filter / gridpp** for bias-correcting gridded forecasts
  against stations ([`spatial/kalman.h`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/spatial/kalman.h)).

Gridded NWP is consumed by feeding each grid point as its own
`geo_point_source` — Shyft still IDWs / BTKs to cell. An outside
orchestrator "could provide its own ready-made interpolated signal,
e.g. temperature input from arome-data" per the implementer comment
at
[`model.ipp` L96–98](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/region/model.ipp#L96).
Shyft ships readers for seNorge, AROME/MEPS, WRF, GFS, ERA-Interim,
ERA5
([`python/shyft/hydrology/repository/netcdf/`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/netcdf/));
[`met_netcdf_data_repository.py` L21–60](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/netcdf/met_netcdf_data_repository.py#L21)
is the MET Norway adapter. It reads static NetCDF, not a live
thredds feed; the "live ingest" piece belongs in nokken-data
regardless.

## 5. Geometry input requirements

**Cell vector fields.** Eleven columns per cell: `x, y, z, area,
catchment_id, radiation_slope_factor, glacier, lake, reservoir,
forest, unspecified`
([`cf_region_model_repository.py` L190–194](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/netcdf/cf_region_model_repository.py#L190)).
Land-cover is **four signed fractions + an unsigned residual**
computed as `unspecified = 1 − (forest + lake + reservoir + glacier)`
([line 187](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/netcdf/cf_region_model_repository.py#L187)).

**DEM resolution.** The Nea-Nidelva config uses a 1 km × 1 km grid
in EPSG:32633 (`nx=109, ny=80, step_x=step_y=1000`,
[`neanidelva_region.yaml` L7–14](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/test_suites/hydrology/netcdf/neanidelva_region.yaml#L7)).
Shyft itself never reads a DEM file; the DEM only matters offline
when building `cell_data.nc`. Our reading suggests 250 m / 500 m
cell grids are fine in principle — they just multiply cell count.

**Catchment polygons.** Shyft sees only an integer `catchment_id`
column in `cell_data.nc`; polygon vertices are not consumed
([line 164–165](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/netcdf/cf_region_model_repository.py#L164)).
An alternate `cf_region_model_repository_tin.py` builds cells from
a triangulated irregular network instead of a regular grid — not
used in any tutorial.

**No NVE polygon → cell rasteriser ships.** The shipped readers
consume a pre-built NetCDF. Intersecting NVE `regine_main` polygons
with a DEM and a land-cover raster to produce that NetCDF is the
consumer's responsibility — typically a `shapely` / `rasterio`
pipeline. The only shapefile handling in Shyft is in
`python/shyft/hydrology/viz/geom_preps/` for plotting, not ingestion
(confirmed by `rg -l 'geopandas\|fiona\|\.shp' python/shyft`). This
is the single biggest geometry-side engineering task Phase 4 would
need to solve.

## 6. Output shape

### 6.1 Outlet / catchment discharge

`region_model.statistics.discharge([catchment_id])` returns a
`TimeSeries` aggregated over cells whose `catchment_id` is in the
list; an empty list means all catchments
([`shyft_intro.rst` L457](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst),
[`run_nea_configured_simulations.rst` L218](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_configured_simulations/run_nea_configured_simulations.rst)).

### 6.2 Internal state and per-cell response

Stack-specific collectors expose per-cell state + response time
series: `gamma_snow_state` for snow water equivalent and snow
water, `gamma_snow_response` for snow-covered area and outflow,
`kirchner_state` for the catchment response single-reservoir `q`,
`actual_evapotranspiration_response`, `priestley_taylor_response`
([`cpp/shyft/py/hydrology/stacks/r_pmv_st_k.cpp` L73–170](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/py/hydrology/stacks/r_pmv_st_k.cpp#L73)).
`calibrate_model.py` routinely pulls `discharge, charge, snow_swe,
snow_sca` per cell
([`examples/hydrology/demo/calibrate_model.py` L67–70](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/examples/hydrology/demo/calibrate_model.py#L67)).

Collection must be enabled via `region_model.set_state_collection(cid,
True)` **before** the run or per-cell state history is discarded for
speed
([`shyft_intro.rst` L391–398](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst#L391));
this trips consumers during calibration because the optimised
region-model clone uses a null-collector.

**Soil moisture as a profile is not exposed.** Kirchner is a
single-reservoir catchment response, so the "soil" state is a scalar
`kirchner.q` per cell. That's a characteristic of the modelling
choice, not an API gap — a project that needs a vadose-zone
profile is not a Shyft project.

### 6.3 Lead-time-aware forecasts

Our reading suggests Shyft's engine is fundamentally a **hindcast
simulator**; "forecast" at the API level means "run the model
forward from state `S` with forecast forcing `F`".
`DefaultSimulator.run_forecast(time_axis, t_c, state)` calls
`geo_ts_repository.get_forecast(...)` for the forecast cycle closest
to reference time `t_c` and then runs the same cell loop
([`simulator.py` L165–174](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulator.py#L165)).
**`t_c` is not stamped into the output.** If we want
`(issue_time, lead_time)` tables — the forecast-sink contract in
`nokken-web` — we build that in application code, running repeatedly
and writing to our own sink with issue / lead columns.

Forecast-issue-time semantics do exist first-class on the
repository side:
[`ForecastSelectionCriteria`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/interfaces.py#L240)
supports `created_within_period`, `covers_period`, `latest_n_older_than`,
`at_reference_times`. The asymmetry is: the repo knows about issue
times, the model doesn't pass them through.

### 6.4 Ensembles and quantiles

Two layers.
`DefaultSimulator.create_ensembles(time_axis, t_c, state)` returns
N runnable simulators, one per member from
`get_forecast_ensemble`; running them is the consumer's job
([`simulator.py` L176–189](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulator.py#L176)).
Percentile / quantile reduction across members is a first-class
`TsVector` op: `ts_vector.percentiles(ta, [10, 50, 90])`
([`shyft_api_essentials.rst` L130](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_api_essentials/shyft_api_essentials.rst)).
There is no "run ensemble and emit quantile fan" one-liner;
orchestration is ours.

### 6.5 Discharge inside a catchment

Two mechanisms, both useful for nokken's multi-section case.

- **Routed rivers.** `cpp/shyft/hydrology/routing/river_network.h`
  defines a network of river nodes, each with `output_m3s(node_id)`,
  `local_inflow`, `upstream_inflow`
  ([`routing_model.h` L160–211](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/routing/routing_model.h#L160)).
  `target_specification` supports a `ROUTED_DISCHARGE` property, so
  a time series is available at any routing node, not just the
  outlet.
- **Sub-catchment grouping.** Assign each paddling section its own
  `catchment_id` and read
  `region_model.statistics.discharge([cid_reach])`. Simpler; doesn't
  need routing; appears to be the natural fit for our ~180 sections
  if their reach boundaries are encoded upstream when the cell
  vector is built.

## 7. Calibration approach

**Four optimisers** ship in
[`calibration_algorithms.h`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/region/calibration_algorithms.h):

- **BOBYQA** (dlib's `find_min_bobyqa`) — local, L46–92.
- **dlib global** (`find_min_global`) — L111–141.
- **DREAM** — in-house MCMC-flavoured global; L169–179.
- **SCE-UA** — Duan/Sorooshian/Gupta shuffled-complex evolution;
  L207–234.

Parameters are normalised to `[0, 1]` before the solver runs and
exposed on a per-stack `*Optimizer` Python class
([`cpp/shyft/py/hydrology/stacks/region_model.h` L905–1105](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/py/hydrology/stacks/region_model.h)).

**Targets.**
[`target_specification`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/cpp/shyft/hydrology/region/calibration_criteria.h#L57)
carries the observed `ts`, one-or-many `catchment_indexes`, a
`scale_factor` weight, a `calc_mode` (`NASH_SUTCLIFFE`,
`KLING_GUPTA`, `ABS_DIFF`, `RMSE`), a `catchment_property`
(`DISCHARGE`, `SNOW_COVERED_AREA`, `SNOW_WATER_EQUIVALENT`,
`ROUTED_DISCHARGE`, `CELL_CHARGE`), and KG sub-weights `s_r, s_a,
s_b`. Multi-gauge weighted calibration and SCA / SWE targets are
first-class.

**Overall objective** is a weighted sum over targets of
`scale_factor * (1 − metric)`; no "global NSE" default. Parameter
bounds are YAML-driven in `ConfigCalibrator`
([`config_simulator.py` L98–114](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulators/config_simulator.py#L98)).

**Data volume.** The Nea tutorial notes "Calibration may take up
to 10 minutes" but does not print iteration counts
([`run_nea_calibration.rst` L97](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_calibration/run_nea_calibration.rst)).
`DefaultSimulator.optimize(...)` defaults `max_n_evaluations=1500`
([`simulator.py` L217](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulator.py#L217)).
Our reading suggests O(1 000–1 500) forward evaluations per run at
tutorial scales, with calibration window length driven by whatever
the user supplies — no fixed 1 year / 5 year convention.

**Reusable machinery vs. example-specific.** Both. `ConfigCalibrator`
([`config_simulator.py` L62–198](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulators/config_simulator.py#L62))
drives calibration from YAML. `DefaultSimulator.optimize(...)` does
the same for code-driven flows. A newer service-based path
(`CalibrateModelArgs`, `DrmClient`) routes through the DRMS
micro-service
([`examples/hydrology/task/calibrate.py` L22–56](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/examples/hydrology/task/calibrate.py#L22)).
There is no `shyft-calibrate` CLI; calibration is assembled per
project.

## 8. Examples in the repo

Hydrology examples + tutorials, unified list. "✓ N-closest" tags
the ones most similar to our use case.

| Name | Demonstrates | Forcing | Catchment | Stack | Entry |
|---|---|---|---|---|---|
| `shyft_intro` | Build a 1-cell model end-to-end | CAMELS-US daily | CAMELS 06191500 | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst) |
| `shyft_api` | Build cells from NetCDF, run a sim | `shyft-data` Nea NetCDFs | Nea-Nidelva | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_api/shyft_api.rst) |
| `shyft_api_essentials` | TimeSeries / Calendar / TimeAxis ergonomics | `shyft-data` Nea | Nea-Nidelva | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_api_essentials/shyft_api_essentials.rst) |
| `single_cell` | Call snow / ET routines directly | Synthetic | 1 point | HBV-snow, method-level | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/single_cell/single_cell.rst) |
| `run_nea_nidelva` ✓ #1 | YAML-configured Nea-Nidelva sim via orchestration | `shyft-data`, `CFDataRepository` + `CFTsRepository` | Nea-Nidelva (27 sub-catchments) | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_nidelva/run_nea_nidelva.rst) |
| `run_nea_nidelva_part2` ✓ #2 | Same catchment via raw API; repository-authoring template | `shyft-data` raw NetCDFs | Nea-Nidelva | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_nidelva_part2/run_nea_nidelva_part2.rst) |
| `run_nea_calibration` | YAML-configured calibration, KGE + NSE | `shyft-data` | Nea-Nidelva | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_calibration/run_nea_calibration.rst) |
| `run_nea_configured_simulations` ✓ #3 | Minimal-code `YAMLSimConfig` Nea run | `shyft-data` | Nea-Nidelva | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_configured_simulations/run_nea_configured_simulations.rst) |
| `repository_intro` | Repository pattern explainer | `shyft-data` | Nea-Nidelva | PTGSK | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/repository_intro/repository_intro.rst) |
| `gridpp_simple` | Kalman bias-correction MET forecasts vs stations | Synthetic | n/a | KalmanFilter | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/gridpp_simple/gridpp_simple.rst) |
| `gridpp_geopoints` | 2-D Kalman + OK kriging + IDW | Synthetic 1 km grid | n/a | Kalman + OK + IDW | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/gridpp_geopoints/gridpp_geopoints.rst) |
| `kalman_updating` | Kalman parameter sensitivity | Synthetic | n/a | Kalman | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/kalman_updating/kalman_updating.rst) |
| `ordinary_kriging_precipitation` | Station-to-grid precip kriging | Synthetic | n/a | OrdinaryKriging | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/ordinary_kriging_precipitation/ordinary_kriging_precipitation.rst) |
| `penman-monteith-sensitivity` | FPM / SPM ET sensitivity | Synthetic | 1 point | PenmanMonteith | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/penman-monteith-sensitivity/penman-monteith-sensitivity.rst) |
| `penman-monteith-verification-single-method` | Verify vs ASCE-EWRI ref | Embedded ref | 1 point | PenmanMonteith | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/penman-monteith-verification-single-method/penman-monteith-verification-single-method.rst) |
| `radiation_sensitivity_analysis` | Allen 2006 eq.38 verification | Synthetic | 1 point | Radiation | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/radiation_sensitivity_analysis/radiation_sensitivity_analysis.rst) |
| `radiation_polar_region` | Radiation at 76.8 °N | Synthetic | Polar | Radiation | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/radiation_polar_region/radiation_polar_region.rst) |
| `radiation_camels_data` | Radiation on CAMELS-US daily | CAMELS-US | 1 CAMELS basin | Radiation | [rst](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/radiation_camels_data/radiation_camels_data.rst) |
| `examples/hydrology/demo/*` | Service-stack PTGSK run + calibrate via DTSS/DRMS/GCD | Synthetic "nea" | Synthetic | PTGSK | [dir](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/examples/hydrology/demo/) |

### 8.1 Closest to our use case

Our use case: 1–7-day flow forecast at a Norwegian NVE gauge, MET
Nordic / AROME forcing, ~180 sections, PTGSK.

1. **`run_nea_nidelva` (Part 1, YAML)** — closest overall. Real
   Norwegian catchment (Nea-Nidelva), PTGSK, 27 sub-catchments,
   multi-gauge calibration targets, hourly time step. What changes
   for us: swap `CFDataRepository` (NetCDF station files) for a
   Postgres-backed `GeoTsRepository`; swap `CFTsRepository`
   discharge reads for our NVE `observations` rows; replace the
   static 2013–2014 window with a rolling training window; add
   forecast-mode plumbing (issue-time / lead-time metadata) on top
   of the hindcast engine.
2. **`run_nea_nidelva_part2`** — same catchment via raw API; the
   template for writing **our own** Postgres repository because it
   walks through "here is how a NetCDF becomes a
   `PrecipitationSourceVector`". Change: swap `netCDF4.Dataset`
   reads for `asyncpg` queries returning `TsVector` /
   `GeoPointVector`; `ARegionEnvironment` assembly stays almost
   verbatim.
3. **`examples/hydrology/demo/`** — closest to an **operational
   service shape**: a long-running DTSS + DRMS stack that executes
   PTGSK runs and calibrations on named tasks. Change: swap
   `fill_dummy_data` synthetic series for a real ingester driven off
   the `nokken-data` Postgres; use the service scheduler for the
   forecast job. Caveat: the service orchestration path depends on
   `shyft.energy_market.stm` for task persistence — it is not
   "hydrology-only" in its current form.

### 8.2 Runnable from a fresh clone?

Every Nea-Nidelva tutorial needs the separate
[`shyft-data`](https://gitlab.com/shyft-os/shyft-data) repo
(referenced via `SHYFT_DATA` env var). `shyft_intro` needs the
`shyft-doc` companion test-data bundle. `radiation_camels_data`
needs CAMELS-US. The `examples/hydrology/demo/` scripts need the
full DTSS / GCD / DRMS / Parameter / State / Task / DSTM / STM /
GoalFunction service stack booted first (`start_services.py` does
that). The only hydrology example fully self-contained in the main
repo is `demo_test.py`, which is a pytest of helper code.

## 9. Python API shape

Condensed from
[`shyft_intro.rst`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst);
ellipses `# …` elide irrelevant lines. All line numbers are in the
linked rst.

```python
import shyft.hydrology as api
import shyft.time_series as sts

# 1. pick stack (PT = Priestley-Taylor, GS = gamma snow, K = Kirchner)
model_type = api.pt_gs_k.PTGSKModel                                    # L143

# 2. build cells
ltf = api.LandTypeFractions(glacier=0.0, lake=0.0, reservoir=0.0,
                            forest=0.0, unspecified=1.0)               # L175
gcd = api.GeoCellData(api.GeoPoint(x, y, z), area, cid,
                      radiation_slope_factor=1.0, ltf=ltf)             # L176
cell = model_type.cell_t(); cell.geo = gcd                             # L177
cell_vector = model_type.cell_t.vector_t(); cell_vector.append(cell)   # L181

# 3. construct region model with region + per-catchment params
region_model = model_type(cell_vector,
                          model_type.parameter_t(),
                          model_type.parameter_t.map_t())              # L212

# 4. build per-variable SourceVectors into region_env
t_ts = sts.TimeSeries(ta_srs, sts.DoubleVector.from_numpy(data.temperature),
                      sts.POINT_AVERAGE_VALUE)                         # L256
region_env = api.ARegionEnvironment()
region_env.temperature = api.TemperatureSourceVector(
    [api.TemperatureSource(api.GeoPoint(0, 0, 0), t_ts)])              # L298
# … precipitation / wind_speed / rel_hum / radiation similar …
region_model.region_env = region_env                                   # L322

# 5. initial state, time-axis, interpolate to cells, run
region_model.state.apply_state(state_with_id_vct, [1])                 # L373
region_model.initialize_cell_environment(ta_sim)                       # L424
region_model.interpolate(region_model.interpolation_parameter,
                         region_model.region_env)                      # L434
region_model.run_cells()                                               # L444

# 6. pull catchment-aggregated discharge
q = region_model.statistics.discharge([1])                             # L457
```

The high-level `YAMLSimConfig` + `ConfigSimulator` wrapper
([`config_simulator.py` L125–163](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulators/config_simulator.py#L125))
collapses the same calls into `simulator.run()`.

### 9.1 The repository abstraction

A "repository" is Shyft's name for the model-to-outside-world
boundary, modelled on Fowler's Repository pattern (cited verbatim:
[`interfaces.py` L9–48](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/repository/interfaces.py#L9)).
Four contracts: `RegionModelRepository` (cells + params),
`GeoTsRepository` (forcing series, plus `get_forecast` and
`get_forecast_ensemble`), `StateRepository` (snapshot lifecycle),
`InterpolationParameterRepository`.

To back Shyft with **Postgres**, we'd subclass `GeoTsRepository` and
implement
`get_timeseries(input_source_types, utc_period, geo_location_criteria)`
returning a dict `{"temperature": TemperatureSourceVector, …}`.
**This is a supported extension point, not a fork.**
`DefaultSimulator` accepts any `GeoTsRepository` implementation via
its constructor
([`simulator.py` L77–84](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulator.py#L77));
the Part 2 tutorial explicitly encourages it: "normally one would
write their own repository to conduct all of this 'under the
covers'"
([`run_nea_nidelva_part2.rst` L211](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/run_nea_nidelva_part2/run_nea_nidelva_part2.rst)).

One sharp edge: `DefaultSimulator` passes a
`shapely.geometry.Polygon` as the `geo_location_criteria` — a
Postgres repository must accept a polygon, not a bbox tuple
([`simulator.py` L158–161](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulator.py#L158)).

## 10. Installability

Scoping-doc §4.2 called PyPI "Windows CPython 3.11 wheel only"; that
remains true on the **latest** release, and the Linux wheels have
frozen 40+ versions behind.

**PyPI** ([`pypi.org/pypi/shyft/json`](https://pypi.org/pypi/shyft/json),
[`pypi.org/simple/shyft/`](https://pypi.org/simple/shyft/)).
Latest is `shyft 26.0.0.post1` (2025-02-26), and it ships **one**
file — `shyft-26.0.0.post1-cp311-cp311-win_amd64.whl`. No sdist,
no macOS, no arm64. Linux wheels stopped at `13.0.3`
(`manylinux1_x86_64`, `cp37m`…`cp311`). Our reading suggests these
older wheels claim a `manylinux1` tag they don't actually satisfy,
which is common but fragile. `pip install shyft` on a Linux CI
today yields `13.0.3`; on macOS it fails outright.

**Anaconda** — only the historical
[`sigbjorn/shyft`](https://anaconda.org/sigbjorn/shyft) channel,
last upload 2023-09-24, Windows-only,
`linux-64: Last supported version 5.1.2`, no macOS. There is no
`conda-forge/shyft`
([`anaconda.org/search?q=shyft`](https://anaconda.org/search?q=shyft)).
Effectively unmaintained and irrelevant at `26.x`+.

**Official distribution today** is **signed RPMs** from the GitLab
package registry, built on Fedora and Arch
([`doc/sphinx/content/releases/overview.rst` L158–160](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/releases/overview.rst#L158):
`shyft-runtime-<ver>.rpm`, `shyft-development-<ver>.rpm`,
`shyft-python-<ver>.rpm`; signed with a Nitrokey-backed key).
"Primary development and CI testing platforms: **Fedora**,
**Arch Linux**"; Windows is explicitly "legacy support for existing
deployments"
([`README.md` L149–162](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/README.md#L149)).

**macOS is not a supported platform.**

- README supported-platforms list omits it
  ([L149–162](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/README.md#L149)).
- `CMakePresets.json` has no Darwin preset
  ([link](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/CMakePresets.json)).
- `.gitlab-ci.yml` has no macOS runner
  ([link](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/.gitlab-ci.yml)).
- No macOS wheel on PyPI, no macOS conda build.
- No `tools/buildah/macos/` — only `archlinux/` and `fedora/`
  ([`tools/buildah/`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/buildah/)).
- `rg -i 'macos|darwin|apple|osx'` across the repo returns **one**
  hit: a host-ID stub at
  [`shyft_prologue.cmake` L94–95](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/cmake/shyft_prologue.cmake#L94).

**From-source requirements.**

- CMake 3.27–3.31 (hard range, not minimum —
  [`CMakeLists.txt` L1](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/CMakeLists.txt#L1)).
- **C++23**. Implies GCC ≥ 13, Clang ≥ 16, MSVC VS 2022
  ([`CMakePresets.json`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/CMakePresets.json#L75)).
- Ninja + `mold` linker in CI
  ([`tools/buildah/build.sh` L121](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/buildah/build.sh#L121)).
- **C++ deps.** Boost ≥ 1.83 (`serialization`, `thread`, `atomic`,
  `chrono`, `program_options`), fmt, OpenSSL, RocksDB, dlib,
  Armadillo + BLAS/LAPACK, pybind11, NumPy + Python dev headers,
  doctest, google benchmark, plus (Arch `packages.dev`) liburing,
  onetbb, SuperLU, libjxl, arpack, gflags, snappy, lz4, zstd, mpfr
  — all distro-packaged
  ([`shyft_find_packages.cmake`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/cmake/shyft_find_packages.cmake),
  [`tools/buildah/archlinux/packages.dev`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/buildah/archlinux/packages.dev)).
  Substantial native stack; RHEL/UBI 9 default toolchain (GCC 11)
  is insufficient.

**Geospatial.** `netcdf4` (HDF5/NetCDF), `pyproj` (PROJ), `shapely`
(GEOS) — yes. **No GDAL.** So polygon rasterisation stays outside
Shyft.

**CI & reproducibility.** Buildah-based images
(`registry.gitlab.com/shyft-os/shyft/archlinux-dev:latest`,
`fedora-dev:latest`) with pinned base-image SHA256s and in-image
`/usr/share/shyft-build/provenance/PROVENANCE-build.txt`
([`build.sh` L266](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/tools/buildah/build.sh#L266)).
A consumer-side CI recipe pulling these images is realistic.

**Realistic install paths.**

- *Apple-Silicon macOS local dev.* Only viable via a Linux
  container under Rosetta emulation — the upstream images are
  amd64-only; no arm64 is published. Homebrew from-source would be
  pioneering; `shyft_prologue.cmake`'s `MACOS→DARWIN` branch is a
  stub, not a supported path; expect to hit Boost.Serialization
  ABI, RocksDB / Armadillo / Qt (if `SHYFT_WITH_ENERGY_MARKET`) and
  `quadmath` (GNU-only) pain.
- *Linux amd64 CI / prod.* Pull the upstream Fedora or Arch dev
  image, or consume the signed RPM release. Do not rely on PyPI.

This lands squarely on open decision 9.

## 11. Limitations and gotchas

- **License inconsistency.** `LICENSE` + `README.md` declare
  **LGPL-3.0**
  ([`README.md` L9](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/README.md#L9),
  [`LICENSE` L1–2](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/LICENSE)),
  but `COPYING` L12–15 asserts "GNU General Public License …
  version 3 of the License" — i.e., **GPLv3**
  ([`COPYING`](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/COPYING#L12)).
  Material wart for anyone needing a clean license opinion; worth
  flagging at Phase 4.
- **Python 3.11 on PyPI.** Our stack is 3.12 — either build from
  source or consume distro packages.
- **Typed vectors, not lists.** `IntVector`, `DoubleVector`,
  `TemperatureSourceVector` etc. are C++-backed; `dv[0] = 2.2`
  works, `iv[0] = 2.2` silently won't; no `.pop()`, `.index()`
  ([`shyft_api_essentials.rst` L44–68](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_api_essentials/shyft_api_essentials.rst)).
- **Two POINT interpretations.** `POINT_AVERAGE_VALUE` vs.
  `POINT_INSTANT_VALUE` — get this wrong on forcing and
  interpolation silently produces wrong values; nothing checks it.
- **Response collection off by default during calibration.** The
  optimiser's region-model clone uses a null-collector; a follow-on
  `gamma_snow_response.sca(...)` returns nothing unless you called
  `set_state_collection(...)` first
  ([`shyft_intro.rst` L391–398](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/tutorials/shyft_intro/shyft_intro.rst#L391)).
- **YAML configs use `!!python/name:` tags.** PyYAML constructor
  errors on bad import paths are obscure; the YAML is active code.
- **PROJ workaround.** `cf_region_model_repository.py` rewrites
  `+e=0` → `+e=1e-100` via regex to sidestep a PROJ bug (line
  82–83) — on newer PROJ builds this can shift transforms by a
  micrometre. Harmless, weird during debugging.
- **`region_model.clone` is a closure installed by the repository.**
  Copy-constructing a region model *without* going through the
  repository loses `bounding_region` / `catchment_id_map`
  ([`simulator.py` L95–107](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/python/shyft/hydrology/orchestration/simulator.py#L95)).
- **`catchment_id`s are user-chosen unbounded ints** — Nea-Nidelva
  uses `1996`, `1000011` verbatim from NVE GIS. Shyft also uses a
  0-based `catchment_index` internally; don't confuse the two.
- **Shyft-data is a separate repo.** No tutorial runs end-to-end
  from this clone alone.
- **Orchestration is in flux.**
  [`concepts.rst` L61–67](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/doc/sphinx/content/hydrology/concepts.rst)
  warns the repository / orchestration layer is "undergoing a
  refactorization"; newer micro-services are referenced but not
  demonstrated in tutorials.
- **FSM2 and snow-tiles stacks are undocumented** in the tutorial
  set despite existing in the C++ core.
- **No native forecast-skill / ensemble diagnostics** (CRPS,
  reliability diagrams, rank histograms) in public tutorials; skill
  assessment is our problem.
- **No native `(issue_time, lead_time)` metadata on model output**
  (§6.3). Downstream sinks have to carry that themselves.

## 12. Community and maintenance signals

- **Release cadence.** Tag `35.1.0-0.1` (2026-04-20) is current, a
  release every 1–2 weeks over the visible window
  ([`tags`](https://gitlab.com/shyft-os/shyft/-/tags), snapshotted
  2026-04-24). Our cloned SHA (2026-04-06) is already two weeks
  behind a newer tag — expected.
- **Commit cadence.** ~40–50 commits visible for 2026-03-09 →
  2026-04-06 alone
  ([`commits/master`](https://gitlab.com/shyft-os/shyft/-/commits/master)),
  extrapolating to several hundred commits/year. Appears highly
  active.
- **Contributor base.** Narrow in the sample — recent top commits
  are Sigbjørn Helset (packaging / CI / release signing);
  `COPYING` L5–7 names Helset, Burkhart, Skavhaug, Abdella +
  Statkraft; README credits Statkraft + UiO (L240–275). Our reading
  suggests a Statkraft / UiO core team with limited outside
  contributor volume; confirming with a wider commit window was
  outside the time budget.
- **Issue tracker.** The rendered GitLab issue list is
  client-side-loaded; we could not snapshot open-vs-closed counts or
  external authors via WebFetch. The README routes community
  discussion to a Google Group
  ([badge L8](https://gitlab.com/shyft-os/shyft/-/blob/8c038f0068f3d3180072554fbdfc387bb3778a01/README.md#L8)),
  which suggests GitLab Issues is not the primary community
  channel. Worth verifying manually at Phase 4.
- **License** — LGPL-3.0 (with the COPYING discrepancy noted in
  §11).

Bottom line: active, well-maintained, but effectively single-team.
Bus factor matches scoping §4.5 — Statkraft's core group.

## 13. Implications for Phase 2

Three concrete follow-ups.

**A. Forcing-variable ingest has to carry five variables, not two.**
PTGSK (and every stack in the default family via the
Priestley-Taylor ET dependency) needs **temperature, precipitation,
downwelling shortwave radiation, relative humidity, and wind speed**
per basin per hour (§4). Any MET Nordic / locationforecast fetcher
design that persists only T and P locks Shyft out of Phase 4
without a re-backfill. This is the most load-bearing finding of this
investigation; it is the input that the forthcoming Phase 2b-ii
variable-decision PR has to respect.

The variable decision is still the variable decision's — whether to
actually persist all five depends on MET availability and
hydrological literature. But the Shyft-side constraint is **all
five**. This needs to be in the variable-decision doc, not a
footnote.

**B. Hourly cadence is the operational norm.**
Statkraft's Nea-Nidelva runs `deltahours(1)` over 8 759 steps/year
(§4.3). Our forecast-sink migration already landed hourly
(`docs/scoping-genesis.md` Decisions-final), so this is already
aligned. The forcing tables should be hourly too — which was
already the `nokken-data` design, but the Shyft reading confirms
it.

**C. macOS local-dev path is genuinely blocked.** Open decision 9
was right to treat Shyft-on-Mac as an open question. The clearest
path is a **Linux container on the operator's laptop** (Podman /
Docker, amd64 under Rosetta emulation — arm64 is not published).
Homebrew-from-source is uncharted. This should inform how Phase 4
scopes "stand up Shyft" — budget for the container route, not the
native-build route.

A further pre-Phase-4 engineering task this investigation surfaced,
which does **not** change Phase 2 but is worth writing down: a
polygon → cell rasteriser that takes NVE `regine_main` polygons, a
DEM, and a land-cover raster and produces Shyft's
`cell_data.nc` for a target section (§5). That is the single biggest
geometry-side piece a Phase 4 head-to-head needs.

---

*This investigation does not itself recommend adopting Shyft-os.
Open decisions 6 and 9 remain deferred. The forthcoming Phase 2b-ii
variable-decision PR, and Phase 4's head-to-head, are the venues
where those decisions close.*
