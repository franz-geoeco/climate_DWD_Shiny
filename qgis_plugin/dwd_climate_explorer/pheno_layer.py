"""Builds the DWD phenology station point layer. This is a separate network
from the climate stations in station_layer.py (different station IDs, no
data-availability tiering to render), so it gets its own simple single-style
layer builder, but reuses station_layer.StationPickTool directly for the
click-to-select map interaction rather than duplicating it."""
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsFeature, QgsGeometry,
    QgsPointXY, QgsMarkerSymbol, QgsSingleSymbolRenderer,
)
from qgis.PyQt.QtCore import QVariant

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
