#### Chapter 1: Setup ####

required_packages <- c("shiny", "RCurl", "shinydashboard", "ggplot2",
                       "leaflet", "tmaptools", "rdwd", "berryFunctions",
                       "terra",    # --> for reading and plotting raster data
                       "R.utils",  # --> for decompressing .asc.gz raster files
                       "maps")     # --> for the Germany country outline on raster maps

install_if_missing <- function(packages) {
  for (pkg in packages) {
    if (!require(pkg, character.only = TRUE)) {
      install.packages(pkg)
      library(pkg, character.only = TRUE)
    }
  }
}
install_if_missing(required_packages)

options(download.file.method = "libcurl")
options(unzip = "internal")

# ---------------------------------------------------------------------------
# Robust reader for DWD product zips (avoids shell unzip on Windows).
read_dwd_zip_internal <- function(zipfile) {
  contents  <- utils::unzip(zipfile, list = TRUE)
  data_name <- contents$Name[grepl("^produkt", contents$Name)]
  if (length(data_name) == 0)
    data_name <- contents$Name[grepl("\\.txt$", contents$Name) &
                                 !grepl("Metadaten|Beschreibung", contents$Name)]
  if (length(data_name) == 0) stop("No data file found inside: ", basename(zipfile))
  data_name <- data_name[1]
  exdir <- file.path(tempdir(), paste0("dwd_", as.integer(runif(1, 1, 1e9))))
  dir.create(exdir, showWarnings = FALSE, recursive = TRUE)
  on.exit(unlink(exdir, recursive = TRUE), add = TRUE)
  utils::unzip(zipfile, files = data_name, exdir = exdir)
  df <- utils::read.table(file.path(exdir, data_name),
                          header = TRUE, sep = ";", strip.white = TRUE,
                          na.strings = c("-999", "-999.0"), fill = TRUE,
                          stringsAsFactors = FALSE)
  names(df) <- trimws(names(df))
  if ("MESS_DATUM" %in% names(df)) {
    n   <- nchar(as.character(df$MESS_DATUM[1]))
    fmt <- if (!is.na(n) && n >= 10) "%Y%m%d%H" else "%Y%m%d"
    df$MESS_DATUM <- as.POSIXct(as.character(df$MESS_DATUM), format = fmt, tz = "GMT")
  }
  df
}

read_dwd_combined <- function(files) {
  parts    <- lapply(files, read_dwd_zip_internal)
  if (length(parts) == 1) return(parts[[1]])
  hist_idx <- grep("hist",       files)
  rec_idx  <- grep("akt|recent", files)
  if (length(hist_idx) == 1 && length(rec_idx) == 1) {
    hist <- parts[[hist_idx]]
    rec  <- parts[[rec_idx]]
    if ("MESS_DATUM" %in% names(rec))
      rec <- rec[rec$MESS_DATUM > as.POSIXct("2023-12-31", tz = "GMT"), , drop = FALSE]
    else if ("MESS_DATUM_BEGINN" %in% names(rec))
      rec <- rec[as.numeric(substr(rec$MESS_DATUM_BEGINN, 1, 4)) > 2023, , drop = FALSE]
    common <- intersect(names(hist), names(rec))
    return(rbind(hist[common], rec[common]))
  }
  common <- Reduce(intersect, lapply(parts, names))
  do.call(rbind, lapply(parts, function(d) d[common]))
}

# ---------------------------------------------------------------------------
# Build per-station data-availability summary from metaIndex at startup.
# metaIndex has one row per (station x res x var x per); hasfile tells us
# whether an actual downloadable file exists for that combination.
#
# The three products this app uses:
#   A  daily   / kl    (temperature + precipitation)
#   B  monthly / kl    (climate chart)
#   C  hourly  / wind  (wind speed + direction)
#
# Colour tiers shown on the map:
#   "full"    A + B + C  ->  dark green
#   "climate" A + B      ->  orange
#   "basic"   A only     ->  red
#   "none"    nothing    ->  grey
# ---------------------------------------------------------------------------
data(metaIndex, package = "rdwd")

# DWD dates are stored as integers like 19610101 -> format as "YYYY-MM-DD"
fmt_date <- function(d) {
  s <- as.character(as.integer(d))
  ifelse(!is.na(s) & nchar(s) == 8,
         paste0(substr(s,1,4), "-", substr(s,5,6), "-", substr(s,7,8)),
         NA_character_)
}

# Derive one-row-per-station summary cleanly using tapply, avoiding
# aggregate(cbind(...)) which returns matrix-columns and causes length mismatches.
build_product_summary <- function(res_val, var_val, suffix) {
  sub <- metaIndex[metaIndex$hasfile == TRUE &
                     metaIndex$res == res_val &
                     metaIndex$var == var_val, ]
  ids <- unique(sub$Stations_id)
  from_col <- paste0("from_", suffix)
  to_col   <- paste0("to_",   suffix)
  has_col  <- paste0("has_",  suffix)
  
  from_vals <- tapply(sub$von_datum, sub$Stations_id, min,  na.rm = TRUE)
  to_vals   <- tapply(sub$bis_datum, sub$Stations_id, max,  na.rm = TRUE)
  
  df <- data.frame(
    Stations_id  = as.integer(names(from_vals)),
    tmp_from     = fmt_date(from_vals),
    tmp_to       = fmt_date(to_vals),
    tmp_has      = TRUE,
    stringsAsFactors = FALSE,
    row.names    = NULL
  )
  setNames(df, c("Stations_id", from_col, to_col, has_col))
}

sum_daily   <- build_product_summary("daily",   "kl",   "daily_kl")
sum_monthly <- build_product_summary("monthly", "kl",   "monthly_kl")
sum_wind    <- build_product_summary("hourly",  "wind", "hourly_wind")

# One row per station from the full metaIndex (with coordinates)
mi_geo <- metaIndex[!is.na(metaIndex$geoBreite) & !is.na(metaIndex$geoLaenge), ]
geo_cols <- c("Stations_id", "Stationsname", "geoBreite", "geoLaenge",
              "Stationshoehe", "Bundesland")
all_stations <- unique(mi_geo[, geo_cols])
# Ensure truly unique station IDs (take first occurrence)
all_stations <- all_stations[!duplicated(all_stations$Stations_id), ]

# Left-join the three product summaries -- safe because both sides have
# unique Stations_id at this point
all_stations <- merge(all_stations, sum_daily,   by = "Stations_id", all.x = TRUE)
all_stations <- merge(all_stations, sum_monthly, by = "Stations_id", all.x = TRUE)
all_stations <- merge(all_stations, sum_wind,    by = "Stations_id", all.x = TRUE)

# Replace NA in has_* columns with FALSE
all_stations$has_daily_kl    <- !is.na(all_stations$has_daily_kl)
all_stations$has_monthly_kl  <- !is.na(all_stations$has_monthly_kl)
all_stations$has_hourly_wind <- !is.na(all_stations$has_hourly_wind)

# Assign colour tier
all_stations$tier <- with(all_stations, ifelse(
  has_daily_kl & has_monthly_kl & has_hourly_wind, "full",
  ifelse(has_daily_kl & has_monthly_kl,            "climate",
         ifelse(has_daily_kl,                             "basic",
                "none"))))

tier_colour <- c(full = "#2ca02c", climate = "#ff7f0e",
                 basic = "#d62728", none = "#aaaaaa")
tier_label  <- c(full    = "Full data (climate + wind)",
                 climate = "Climate only (no hourly wind)",
                 basic   = "Daily climate only (no monthly / wind)",
                 none    = "No downloadable files")

all_stations$tier[is.na(all_stations$tier)] <- "none"
all_stations$colour    <- unname(tier_colour[all_stations$tier])
all_stations$tier_desc <- unname(tier_label [all_stations$tier])
all_stations$colour[is.na(all_stations$colour)]       <- "#aaaaaa"
all_stations$tier_desc[is.na(all_stations$tier_desc)] <- "Unknown"

# ---------------------------------------------------------------------------
# Phenology (plant development stages) -- a separate DWD network from the
# climate stations above (different station IDs, different geo network),
# split across two "reporter" sub-networks (annual/immediate) and several
# plant categories. Unlike the climate zips, the data files themselves are
# plain uncompressed semicolon-delimited text. Everything here is loaded
# lazily inside the server function (first Phenology-tab visit / genuine
# user interaction), not at app startup like metaIndex/all_stations above.
# ---------------------------------------------------------------------------
pheno_base      <- "https://opendata.dwd.de/climate_environment/CDC/observations_germany/phenology/"
pheno_help_base <- "https://opendata.dwd.de/climate_environment/CDC/help/"

pheno_reporter_types <- list(
  annual    = list(prefix = "Jahresmelder",  station_file = "PH_Beschreibung_Phaenologie_Stationen_Jahresmelder.txt"),
  immediate = list(prefix = "Sofortmelder",  station_file = "PH_Beschreibung_Phaenologie_Stationen_Sofortmelder.txt")
)

# reporter -> category -> list(folder, infix, label). infix = "" means the
# category has no shared infix token (see pheno_dedupe_species_label).
pheno_categories <- list(
  annual = list(
    crops   = list(folder = "annual_reporters/crops",   infix = "Landwirtschaft_Kulturpflanze", label = "Field crops"),
    farming = list(folder = "annual_reporters/farming", infix = "",                              label = "Farming (discontinued ~1990/91)"),
    fruit   = list(folder = "annual_reporters/fruit",   infix = "Obst",                           label = "Fruit"),
    vine    = list(folder = "annual_reporters/vine",    infix = "Weinrebe",                       label = "Vine"),
    wild    = list(folder = "annual_reporters/wild",    infix = "Wildwachsende_Pflanze",          label = "Wild plants")
  ),
  immediate = list(
    crops = list(folder = "immediate_reporters/crops", infix = "Landwirtschaft_Kulturpflanze", label = "Field crops"),
    fruit = list(folder = "immediate_reporters/fruit", infix = "Obst",                          label = "Fruit"),
    wild  = list(folder = "immediate_reporters/wild",  infix = "Wildwachsende_Pflanze",          label = "Wild plants")
  )
)

pheno_category_choices <- function(reporter) {
  cats <- pheno_categories[[reporter]]
  setNames(names(cats), vapply(cats, function(x) x$label, character(1)))
}

