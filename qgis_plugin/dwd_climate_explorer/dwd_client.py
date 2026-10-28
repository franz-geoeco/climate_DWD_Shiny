"""
Pure-Python data access layer for the DWD (Deutscher Wetterdienst) Open Data
portal (https://opendata.dwd.de). No QGIS / Qt imports here on purpose, so
this module can be unit-tested with a plain Python interpreter.

Ports the data-fetching logic of the original "German Climate Dashboard"
R/Shiny app (which used the R package `rdwd`) to plain requests + pandas.

Four DWD "products" are used, and all four turn out to share the exact same
historical/recent + fixed-width-description-file layout on the server, so a
single generic pipeline handles all of them (the R app had to hand-roll a
directory-listing scan just for the annual indices product; that's not
needed here).
"""
import gzip
import io
import os
import re
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

OBS_BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/"
GRID_BASE = "https://opendata.dwd.de/climate_environment/CDC/grids_germany/multi_annual/"

USER_AGENT = "QGIS-DWD-Climate-Explorer/1.0 (+https://opendata.dwd.de)"

PRODUCTS = {
    "daily_kl": dict(
        hist_dir=OBS_BASE + "daily/kl/historical/",
        recent_dir=OBS_BASE + "daily/kl/recent/",
        desc_file="KL_Tageswerte_Beschreibung_Stationen.txt",
        hist_prefix="tageswerte_KL",
        recent_prefix="tageswerte_KL",
        date_col="MESS_DATUM",
        label="Daily climate (temperature / precipitation)",
    ),
    "monthly_kl": dict(
        hist_dir=OBS_BASE + "monthly/kl/historical/",
        recent_dir=OBS_BASE + "monthly/kl/recent/",
        desc_file="KL_Monatswerte_Beschreibung_Stationen.txt",
        hist_prefix="monatswerte_KL",
        recent_prefix="monatswerte_KL",
        date_col="MESS_DATUM_BEGINN",
        label="Monthly climate (30-year climate chart)",
    ),
    "hourly_wind": dict(
        hist_dir=OBS_BASE + "hourly/wind/historical/",
        recent_dir=OBS_BASE + "hourly/wind/recent/",
        desc_file="FF_Stundenwerte_Beschreibung_Stationen.txt",
        hist_prefix="stundenwerte_FF",
        recent_prefix="stundenwerte_FF",
        date_col="MESS_DATUM",
        label="Hourly wind speed & direction",
    ),
    "annual_klindex": dict(
        hist_dir=OBS_BASE + "annual/climate_indices/kl/historical/",
        recent_dir=OBS_BASE + "annual/climate_indices/kl/recent/",
        desc_file="jahreswerte_klindex_Beschreibung_Stationen.txt",
        hist_prefix="jahreswerte_KLINDEX",
        recent_prefix="jahreswerte_KLINDEX",
        date_col="MESS_DATUM_BEGINN",
        label="Annual climate indices",
    ),
}

TIER_COLOUR = {
    "full": "#2ca02c",
    "climate": "#ff7f0e",
    "basic": "#d62728",
    "none": "#aaaaaa",
}
TIER_LABEL = {
    "full": "Full data (climate + wind)",
    "climate": "Climate only (no hourly wind)",
    "basic": "Daily climate only (no monthly / wind)",
    "none": "No downloadable files",
}

RASTER_CATALOGUE = {
    "air_temperature_mean": dict(
        folder="air_temperature_mean", abbr="air_temp_mean",
        label="Mean air temperature", unit="°C",
        palette=["#053061", "#2166ac", "#4393c3", "#92c5de", "#d1e5f0",
                 "#f7f7f7", "#fddbc7", "#f4a582", "#d6604d", "#b2182b", "#67001f"],
        periods="monthly_and_annual"),
    "air_temperature_max": dict(
        folder="air_temperature_max", abbr="air_temp_max",
        label="Mean daily max. temperature", unit="°C",
        palette=["#2166ac", "#4393c3", "#92c5de", "#d1e5f0",
                 "#f7f7f7", "#fddbc7", "#f4a582", "#d6604d", "#b2182b", "#67001f"],
        periods="monthly_and_annual"),
    "air_temperature_min": dict(
        folder="air_temperature_min", abbr="air_temp_min",
        label="Mean daily min. temperature", unit="°C",
        palette=["#053061", "#2166ac", "#4393c3", "#92c5de", "#d1e5f0",
                 "#f7f7f7", "#fddbc7", "#f4a582", "#d6604d", "#b2182b"],
        periods="monthly_and_annual"),
    "precipitation": dict(
        folder="precipitation", abbr=None,
        label="Precipitation", unit="mm",
        palette=["#ffffff", "#d0e8f5", "#9ecae1", "#4393c3", "#08519c",
                 "#08306b", "#4a1486", "#810f7c", "#ae017e", "#f768a1"],
        periods="monthly_and_annual"),
    "sunshine_duration": dict(
        folder="sunshine_duration", abbr=None,
        label="Sunshine duration", unit="h",
        palette=["#ffffff", "#ffffcc", "#ffeda0", "#fed976", "#feb24c",
                 "#fd8d3c", "#fc4e2a", "#e31a1c", "#bd0026", "#800026"],
        periods="monthly_and_annual"),
    "frost_days": dict(
        folder="frost_days", abbr=None,
        label="Frost days per year (Tmin < 0°C)", unit="days",
        palette=["#ffffcc", "#c7e9b4", "#7fcdbb", "#41b6c4", "#2c7fb8", "#253494"],
        periods="annual_only"),
    "ice_days": dict(
        folder="ice_days", abbr=None,
        label="Ice days per year (Tmax < 0°C)", unit="days",
        palette=["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#2171b5", "#084594"],
        periods="annual_only"),
    "hot_days": dict(
        folder="hot_days", abbr=None,
        label="Hot days per year (Tmax ≥ 30°C)", unit="days",
        palette=["#ffffb2", "#fed976", "#feb24c", "#fd8d3c", "#f03b20", "#bd0026"],
        periods="annual_only"),
    "summer_days": dict(
        folder="summer_days", abbr=None,
        label="Summer days per year (Tmax ≥ 25°C)", unit="days",
        palette=["#ffffd4", "#fed98e", "#fe9929", "#d95f0e", "#993404"],
        periods="annual_only"),
    "snowcover_days": dict(
        folder="snowcover_days", abbr=None,
        label="Snow cover days per year", unit="days",
        palette=["#f7fbff", "#deebf7", "#9ecae1", "#3182bd", "#08519c", "#08306b"],
        periods="annual_only"),
}

