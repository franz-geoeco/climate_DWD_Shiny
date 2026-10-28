"""Timeline support: raster series grouped in the layer tree, made
time-aware for QGIS's Temporal Controller where they represent real dates
(RADOLAN, phenology observations), and point-sampled across the whole
series by a map-click tool -- the "click anywhere, get a time series"
interaction the Timeline tab is built around.

QGIS renamed most temporal enums to scoped Qgis.* enums after 3.22; the
small _mode() helpers pick whichever spelling the running QGIS has, so the
plugin's qgisMinimumVersion stays at 3.22."""
from qgis.core import (
    Qgis, QgsProject, QgsDateTimeRange, QgsInterval, QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsPointXY,
    QgsRasterLayerTemporalProperties, QgsVectorLayerTemporalProperties,
    QgsTemporalNavigationObject,
)
from qgis.gui import QgsMapTool
from qgis.PyQt.QtCore import Qt, QDate, QTime, QDateTime, pyqtSignal

# Custom layer property marking a layer as one step of a timeline series,
# so "replace the previous single map" logic elsewhere leaves it alone.
SERIES_PROP = "dwd_climate_explorer/timeline_series"


def _mode(scoped_enum, scoped_member, legacy_owner, legacy_member):
    enum = getattr(Qgis, scoped_enum, None)
    if enum is not None and hasattr(enum, scoped_member):
        return getattr(enum, scoped_member)
    return getattr(legacy_owner, legacy_member)


def to_qdatetime(dt):
    """Naive datetime (meaning UTC, as all DWD grid timestamps are) -> UTC QDateTime."""
    return QDateTime(QDate(dt.year, dt.month, dt.day), QTime(dt.hour, dt.minute), Qt.UTC)


def set_raster_time_range(layer, start, end):
    props = layer.temporalProperties()
    props.setMode(_mode("RasterTemporalMode", "FixedTemporalRange",
                        QgsRasterLayerTemporalProperties, "ModeFixedTemporalRange"))
    # End-exclusive, so back-to-back grids never both match one animation frame.
    props.setFixedTemporalRange(QgsDateTimeRange(to_qdatetime(start), to_qdatetime(end), True, False))
    props.setIsActive(True)


def set_vector_time_fields(layer, start_field, end_field):
    props = layer.temporalProperties()
    props.setMode(_mode("VectorTemporalMode", "FeatureDateTimeStartAndEndFromFields",
                        QgsVectorLayerTemporalProperties, "ModeFeatureDateTimeStartAndEndFromFields"))
    props.setStartField(start_field)
    props.setEndField(end_field)
    props.setIsActive(True)


def configure_temporal_controller(canvas, start, end, frame_seconds, animate=True):
    """Points the Temporal Controller at a newly loaded series: extent,
    frame length, and (optionally) switches it to animated mode at frame 0.
    Returns False if the canvas has no navigation object (e.g. headless)."""
    nav = canvas.temporalController()
    if not isinstance(nav, QgsTemporalNavigationObject):
        return False
    try:
        nav.setTemporalExtents(QgsDateTimeRange(to_qdatetime(start), to_qdatetime(end)))
        nav.setFrameDuration(QgsInterval(frame_seconds))
        if animate:
            nav.setNavigationMode(_mode("TemporalNavigationMode", "Animated",
                                        QgsTemporalNavigationObject, "Animated"))
            nav.setCurrentFrameNumber(0)
    except Exception:
        return False
    return True


def remove_series_group(group_name):
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    old = root.findGroup(group_name)
    if old is not None:
        project.removeMapLayers(old.findLayerIds())
        root.removeChildNode(old)


def add_series_group(layers, group_name, series_id, only_last_visible=False):
    """Adds `layers` (in time order) under one collapsed layer-tree group,
    replacing any previous group of the same name. Time-aware series keep
    every layer visible (the Temporal Controller decides what is drawn);
    series without real dates show only the last step."""
    remove_series_group(group_name)
    project = QgsProject.instance()
    group = project.layerTreeRoot().insertGroup(0, group_name)
    for i, layer in enumerate(layers):
        layer.setCustomProperty(SERIES_PROP, series_id)
        project.addMapLayer(layer, False)
        node = group.addLayer(layer)
        if only_last_visible and i != len(layers) - 1:
            node.setItemVisibilityChecked(False)
    group.setExpanded(False)
    return group


def sample_layers(layer_ids, point, point_crs):
    """Band-1 value of every layer at `point` (given in `point_crs`), None
    where the layer is gone, the point is outside it, or the cell is no-data.
    Transforms are cached per CRS -- a RADOLAN week is 168 layers sharing one."""
    project = QgsProject.instance()
    transforms = {}
    values = []
    for lid in layer_ids:
        layer = project.mapLayer(lid)
        if layer is None:
            values.append(None)
            continue
        key = layer.crs().toWkt()
        if key not in transforms:
            transforms[key] = QgsCoordinateTransform(point_crs, layer.crs(), project)
        try:
            layer_point = transforms[key].transform(point)
        except Exception:
            values.append(None)
            continue
        value, ok = layer.dataProvider().sample(layer_point, 1)
        values.append(float(value) if ok and value == value else None)
    return values


def to_wgs84(point, point_crs):
    xform = QgsCoordinateTransform(point_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                   QgsProject.instance())
    p = xform.transform(point)
    return p.y(), p.x()


class PointPickTool(QgsMapTool):
    """Emits pointPicked(QgsPointXY in canvas CRS) on every click."""
    pointPicked = pyqtSignal(object)

    def canvasReleaseEvent(self, event):
        self.pointPicked.emit(QgsPointXY(self.toMapCoordinates(event.pos())))
