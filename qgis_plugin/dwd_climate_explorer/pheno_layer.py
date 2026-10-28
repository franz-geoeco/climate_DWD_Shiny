"""Builds the DWD phenology station point layer. This is a separate network
from the climate stations in station_layer.py (different station IDs, no
data-availability tiering to render), so it gets its own simple single-style
layer builder, but reuses station_layer.StationPickTool directly for the
click-to-select map interaction rather than duplicating it.

Also builds the all-station *observation* layer: one point per (station,
year) entry date of a chosen phase, coloured by day of year and made
time-aware for the Temporal Controller."""
from datetime import date, timedelta

import numpy as np
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsFeature, QgsGeometry,
    QgsPointXY, QgsMarkerSymbol, QgsSingleSymbolRenderer,
    QgsGraduatedSymbolRenderer, QgsRendererRange,
)
from qgis.PyQt.QtCore import QVariant, QDate

from . import timeline as tl

LAYER_NAME = "DWD Phenology Stations"
MARKER_COLOUR = "#6a51a3"

FIELDS = [
    ("Stations_id", QVariant.Int),
    ("Stationsname", QVariant.String),
    ("Bundesland", QVariant.String),
    ("Stationshoehe", QVariant.Double),
]


def build_pheno_station_layer(stations_df, reporter_label):
    layer = QgsVectorLayer("Point?crs=EPSG:4326", f"{LAYER_NAME} ({reporter_label})", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(name, dtype) for name, dtype in FIELDS])
    layer.updateFields()

    features = []
    for row in stations_df.itertuples(index=False):
        row = row._asdict() if hasattr(row, "_asdict") else dict(zip(stations_df.columns, row))
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(row["geoLaenge"], row["geoBreite"])))
        values = []
        for name, _ in FIELDS:
            v = row.get(name)
            if v is not None and hasattr(v, "item"):
                v = v.item()
            values.append(v)
        feat.setAttributes(values)
        features.append(feat)
    provider.addFeatures(features)
    layer.updateExtents()

    symbol = QgsMarkerSymbol.createSimple({
        "name": "circle", "size": "3.2",
        "color": MARKER_COLOUR, "outline_color": "white", "outline_width": "0.4",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()
    return layer


def add_or_replace_pheno_station_layer(stations_df, reporter_label):
    """Adds the phenology station layer to the current project, replacing
    any previous copy (any layer whose name starts with LAYER_NAME) so
    repeated 'Load phenology stations' clicks, including reporter-type
    switches, don't pile up."""
    project = QgsProject.instance()
    for lyr in list(project.mapLayersByName(f"{LAYER_NAME} ({reporter_label})")):
        project.removeMapLayer(lyr.id())
    for lyr in [l for l in project.mapLayers().values() if l.name().startswith(LAYER_NAME)]:
        project.removeMapLayer(lyr.id())
    layer = build_pheno_station_layer(stations_df, reporter_label)
    project.addMapLayer(layer)
    return layer


# ---------------------------------------------------------------------------
# All-station observation layer (one species + phase, many years)
# ---------------------------------------------------------------------------
OBS_LAYER_NAME = "DWD Phenology Observations"

# Early -> late in the year (viridis), so a "front" moving north / uphill
# reads as a colour gradient even without animating.
DOY_PALETTE = ["#440154", "#414487", "#2a788e", "#22a884", "#7ad151", "#fde725"]

OBS_FIELDS = [
    ("Stations_id", QVariant.Int),
    ("Stationsname", QVariant.String),
    ("Bundesland", QVariant.String),
    ("Referenzjahr", QVariant.Int),
    ("Phase_id", QVariant.Int),
    ("Phase", QVariant.String),
    ("Species", QVariant.String),
    ("obs_date", QVariant.Date),
    # Last day the point stays on the map when the Temporal Controller
    # animates: the end of the observation's calendar year, so stepping
    # through a year accumulates that year's observations, then resets.
    ("visible_to", QVariant.Date),
    ("doy", QVariant.Int),
    ("quality_flag", QVariant.String),
]


def _doy_label(doy):
    d = date(2001, 1, 1) + timedelta(days=int(doy) - 1)  # non-leap reference year
    return d.strftime("%d %b")


def _clean(v):
    """NaN (pandas' missing value) -> None, so QGIS stores a NULL, not 'nan'."""
    return None if v is None or (isinstance(v, float) and v != v) else v


def build_pheno_obs_layer(df, label):
    layer = QgsVectorLayer("Point?crs=EPSG:4326", f"{OBS_LAYER_NAME} ({label})", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField(name, dtype) for name, dtype in OBS_FIELDS])
    layer.updateFields()

    features = []
    # to_dict, not itertuples: itertuples renames the "_date" column.
    for row in df.to_dict("records"):
        d = row["_date"]
        doy = _clean(row.get("Jultag"))
        doy = int(doy) if doy is not None else int(d.dayofyear)
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(row["geoLaenge"], row["geoBreite"])))
        feat.setAttributes([
            int(row["Stations_id"]), _clean(row.get("Stationsname")), _clean(row.get("Bundesland")),
            int(row["Referenzjahr"]), int(row["Phase_id"]), _clean(row.get("Phase_de")),
            _clean(row.get("Objekt_de")), QDate(d.year, d.month, d.day), QDate(d.year, 12, 31),
            doy, _clean(row.get("Eintrittsdatum_QB")),
        ])
        features.append(feat)
    provider.addFeatures(features)
    layer.updateExtents()

    apply_doy_renderer(layer)
    tl.set_vector_time_fields(layer, "obs_date", "visible_to")
    return layer


def apply_doy_renderer(layer):
    """Graduated by entry day-of-year, quantile classes so each colour
    holds roughly the same number of observations."""
    doys = np.array([f["doy"] for f in layer.getFeatures() if f["doy"] is not None], dtype=float)
    if doys.size == 0:
        return
    breaks = np.unique(np.round(np.quantile(doys, np.linspace(0, 1, len(DOY_PALETTE) + 1))))
    if breaks.size == 1:
        breaks = np.array([breaks[0], breaks[0]])
    n = breaks.size - 1
    ranges = []
    for i in range(n):
        lo, hi = breaks[i], breaks[i + 1]
        colour = DOY_PALETTE[round(i * (len(DOY_PALETTE) - 1) / max(n - 1, 1))]
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle", "size": "2.6",
            "color": colour, "outline_color": "#333333", "outline_width": "0.2",
        })
        label = f"DOY {lo:.0f}-{hi:.0f}  ({_doy_label(lo)} - {_doy_label(hi)})"
        ranges.append(QgsRendererRange(lo, hi, symbol, label))
    layer.setRenderer(QgsGraduatedSymbolRenderer("doy", ranges))
    layer.triggerRepaint()


def add_or_replace_pheno_obs_layer(df, label):
    project = QgsProject.instance()
    for lyr in [l for l in project.mapLayers().values() if l.name().startswith(OBS_LAYER_NAME)]:
        project.removeMapLayer(lyr.id())
    layer = build_pheno_obs_layer(df, label)
    project.addMapLayer(layer)
    return layer