PERIOD_LABELS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
    "13": "Spring (Mar-May)", "14": "Summer (Jun-Aug)",
    "15": "Autumn (Sep-Nov)", "16": "Winter (Dec-Feb)",
    "17": "Annual mean",
}
YEAR_RANGE_LABELS = {
    "6190": "1961-1990", "7100": "1971-2000",
    "8110": "1981-2010", "9120": "1991-2020",
}


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


class DwdError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Station description files (fixed-width) -> DataFrame
# ---------------------------------------------------------------------------
def _parse_fixed_width(text):
    # DWD "Beschreibung Stationen" files are fixed-width at a constant
    # 1000 chars/line, but the dashed underline on row 2 (which looks like
    # a column-width guide) is sized to the *header labels*, not the actual
    # data columns -- it does not line up with the real field boundaries.
    # The true offsets (verified against daily/monthly/wind/annual-index,
    # historical + recent alike) are constant across all DWD CDC station
    # description files: id[0:5] von[6:14] bis[15:23] "height lat lon"[23:61]
    # name[61:102] bundesland[102:143].
    rows = []
    for line in text.splitlines()[2:]:
        if not line.strip():
            continue
        sid = line[0:5].strip()
        von = line[6:14].strip()
        bis = line[15:23].strip()
        mid = line[23:61].split()
        if not sid or len(mid) < 3:
            continue
        height, lat, lon = mid[0], mid[1], mid[2]
        name = line[61:102].strip()
        bundesland = line[102:143].strip()
        rows.append((int(sid), von, bis, int(height), float(lat), float(lon), name, bundesland))
    return pd.DataFrame(rows, columns=[
        "Stations_id", "von_datum", "bis_datum", "Stationshoehe",
        "geoBreite", "geoLaenge", "Stationsname", "Bundesland"])


