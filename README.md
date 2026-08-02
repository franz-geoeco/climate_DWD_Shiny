# German Climate Dashboard

An interactive R Shiny dashboard for exploring historical weather data from the **Deutscher Wetterdienst (DWD)** – Germany's national meteorological service.

![R](https://img.shields.io/badge/R-%3E%3D4.0-blue) ![Shiny](https://img.shields.io/badge/Shiny-dashboard-brightgreen) ![License](https://img.shields.io/badge/license-MIT-lightgrey) ![Data](https://img.shields.io/badge/data-DWD%20Open%20Data-orange)

---

## Features

| Feature | Description |
|---|---|
| 🗺️ **Station Map** | Interactive Leaflet map showing all DWD stations across Germany. Click any marker to select it. |
| 🌡️ **Climate Data** | Daily temperature and precipitation time-series for any station, any year range. |
| 💨 **Wind Data** | Hourly wind speed and direction aggregated to daily means. |
| 📊 **Climate Chart** | Classic Walter-Lieth climate diagram (temperature + precipitation by month) for a custom 30-year normal period. |
| 📥 **CSV Export** | Download the displayed climate or wind data as a CSV file. |
| 📍 **Flexible location input** | Search by city name (geocoded via OpenStreetMap), decimal coordinates, or by clicking the station map. |

---

## Installation

### Prerequisites

- **R ≥ 4.0** – [Download R](https://cran.r-project.org/)
- **RStudio** (recommended) – [Download RStudio](https://posit.co/download/rstudio-desktop/)

### Steps

```r
# 1. Clone the repository
# git clone https://github.com/<your-username>/german-climate-dashboard.git

# 2. Open DWD_dashboard.R in RStudio

# 3. The app auto-installs missing packages on first run.
#    To install manually:
install.packages(c("shiny", "shinydashboard", "RCurl",
                   "ggplot2", "leaflet", "tmaptools",
                   "rdwd", "berryFunctions"))

# 4. Run the app
shiny::runApp("DWD_dashboard.R")
```

### Windows note

On Windows the app sets `options(unzip = "internal")` and uses R's built-in `unzip()` to read DWD zip archives. This avoids the common *"exit code 9"* error caused by a missing or mis-configured system `unzip.exe`.

---

## Usage

### Station Map tab

1. Open the **Station Map** tab.
2. Browse or zoom the map – every dot is an active or historical DWD station.
3. Click a marker → a popup shows the station name, ID, elevation and coordinates.
4. Press **"Load data for selected station"** to fetch all data and jump to the Climate tab.

### German Climate tab

You can also specify a location manually:

| Mode | How it works |
|---|---|
| City name | Geocodes the entered name via OpenStreetMap and finds the nearest station within the search radius. |
| Coordinates | Uses decimal degrees (WGS 84) directly. |
| Station map | Uses whichever station you selected on the Station Map tab. |

Press **Plot Climate Data** to fetch and display the data.

---

## Data source & legal notes

### DWD Open Data licence

All meteorological data retrieved by this app comes from the **DWD Open Data Portal**:

> **https://opendata.dwd.de**

DWD publishes its observation data under the **GeoNutzV** (Geodatenzugangsgesetz) – Germany's public-sector open-data licence. Key conditions:

- ✅ Free to use, share and redistribute, including for commercial purposes.
- ✅ No registration or API key required.
- ⚠️ **Attribution required**: You must clearly state `Source: Deutscher Wetterdienst` wherever you display or redistribute the data. The app already adds this attribution to every plot.
- ⚠️ DWD's brand, logo and name may not be used to imply endorsement.

Official licence text (German): https://www.dwd.de/DE/service/copyright/copyright_node.html

### rdwd package licence

The R package **rdwd** (by Berry Boessenkool) is the programmatic interface to the DWD Open Data Portal used by this app. It is released under the **GPL-3** licence. If you distribute modified versions of this app that include `rdwd`, your code must also be GPL-3 compatible.

- rdwd on CRAN: https://cran.r-project.org/package=rdwd
- rdwd source: https://github.com/brry/rdwd

### This app's licence

This dashboard is released under the **MIT Licence** (see `LICENSE` file).  
Attribution for the underlying data source must still be preserved per the DWD terms above.

### Third-party map tiles

The station overview map uses **CartoDB Positron** tiles (light basemap). These are © [CARTO](https://carto.com/attributions), based on © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available under CC BY 3.0. The sidebar mini-map uses standard OpenStreetMap tiles (© OpenStreetMap contributors, ODbL).

---

## Project structure

```
.
├── DWD_dashboard.R   # Shiny app (single-file)
├── README.md
└── LICENSE
```

---

## Contributing

Pull requests are welcome. Please open an issue first to discuss what you would like to change.

---

## Acknowledgements

- [Deutscher Wetterdienst](https://www.dwd.de) for providing free, open access to decades of meteorological observations.
- [Berry Boessenkool](https://github.com/brry) for the `rdwd` and `berryFunctions` packages.
- The R Shiny and Leaflet communities.
