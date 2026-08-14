"""Builds the DWD station point layer as a real QGIS vector layer (instead
of an embedded web-map widget, this is rendered straight onto the QGIS
canvas, so panning, zooming, other basemaps, the attribute table, and the
usual QGIS tools all work on it for free) and a QgsMapTool that lets the
user click a station on the canvas to select it -- the native-QGIS
equivalent of the R app's Leaflet marker-click handler."""
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsFeature, QgsGeometry,
    QgsPointXY, QgsCategorizedSymbolRenderer, QgsRendererCategory,
    QgsMarkerSymbol, QgsFeatureRequest, QgsRectangle,
)
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import QVariant, pyqtSignal, QPoint

from . import dwd_client as c

LAYER_NAME = "DWD Weather Stations"

FIELDS = [
    ("Stations_id", QVariant.Int),
    ("Stationsname", QVariant.String),
    ("Bundesland", QVariant.String),
    ("Stationshoehe", QVariant.Int),
    ("tier", QVariant.String),
    ("tier_desc", QVariant.String),
    ("has_daily_kl", QVariant.Bool),
    ("from_daily_kl", QVariant.String),
    ("to_daily_kl", QVariant.String),
    ("has_monthly_kl", QVariant.Bool),
    ("from_monthly_kl", QVariant.String),
    ("to_monthly_kl", QVariant.String),
    ("has_hourly_wind", QVariant.Bool),
    ("from_hourly_wind", QVariant.String),
    ("to_hourly_wind", QVariant.String),
    ("has_annual_klindex", QVariant.Bool),
    ("from_annual_klindex", QVariant.String),
    ("to_annual_klindex", QVariant.String),
]


def build_station_layer(stations_df):
    """Turns the `all_stations`-style DataFrame into a QgsVectorLayer,
    styled by data-availability tier with the same 4-colour scheme as the
    original dashboard."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326", LAYER_NAME, "memory")
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

    _apply_tier_renderer(layer)
    return layer


def _apply_tier_renderer(layer):
    categories = []
    for tier, colour in c.TIER_COLOUR.items():
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle", "size": "3.2",
            "color": colour, "outline_color": "white", "outline_width": "0.4",
        })
        categories.append(QgsRendererCategory(tier, symbol, c.TIER_LABEL[tier]))
    renderer = QgsCategorizedSymbolRenderer("tier", categories)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def add_or_replace_station_layer(stations_df):
    """Adds the station layer to the current project, replacing any
    previous copy so repeated 'Load stations' clicks don't pile up."""
    project = QgsProject.instance()
    for lyr in project.mapLayersByName(LAYER_NAME):
        project.removeMapLayer(lyr.id())
    layer = build_station_layer(stations_df)
    project.addMapLayer(layer)
    return layer


def highlight_feature(layer, canvas, station_id):
    """Selects the given station feature and flashes it, so the user can
    see which station they just clicked / jumped to."""
    layer.removeSelection()
    request_ids = [f.id() for f in layer.getFeatures()
                   if f["Stations_id"] == station_id]
    if request_ids:
        layer.selectByIds(request_ids)
        canvas.flashFeatureIds(layer, request_ids)


class StationPickTool(QgsMapTool):
    """Click-to-select map tool: clicking near a station marker on the
    canvas emits stationPicked(Stations_id). `layer_getter` is a callable
    rather than a fixed layer reference because "Load stations" rebuilds
    the layer (add_or_replace_station_layer), which would otherwise leave
    the tool pointing at a removed layer."""
    stationPicked = pyqtSignal(int)

    PICK_TOLERANCE_PX = 8

    def __init__(self, canvas, layer_getter):
        super().__init__(canvas)
        self._layer_getter = layer_getter

    def canvasReleaseEvent(self, event):
        layer = self._layer_getter()
        if layer is None:
            return
        tol = self.PICK_TOLERANCE_PX
        click_point = self.toMapCoordinates(event.pos())
        p1 = self.toMapCoordinates(event.pos() - QPoint(tol, tol))
        p2 = self.toMapCoordinates(event.pos() + QPoint(tol, tol))
        search_rect = QgsRectangle(p1, p2)
        layer_rect = self.canvas().mapSettings().mapToLayerCoordinates(layer, search_rect)

        best_feat, best_dist = None, None
        click_geom = QgsGeometry.fromPointXY(click_point)
        for feat in layer.getFeatures(QgsFeatureRequest(layer_rect)):
            layer_point = self.canvas().mapSettings().layerToMapCoordinates(layer, feat.geometry().asPoint())
            dist = click_geom.distance(QgsGeometry.fromPointXY(layer_point))
            if best_dist is None or dist < best_dist:
                best_dist, best_feat = dist, feat
        if best_feat is not None:
            self.stationPicked.emit(int(best_feat["Stations_id"]))