pheno_dedupe_species_label <- function(token) {
  # DWD sometimes bakes the category name into the species token itself
  # (farming's 'Feldarbeit_Feldarbeit', vine's 'Weinrebe_fruehe_Reife' after
  # only the leading 'Weinrebe_' infix is stripped -- in vine's case the
  # repeat is a legitimate variety name). Only collapse an *exact* repeated
  # leading token, so farming's literal duplication is cleaned up without
  # touching vine's real variety names.
  parts <- strsplit(token, "_")[[1]]
  if (length(parts) >= 2 && parts[1] == parts[2]) parts <- parts[-1]
  paste(parts, collapse = "_")
}

pheno_pretty_species_label <- function(token) {
  trimws(gsub("[_-]", " ", pheno_dedupe_species_label(token)))
}

# Scans the historical/ and recent/ directory listings of one
# reporter/category and returns a data.frame(token, label, hist_file,
# recent_file) -- same readLines(url())+regex directory-scan technique
# already used by find_klindex_url() above.
list_phenology_species <- function(reporter, category) {
  rep_spec <- pheno_reporter_types[[reporter]]
  cat_spec <- pheno_categories[[reporter]][[category]]
  prefix   <- rep_spec$prefix
  infix    <- cat_spec$infix
  infix_part <- if (nzchar(infix)) paste0(infix, "_") else ""

  hist_pattern   <- paste0("PH_", prefix, "_", infix_part, "(.+?)_(\\d{4})_(\\d{4})_hist\\.txt")
  recent_pattern <- paste0("PH_", prefix, "_", infix_part, "(.+?)_akt\\.txt")

  fetch_listing <- function(sub) {
    url_str <- paste0(pheno_base, cat_spec$folder, "/", sub, "/")
    tryCatch({
      con <- url(url_str)
      on.exit(close(con), add = TRUE)
      paste(readLines(con, warn = FALSE), collapse = "\n")
    }, error = function(e) "")
  }

  hist_txt   <- fetch_listing("historical")
  recent_txt <- fetch_listing("recent")

  hist_end    <- list()  # token -> end year (int), keeps the longest hist file
  hist_file   <- list()  # token -> filename
  recent_file <- list()  # token -> filename

  if (nzchar(hist_txt)) {
    hits <- regmatches(hist_txt, gregexpr(hist_pattern, hist_txt, perl = TRUE))[[1]]
    for (fname in hits) {
      mm <- regmatches(fname, regexec(hist_pattern, fname, perl = TRUE))[[1]]
      token <- mm[2]; end_yr <- as.integer(mm[4])
      if (is.null(hist_end[[token]]) || end_yr > hist_end[[token]]) {
        hist_end[[token]]  <- end_yr
        hist_file[[token]] <- fname
      }
    }
  }
  if (nzchar(recent_txt)) {
    hits <- regmatches(recent_txt, gregexpr(recent_pattern, recent_txt, perl = TRUE))[[1]]
    for (fname in hits) {
      mm <- regmatches(fname, regexec(recent_pattern, fname, perl = TRUE))[[1]]
      recent_file[[mm[2]]] <- fname
    }
  }

  tokens <- union(names(hist_file), names(recent_file))
  if (length(tokens) == 0) {
    return(data.frame(token = character(0), label = character(0),
                       hist_file = character(0), recent_file = character(0),
                       stringsAsFactors = FALSE))
  }
  labels <- vapply(tokens, pheno_pretty_species_label, character(1))
  out <- data.frame(
    token       = tokens,
    label       = labels,
    hist_file   = vapply(tokens, function(t) if (is.null(hist_file[[t]]))   NA_character_ else hist_file[[t]],   character(1)),
    recent_file = vapply(tokens, function(t) if (is.null(recent_file[[t]])) NA_character_ else recent_file[[t]], character(1)),
    stringsAsFactors = FALSE
  )
  out[order(tolower(out$label)), ]
}

# Downloads to a temp file (same robustness philosophy as
# read_dwd_zip_internal above -- avoids in-memory URL-connection quirks on
# Windows), then auto-detects UTF-8 vs. latin-1 before parsing.
read_pheno_semicolon <- function(url_str) {
  tmp <- tempfile(fileext = ".txt")
  on.exit(unlink(tmp), add = TRUE)
  utils::download.file(url_str, tmp, mode = "wb", quiet = TRUE)
  raw_bytes <- readBin(tmp, "raw", n = file.info(tmp)$size)
  txt <- rawToChar(raw_bytes)
  Encoding(txt) <- "unknown"
  if (is.na(iconv(txt, from = "UTF-8", to = "UTF-8", sub = NA))) {
    txt <- iconv(txt, from = "latin1", to = "UTF-8")
  } else {
    Encoding(txt) <- "UTF-8"
  }
  df <- read.csv(text = txt, sep = ";", stringsAsFactors = FALSE, strip.white = TRUE,
                  check.names = FALSE)
  names(df) <- trimws(names(df))
  keep <- names(df)[nzchar(names(df)) & names(df) != "eor" & !grepl("^X(\\.\\d+)?$", names(df))]
  df <- df[, keep, drop = FALSE]
  for (col in keep) if (is.character(df[[col]])) df[[col]] <- trimws(df[[col]])
  df
}

.pheno_phase_lookup <- NULL
.pheno_plant_lookup <- NULL

fetch_phase_lookup <- function() {
  if (!is.null(.pheno_phase_lookup)) return(.pheno_phase_lookup)
  df <- read_pheno_semicolon(paste0(pheno_help_base, "PH_Beschreibung_Phase.txt"))
  names(df)[names(df) == "Phase_ID"]       <- "Phase_id"
  names(df)[names(df) == "Phase"]          <- "Phase_de"
  names(df)[names(df) == "Phase_englisch"] <- "Phase_en"
  df$Phase_id <- as.integer(df$Phase_id)
  .pheno_phase_lookup <<- df[, c("Phase_id", "Phase_de", "Phase_en")]
  .pheno_phase_lookup
}

fetch_plant_lookup <- function() {
  if (!is.null(.pheno_plant_lookup)) return(.pheno_plant_lookup)
  df <- read_pheno_semicolon(paste0(pheno_help_base, "PH_Beschreibung_Pflanze.txt"))
  names(df)[names(df) == "Objekt_ID"]       <- "Objekt_id"
  names(df)[names(df) == "Objekt"]          <- "Objekt_de"
  names(df)[names(df) == "Objekt_englisch"] <- "Objekt_en"
  names(df)[names(df) == "Objekt_latein"]   <- "Objekt_latin"
  df$Objekt_id <- as.integer(df$Objekt_id)
  .pheno_plant_lookup <<- df[, c("Objekt_id", "Objekt_de", "Objekt_en", "Objekt_latin")]
  .pheno_plant_lookup
}

fetch_phenology_stations <- function(reporter) {
  spec <- pheno_reporter_types[[reporter]]
  df <- read_pheno_semicolon(paste0(pheno_help_base, spec$station_file))
  names(df)[names(df) == "geograph.Breite"] <- "geoBreite"
  names(df)[names(df) == "geograph.Laenge"] <- "geoLaenge"
  df$Stations_id   <- suppressWarnings(as.integer(df$Stations_id))
  df$geoBreite     <- suppressWarnings(as.numeric(df$geoBreite))
  df$geoLaenge     <- suppressWarnings(as.numeric(df$geoLaenge))
  df$Stationshoehe <- suppressWarnings(as.numeric(df$Stationshoehe))
  df <- df[!is.na(df$Stations_id) & !is.na(df$geoBreite) & !is.na(df$geoLaenge), ]
  keep <- intersect(c("Stations_id", "Stationsname", "geoBreite", "geoLaenge", "Stationshoehe", "Bundesland"), names(df))
  df[, keep, drop = FALSE]
}

pheno_haversine_km <- function(lat1, lon1, lat2, lon2) {
  r <- 6371.0
  p1 <- lat1 * pi / 180; p2 <- lat2 * pi / 180
  dphi <- (lat2 - lat1) * pi / 180
  dlambda <- (lon2 - lon1) * pi / 180
  a <- sin(dphi / 2)^2 + cos(p1) * cos(p2) * sin(dlambda / 2)^2
  2 * r * asin(sqrt(a))
}

pheno_nearby_stations <- function(stations_df, lat, lon, radius_km) {
  out <- stations_df
  out$dist_km <- pheno_haversine_km(lat, lon, out$geoBreite, out$geoLaenge)
  out <- out[out$dist_km <= radius_km, ]
  out[order(out$dist_km), ]
}

# Downloads + combines the historical and recent records for one species,
# filters to one station, and joins the phase/plant name lookups (via the
# Phase_id/Objekt_id columns actually present in the data -- not guessed
# from the filename). Overlapping (year, phase) rows keep the *recent*
# file's value, mirroring read_dwd_combined()'s historical/recent policy.
fetch_phenology_species_data <- function(reporter, category, species_row, station_id) {
  cat_spec <- pheno_categories[[reporter]][[category]]
  folder   <- cat_spec$folder
  parts <- list()
  if (!is.na(species_row$hist_file)) {
    df <- read_pheno_semicolon(paste0(pheno_base, folder, "/historical/", species_row$hist_file))
    df$X_recent <- FALSE
    parts[[length(parts) + 1]] <- df
  }
  if (!is.na(species_row$recent_file)) {
    df <- read_pheno_semicolon(paste0(pheno_base, folder, "/recent/", species_row$recent_file))
    df$X_recent <- TRUE
    parts[[length(parts) + 1]] <- df
  }
  if (length(parts) == 0) return(NULL)

  common   <- Reduce(intersect, lapply(parts, names))
  combined <- do.call(rbind, lapply(parts, function(d) d[common]))

  for (col in c("Stations_id", "Referenzjahr", "Objekt_id", "Phase_id", "Jultag")) {
    if (col %in% names(combined)) combined[[col]] <- suppressWarnings(as.numeric(combined[[col]]))
  }
  combined <- combined[!is.na(combined$Stations_id) & !is.na(combined$Referenzjahr) &
                          !is.na(combined$Phase_id) & !is.na(combined$Objekt_id), ]
  combined$Stations_id  <- as.integer(combined$Stations_id)
  combined$Referenzjahr <- as.integer(combined$Referenzjahr)
  combined$Phase_id     <- as.integer(combined$Phase_id)
  combined$Objekt_id    <- as.integer(combined$Objekt_id)

  # Keep the *recent*-sourced row on (station, year, phase) overlap: sort
  # historical (FALSE) before recent (TRUE), then drop_duplicates-keep-last.
  combined <- combined[order(combined$X_recent), ]
  key <- paste(combined$Stations_id, combined$Referenzjahr, combined$Phase_id, sep = "|")
  combined <- combined[!duplicated(key, fromLast = TRUE), ]
  combined$X_recent <- NULL

  sub <- combined[combined$Stations_id == station_id, ]
  if (nrow(sub) == 0) return(sub)

  sub <- merge(sub, fetch_phase_lookup(), by = "Phase_id", all.x = TRUE, sort = FALSE)
  sub <- merge(sub, fetch_plant_lookup(), by = "Objekt_id", all.x = TRUE, sort = FALSE)
  sub[order(sub$Referenzjahr, sub$Phase_id), ]
}