def _fmt_date(yyyymmdd):
    if not isinstance(yyyymmdd, str) or len(yyyymmdd) != 8:
        return None
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _fetch_description(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = "latin-1"
    return _parse_fixed_width(r.text)


def _list_hist_filenames(session, dir_url, prefix):
    """Scans a DWD 'historical/' directory listing for zip filenames. The
    station description file's von_datum/bis_datum cannot be trusted to
    reconstruct these: for a currently-reporting station its bis_datum
    reflects the *station's* last reporting date (i.e. today), not the
    date the historical archive file was actually cut off at -- those two
    routinely differ by months, so guessing the filename 404s."""
    r = session.get(dir_url, timeout=30)
    r.raise_for_status()
    pattern = re.compile(re.escape(prefix) + r"_(\d{5})_(\d{8}_\d{8})_hist\.zip")
    out = {}
    for m in pattern.finditer(r.text):
        out[int(m.group(1))] = f"{prefix}_{m.group(1)}_{m.group(2)}_hist.zip"
    return out


class DWDStationIndex:
    """Holds the merged station table plus the exact per-product filenames
    needed to build download URLs later."""

    def __init__(self):
        self.stations = None          # public DataFrame, one row per station
        self._hist_filenames = {}     # product -> {Stations_id: filename}
        self._recent_ids = {}         # product -> set(Stations_id)

    def zip_urls(self, product, station_id):
        spec = PRODUCTS[product]
        urls = []
        fname = self._hist_filenames.get(product, {}).get(station_id)
        if fname:
            urls.append(spec["hist_dir"] + fname)
        if station_id in self._recent_ids.get(product, set()):
            fname = f"{spec['recent_prefix']}_{station_id:05d}_akt.zip"
            urls.append(spec["recent_dir"] + fname)
        return urls


def build_station_index(progress_cb=None):
    """Downloads the 4 x (historical + recent) station description files
    and builds a per-station data-availability table, mirroring the R
    app's `all_stations` / `metaIndex`-derived summary."""
    session = _session()
    index = DWDStationIndex()
    per_product = {}
    steps = list(PRODUCTS.items())
    for i, (product, spec) in enumerate(steps):
        if progress_cb:
            progress_cb(i, len(steps), f"Fetching station list: {spec['label']}")
        hist_df = _fetch_description(session, spec["hist_dir"] + spec["desc_file"])
        recent_df = _fetch_description(session, spec["recent_dir"] + spec["desc_file"])

        index._hist_filenames[product] = _list_hist_filenames(
            session, spec["hist_dir"], spec["hist_prefix"])
        index._recent_ids[product] = set(recent_df["Stations_id"].astype(int))

        combined = pd.concat([hist_df, recent_df], ignore_index=True)
        summary = combined.groupby("Stations_id").agg(
            von_datum=("von_datum", "min"),
            bis_datum=("bis_datum", "max"),
        ).reset_index()
        summary[f"from_{product}"] = summary["von_datum"].map(_fmt_date)
        summary[f"to_{product}"] = summary["bis_datum"].map(_fmt_date)
        summary[f"has_{product}"] = True

        # Keep one geo/name row per station (prefer the most recent entry)
        geo_cols = ["Stations_id", "Stationsname", "geoBreite", "geoLaenge",
                    "Stationshoehe", "Bundesland"]
        geo = combined.sort_values("bis_datum").drop_duplicates("Stations_id", keep="last")[geo_cols]

        per_product[product] = (
            geo,
            summary[["Stations_id", f"from_{product}", f"to_{product}", f"has_{product}"]],
        )

    if progress_cb:
        progress_cb(len(steps), len(steps), "Merging station tables")

    # Union of all stations seen across all four products, geo info from
    # whichever product has it (daily_kl covers almost all of them).
    all_geo = pd.concat([g for g, _ in per_product.values()], ignore_index=True)
    all_geo = all_geo.dropna(subset=["geoBreite", "geoLaenge"])
    all_geo = all_geo.sort_values("Stations_id").drop_duplicates("Stations_id", keep="first")

    stations = all_geo.copy()
    for product, (_, summary) in per_product.items():
        stations = stations.merge(summary, on="Stations_id", how="left")
        stations[f"has_{product}"] = stations[f"has_{product}"] == True  # noqa: E712 (NaN-safe bool coercion)

    def tier_of(row):
        if row.has_daily_kl and row.has_monthly_kl and row.has_hourly_wind:
            return "full"
        if row.has_daily_kl and row.has_monthly_kl:
            return "climate"
        if row.has_daily_kl:
            return "basic"
        return "none"

    stations["tier"] = stations.apply(tier_of, axis=1)
    stations["colour"] = stations["tier"].map(TIER_COLOUR)
    stations["tier_desc"] = stations["tier"].map(TIER_LABEL)
    stations = stations.reset_index(drop=True)

    index.stations = stations
    return index


# ---------------------------------------------------------------------------
# Product zip download + parse
# ---------------------------------------------------------------------------
def _read_produkt_zip(session, url):
    r = session.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = [n for n in z.namelist() if n.startswith("produkt")]
    if not names:
        return None
    text = z.read(names[0]).decode("latin-1")
    df = pd.read_csv(io.StringIO(text), sep=";", skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    df = df.replace(-999, np.nan)
    return df


def fetch_product(index, product, station_id, session=None):
    """Downloads + combines the historical and recent files for one
    station/product. Overlapping dates are resolved by keeping the *recent*
    file's rows (recent is re-published more often), avoiding the R app's
    hard-coded "> 2023-12-31" cutoff, which goes stale every year."""
    session = session or _session()
    spec = PRODUCTS[product]
    urls = index.zip_urls(product, station_id)
    if not urls:
        return None
    parts = []
    for url in urls:
        df = _read_produkt_zip(session, url)
        if df is not None and len(df):
            parts.append(df)
    if not parts:
        return None
    combined = pd.concat(parts, ignore_index=True)
    date_col = spec["date_col"]
    if date_col in combined.columns:
        combined = combined.drop_duplicates(subset=[date_col], keep="last")
        combined = combined.sort_values(date_col).reset_index(drop=True)
        if date_col == "MESS_DATUM":
            fmt = "%Y%m%d%H" if combined[date_col].astype(str).str.len().max() >= 10 else "%Y%m%d"
            combined["_date"] = pd.to_datetime(combined[date_col].astype(str), format=fmt, errors="coerce")
    return combined


def fetch_station_bundle(index, station_row, session=None, progress_cb=None):
    """Equivalent of the R app's `fetch_climate_data()`: pulls daily,
    monthly, wind and annual-index data for one station."""
    session = session or _session()
    station_id = int(station_row["Stations_id"])
    steps = [("daily_kl", "daily climate"), ("monthly_kl", "monthly climate chart"),
             ("hourly_wind", "hourly wind"), ("annual_klindex", "annual indices")]
    out = {"station": station_row}
    for i, (product, label) in enumerate(steps):
        if progress_cb:
            progress_cb(i, len(steps), f"Fetching {label} for {station_row['Stationsname']}")
        try:
            out[product] = fetch_product(index, product, station_id, session=session)
        except Exception:
            out[product] = None
    out["data"] = out.pop("daily_kl")
    out["data_chart"] = out.pop("monthly_kl")
    out["data_wind"] = out.pop("hourly_wind")
    out["data_indices"] = out.pop("annual_klindex")
    out["height"] = int(station_row["Stationshoehe"])
    return out


# ---------------------------------------------------------------------------
# Geocoding + nearest-station search
# ---------------------------------------------------------------------------
def geocode_city(name, session=None):
    session = session or _session()
    r = session.get(
        "https://nominatim.openstreetmap.org/search",
        params={"format": "json", "q": name, "countrycodes": "de", "limit": 1},
        timeout=20,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"]), results[0]["display_name"]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlambda / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def nearby_stations(stations_df, lat, lon, radius_km, require="has_daily_kl"):
    df = stations_df[stations_df[require]].copy() if require else stations_df.copy()
    df["dist_km"] = haversine_km(lat, lon, df["geoBreite"].values, df["geoLaenge"].values)
    df = df[df["dist_km"] <= radius_km].sort_values("dist_km")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Raster grids
# ---------------------------------------------------------------------------
# DWD stores the temperature grids as integers in tenths of a degree C (e.g.
# 96 means 9.6 degC) to keep the ASCII files compact -- confirmed by
# checking real downloaded values (a "mean annual temperature" grid with raw
# values in the 80-100 range only makes sense as 8.0-10.0 degC). Precipitation,
# sunshine duration and the day-count indices are NOT scaled -- their raw
# values already match known German climatology (e.g. ~700-900 mm/year).
TEMPERATURE_VARIABLES = {"air_temperature_mean", "air_temperature_max", "air_temperature_min"}


def raster_filename(variable, period, year_code, year_str_override=None):
    meta = RASTER_CATALOGUE[variable]
    abbr = meta["abbr"] or meta["folder"]
    year_str = year_str_override or YEAR_RANGE_LABELS[year_code]
    return f"grids_germany_multi_annual_{abbr}_{year_str}_{period}.asc.gz"


def _rescale_temperature_asc(raw_bytes):
    """Divides every data cell (but not the NODATA sentinel) by 10, keeping
    the ESRI ASCII grid header and cell layout intact."""
    text = raw_bytes.decode("latin-1")
    lines = text.splitlines()
    header_lines = lines[:6]
    header = dict(l.split() for l in header_lines)
    nodata_str = header["NODATA_VALUE"]
    nodata = float(nodata_str)
    ncols = int(header["NCOLS"])

    flat = np.array(" ".join(lines[6:]).split(), dtype=float)
    scaled = np.where(flat == nodata, flat, flat / 10.0)
    rows = scaled.reshape(-1, ncols)

    body = "\n".join(
        " ".join(nodata_str if v == nodata else f"{v:.1f}" for v in row)
        for row in rows
    )
    new_text = "\n".join(header_lines) + "\n" + body + "\n"
    return new_text.encode("latin-1")


def fetch_raster_grid(variable, period, year_code, dest_path, session=None):
    """Downloads + gunzips a DWD multi-annual ESRI-ASCII grid. DWD's server
    is inconsistent: every variable spells the 1991-2020 period with a
    hyphen except air_temperature_mean, which (as of 2026) uses an
    underscore. Try the normal hyphenated name first and fall back."""
    session = session or _session()
    meta = RASTER_CATALOGUE[variable]
    folder_url = GRID_BASE + meta["folder"] + "/"

    candidates = [raster_filename(variable, period, year_code)]
    if year_code == "9120":
        candidates.append(raster_filename(variable, period, year_code, year_str_override="1991_2020"))

    last_status = None
    for fname in candidates:
        url = folder_url + fname
        r = session.get(url, timeout=60)
        if r.status_code == 200:
            data = gzip.decompress(r.content)
            if variable in TEMPERATURE_VARIABLES:
                data = _rescale_temperature_asc(data)
            with open(dest_path, "wb") as f:
                f.write(data)
            return dest_path, url
        last_status = r.status_code
    raise DwdError(f"Could not download raster grid (last HTTP status {last_status}). "
                    f"Tried: {candidates}")


# ---------------------------------------------------------------------------
# Phenology (plant development stages). This is a separate DWD network from
# the climate stations above -- different station IDs, different geo
# network -- split across two "reporter" sub-networks (annual / immediate)
# and several plant categories. Unlike the climate zips, the data files
# themselves are plain uncompressed semicolon-delimited text.
# ---------------------------------------------------------------------------
PHENO_BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/phenology/"
PHENO_HELP_BASE = "https://opendata.dwd.de/climate_environment/CDC/help/"

PHENO_REPORTER_TYPES = {
    "annual": dict(
        prefix="Jahresmelder",
        station_file="PH_Beschreibung_Phaenologie_Stationen_Jahresmelder.txt",
        label="Annual reporters (Jahresmelder) – long-term network, most species",
    ),
    "immediate": dict(
        prefix="Sofortmelder",
        station_file="PH_Beschreibung_Phaenologie_Stationen_Sofortmelder.txt",
        label="Immediate reporters (Sofortmelder) – faster real-time reports, fewer species",
    ),
}

# (reporter, category) -> folder + filename infix used to discover species by
# scanning the historical/recent directory listings. infix="" means the
# category has no shared infix token (see _dedupe_species_label).
PHENO_CATEGORIES = {
    ("annual", "crops"): dict(folder="annual_reporters/crops", infix="Landwirtschaft_Kulturpflanze", label="Field crops"),
    ("annual", "farming"): dict(folder="annual_reporters/farming", infix="", label="Farming (discontinued ~1990/91)"),
    ("annual", "fruit"): dict(folder="annual_reporters/fruit", infix="Obst", label="Fruit"),
    ("annual", "vine"): dict(folder="annual_reporters/vine", infix="Weinrebe", label="Vine"),
    ("annual", "wild"): dict(folder="annual_reporters/wild", infix="Wildwachsende_Pflanze", label="Wild plants"),
    ("immediate", "crops"): dict(folder="immediate_reporters/crops", infix="Landwirtschaft_Kulturpflanze", label="Field crops"),
    ("immediate", "fruit"): dict(folder="immediate_reporters/fruit", infix="Obst", label="Fruit"),
    ("immediate", "wild"): dict(folder="immediate_reporters/wild", infix="Wildwachsende_Pflanze", label="Wild plants"),
}


def pheno_categories_for(reporter):
    """Ordered (category_key, label) pairs available for one reporter type."""
    return [(cat, meta["label"]) for (rep, cat), meta in PHENO_CATEGORIES.items() if rep == reporter]


def _dedupe_species_label(token):
    """DWD sometimes bakes the category name into the species token itself
    (farming's 'Feldarbeit_Feldarbeit', vine's 'Weinrebe_fruehe_Reife' after
    only the leading 'Weinrebe_' infix is stripped -- in vine's case the
    repeat is a legitimate variety name, e.g. "Weinrebe (fruehe Reife)").
    Only collapse an *exact* repeated leading token, so farming's literal
    duplication is cleaned up without touching vine's real variety names."""
    parts = token.split("_")
    if len(parts) >= 2 and parts[0] == parts[1]:
        parts = parts[1:]
    return "_".join(parts)


def _pretty_species_label(token):
    return _dedupe_species_label(token).replace("_", " ").replace("-", " ").strip()


class PhenoSpeciesIndex:
    """species_token -> {hist_url, recent_url, label}, built by scanning the
    historical/ and recent/ directory listings of one (reporter, category)
    -- same directory-listing-regex technique as _list_hist_filenames above."""

    def __init__(self):
        self.species = {}

    def sorted_items(self):
        return sorted(self.species.items(), key=lambda kv: kv[1]["label"])


def list_phenology_species(reporter, category, session=None):
    session = session or _session()
    rep_spec = PHENO_REPORTER_TYPES[reporter]
    cat_spec = PHENO_CATEGORIES[(reporter, category)]
    prefix = rep_spec["prefix"]
    infix = cat_spec["infix"]
    infix_part = re.escape(infix) + "_" if infix else ""
    hist_pattern = re.compile(
        r"PH_" + re.escape(prefix) + "_" + infix_part + r"(.+?)_(\d{4})_(\d{4})_hist\.txt")
    recent_pattern = re.compile(
        r"PH_" + re.escape(prefix) + "_" + infix_part + r"(.+?)_akt\.txt")

    index = PhenoSpeciesIndex()
    best_end = {}

    hist_url = PHENO_BASE + cat_spec["folder"] + "/historical/"
    r = session.get(hist_url, timeout=30)
    if r.status_code == 200:
        for m in hist_pattern.finditer(r.text):
            token, end = m.group(1), int(m.group(3))
            if token not in best_end or end > best_end[token]:
                best_end[token] = end
                index.species.setdefault(token, {})["hist_url"] = hist_url + m.group(0)

    recent_url = PHENO_BASE + cat_spec["folder"] + "/recent/"
    r = session.get(recent_url, timeout=30)
    if r.status_code == 200:
        for m in recent_pattern.finditer(r.text):
            token = m.group(1)
            index.species.setdefault(token, {})["recent_url"] = recent_url + m.group(0)

    for token, entry in index.species.items():
        entry.setdefault("hist_url", None)
        entry.setdefault("recent_url", None)
        entry["label"] = _pretty_species_label(token)

    return index


_PHENO_LOOKUP_CACHE = {}


def _read_pheno_semicolon(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    try:
        text = r.content.decode("utf-8")
    except UnicodeDecodeError:
        text = r.content.decode("latin-1")
    df = pd.read_csv(io.StringIO(text), sep=";", skipinitialspace=True, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    keep = [c for c in df.columns if c and c != "eor" and not c.startswith("Unnamed")]
    df = df[keep]
    for col in df.columns:
        df[col] = df[col].str.strip()
    return df


def _int_id_column(df, col):
    """The help files end with a DOS end-of-file byte (0x1A) that parses as
    one extra junk row; drop any row whose id isn't a number."""
    df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=[col]).copy()
    df[col] = df[col].astype(int)
    return df


def fetch_phase_lookup(session=None):
    """Global Phase_id -> name lookup, shared across every phenology
    category (not category-specific)."""
    if "phase" in _PHENO_LOOKUP_CACHE:
        return _PHENO_LOOKUP_CACHE["phase"]
    session = session or _session()
    df = _read_pheno_semicolon(session, PHENO_HELP_BASE + "PH_Beschreibung_Phase.txt")
    df = df.rename(columns={"Phase_ID": "Phase_id", "Phase": "Phase_de", "Phase_englisch": "Phase_en"})
    df = _int_id_column(df, "Phase_id")
    lookup = df.set_index("Phase_id")[["Phase_de", "Phase_en"]]
    _PHENO_LOOKUP_CACHE["phase"] = lookup
    return lookup


def fetch_plant_lookup(session=None):
    """Global Objekt_id -> species name lookup (DE/EN/Latin)."""
    if "plant" in _PHENO_LOOKUP_CACHE:
        return _PHENO_LOOKUP_CACHE["plant"]
    session = session or _session()
    df = _read_pheno_semicolon(session, PHENO_HELP_BASE + "PH_Beschreibung_Pflanze.txt")
    df = df.rename(columns={
        "Objekt_ID": "Objekt_id", "Objekt": "Objekt_de",
        "Objekt_englisch": "Objekt_en", "Objekt_latein": "Objekt_latin",
    })
    df = _int_id_column(df, "Objekt_id")
    lookup = df.set_index("Objekt_id")[["Objekt_de", "Objekt_en", "Objekt_latin"]]
    _PHENO_LOOKUP_CACHE["plant"] = lookup
    return lookup


def fetch_phenology_stations(reporter, session=None):
    """The phenology station network -- a different set of stations (and
    IDs) than the climate network built by build_station_index()."""
    session = session or _session()
    spec = PHENO_REPORTER_TYPES[reporter]
    df = _read_pheno_semicolon(session, PHENO_HELP_BASE + spec["station_file"])
    df = df.rename(columns={"geograph.Breite": "geoBreite", "geograph.Laenge": "geoLaenge"})
    df["Stations_id"] = pd.to_numeric(df["Stations_id"], errors="coerce")
    df["geoBreite"] = pd.to_numeric(df["geoBreite"], errors="coerce")
    df["geoLaenge"] = pd.to_numeric(df["geoLaenge"], errors="coerce")
    df["Stationshoehe"] = pd.to_numeric(df["Stationshoehe"], errors="coerce")
    df = df.dropna(subset=["Stations_id", "geoBreite", "geoLaenge"])
    df["Stations_id"] = df["Stations_id"].astype(int)
    keep = ["Stations_id", "Stationsname", "geoBreite", "geoLaenge", "Stationshoehe", "Bundesland"]
    return df[[c for c in keep if c in df.columns]].reset_index(drop=True)


def fetch_phenology_species_data(reporter, category, species_token, station_id,
                                  session=None, species_index=None):
    """Downloads + combines the historical and recent records for one
    species, filters to one station (station_id=None keeps all), and joins the phase/plant name lookups
    (via the Phase_id/Objekt_id columns actually present in the data -- not
    guessed from the filename, which is far more robust). Overlapping
    (year, phase) rows keep the *recent* file's value, mirroring
    fetch_product()'s historical/recent overlap policy above."""
    session = session or _session()
    if species_index is None:
        species_index = list_phenology_species(reporter, category, session=session)
    entry = species_index.species.get(species_token)
    if entry is None:
        raise DwdError(f"Unknown phenology species '{species_token}' for {reporter}/{category}")

    parts = []
    for key, is_recent in (("hist_url", False), ("recent_url", True)):
        url = entry.get(key)
        if not url:
            continue
        df = _read_pheno_semicolon(session, url)
        df["_recent"] = is_recent
        parts.append(df)
    if not parts:
        return None

    combined = pd.concat(parts, ignore_index=True)
    for col in ("Stations_id", "Referenzjahr", "Objekt_id", "Phase_id", "Jultag"):
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")
    combined = combined.dropna(subset=["Stations_id", "Referenzjahr", "Phase_id", "Objekt_id"])
    combined["Stations_id"] = combined["Stations_id"].astype(int)
    combined["Referenzjahr"] = combined["Referenzjahr"].astype(int)
    combined["Phase_id"] = combined["Phase_id"].astype(int)
    combined["Objekt_id"] = combined["Objekt_id"].astype(int)

    combined = combined.sort_values("_recent").drop_duplicates(
        subset=["Stations_id", "Referenzjahr", "Phase_id"], keep="last")
    combined = combined.drop(columns=["_recent"])

    if station_id is None:
        sub = combined.copy()
    else:
        sub = combined[combined["Stations_id"] == int(station_id)].copy()
    if len(sub) == 0:
        return sub

    return _finish_pheno_records(sub, session)


def _finish_pheno_records(df, session):
    df = df.join(fetch_phase_lookup(session=session), on="Phase_id")
    df = df.join(fetch_plant_lookup(session=session), on="Objekt_id")
    if "Eintrittsdatum" in df.columns:
        df["_date"] = pd.to_datetime(df["Eintrittsdatum"], format="%Y%m%d", errors="coerce")
    return df.sort_values(["Referenzjahr", "Phase_id"]).reset_index(drop=True)


def fetch_phenology_observations(reporter, category, species_token, stations_df,
                                 phase_id=None, year_range=None,
                                 session=None, species_index=None):
    """All-station variant of fetch_phenology_species_data(): every
    observation of one species (optionally one phase / year range) joined to
    the phenology station coordinates, ready to become a map layer."""
    session = session or _session()
    df = fetch_phenology_species_data(reporter, category, species_token, None,
                                      session=session, species_index=species_index)
    return attach_pheno_station_coords(df, stations_df, phase_id, year_range)


def attach_pheno_station_coords(df, stations_df, phase_id=None, year_range=None):
    """Filters all-station phenology records and joins station coordinates.
    Rows whose station is missing from the station list (DWD keeps records
    of stations that have since been dropped from the description file)
    are discarded, since they cannot be placed on the map."""
    if df is None or len(df) == 0:
        return df
    if phase_id is not None:
        df = df[df["Phase_id"] == int(phase_id)]
    if year_range is not None:
        df = df[(df["Referenzjahr"] >= year_range[0]) & (df["Referenzjahr"] <= year_range[1])]
    geo = stations_df[["Stations_id", "Stationsname", "geoBreite", "geoLaenge", "Bundesland"]]
    df = df.merge(geo, on="Stations_id", how="inner")
    return df.dropna(subset=["_date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# RADOLAN (radar-based, gauge-adjusted precipitation, 1km x 1km, 900x900
# national composite). Two products are exposed:
#   RW -- hourly sums. Recent days ship as one RW-YYYYMMDD.tar.gz of 24
#         ESRI-ASCII grids (values in 0.1 mm); older years only as monthly
#         RW-YYYYMM.tar bundles of those same per-day archives.
#   SF -- 24h sums ending 23:50 UTC, in RADOLAN's own binary format (which
#         GDAL cannot read, hence decode_radolan_binary below). Only the
#         recent/ folder is fetched for SF: the historical monthly SF
#         bundles are ~190 MB each (they hold an SF grid for *every* hour),
#         so for older dates the daily sum is rebuilt from that day's 24 RW
#         grids instead -- same 23:50-to-23:50 window, and checked against
#         real SF files to agree to within rounding.
# All timestamps are UTC and mark the END of the accumulation interval.
# ---------------------------------------------------------------------------
RADOLAN_BASE = "https://opendata.dwd.de/climate_environment/CDC/grids_germany/"

# Polar stereographic on a sphere, as defined in the RADOLAN format
# description; the ASCII grids' xllcorner/yllcorner are in these metres.
RADOLAN_PROJ4 = ("+proj=stere +lat_0=90 +lat_ts=60 +lon_0=10 +a=6370040 +b=6370040 "
                 "+x_0=0 +y_0=0 +units=m +no_defs")
RADOLAN_NCOLS = 900
RADOLAN_NROWS = 900
RADOLAN_XLL = -523462.0
RADOLAN_YLL = -4658645.0
RADOLAN_CELL = 1000.0
# GDAL-style (x_topleft, dx, 0, y_topleft, 0, -dy), north-up.
RADOLAN_GEOTRANSFORM = (RADOLAN_XLL, RADOLAN_CELL, 0.0,
                        RADOLAN_YLL + RADOLAN_NROWS * RADOLAN_CELL, 0.0, -RADOLAN_CELL)

RADOLAN_PRODUCTS = {
    "RW": dict(label="Hourly precipitation (RW, 1-hour sum)", step_hours=1, unit="mm",
               max_days=7),
    "SF": dict(label="Daily precipitation (SF, 24-hour sum to 23:50 UTC)", step_hours=24,
               unit="mm", max_days=120),
}

RADOLAN_PALETTE = ["#00ffffff", "#c6dbef", "#9ecae1", "#6baed6", "#3182bd", "#08519c",
                   "#54278f", "#980043", "#e7298a", "#fdae61"]


def decode_radolan_binary(raw):
    """Parses a RADOLAN binary composite: an ASCII header terminated by
    ETX (0x03), then rows x cols little-endian uint16, stored SOUTH row
    first. The low 12 bits are the value in units of 10**precision; bit
    0x2000 flags 'no data' (outside radar coverage). Returns (header,
    float32 array north-up, NaN where no data)."""
    end = raw.index(b"\x03")
    header = raw[:end].decode("latin-1")
    m = re.search(r"GP\s*(\d+)\s*x\s*(\d+)", header)
    nrows, ncols = (int(m.group(1)), int(m.group(2))) if m else (RADOLAN_NROWS, RADOLAN_NCOLS)
    m = re.search(r"PR\s*E([-+]?\d+)", header)
    scale = 10.0 ** int(m.group(1)) if m else 0.1
    arr = np.frombuffer(raw[end + 1:end + 1 + nrows * ncols * 2], dtype="<u2").reshape(nrows, ncols)
    values = (arr & 0x0FFF).astype(np.float32) * np.float32(scale)
    values[(arr & 0x2000) != 0] = np.nan
    return header, values[::-1].copy()


def _parse_radolan_asc(raw_bytes):
    """RW ASCII grid -> float32 mm array (north-up, as ESRI ASCII always
    is), NaN for the -1 no-data cells."""
    text = raw_bytes.decode("latin-1")
    lines = text.split("\n", 6)
    header = dict(l.split() for l in lines[:6])
    ncols, nrows = int(header["ncols"]), int(header["nrows"])
    nodata = float(header["NODATA_value"])
    values = np.array(lines[6].split(), dtype=np.float32).reshape(nrows, ncols)
    values[values == nodata] = np.nan
    return values / np.float32(10.0)


def _rw_day_archive(session, day, cache_dir):
    """Returns the bytes of RW-YYYYMMDD.tar.gz for `day`, from recent/ if
    DWD still keeps it there, otherwise extracted from the monthly
    historical bundle (downloaded once into cache_dir, then reused)."""
    ymd = day.strftime("%Y%m%d")
    url = f"{RADOLAN_BASE}hourly/radolan/recent/asc/RW-{ymd}.tar.gz"
    r = session.get(url, timeout=120)
    if r.status_code == 200:
        return r.content, url
    if r.status_code != 404:
        r.raise_for_status()

    ym = day.strftime("%Y%m")
    hist_url = f"{RADOLAN_BASE}hourly/radolan/historical/asc/{day.year}/RW-{ym}.tar"
    local = os.path.join(cache_dir, f"RW-{ym}.tar")
    if not os.path.exists(local):
        with session.get(hist_url, timeout=600, stream=True) as hr:
            if hr.status_code == 404:
                raise DwdError(f"No RADOLAN RW data for {day:%Y-%m-%d} (neither {url} nor {hist_url}).")
            hr.raise_for_status()
            tmp = local + ".part"
            with open(tmp, "wb") as f:
                for chunk in hr.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            os.replace(tmp, local)
    with tarfile.open(local) as tf:
        try:
            member = tf.extractfile(f"RW-{ymd}.tar.gz")
        except KeyError:
            member = None
        if member is None:
            raise DwdError(f"RW-{ymd}.tar.gz is missing from {hist_url}.")
        return member.read(), hist_url


def _rw_day_grids(session, day, cache_dir):
    """[(end_datetime_utc, array)] for the (normally 24) hourly RW grids
    of one day, sorted by time."""
    content, url = _rw_day_archive(session, day, cache_dir)
    out = []
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
        for member in tf.getmembers():
            m = re.search(r"RW_(\d{8})-(\d{4})\.asc$", member.name)
            if not m:
                continue
            end = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
            out.append((end, _parse_radolan_asc(tf.extractfile(member).read())))
    out.sort(key=lambda t: t[0])
    return out, url


def _sf_day_grid(session, day, cache_dir):
    end = datetime(day.year, day.month, day.day, 23, 50)
    url = f"{RADOLAN_BASE}daily/radolan/recent/bin/raa01-sf_10000-{day:%y%m%d}2350-dwd---bin.gz"
    r = session.get(url, timeout=60)
    if r.status_code == 200:
        _header, values = decode_radolan_binary(gzip.decompress(r.content))
        return end, values, url
    if r.status_code != 404:
        r.raise_for_status()

    hours, src = _rw_day_grids(session, day, cache_dir)
    if not hours:
        raise DwdError(f"No RADOLAN data for {day:%Y-%m-%d}.")
    stack = np.stack([a for _, a in hours])
    total = np.nansum(stack, axis=0)
    total[np.all(np.isnan(stack), axis=0)] = np.nan
    return end, total.astype(np.float32), f"{src} (sum of {len(hours)} hourly RW grids)"


def iter_radolan_grids(product, start_date, end_date, hour=None, session=None,
                       cache_dir=None, progress_cb=None):
    """Yields dict(start, end, data, url) -- naive UTC datetimes bounding
    the accumulation interval, plus a north-up float32 mm array -- for
    every RADOLAN grid of `product` between two dates (inclusive). For RW,
    `hour` (0-23, the hour the interval ends at, i.e. HH:50 UTC) restricts
    the output to a single grid per day."""
    session = session or _session()
    cache_dir = cache_dir or tempfile.gettempdir()
    step = timedelta(hours=RADOLAN_PRODUCTS[product]["step_hours"])
    days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]
    for i, day in enumerate(days):
        if progress_cb:
            progress_cb(i, len(days), f"Fetching RADOLAN {product} for {day:%Y-%m-%d}")
        if product == "SF":
            end, values, url = _sf_day_grid(session, day, cache_dir)
            yield dict(start=end - step, end=end, data=values, url=url)
            continue
        grids, url = _rw_day_grids(session, day, cache_dir)
        for end, values in grids:
            if hour is not None and end.hour != hour:
                continue
            yield dict(start=end - step, end=end, data=values, url=url)
