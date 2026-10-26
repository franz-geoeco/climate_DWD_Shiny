# DWD Climate Explorer (QGIS plugin)

A QGIS port of the "German Climate Dashboard" R/Shiny app. Instead of an embedded Leaflet map and static R plots, it drives the real QGIS map canvas and hosts interactive matplotlib charts in a dock widget.

## What it does

-   **Stations tab** - loads all \~1,400 DWD weather stations that publish daily climate, monthly climate, hourly wind and/or annual climate-index data as a real QGIS point layer, colour-coded by data-availability tier (full / climate-only / basic / none - same 4-tier scheme as the original dashboard). Click "Pick station on map" and click a marker on the canvas to select a station and see its details (state, elevation, exact date ranges per product).
-   **Climate tab** - fetch data by city name (geocoded via OpenStreetMap Nominatim), by coordinates, or by whichever station you picked on the map. Five sub-tabs, each with a two-handle year-range slider and an interactive matplotlib panel (pan/zoom/save-PNG toolbar on every chart):
    -   Temperature / Precipitation (daily)
    -   Wind speed / direction (hourly, aggregated to daily means)
    -   30-year climate diagram (classic temperature+precipitation chart)
    -   Temperature trend & anomaly vs. the 1961-1990 baseline
    -   Annual climate indices (frost/ice/hot/summer days, tropical nights)
    -   CSV export for the climate and wind series
-   **Raster Grids tab** - downloads a Germany-wide 1km x 1km DWD multi-annual grid (temperature, precipitation, sunshine duration, or one of five day-count indices) for a chosen month/season/year and reference period (1961-1990 ... 1991-2020), and adds it to the map canvas as a properly georeferenced, styled raster layer (not a static image) that lines up with the station layer and reprojects on the fly. Includes a **change map** section: pick two reference periods and it computes period B minus period A (e.g. 1991-2020 minus 1961-1990 mean temperature) via QGIS's own raster calculator, styled with a diverging ramp centred on zero.
-   **Phenology tab** - plant development-stage data (sowing, flowering, ripening, leaf colouring, ...) for field crops, fruit, vine and wild plants, from DWD's phenology network -- a separate set of stations from the Stations/Climate tabs, with its own point layer and map-click picker. Choose annual reporters (Jahresmelder, the long-term ~100-year record) or immediate reporters (Sofortmelder, faster but shorter records back to ~1979), a category, a species, and a station (city name / coordinates / map click); the phase list is populated from whatever that station actually reports. Charts entry day-of-year vs. year for the selected phase with a linear trend line (e.g. "this variety's flowering date has shifted N days/decade earlier"), plus CSV export.
-   **PDF report** - a "Generate PDF report for this station" button on the Climate tab exports a multi-page PDF: a Germany-wide overview map (every station as a dot, this one highlighted), the station's facts and data-availability table, then one page per chart at report resolution.
-   **Processing algorithms** - the station-list, station-data, raster-grid, delta/change-map, phenology-station-list and phenology-data fetches are all also registered as QGIS Processing algorithms (search "DWD" in the Processing Toolbox), so they're usable from Model Builder, batch mode and `qgis_process`, not just the dock widget.

All data comes live from `opendata.dwd.de` - no API key, no registration.

## Installation

1.  Copy the `dwd_climate_explorer` folder into your QGIS profile's plugins directory: `%APPDATA%\QGIS\QGIS3\profiles\<profile>\python\plugins\` (for the default profile: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`)
2.  In QGIS: **Plugins -\> Manage and Install Plugins -\> Installed**, tick **DWD Climate Explorer**.
3.  Click its toolbar icon (or **Plugins -\> DWD Climate Explorer**) to open the dock widget.

### Dependencies

Needs `requests`, `pandas`, `numpy` and `matplotlib` in QGIS's own Python environment. Recent QGIS installs (via the standalone Windows installer) already bundle all four. If QGIS reports an import error, install the missing package into QGIS's Python via the OSGeo4W shell:

```         
python -m pip install requests pandas numpy matplotlib
```

## Notes / known limitations

-   Network calls run synchronously with a wait cursor - QGIS's UI is briefly unresponsive while a fetch is in flight (a handful of small HTTP requests, typically a few seconds).
-   The Stations tab's per-station tiering is derived directly from DWD's station description files (four products x historical/recent), which is a faithful but independently-computed equivalent of the R app's `rdwd::metaIndex`-based summary - tier counts may differ very slightly at the margins.
-   DWD's server has one file-naming inconsistency: the 1991-2020 reference period is hyphenated (`1991-2020`) for every raster variable except `air_temperature_mean`, which uses an underscore (`1991_2020`) as of
    2026. The plugin tries the normal form first and falls back automatically.

## Attribution

All meteorological data: **Deutscher Wetterdienst** (opendata.dwd.de), published under GeoNutzV. Geocoding: OpenStreetMap Nominatim.

## License

GPL-2.0-or-later (see `LICENSE`) - required for QGIS plugins since they link against QGIS's own GPL-licensed Python bindings.

## Links

-   Repository / issue tracker: https://github.com/franz-geoeco/climate_DWD_Shiny
-   Author: Franz Schulze (franz.schulze@geo.uni-halle.de)