#### Chapter 2: User Interface (UI) ####

ui <- dashboardPage(
  dashboardHeader(title = "German Climate Dashboard"),
  
  dashboardSidebar(
    sidebarMenu(
      menuItem("Station Map",    tabName = "station_map_tab", icon = icon("map")),
      menuItem("German Climate", tabName = "climate",         icon = icon("cloud")),
      menuItem("Raster Maps",    tabName = "raster_tab",       icon = icon("th")),
      menuItem("Phenology",      tabName = "phenology",       icon = icon("seedling"))
    ),
    br(), br(), br(), br(), br(), br(), br(), br(), br(), br(),
    leafletOutput("station_map_sidebar", height = 200)
  ),
  
  dashboardBody(
    tabItems(
      
      # -- Tab 1: interactive DWD station overview map ------------------------
      tabItem(tabName = "station_map_tab",
              fluidRow(
                box(
                  title = "All DWD Weather Stations - click a marker to select it",
                  status = "primary", solidHeader = TRUE, width = 12,
                  
                  # Legend
                  tags$div(
                    style = "margin-bottom:10px; font-size:13px;",
                    tags$b("Data availability:"), "  ",
                    tags$span(style = "color:#2ca02c; font-weight:bold;", "* Full"),
                    " (daily climate + monthly climate chart + hourly wind)   ",
                    tags$span(style = "color:#ff7f0e; font-weight:bold;", "* Climate"),
                    " (daily + monthly, no hourly wind)   ",
                    tags$span(style = "color:#d62728; font-weight:bold;", "* Basic"),
                    " (daily only, no monthly / wind)   ",
                    tags$span(style = "color:#aaaaaa; font-weight:bold;", "* None"),
                    " (no files)"
                  ),
                  
                  leafletOutput("all_stations_map", height = 600),
                  br(),
                  uiOutput("selected_station_info"),
                  br(),
                  uiOutput("load_station_btn")
                )
              )
      ),
      
      # -- Tab 2: climate plots -----------------------------------------------
      tabItem(tabName = "climate",
              fluidRow(
                box(title = "German Climate Data", status = "primary", solidHeader = TRUE, width = 12,
                    radioButtons("location_mode", "Specify location by:",
                                 choices = c("City name"   = "city",
                                             "Coordinates" = "coords",
                                             "Station map" = "map_pick"),
                                 selected = "city", inline = TRUE),
                    conditionalPanel(condition = "input.location_mode == 'city'",
                                     textInput("climate_city", "Enter German city name:")),
                    conditionalPanel(condition = "input.location_mode == 'coords'",
                                     numericInput("climate_lat", "Latitude (degN):",  value = 51.34, min = 47, max = 56, step = 0.0001),
                                     numericInput("climate_lon", "Longitude (degE):", value = 12.38, min = 5,  max = 16, step = 0.0001)),
                    conditionalPanel(condition = "input.location_mode == 'map_pick'",
                                     uiOutput("map_pick_info")),
                    numericInput("search_radius", "Search radius (km):", value = 20, min = 1, max = 100),
                    actionButton("plot_climate", "Plot Climate Data"),
                    radioButtons("climate_var", "Select climate variable:",
                                 choices = c("Temperature" = "TMK", "Precipitation" = "RSK"),
                                 selected = "TMK", inline = TRUE),
                    sliderInput("date_range", "Select the range:",
                                min = 1900, max = 2024, value = c(2020, 2024), step = 1, sep = ""),
                    plotOutput("climate_plot", height = 400),
                    downloadButton("download_climate", "Download climate data (CSV)")
                )
              ),
              fluidRow(
                box(title = "Wind Speed and Direction", status = "info", solidHeader = TRUE, width = 12,
                    radioButtons("wind_var", "Select wind variable:",
                                 choices = c("Wind Speed" = "F", "Wind Direction" = "D"),
                                 selected = "F", inline = TRUE),
                    sliderInput("wind_date_range", "Select the range:",
                                min = 1900, max = 2024, value = c(2020, 2024), step = 1, sep = ""),
                    plotOutput("wind_plot", height = 400),
                    downloadButton("download_wind", "Download wind data (CSV)")
                )
              ),
              fluidRow(
                box(title = "30 year Climate Chart", status = "primary", solidHeader = TRUE, width = 12,
                    sliderInput("year_range", "Select the range:",
                                min = 1900, max = 2023, value = c(1970, 2000), step = 1, sep = ""),
                    plotOutput("climate_chart", height = 400)
                )
              ),
              fluidRow(
                box(title = "Temperature Trend & Anomaly", status = "warning", solidHeader = TRUE, width = 12,
                    tags$p(style = "color:#888; font-size:13px;",
                           "Annual mean temperatures with a linear trend line and anomaly shading relative to the 1961-1990 baseline. Uses the full daily record of the loaded station."),
                    sliderInput("trend_year_range", "Year range:",
                                min = 1900, max = 2024, value = c(1960, 2024), step = 1, sep = ""),
                    plotOutput("trend_plot", height = 420)
                )
              ),
              fluidRow(
                box(title = "Annual Climate Indices", status = "success", solidHeader = TRUE, width = 12,
                    tags$p(style = "color:#888; font-size:13px;",
                           "Pre-computed yearly counts from DWD. Not all stations provide all indices."),
                    sliderInput("indices_year_range", "Year range:",
                                min = 1900, max = 2024, value = c(1960, 2024), step = 1, sep = ""),
                    radioButtons("indices_var", "Index:",
                                 choices = c(
                                   "Frost days (Tmin < 0\u00b0C)"           = "JA_FROSTTAGE",
                                   "Ice days (Tmax < 0\u00b0C)"             = "JA_EISTAGE",
                                   "Hot days (Tmax \u2265 30\u00b0C)"       = "JA_HEISSE_TAGE",
                                   "Summer days (Tmax \u2265 25\u00b0C)"    = "JA_SOMMERTAGE",
                                   "Tropical nights (Tmin \u2265 20\u00b0C)"= "JA_TROPENNAECHTE"
                                 ),
                                 selected = "JA_FROSTTAGE", inline = FALSE),
                    plotOutput("indices_plot", height = 380),
                    uiOutput("indices_unavailable")
                )
              )
      ),
      
      # -- Tab 3: Germany-wide interpolated raster maps -------------------------------
      tabItem(tabName = "raster_tab",
              fluidRow(
                box(title = "Germany-wide Climate Raster Maps (1 km x 1 km interpolated grids)",
                    status = "primary", solidHeader = TRUE, width = 12,
                    tags$p(style = "color:#666; font-size:13px;",
                           "DWD interpolates station observations onto a 1 km x 1 km grid covering all of Germany. ",
                           "Files are ~0.2 MB each. Data goes back to 1881 depending on variable."),
                    fluidRow(
                      column(4,
                             selectInput("raster_var", "Variable:",
                                         choices = c(
                                           "Mean temperature (deg C)"       = "air_temperature_mean",
                                           "Max. temperature (deg C)"       = "air_temperature_max",
                                           "Min. temperature (deg C)"       = "air_temperature_min",
                                           "Precipitation (mm)"             = "precipitation",
                                           "Sunshine duration (h)"          = "sunshine_duration",
                                           "Frost days (annual)"            = "frost_days",
                                           "Ice days (annual)"              = "ice_days",
                                           "Hot days >= 30 deg C (annual)"  = "hot_days",
                                           "Summer days >= 25 deg C (ann.)" = "summer_days",
                                           "Snow cover days (annual)"       = "snowcover_days"
                                         ), selected = "air_temperature_mean")
                      ),
                      column(4,
                             selectInput("raster_period", "Period:",
                                         choices = c(
                                           "January" = "01", "February" = "02", "March" = "03",
                                           "April"   = "04", "May"       = "05", "June"  = "06",
                                           "July"    = "07", "August"    = "08", "September" = "09",
                                           "October" = "10", "November"  = "11", "December"  = "12",
                                           "Spring (Mar-May)" = "13",
                                           "Summer (Jun-Aug)" = "14",
                                           "Autumn (Sep-Nov)" = "15",
                                           "Winter (Dec-Feb)" = "16",
                                           "Full year"        = "17"
                                         ), selected = "17")
                      ),
                      column(4,
                             # Year range: multi-annual reference periods DWD provides
                             selectInput("raster_year", "Reference period (multi-annual mean):",
                                         choices = c(
                                           "1961-1990" = "6190",
                                           "1971-2000" = "7100",
                                           "1981-2010" = "8110",
                                           "1991-2020" = "9120"
                                         ), selected = "9120")
                      )
                    ),
                    actionButton("load_raster", "Load raster map", icon = icon("download"),
                                 class = "btn-primary"),
                    br(), br(),
                    uiOutput("raster_status"),
                    plotOutput("raster_plot", height = 600)
                )
              )
      ),

      # -- Tab 4: Phenology (plant development stages) -------------------------
      tabItem(tabName = "phenology",
              fluidRow(
                box(title = "Phenology - Plant Development Stages", status = "primary", solidHeader = TRUE, width = 12,
                    tags$p(style = "color:#666; font-size:13px;",
                           "Plant development-stage observations (sowing, flowering, ripening, leaf colouring, ...) ",
                           "from DWD's phenology network -- a separate set of stations from the climate network on the other tabs."),
                    fluidRow(
                      column(4, radioButtons("pheno_reporter", "Reporter type:",
                                              choices = c("Annual reporters (Jahresmelder)" = "annual",
                                                          "Immediate reporters (Sofortmelder)" = "immediate"),
                                              selected = "annual")),
                      column(4, selectInput("pheno_category", "Category:", choices = character(0))),
                      column(4, selectInput("pheno_species", "Species:", choices = character(0)))
                    ),
                    radioButtons("pheno_location_mode", "Specify station by:",
                                 choices = c("City name"   = "city",
                                             "Coordinates" = "coords",
                                             "Station map" = "map_pick"),
                                 selected = "city", inline = TRUE),
                    conditionalPanel(condition = "input.pheno_location_mode == 'city'",
                                     textInput("pheno_city", "Enter German city name:")),
                    conditionalPanel(condition = "input.pheno_location_mode == 'coords'",
                                     numericInput("pheno_lat", "Latitude (degN):",  value = 51.34, min = 47, max = 56, step = 0.0001),
                                     numericInput("pheno_lon", "Longitude (degE):", value = 12.38, min = 5,  max = 16, step = 0.0001)),
                    conditionalPanel(condition = "input.pheno_location_mode == 'map_pick'",
                                     uiOutput("pheno_map_pick_info")),
                    numericInput("pheno_search_radius", "Search radius (km):", value = 30, min = 1, max = 300),
                    actionButton("pheno_fetch", "Fetch phenology data", icon = icon("download"), class = "btn-primary"),
                    uiOutput("pheno_status")
                )
              ),
              fluidRow(
                box(title = "Phenology Station Map - click a marker to select it", status = "info", solidHeader = TRUE, width = 12,
                    leafletOutput("pheno_stations_map", height = 400)
                )
              ),
              fluidRow(
                box(title = "Phenological Phase Trend", status = "success", solidHeader = TRUE, width = 12,
                    tags$p(style = "color:#888; font-size:13px;",
                           "Entry day-of-year for the selected phase across the station's full record, with a linear trend line -- ",
                           "a negative slope means this phase is happening earlier over time, the standard phenology climate-change signal."),
                    uiOutput("pheno_phase_ui"),
                    sliderInput("pheno_year_range", "Year range:",
                                min = 1900, max = 2024, value = c(1960, 2024), step = 1, sep = ""),
                    plotOutput("pheno_plot", height = 420),
                    downloadButton("download_pheno", "Download phenology data (CSV)")
                )
              )
      )
    )
  )
)


#### Chapter 3: Server Logic ####

server <- function(input, output, session) {
  
  climate_data     <- reactiveVal(NULL)
  selected_station <- reactiveVal(NULL)
  
  # -- 30-year slider constraint ----------------------------------------------
  observeEvent(input$year_range, {
    s <- input$year_range[1]; e <- input$year_range[2]
    if (e != s + 29) updateSliderInput(session, "year_range", value = c(s, s + 29))
    if (s != e - 29) updateSliderInput(session, "year_range", value = c(e - 29, e))
  })
  
  # -- Helper: build popup HTML for one station (all args are plain scalars) --
  make_popup <- function(name, id, lat, lon, ele, state,
                         tier, colour,
                         has_daily,   from_daily,   to_daily,
                         has_monthly, from_monthly, to_monthly,
                         has_wind,    from_wind,    to_wind) {
    
    tier_safe <- if (is.na(tier)) "none" else tier
    badge_col <- switch(tier_safe,
                        full    = "#2ca02c", climate = "#ff7f0e",
                        basic   = "#d62728", "#aaaaaa")
    badge_txt <- switch(tier_safe,
                        full    = "Full",
                        climate = "Climate only (no wind)",
                        basic   = "Basic (daily only)",
                        "No files")
    tier_badge <- paste0("<span style='color:", badge_col,
                         ";font-weight:bold;'>* ", badge_txt, "</span>")
    
    row <- function(ok, label, from, to) {
      if (isTRUE(ok) && !is.na(from))
        paste0("<b>", label, ":</b> ", from, " - ", to, "<br>")
      else
        paste0("<b>", label, ":</b> <i>not available</i><br>")
    }
    
    paste0(
      "<b style='font-size:14px;'>", name, "</b><br>",
      "ID: ", id, "  |  ",
      round(lat, 4), "degN  ", round(lon, 4), "degE  |  ", ele, " m a.s.l.<br>",
      "State: ", state, "<br>",
      "Data: ", tier_badge,
      "<hr style='margin:4px 0;'>",
      row(has_daily,   "Daily climate (kl)",         from_daily,   to_daily),
      row(has_monthly, "Monthly climate chart",       from_monthly, to_monthly),
      row(has_wind,    "Hourly wind speed/direction", from_wind,    to_wind)
    )
  }
  
  # Pre-compute popup strings once -- avoids formula-scope issues in leaflet
  station_popups <- with(all_stations,
                         mapply(make_popup,
                                name        = Stationsname,
                                id          = Stations_id,
                                lat         = geoBreite,
                                lon         = geoLaenge,
                                ele         = Stationshoehe,
                                state       = Bundesland,
                                tier        = tier,
                                colour      = colour,
                                has_daily   = has_daily_kl,
                                from_daily  = from_daily_kl,
                                to_daily    = to_daily_kl,
                                has_monthly = has_monthly_kl,
                                from_monthly= from_monthly_kl,
                                to_monthly  = to_monthly_kl,
                                has_wind    = has_hourly_wind,
                                from_wind   = from_hourly_wind,
                                to_wind     = to_hourly_wind,
                                SIMPLIFY    = TRUE, USE.NAMES = FALSE
                         )
  )
  
  # -- Full station overview map ----------------------------------------------
  output$all_stations_map <- renderLeaflet({
    leaflet(all_stations) %>%
      addProviderTiles(providers$CartoDB.Positron) %>%
      setView(lng = 10.4, lat = 51.2, zoom = 6) %>%
      addCircleMarkers(
        lng         = ~geoLaenge,
        lat         = ~geoBreite,
        layerId     = ~Stations_id,
        radius      = 5,
        color       = ~colour,
        fillColor   = ~colour,
        fillOpacity = 0.75,
        stroke      = TRUE,
        weight      = 1,
        popup       = station_popups,
        label       = ~paste0(Stationsname, " [", tier_desc, "]"),
        labelOptions = labelOptions(direction = "top", textsize = "12px")
      )
  })
  
  # -- React to marker clicks -------------------------------------------------
  observeEvent(input$all_stations_map_marker_click, {
    click <- input$all_stations_map_marker_click
    req(click$id)
    st <- all_stations[all_stations$Stations_id == click$id, ]
    selected_station(st)
    
    # Highlight selected marker with a larger white-rimmed circle
    leafletProxy("all_stations_map") %>%
      clearGroup("highlight") %>%
      addCircleMarkers(
        lng = st$geoLaenge, lat = st$geoBreite,
        group = "highlight",
        radius = 11, color = "white", fillColor = st$colour,
        fillOpacity = 1, stroke = TRUE, weight = 3
      )
    
  })
  
  # -- Detailed info panel below map -----------------------------------------
  output$selected_station_info <- renderUI({
    st <- selected_station()
    if (is.null(st)) {
      return(tags$p(style = "color:#888;",
                    icon("info-circle"),
                    " Click a station marker to see details and select it."))
    }
    
    # Build availability rows with icons and exact date ranges
    avail_row <- function(ok, label, from_col, to_col) {
      if (isTRUE(ok) && !is.na(st[[from_col]])) {
        tags$tr(
          tags$td(tags$span(style = "color:#2ca02c; font-size:16px;", "&#10004;"), " ", label),
          tags$td(style = "padding-left:16px; color:#555;",
                  st[[from_col]], " - ", st[[to_col]])
        )
      } else {
        tags$tr(
          tags$td(tags$span(style = "color:#d62728; font-size:16px;", "&#10008;"), " ", label),
          tags$td(style = "padding-left:16px; color:#aaa;", "not available")
        )
      }
    }
    
    safe_tier <- if (is.na(st$tier)) "none" else st$tier
    tier_col  <- unname(tier_colour[safe_tier])
    tier_txt  <- unname(tier_label [safe_tier])
    
    tags$div(
      class = "panel panel-default",
      style = paste0("border-left: 5px solid ", tier_col, "; padding: 10px 14px;"),
      
      tags$h4(style = "margin-top:0;", st$Stationsname,
              tags$small(style = "color:#666;", paste0("  (ID ", st$Stations_id, ")"))),
      
      tags$p(
        tags$b("State: "), st$Bundesland, "  |  ",
        tags$b("Elevation: "), st$Stationshoehe, " m a.s.l.  |  ",
        tags$b("Coords: "), round(st$geoBreite,4), "degN  ", round(st$geoLaenge,4), "degE"
      ),
      
      tags$p(tags$b("Overall data tier: "),
             tags$span(style = paste0("color:", tier_col, "; font-weight:bold;"), tier_txt)),
      
      tags$table(
        style = "width:100%; font-size:13px;",
        tags$thead(tags$tr(
          tags$th("Product"),
          tags$th(style = "padding-left:16px;", "Available date range")
        )),
        tags$tbody(
          avail_row(st$has_daily_kl,    "Daily climate (temperature / precipitation)",
                    "from_daily_kl",    "to_daily_kl"),
          avail_row(st$has_monthly_kl,  "Monthly climate (for 30-year climate chart)",
                    "from_monthly_kl",  "to_monthly_kl"),
          avail_row(st$has_hourly_wind, "Hourly wind speed & direction",
                    "from_hourly_wind", "to_hourly_wind")
        )
      )
    )
  })
  
  # -- Load station button (shown only when a station is selected) -------------
  output$load_station_btn <- renderUI({
    if (is.null(selected_station())) return(NULL)
    actionButton("load_selected_station",
                 "Load data for selected station",
                 icon  = icon("download"),
                 class = "btn-success")
  })
  
  # -- Map-pick panel on Climate tab -----------------------------------------
  output$map_pick_info <- renderUI({
    st <- selected_station()
    if (is.null(st))
      return(tags$p(style = "color:#888;",
                    icon("info-circle"),
                    " Go to 'Station Map', click a station, then press 'Load data for selected station'."))
    tags$div(class = "alert alert-success",
             tags$b("Using station: "), st$Stationsname, " (ID ", st$Stations_id, ")")
  })
  
  # -- "Load data" button -----------------------------------------------------
  observeEvent(input$load_selected_station, {
    st <- selected_station()
    req(st)
    updateRadioButtons(session, "location_mode", selected = "map_pick")
    updateTabItems(session, "tabs", selected = "climate")
    fetch_climate_data(station_row = st)
  })
  
  # -- Central data-fetching function ----------------------------------------
  fetch_climate_data <- function(station_row = NULL) {
    withProgress(message = "Finding weather stations...", value = 0, {
      tryCatch({
        if (!is.null(station_row)) {
          nearest_station <- station_row
        } else {
          if (input$location_mode == "city") {
            req(input$climate_city)
            coords <- tmaptools::geocode_OSM(input$climate_city)
            if (is.null(coords)) {
              showNotification("Could not geocode city name.", type = "error"); return()
            }
            search_lat <- coords$coords[2]; search_lon <- coords$coords[1]
          } else {
            req(input$climate_lat, input$climate_lon)
            search_lat <- input$climate_lat; search_lon <- input$climate_lon
            if (is.na(search_lat) || is.na(search_lon)) {
              showNotification("Enter valid coordinates.", type = "error"); return()
            }
          }
          nearby <- rdwd::nearbyStations(lat = search_lat, lon = search_lon,
                                         radius = input$search_radius,
                                         res = "daily", var = "kl", per = "recent")
          if (nrow(nearby) == 0) {
            showNotification("No stations within radius.", type = "warning"); return()
          }
          nearest_station <- if (is.na(nearby$Stations_id[1])) nearby[2, ] else nearby[1, ]
        }
        
        incProgress(0.4, detail = paste("Fetching data for", nearest_station$Stationsname))
        
        link <- rdwd::selectDWD(id = nearest_station$Stations_id,
                                res = "daily", var = "kl", per = "hr", current = TRUE)
        file <- rdwd::dataDWD(link, hr = 4, read = FALSE, dir = tempdir())
        clim <- read_dwd_combined(file)
        
        link_chart <- rdwd::selectDWD(id = nearest_station$Stations_id,
                                      res = "monthly", var = "kl", per = "h", current = TRUE)
        file_chart  <- rdwd::dataDWD(link_chart, read = FALSE, dir = tempdir())
        clim_chart  <- read_dwd_combined(file_chart)
        
        incProgress(0.3, detail = "Fetching wind data")
        
        wind <- tryCatch({
          link_wind <- rdwd::selectDWD(id = nearest_station$Stations_id,
                                       res = "hourly", var = "wind", per = "hr", current = TRUE)
          file_wind <- rdwd::dataDWD(link_wind, hr = 4, read = FALSE, dir = tempdir())
          read_dwd_combined(file_wind)
        }, error = function(e) NULL)   # no wind data -> silently return NULL
        
        incProgress(0.1, detail = "Fetching annual climate indices")
        
        # Annual climate indices -- not in rdwd's fileIndex, so build the URL directly.
        # Both historical and recent folders exist; try recent first (smaller file),
        # fall back to historical if recent is missing for this station.
        indices <- tryCatch({
          id_pad   <- formatC(nearest_station$Stations_id, width = 5, flag = "0")
          base_url <- "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/annual/climate_indices/kl"
          
          # Scan the recent folder for a matching filename
          find_klindex_url <- function(per) {
            folder  <- paste0(base_url, "/", per, "/")
            listing <- readLines(url(folder), warn = FALSE)
            close(url(folder))
            pattern <- paste0("jahreswerte_KLINDEX_", id_pad)
            hits    <- grep(pattern, listing, value = TRUE)
            if (length(hits) == 0) return(NULL)
            # Extract href value
            m <- regmatches(hits[1], regexpr('href="([^"]+\\.zip)"', hits[1]))
            if (length(m) == 0) return(NULL)
            fname <- gsub('href="|"', "", m)
            paste0(folder, fname)
          }
          
          zip_url <- find_klindex_url("recent")
          if (is.null(zip_url)) zip_url <- find_klindex_url("historical")
          if (is.null(zip_url)) stop("No KLINDEX file found for station ", id_pad)
          
          # Download to temp file and read
          tmp <- tempfile(fileext = ".zip")
          utils::download.file(zip_url, tmp, mode = "wb", quiet = TRUE)
          read_dwd_zip_internal(tmp)
        }, error = function(e) NULL)
        
        data(metaIndex, package = "rdwd")
        height <- subset(metaIndex, Stations_id == nearest_station$Stations_id)$Stationshoehe[1]
        
        climate_data(list(data = clim, data_chart = clim_chart, data_wind = wind,
                          data_indices = indices,
                          station = nearest_station, height = height))
        selected_station(nearest_station)
        
        # -- Auto-update sliders to match actual data in this station ----------
        # daily climate slider (date_range)
        clim_years <- as.integer(format(clim$MESS_DATUM, "%Y"))
        clim_years <- clim_years[!is.na(clim_years)]
        if (length(clim_years) > 0) {
          yr_min <- min(clim_years)
          yr_max <- max(clim_years)
          new_end   <- yr_max
          new_start <- max(yr_min, yr_max - 4)   # show last 5 years by default
          updateSliderInput(session, "date_range",
                            min = yr_min, max = yr_max,
                            value = c(new_start, new_end))
        }
        
        # wind slider (wind_date_range)
        if (!is.null(wind) && nrow(wind) > 0) {
          wind_years <- as.integer(format(wind$MESS_DATUM, "%Y"))
          wind_years <- wind_years[!is.na(wind_years)]
          if (length(wind_years) > 0) {
            wy_min <- min(wind_years)
            wy_max <- max(wind_years)
            updateSliderInput(session, "wind_date_range",
                              min = wy_min, max = wy_max,
                              value = c(max(wy_min, wy_max - 4), wy_max))
          }
        }
        
        # climate chart slider (year_range) -- must stay a 30-year window
        chart_years <- as.integer(substr(clim_chart$MESS_DATUM_BEGINN, 1, 4))
        chart_years <- chart_years[!is.na(chart_years)]
        if (length(chart_years) > 0) {
          cy_min <- min(chart_years)
          cy_max <- max(chart_years)
          # last full 30-year period ending at cy_max, clamped to available range
          cy_end   <- cy_max
          cy_start <- max(cy_min, cy_end - 29)
          # if range shorter than 30 years, extend end as far as possible
          if (cy_end - cy_start < 29) cy_end <- min(cy_max, cy_start + 29)
          updateSliderInput(session, "year_range",
                            min = cy_min, max = cy_max,
                            value = c(cy_start, cy_end))
        }
        
        # annual indices slider
        if (!is.null(indices) && nrow(indices) > 0) {
          idx_yr_col <- if ("MESS_DATUM_BEGINN" %in% names(indices)) "MESS_DATUM_BEGINN" else "MESS_DATUM"
          idx_years  <- as.integer(substr(as.character(indices[[idx_yr_col]]), 1, 4))
          idx_years  <- idx_years[!is.na(idx_years)]
          if (length(idx_years) > 0) {
            iy_min <- min(idx_years); iy_max <- max(idx_years)
            updateSliderInput(session, "indices_year_range",
                              min = iy_min, max = iy_max,
                              value = c(max(iy_min, iy_max - 29), iy_max))
          }
        }
        
      }, error = function(e) {
        showNotification(paste("Error:", e$message), type = "error")
      })
    })
  }
  
  # -- Plot Climate Data button -----------------------------------------------
  observeEvent(input$plot_climate, {
    if (input$location_mode == "map_pick") {
      req(selected_station())
      fetch_climate_data(station_row = selected_station())
    } else {
      fetch_climate_data()
    }
  })
  
  # -- Climate plot -----------------------------------------------------------
  output$climate_plot <- renderPlot({
    req(climate_data())
    clim    <- climate_data()$data
    station <- climate_data()$station
    height  <- climate_data()$height
    clim$year <- substr(clim$MESS_DATUM, 1, 4)
    clim <- subset(clim, year >= input$date_range[1] & year <= input$date_range[2])
    
    if (input$climate_var == "TMK") {
      plot(clim$MESS_DATUM, clim$TMK, type = "l", col = "darkred",
           xlab = "Date", ylab = "Air Temperature [degC]"); abline(h = 0)
      ttl <- paste("Daily Mean Temperature (degC) from",
                   input$date_range[1], "-", input$date_range[2])
    } else {
      plot(clim$MESS_DATUM, clim$RSK, type = "h", col = "steelblue",
           xlab = "Date", ylab = "Precipitation Sum [mm]")
      ttl <- paste("Daily Precipitation Sum [mm] from",
                   input$date_range[1], "-", input$date_range[2])
    }
    mtext(paste(ttl, station$Stationsname,
                "(", round(station$geoLaenge,2), ",", round(station$geoBreite,2), ")",
                "\n", height, "m.a.s.l."),
          adj = 0, line = 0.5, font = 3, cex = 1.1)
    mtext("- Source: Deutscher Wetterdienst", adj = 0.95, line = 0.5, font = 3, cex = 1.1)
  })
  
  # -- Wind subset ------------------------------------------------------------
  wind_subset <- reactive({
    req(climate_data())
    wind <- climate_data()$data_wind
    if (is.null(wind)) return(NULL)   # propagate NULL; plot handles the message
    wind$year <- substr(wind$MESS_DATUM, 1, 4)
    subset(wind, year >= input$wind_date_range[1] & year <= input$wind_date_range[2])
  })
  
  resolve_wind_col <- function(df, kind) {
    cn  <- names(df)
    hit <- if (kind == "F") grep("^F$|^F\\.|Windgeschwindigkeit", cn, value = TRUE)
    else              grep("^D$|^D\\.|Windrichtung",       cn, value = TRUE)
    if (length(hit) == 0) NA_character_ else hit[1]
  }
  
  # -- Wind plot --------------------------------------------------------------
  output$wind_plot <- renderPlot({
    req(climate_data())
    wind <- wind_subset()
    
    # No wind data available for this station -> show a plain message
    if (is.null(wind)) {
      par(mar = c(0,0,0,0))
      plot.new()
      text(0.5, 0.5, "No hourly wind data available for this station.",
           cex = 1.3, col = "#888888")
      return()
    }
    
    station <- climate_data()$station
    height  <- climate_data()$height
    col     <- resolve_wind_col(wind, input$wind_var)
    
    if (is.na(col)) {
      plot.new()
      text(0.5, 0.5,
           paste0("Wind variable not available (",
                  if (input$wind_var == "F") "Wind Speed" else "Wind Direction", ")"),
           cex = 1.2)
      return()
    }
    wind$day <- as.Date(wind$MESS_DATUM)
    vals <- suppressWarnings(as.numeric(wind[[col]]))
    vals[vals == -999] <- NA
    
    if (input$wind_var == "F") {
      daily <- tapply(vals, wind$day, mean, na.rm = TRUE)
      dd    <- as.Date(names(daily)); ord <- order(dd)
      plot(dd[ord], daily[ord], type = "l", col = "darkblue",
           xlab = "Date", ylab = "Wind Speed [m/s]")
      ttl <- paste("Daily Mean Wind Speed [m/s] from",
                   input$wind_date_range[1], "-", input$wind_date_range[2])
    } else {
      rad <- vals * pi / 180
      u   <- tapply(sin(rad), wind$day, mean, na.rm = TRUE)
      v   <- tapply(cos(rad), wind$day, mean, na.rm = TRUE)
      dir <- (atan2(u, v) * 180 / pi) %% 360
      dd  <- as.Date(names(dir)); ord <- order(dd)
      plot(dd[ord], dir[ord], type = "p", pch = 20, col = "darkgreen",
           ylim = c(0, 360), yaxt = "n", xlab = "Date", ylab = "Wind Direction [deg]")
      axis(2, at = c(0, 90, 180, 270, 360), labels = c("N", "E", "S", "W", "N"))
      ttl <- paste("Daily Mean Wind Direction from",
                   input$wind_date_range[1], "-", input$wind_date_range[2])
    }
    mtext(paste(ttl, station$Stationsname,
                "(", round(station$geoLaenge,2), ",", round(station$geoBreite,2), ")",
                "\n", height, "m.a.s.l."),
          adj = 0, line = 0.5, font = 3, cex = 1.1)
    mtext("- Source: Deutscher Wetterdienst", adj = 0.95, line = 0.5, font = 3, cex = 1.1)
  })
  
  # -- Climate chart ----------------------------------------------------------
  output$climate_chart <- renderPlot({
    req(climate_data())
    dc      <- climate_data()$data_chart
    station <- climate_data()$station
    height  <- climate_data()$height
    dc$month <- substr(dc$MESS_DATUM_BEGINN, 5, 6)
    dc$year  <- substr(dc$MESS_DATUM_BEGINN, 1, 4)
    dc <- subset(dc, year >= input$year_range[1] & year <= input$year_range[2])
    temp <- tapply(dc$MO_TT, dc$month, mean, na.rm = TRUE)
    prec <- tapply(dc$MO_RR, dc$month, mean, na.rm = TRUE)
    berryFunctions::climateGraph(temp, prec,
                                 main = paste(range(dc$year)[1], "-", range(dc$year)[2],
                                              station$Stationsname, "\n",
                                              round(station$geoBreite, 2), "N",
                                              round(station$geoLaenge, 2), "E", "\n",
                                              height, "m.a.s.l."))
    mtext("Source: Deutscher Wetterdienst", adj = 0.01, line = 2.8, font = 3)
  })
  
  # -- Sidebar mini-map -------------------------------------------------------
  output$station_map_sidebar <- renderLeaflet({
    req(climate_data())
    st <- climate_data()$station
    leaflet() %>%
      addTiles() %>%
      setView(lng = st$geoLaenge, lat = st$geoBreite, zoom = 8) %>%
      addMarkers(lng = st$geoLaenge, lat = st$geoBreite,
                 popup = paste("Station:", st$Stationsname))
  })
  
  # -- Download handlers ------------------------------------------------------
  output$download_climate <- downloadHandler(
    filename = function() {
      st <- if (!is.null(climate_data())) climate_data()$station$Stationsname else "station"
      paste0("climate_data_", gsub("[^A-Za-z0-9]", "_", st), "_",
             input$date_range[1], "-", input$date_range[2], ".csv")
    },
    content = function(file) {
      req(climate_data())
      clim <- climate_data()$data
      clim$year <- substr(clim$MESS_DATUM, 1, 4)
      clim <- subset(clim, year >= input$date_range[1] & year <= input$date_range[2])
      write.csv(clim, file, row.names = FALSE, fileEncoding = "UTF-8")
    }
  )
  
  output$download_wind <- downloadHandler(
    filename = function() {
      st <- if (!is.null(climate_data())) climate_data()$station$Stationsname else "station"
      paste0("wind_data_", gsub("[^A-Za-z0-9]", "_", st), "_",
             input$wind_date_range[1], "-", input$wind_date_range[2], ".csv")
    },
    content = function(file) {
      write.csv(wind_subset(), file, row.names = FALSE, fileEncoding = "UTF-8")
    }
  )
  
  # -- Temperature Trend & Anomaly plot ----------------------------------------------------------
  output$trend_plot <- renderPlot({
    req(climate_data())
    clim    <- climate_data()$data
    station <- climate_data()$station
    
    # Annual means from daily data
    clim$year <- as.integer(format(clim$MESS_DATUM, "%Y"))
    ann <- tapply(clim$TMK, clim$year, mean, na.rm = TRUE)
    ann <- ann[!is.na(ann)]
    years <- as.integer(names(ann))
    temps <- as.numeric(ann)
    
    # Filter to slider range
    keep  <- years >= input$trend_year_range[1] & years <= input$trend_year_range[2]
    years <- years[keep]; temps <- temps[keep]
    if (length(years) < 3) {
      plot.new(); text(0.5, 0.5, "Not enough data for trend analysis.", cex = 1.2, col = "#888"); return()
    }
    
    # Baseline 1961-1990 mean (use all available data in that window)
    base_temps <- as.numeric(ann[names(ann) >= "1961" & names(ann) <= "1990"])
    baseline   <- if (length(base_temps) >= 5) mean(base_temps, na.rm = TRUE) else mean(temps, na.rm = TRUE)
    base_label <- if (length(base_temps) >= 5) "1961-1990 baseline" else "period mean"
    
    anomaly <- temps - baseline
    pos_col <- "#d62728"; neg_col <- "#1f77b4"
    
    # Layout: extra top margin for title
    par(mar = c(4, 4.5, 3, 2))
    ylim_pad <- max(abs(anomaly), na.rm = TRUE) * 1.25
    plot(years, anomaly, type = "n",
         ylim = c(-ylim_pad, ylim_pad),
         xlab = "Year", ylab = paste0("Temperature anomaly [degC] vs ", base_label),
         las = 1)
    abline(h = 0, col = "grey60", lwd = 1)
    grid(nx = NA, ny = NULL, col = "grey90", lty = 1)
    
    # Anomaly bars
    for (i in seq_along(years)) {
      col_i <- if (anomaly[i] >= 0) pos_col else neg_col
      lines(c(years[i], years[i]), c(0, anomaly[i]), col = col_i, lwd = 3, lend = 1)
    }
    
    # Linear trend
    fit <- lm(anomaly ~ years)
    trend_x <- range(years)
    trend_y <- coef(fit)[1] + coef(fit)[2] * trend_x
    lines(trend_x, trend_y, col = "black", lwd = 2, lty = 1)
    
    # Trend annotation
    per_decade <- coef(fit)[2] * 10
    trend_txt  <- sprintf("Trend: %+.2f degC / decade", per_decade)
    legend("topleft", legend = c(trend_txt,
                                 paste0("Warm year (above ", base_label, ")"),
                                 paste0("Cool year (below ", base_label, ")")),
           col = c("black", pos_col, neg_col),
           lwd = c(2, 3, 3), bty = "n", cex = 0.9)
    
    title(main = paste("Annual Temperature Anomaly --", station$Stationsname),
          font.main = 1, cex.main = 1.1)
    mtext("Source: Deutscher Wetterdienst", adj = 0.99, line = 0.3, font = 3, cex = 0.9)
  })
  
  # Update trend slider when data loads (mirror date_range bounds)
  observeEvent(climate_data(), {
    req(climate_data())
    clim <- climate_data()$data
    yrs  <- as.integer(format(clim$MESS_DATUM, "%Y"))
    yrs  <- yrs[!is.na(yrs)]
    if (length(yrs) > 0)
      updateSliderInput(session, "trend_year_range",
                        min = min(yrs), max = max(yrs),
                        value = c(min(yrs), max(yrs)))
  })
  
  # -- Annual Climate Indices plot ------------------------------------------------------------------
  output$indices_plot <- renderPlot({
    req(climate_data())
    idx <- climate_data()$data_indices
    
    if (is.null(idx)) {
      par(mar = c(0,0,0,0)); plot.new()
      text(0.5, 0.5, "No annual climate index data available for this station.",
           cex = 1.3, col = "#888888"); return()
    }
    
    var_col <- input$indices_var
    if (!var_col %in% names(idx)) {
      par(mar = c(0,0,0,0)); plot.new()
      text(0.5, 0.5, paste0("Index '", var_col, "' not available for this station."),
           cex = 1.2, col = "#888888"); return()
    }
    
    station <- climate_data()$station
    
    # Annual product uses MESS_DATUM_BEGINN (YYYYMMDD); extract year from first 4 chars
    yr_col <- if ("MESS_DATUM_BEGINN" %in% names(idx)) "MESS_DATUM_BEGINN" else "MESS_DATUM"
    idx$year <- as.integer(substr(as.character(idx[[yr_col]]), 1, 4))
    idx <- subset(idx, !is.na(year) &
                    year >= input$indices_year_range[1] &
                    year <= input$indices_year_range[2])
    
    vals <- suppressWarnings(as.numeric(idx[[var_col]]))
    vals[vals == -999 | vals == -999.0] <- NA
    keep <- !is.na(vals)
    years <- idx$year[keep]; vals <- vals[keep]
    
    if (length(years) == 0) {
      par(mar = c(0,0,0,0)); plot.new()
      text(0.5, 0.5, "No valid values in the selected year range.", cex = 1.2, col = "#888"); return()
    }
    
    # Nice label lookup (keys = actual DWD column names)
    idx_labels <- c(
      JA_FROSTTAGE     = "Frost days per year (Tmin < 0degC)",
      JA_EISTAGE       = "Ice days per year (Tmax < 0degC)",
      JA_HEISSE_TAGE   = "Hot days per year (Tmax >= 30degC)",
      JA_SOMMERTAGE    = "Summer days per year (Tmax >= 25degC)",
      JA_TROPENNAECHTE = "Tropical nights per year (Tmin >= 20degC)"
    )
    y_label <- if (var_col %in% names(idx_labels)) idx_labels[var_col] else var_col
    
    # Colour bars by warm/cold index type
    warm_idx <- c("JA_HEISSE_TAGE", "JA_SOMMERTAGE", "JA_TROPENNAECHTE")
    bar_col  <- if (var_col %in% warm_idx) "#d62728" else "#1f77b4"
    
    par(mar = c(4, 5, 3, 2))
    bp <- barplot(vals, names.arg = years, col = bar_col, border = NA,
                  ylab = y_label, xlab = "Year",
                  las = 2, cex.names = 0.75, cex.axis = 0.9)
    
    # Trend line over bars
    if (length(years) >= 5) {
      fit <- lm(vals ~ years)
      trend_y <- coef(fit)[1] + coef(fit)[2] * years
      # bp gives bar midpoints; use those for x-coords
      lines(bp, trend_y, col = "black", lwd = 2)
      per_decade  <- coef(fit)[2] * 10
      legend("topleft",
             legend = sprintf("Trend: %+.1f days / decade", per_decade),
             col = "black", lwd = 2, bty = "n", cex = 0.9)
    }
    
    title(main = paste(y_label, "--", station$Stationsname), font.main = 1, cex.main = 1.0)
    mtext("Source: Deutscher Wetterdienst", adj = 0.99, line = 0.3, font = 3, cex = 0.9)
  })
  
  output$indices_unavailable <- renderUI({
    req(climate_data())
    if (is.null(climate_data()$data_indices))
      tags$p(style = "color:#d62728; font-size:13px; margin-top:6px;",
             icon("exclamation-triangle"),
             " This station does not publish annual climate indices.")
    else
      NULL
  })
  
  # -- Raster tab --------------------------------------------------------------------------------
  
  # ---------------------------------------------------------------------------
  # Raster variable catalogue - correct DWD folder/filename structure
  # Filename: grids_germany_multi_annual_{abbr}_{year-range}_{period}.asc.gz
  # e.g.  grids_germany_multi_annual_air_temp_mean_1961-1990_01.asc.gz
  #       grids_germany_multi_annual_frost_days_1961-1990_17.asc.gz
  # ---------------------------------------------------------------------------
  raster_catalogue <- list(
    air_temperature_mean = list(
      folder="air_temperature_mean", abbr="air_temp_mean",
      label="Mean air temperature [deg C]", unit="deg C",
      palette=c("#053061","#2166ac","#4393c3","#92c5de","#d1e5f0",
                "#f7f7f7","#fddbc7","#f4a582","#d6604d","#b2182b","#67001f"),
      periods="monthly_and_annual"),
    air_temperature_max = list(
      folder="air_temperature_max", abbr="air_temp_max",
      label="Mean daily max. temperature [deg C]", unit="deg C",
      palette=c("#2166ac","#4393c3","#92c5de","#d1e5f0",
                "#f7f7f7","#fddbc7","#f4a582","#d6604d","#b2182b","#67001f"),
      periods="monthly_and_annual"),
    air_temperature_min = list(
      folder="air_temperature_min", abbr="air_temp_min",
      label="Mean daily min. temperature [deg C]", unit="deg C",
      palette=c("#053061","#2166ac","#4393c3","#92c5de","#d1e5f0",
                "#f7f7f7","#fddbc7","#f4a582","#d6604d","#b2182b"),
      periods="monthly_and_annual"),
    precipitation = list(
      folder="precipitation", abbr=NULL,
      label="Precipitation [mm]", unit="mm",
      palette=c("#ffffff","#d0e8f5","#9ecae1","#4393c3","#08519c",
                "#08306b","#4a1486","#810f7c","#ae017e","#f768a1"),
      periods="monthly_and_annual"),
    sunshine_duration = list(
      folder="sunshine_duration", abbr=NULL,
      label="Sunshine duration [h]", unit="h",
      palette=c("#ffffff","#ffffcc","#ffeda0","#fed976","#feb24c",
                "#fd8d3c","#fc4e2a","#e31a1c","#bd0026","#800026"),
      periods="monthly_and_annual"),
    frost_days = list(
      folder="frost_days", abbr=NULL,
      label="Frost days per year (Tmin < 0 deg C)", unit="days",
      palette=c("#ffffcc","#c7e9b4","#7fcdbb","#41b6c4","#2c7fb8","#253494"),
      periods="annual_only"),
    ice_days = list(
      folder="ice_days", abbr=NULL,
      label="Ice days per year (Tmax < 0 deg C)", unit="days",
      palette=c("#f7fbff","#deebf7","#c6dbef","#9ecae1","#6baed6","#2171b5","#084594"),
      periods="annual_only"),
    hot_days = list(
      folder="hot_days", abbr=NULL,
      label="Hot days per year (Tmax >= 30 deg C)", unit="days",
      palette=c("#ffffb2","#fed976","#feb24c","#fd8d3c","#f03b20","#bd0026"),
      periods="annual_only"),
    summer_days = list(
      folder="summer_days", abbr=NULL,
      label="Summer days per year (Tmax >= 25 deg C)", unit="days",
      palette=c("#ffffd4","#fed98e","#fe9929","#d95f0e","#993404"),
      periods="annual_only"),
    snowcover_days = list(
      folder="snowcover_days", abbr=NULL,
      label="Snow cover days per year", unit="days",
      palette=c("#f7fbff","#deebf7","#9ecae1","#3182bd","#08519c","#08306b"),
      periods="annual_only")
  )
  
  # Map period code to readable label
  period_label <- function(code) {
    switch(code,
           "01"="January","02"="February","03"="March","04"="April",
           "05"="May","06"="June","07"="July","08"="August",
           "09"="September","10"="October","11"="November","12"="December",
           "13"="Spring (Mar-May)","14"="Summer (Jun-Aug)",
           "15"="Autumn (Sep-Nov)","16"="Winter (Dec-Feb)",
           "17"="Annual mean", code)
  }
  
  # Map short year code to the full range string used in DWD filenames
  year_range_str <- function(yr) {
    switch(yr,
           "6190"="1961-1990", "7100"="1971-2000",
           "8110"="1981-2010", "9120"="1991-2020", yr)
  }
  
  raster_data <- reactiveVal(NULL)
  
  # Update the period selector when variable changes
  observeEvent(input$raster_var, {
    entry <- raster_catalogue[[input$raster_var]]
    if (is.null(entry)) return()
    if (entry$periods == "annual_only") {
      updateSelectInput(session, "raster_period",
                        choices = c("Annual mean" = "17"), selected = "17")
    } else {
      updateSelectInput(session, "raster_period",
                        choices = c(
                          "January"="01","February"="02","March"="03","April"="04",
                          "May"="05","June"="06","July"="07","August"="08",
                          "September"="09","October"="10","November"="11","December"="12",
                          "Spring (Mar-May)"="13","Summer (Jun-Aug)"="14",
                          "Autumn (Sep-Nov)"="15","Winter (Dec-Feb)"="16",
                          "Annual mean"="17"),
                        selected = "17")
    }
  })
  
  observeEvent(input$load_raster, {
    var    <- input$raster_var
    period <- input$raster_period
    yr     <- input$raster_year
    
    entry <- raster_catalogue[[var]]
    if (is.null(entry)) {
      showNotification("Unknown variable.", type = "error"); return()
    }
    
    withProgress(message = "Downloading raster grid...", value = 0.2, {
      tryCatch({
        gridbase <- rdwd::gridbase
        abbr     <- if (!is.null(entry$abbr)) entry$abbr else entry$folder
        yr_str   <- year_range_str(yr)
        fname    <- paste0("grids_germany_multi_annual_", abbr,
                           "_", yr_str, "_", period, ".asc.gz")
        link     <- paste0("multi_annual/", entry$folder, "/", fname)
        
        incProgress(0.4, detail = paste("Fetching", fname))
        file <- rdwd::dataDWD(link, base = gridbase, joinbf = TRUE,
                              read = FALSE, dir = tempdir(), quiet = TRUE)
        
        incProgress(0.3, detail = "Reading raster")
        r <- rdwd::readDWD(file, quiet = TRUE)
        
        raster_data(list(raster = r, var = var, period = period, yr = yr))
        
      }, error = function(e) {
        showNotification(paste("Could not load raster:", e$message), type = "error")
      })
    })
  })
  
  output$raster_status <- renderUI({
    rd <- raster_data()
    if (is.null(rd)) return(NULL)
    meta <- raster_catalogue[[rd$var]]
    r    <- rd$raster
    vals <- terra::values(r)
    vals <- vals[!is.na(vals)]
    tags$div(
      class = "alert alert-info",
      style = "padding: 8px 12px; font-size: 13px;",
      tags$b(meta$label), " | ",
      period_label(rd$period), " | ", rd$yr, " reference period | ",
      sprintf("Range: %.1f - %.1f %s | Grid cells: %s",
              min(vals), max(vals), meta$unit,
              format(length(vals), big.mark = ","))
    )
  })
  
  output$raster_plot <- renderPlot({
    rd <- raster_data()
    if (is.null(rd)) {
      par(mar = c(0,0,0,0)); plot.new()
      text(0.5, 0.5,
           "Select variable, period and reference period, then click 'Load raster map'.",
           cex = 1.2, col = "#888888")
      return()
    }
    
    r    <- rd$raster
    meta <- raster_catalogue[[rd$var]]
    
    # Project from DWD Gauss-Krueger (EPSG:31467) to WGS84 for display
    r_wgs <- tryCatch(
      terra::project(r, "EPSG:4326"),
      error = function(e) r   # fall back to original if projection fails
    )
    
    par(mar = c(1, 1, 3, 4))
    terra::plot(r_wgs,
                col    = grDevices::colorRampPalette(meta$palette)(100),
                main   = "",
                legend = TRUE,
                axes   = FALSE)
    
    # Add a rough Germany outline via wrld_simpl from maptools, or skip gracefully
    tryCatch({
      if (requireNamespace("maps", quietly = TRUE)) {
        maps::map("world", regions = "Germany", add = TRUE,
                  col = "grey30", lwd = 0.8)
      }
    }, error = function(e) NULL)
    
    title(main = paste(meta$label, "--",
                       period_label(rd$period), "|",
                       gsub("(\\d{2})(\\d{2})", "19\\1-20\\2", rd$yr),
                       "reference period"),
          font.main = 1, cex.main = 1.1)
    mtext("Source: Deutscher Wetterdienst | 1 km x 1 km interpolated grid",
          side = 1, line = -0.5, adj = 0.5, font = 3, cex = 0.9)
  })

  # -- Phenology tab -----------------------------------------------------------------------------
  # Everything here is loaded lazily on first genuine visit to the Phenology
  # tab (pheno_tab_initialized), not at session start -- unlike all_stations
  # above (built once at script top-level, shared across all sessions), a
  # per-session Shiny observer would otherwise refetch this on every single
  # page load even for users who never open this tab.

  pheno_stations          <- reactiveVal(NULL)  # data.frame, current reporter
  pheno_selected_station  <- reactiveVal(NULL)  # one-row data.frame
  pheno_species_catalogue <- reactiveVal(NULL)  # data.frame from list_phenology_species
  pheno_data              <- reactiveVal(NULL)  # fetched species/station data.frame
  pheno_tab_initialized   <- reactiveVal(FALSE)

  output$pheno_stations_map <- renderLeaflet({
    leaflet() %>%
      addProviderTiles(providers$CartoDB.Positron) %>%
      setView(lng = 10.4, lat = 51.2, zoom = 6)
  })

  load_pheno_stations <- function(reporter) {
    df <- tryCatch(fetch_phenology_stations(reporter), error = function(e) {
      showNotification(paste("Could not load phenology station list:", e$message), type = "error")
      NULL
    })
    pheno_stations(df)
    leafletProxy("pheno_stations_map") %>% clearMarkers() %>% clearGroup("pheno_highlight")
    if (!is.null(df) && nrow(df) > 0) {
      leafletProxy("pheno_stations_map") %>%
        addCircleMarkers(data = df, lng = ~geoLaenge, lat = ~geoBreite, layerId = ~Stations_id,
                          radius = 4, color = "#6a51a3", fillColor = "#6a51a3", fillOpacity = 0.75,
                          stroke = TRUE, weight = 1, label = ~Stationsname)
    }
    df
  }

  load_pheno_species <- function(reporter, category) {
    req(reporter, category)
    cat_df <- tryCatch(list_phenology_species(reporter, category), error = function(e) {
      showNotification(paste("Could not load phenology species list:", e$message), type = "error")
      NULL
    })
    pheno_species_catalogue(cat_df)
    if (!is.null(cat_df) && nrow(cat_df) > 0) {
      updateSelectInput(session, "pheno_species", choices = setNames(cat_df$token, cat_df$label))
    } else {
      updateSelectInput(session, "pheno_species", choices = character(0))
    }
    cat_df
  }

  # Category choices are pure local config (no network) -- safe to populate
  # eagerly, mirrors the QGIS plugin's _populate_pheno_categories().
  observe({
    updateSelectInput(session, "pheno_category", choices = pheno_category_choices(input$pheno_reporter))
  })

  # First real visit to the tab: load both the station network (for the
  # map) and the species list for whatever category is currently selected.
  observeEvent(input$tabs, {
    if (identical(input$tabs, "phenology") && !isTRUE(pheno_tab_initialized())) {
      pheno_tab_initialized(TRUE)
      load_pheno_stations(input$pheno_reporter)
      load_pheno_species(input$pheno_reporter, input$pheno_category)
    }
  }, ignoreInit = TRUE)

  # Genuine subsequent changes (tab already visited) reload accordingly.
  observeEvent(input$pheno_reporter, {
    pheno_selected_station(NULL)
    if (isTRUE(pheno_tab_initialized())) load_pheno_stations(input$pheno_reporter)
  }, ignoreInit = TRUE)

  observeEvent(input$pheno_category, {
    if (isTRUE(pheno_tab_initialized())) load_pheno_species(input$pheno_reporter, input$pheno_category)
  }, ignoreInit = TRUE)

  observeEvent(input$pheno_stations_map_marker_click, {
    click <- input$pheno_stations_map_marker_click
    req(click$id)
    df <- pheno_stations()
    req(df)
    st <- df[df$Stations_id == click$id, ]
    if (nrow(st) == 0) return()
    pheno_selected_station(st[1, ])
    leafletProxy("pheno_stations_map") %>%
      clearGroup("pheno_highlight") %>%
      addCircleMarkers(lng = st$geoLaenge[1], lat = st$geoBreite[1], group = "pheno_highlight",
                        radius = 9, color = "white", fillColor = "#6a51a3",
                        fillOpacity = 1, stroke = TRUE, weight = 3)
  })

  output$pheno_map_pick_info <- renderUI({
    st <- pheno_selected_station()
    if (is.null(st)) {
      return(tags$p(style = "color:#888;", icon("info-circle"),
                    " Click a phenology station marker on the map below."))
    }
    tags$div(class = "alert alert-success",
             tags$b("Using station: "), st$Stationsname, " (ID ", st$Stations_id, ")")
  })

  fetch_pheno_data <- function(station_row) {
    req(station_row)
    cat_df <- pheno_species_catalogue()
    req(cat_df)
    species_row <- cat_df[cat_df$token == input$pheno_species, ]
    if (nrow(species_row) == 0) {
      showNotification("No species selected.", type = "error"); return()
    }
    withProgress(message = paste("Fetching phenology data for", station_row$Stationsname), value = 0.3, {
      df <- tryCatch(
        fetch_phenology_species_data(input$pheno_reporter, input$pheno_category,
                                      species_row[1, ], station_row$Stations_id),
        error = function(e) {
          showNotification(paste("Error:", e$message), type = "error")
          NULL
        }
      )
      incProgress(0.7)
    })
    pheno_data(df)
    pheno_selected_station(station_row)

    if (!is.null(df) && nrow(df) > 0) {
      yrs <- df$Referenzjahr
      updateSliderInput(session, "pheno_year_range",
                        min = min(yrs), max = max(yrs), value = c(min(yrs), max(yrs)))
    }
  }

  observeEvent(input$pheno_fetch, {
    cat_df <- pheno_species_catalogue()
    if (is.null(cat_df) || nrow(cat_df) == 0 ||
        is.null(input$pheno_species) || !nzchar(input$pheno_species)) {
      showNotification("Species list is still loading -- please wait a moment and try again.", type = "warning")
      return()
    }

    if (input$pheno_location_mode == "map_pick") {
      st <- pheno_selected_station()
      if (is.null(st)) {
        showNotification("Pick a phenology station on the map first.", type = "warning"); return()
      }
      fetch_pheno_data(st)
    } else {
      withProgress(message = "Finding phenology stations...", value = 0, {
        stations_df <- pheno_stations()
        if (is.null(stations_df)) stations_df <- load_pheno_stations(input$pheno_reporter)
        req(stations_df)
        if (input$pheno_location_mode == "city") {
          req(input$pheno_city)
          coords <- tmaptools::geocode_OSM(input$pheno_city)
          if (is.null(coords)) {
            showNotification("Could not geocode city name.", type = "error"); return()
          }
          search_lat <- coords$coords[2]; search_lon <- coords$coords[1]
        } else {
          req(input$pheno_lat, input$pheno_lon)
          search_lat <- input$pheno_lat; search_lon <- input$pheno_lon
        }
        near <- pheno_nearby_stations(stations_df, search_lat, search_lon, input$pheno_search_radius)
        if (nrow(near) == 0) {
          showNotification("No phenology station within radius.", type = "warning"); return()
        }
        fetch_pheno_data(near[1, ])
      })
    }
  })

  output$pheno_status <- renderUI({
    df <- pheno_data()
    st <- pheno_selected_station()
    if (is.null(df) || is.null(st)) return(NULL)
    if (nrow(df) == 0) {
      return(tags$p(style = "color:#d62728; font-size:13px;",
                    "No phenology records for ", st$Stationsname, "."))
    }
    n_phases <- length(unique(df$Phase_id))
    tags$p(style = "color:#2ca02c; font-size:13px;",
           "Showing ", st$Stationsname, " (ID ", st$Stations_id, ") -- ",
           nrow(df), " records across ", n_phases, " phase(s).")
  })

  output$pheno_phase_ui <- renderUI({
    df <- pheno_data()
    if (is.null(df) || nrow(df) == 0) {
      return(tags$p(style = "color:#888;", "No phenology data loaded yet."))
    }
    phases <- unique(df[, c("Phase_id", "Phase_de")])
    phases <- phases[order(phases$Phase_id), ]
    selectInput("pheno_phase", "Phenological phase:",
                choices = setNames(phases$Phase_id, phases$Phase_de))
  })

  output$pheno_plot <- renderPlot({
    df <- pheno_data()
    if (is.null(df) || nrow(df) == 0) {
      par(mar = c(0, 0, 0, 0)); plot.new()
      text(0.5, 0.5, "No phenology data available for this station/species.",
           cex = 1.2, col = "#888888")
      return()
    }
    req(input$pheno_phase)
    phase_id <- as.integer(input$pheno_phase)
    sub <- df[df$Phase_id == phase_id & !is.na(df$Jultag), ]
    sub <- sub[sub$Referenzjahr >= input$pheno_year_range[1] &
                 sub$Referenzjahr <= input$pheno_year_range[2], ]
    if (nrow(sub) == 0) {
      par(mar = c(0, 0, 0, 0)); plot.new()
      text(0.5, 0.5, "No data for this phase in the selected year range.",
           cex = 1.2, col = "#888888")
      return()
    }

    years <- sub$Referenzjahr
    doy   <- sub$Jultag
    station <- pheno_selected_station()

    par(mar = c(4, 4.5, 3, 2))
    plot(years, doy, pch = 19, col = "#b2182b",
         xlab = "Year", ylab = "Day of year", las = 1)
    grid(nx = NA, ny = NULL, col = "grey90", lty = 1)

    if (nrow(sub) >= 5) {
      fit <- lm(doy ~ years)
      trend_x <- range(years)
      trend_y <- coef(fit)[1] + coef(fit)[2] * trend_x
      lines(trend_x, trend_y, col = "black", lwd = 2)
      per_decade <- coef(fit)[2] * 10
      legend("topleft", legend = sprintf("Trend: %+.1f days / decade", per_decade),
             col = "black", lwd = 2, bty = "n", cex = 0.9)
    }

    phase_label <- sub$Phase_de[1]
    plant_label <- if ("Objekt_de" %in% names(sub)) sub$Objekt_de[1] else ""
    station_name <- if (!is.null(station)) station$Stationsname else ""
    title(main = paste0(plant_label, " -- ", phase_label, " -- ", station_name),
          font.main = 1, cex.main = 1.1)
    mtext("Source: Deutscher Wetterdienst", adj = 0.99, line = 0.3, font = 3, cex = 0.9)
  })

  output$download_pheno <- downloadHandler(
    filename = function() {
      st <- pheno_selected_station()
      name <- if (!is.null(st)) st$Stationsname else "station"
      paste0("phenology_data_", gsub("[^A-Za-z0-9]", "_", name), ".csv")
    },
    content = function(file) {
      df <- pheno_data()
      req(df)
      write.csv(df, file, row.names = FALSE, fileEncoding = "UTF-8")
    }
  )
}

#### Chapter 4: Run the App ####
shinyApp(ui = ui, server = server)