"""Exposes DWD Climate Explorer's data-fetching as QGIS Processing
algorithms, so they're usable from Model Builder, the Processing history,
batch mode, and `qgis_process` -- not just the dock widget."""
import os

import pandas as pd
from qgis.core import (
    Qgis, QgsProcessingAlgorithm, QgsProcessingProvider,
    QgsProcessingParameterNumber, QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination, QgsProcessingParameterRasterDestination,
    QgsProcessingParameterFeatureSink, QgsProcessingException,
    QgsFields, QgsField, QgsFeature, QgsFeatureSink,
    QgsCoordinateReferenceSystem, QgsRasterLayer,
    QgsProcessingLayerPostProcessorInterface,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QIcon

from . import dwd_client as c
from . import station_layer as sl
from . import raster_layer as rl

ICON_PATH = os.path.join(os.path.dirname(__file__), "icons", "dwd_icon.svg")

RASTER_VARIABLES = list(c.RASTER_CATALOGUE.keys())
RASTER_VARIABLE_LABELS = [c.RASTER_CATALOGUE[k]["label"] for k in RASTER_VARIABLES]
PERIOD_CODES = list(c.PERIOD_LABELS.keys())
PERIOD_LABELS_LIST = [c.PERIOD_LABELS[k] for k in PERIOD_CODES]
YEAR_CODES = list(c.YEAR_RANGE_LABELS.keys())
YEAR_LABELS_LIST = [c.YEAR_RANGE_LABELS[k] for k in YEAR_CODES]


class _StyleRasterPostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Applies the same colour ramp the dock widget uses, once Processing
    has loaded the output layer. Must be kept alive (self._post_processor)
    until postProcessLayer actually runs -- the interface isn't a QObject,
    so nothing else holds a reference to it."""

    def __init__(self, variable):
        super().__init__()
        self.variable = variable

    def postProcessLayer(self, layer, context, feedback):
        if not isinstance(layer, QgsRasterLayer):
            return
        layer.setCrs(QgsCoordinateReferenceSystem(rl.GRID_CRS))
        rl._style_layer(layer, c.RASTER_CATALOGUE[self.variable]["palette"])


class _StyleDeltaPostProcessor(QgsProcessingLayerPostProcessorInterface):
    def postProcessLayer(self, layer, context, feedback):
        if not isinstance(layer, QgsRasterLayer):
            return
        layer.setCrs(QgsCoordinateReferenceSystem(rl.GRID_CRS))
        rl._style_delta_layer(layer)


def _progress_feedback(feedback):
    def progress(i, n, msg):
        feedback.setProgress(int(100 * i / max(n, 1)))
        feedback.pushInfo(msg)
        if feedback.isCanceled():
            raise QgsProcessingException("Cancelled by user")
    return progress


class DWDProcessingProvider(QgsProcessingProvider):
    def id(self):
        return "dwdclimateexplorer"

    def name(self):
        return "DWD Climate Explorer"

    def icon(self):
        return QIcon(ICON_PATH)

    def loadAlgorithms(self):
        self.addAlgorithm(FetchStationListAlgorithm())
        self.addAlgorithm(FetchStationDataAlgorithm())
        self.addAlgorithm(FetchRasterGridAlgorithm())
        self.addAlgorithm(DeltaRasterAlgorithm())


class _DWDAlgorithmBase(QgsProcessingAlgorithm):
    def group(self):
        return "DWD Climate Explorer"

    def groupId(self):
        return "dwdclimateexplorer"


class FetchStationListAlgorithm(_DWDAlgorithmBase):
    OUTPUT = "OUTPUT"

    def name(self):
        return "fetch_station_list"

    def displayName(self):
        return "Fetch DWD station list"

    def shortHelpString(self):
        return ("Downloads the current DWD station description files and builds a point "
                "layer of every station, colour-coded by data-availability tier "
                "(full / climate-only / basic / none).")

    def createInstance(self):
        return FetchStationListAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, "DWD stations"))

    def processAlgorithm(self, parameters, context, feedback):
        feedback.pushInfo("Fetching station description files from opendata.dwd.de...")
        index = c.build_station_index(progress_cb=_progress_feedback(feedback))
        layer = sl.build_station_layer(index.stations)

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, layer.fields(), Qgis.WkbType.Point, layer.crs())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        for feat in layer.getFeatures():
            sink.addFeature(feat, QgsFeatureSink.FastInsert)

        try:
            details = context.layerToLoadOnCompletionDetails(dest_id)
            processor = _TierStylePostProcessor()
            details.setPostProcessor(processor)
            self._post_processor = processor
        except Exception:
            pass

        feedback.pushInfo(f"{len(index.stations)} stations written.")
        return {self.OUTPUT: dest_id}


class _TierStylePostProcessor(QgsProcessingLayerPostProcessorInterface):
    def postProcessLayer(self, layer, context, feedback):
        try:
            sl._apply_tier_renderer(layer)
        except Exception:
            pass


class FetchStationDataAlgorithm(_DWDAlgorithmBase):
    STATION_ID = "STATION_ID"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    OUTPUT = "OUTPUT"

    def name(self):
        return "fetch_station_data"

    def displayName(self):
        return "Fetch DWD station climate data"

    def shortHelpString(self):
        return ("Downloads daily climate, monthly climate, hourly wind and annual "
                "climate-index data for one DWD station (by station ID). Monthly/wind/"
                "indices are written as CSV to the output folder; the daily climate "
                "series is also loaded as a table.")

    def createInstance(self):
        return FetchStationDataAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterNumber(
            self.STATION_ID, "DWD station ID", Qgis.ProcessingNumberParameterType.Integer,
            defaultValue=3987, minValue=1))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_FOLDER, "Folder for monthly / wind / indices CSVs"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Daily climate table"))

    def processAlgorithm(self, parameters, context, feedback):
        station_id = self.parameterAsInt(parameters, self.STATION_ID, context)
        out_folder = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        os.makedirs(out_folder, exist_ok=True)

        feedback.pushInfo("Looking up station list...")
        index = c.build_station_index(progress_cb=_progress_feedback(feedback))
        rows = index.stations[index.stations["Stations_id"] == station_id]
        if len(rows) == 0:
            raise QgsProcessingException(f"Station ID {station_id} not found.")
        station_row = rows.iloc[0]

        bundle = c.fetch_station_bundle(index, station_row, progress_cb=_progress_feedback(feedback))

        for key, fname in (("data_chart", "monthly_climate.csv"),
                            ("data_wind", "hourly_wind.csv"),
                            ("data_indices", "annual_indices.csv")):
            df = bundle.get(key)
            if df is not None:
                df.to_csv(os.path.join(out_folder, fname), index=False, encoding="utf-8")
                feedback.pushInfo(f"Wrote {fname} ({len(df)} rows)")
            else:
                feedback.pushInfo(f"{fname}: not available for this station")

        daily = bundle.get("data")
        cols = [col for col in daily.columns if col != "_date"] if daily is not None else []
        fields = QgsFields()
        for col in cols:
            fields.append(QgsField(col, QVariant.String))

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, Qgis.WkbType.NoGeometry,
            QgsCoordinateReferenceSystem())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        if daily is not None:
            for _, row in daily.iterrows():
                feat = QgsFeature(fields)
                feat.setAttributes([None if pd.isna(row[col]) else str(row[col]) for col in cols])
                sink.addFeature(feat, QgsFeatureSink.FastInsert)

        return {self.OUTPUT: dest_id, self.OUTPUT_FOLDER: out_folder}


class FetchRasterGridAlgorithm(_DWDAlgorithmBase):
    VARIABLE = "VARIABLE"
    PERIOD = "PERIOD"
    YEAR = "YEAR"
    OUTPUT = "OUTPUT"

    def name(self):
        return "fetch_raster_grid"

    def displayName(self):
        return "Fetch DWD climate grid"

    def shortHelpString(self):
        return ("Downloads a Germany-wide 1km DWD multi-annual climate grid (temperature, "
                "precipitation, sunshine duration, or a day-count index) for the chosen "
                "month/season/year and reference period, styled with the same colour ramp "
                "as the dock widget's Raster Grids tab.")

    def createInstance(self):
        return FetchRasterGridAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            self.VARIABLE, "Variable", options=RASTER_VARIABLE_LABELS, defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.PERIOD, "Period", options=PERIOD_LABELS_LIST,
            defaultValue=PERIOD_CODES.index("17")))
        self.addParameter(QgsProcessingParameterEnum(
            self.YEAR, "Reference period", options=YEAR_LABELS_LIST,
            defaultValue=len(YEAR_CODES) - 1))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, "Output grid"))

    def processAlgorithm(self, parameters, context, feedback):
        variable = RASTER_VARIABLES[self.parameterAsEnum(parameters, self.VARIABLE, context)]
        period = PERIOD_CODES[self.parameterAsEnum(parameters, self.PERIOD, context)]
        year_code = YEAR_CODES[self.parameterAsEnum(parameters, self.YEAR, context)]
        meta = c.RASTER_CATALOGUE[variable]
        if meta["periods"] == "annual_only" and period != "17":
            feedback.pushWarning(f"{meta['label']} only has an annual grid; ignoring the chosen period.")
            period = "17"

        dest = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        feedback.pushInfo("Downloading grid from opendata.dwd.de...")
        _asc_path, url = c.fetch_raster_grid(variable, period, year_code, dest)
        feedback.pushInfo(f"Downloaded {url}")
        rl.write_prj_sidecar(dest)

        try:
            details = context.layerToLoadOnCompletionDetails(dest)
            processor = _StyleRasterPostProcessor(variable)
            details.setPostProcessor(processor)
            self._post_processor = processor
        except Exception:
            pass

        return {self.OUTPUT: dest}


class DeltaRasterAlgorithm(_DWDAlgorithmBase):
    VARIABLE = "VARIABLE"
    PERIOD = "PERIOD"
    YEAR_A = "YEAR_A"
    YEAR_B = "YEAR_B"
    OUTPUT = "OUTPUT"

    def name(self):
        return "delta_raster_grid"

    def displayName(self):
        return "DWD climate change map (delta)"

    def shortHelpString(self):
        return ("Downloads the same DWD climate grid for two reference periods and maps "
                "period B minus period A -- e.g. 1991-2020 minus 1961-1990 mean temperature "
                "shows where and how much Germany has warmed. Styled with a diverging ramp "
                "centred on zero.")

    def createInstance(self):
        return DeltaRasterAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            self.VARIABLE, "Variable", options=RASTER_VARIABLE_LABELS, defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.PERIOD, "Period", options=PERIOD_LABELS_LIST,
            defaultValue=PERIOD_CODES.index("17")))
        self.addParameter(QgsProcessingParameterEnum(
            self.YEAR_A, "Reference period A (subtracted)", options=YEAR_LABELS_LIST,
            defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.YEAR_B, "Reference period B (minus A)", options=YEAR_LABELS_LIST,
            defaultValue=len(YEAR_CODES) - 1))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, "Change map (B - A)"))

    def processAlgorithm(self, parameters, context, feedback):
        variable = RASTER_VARIABLES[self.parameterAsEnum(parameters, self.VARIABLE, context)]
        period = PERIOD_CODES[self.parameterAsEnum(parameters, self.PERIOD, context)]
        year_a = YEAR_CODES[self.parameterAsEnum(parameters, self.YEAR_A, context)]
        year_b = YEAR_CODES[self.parameterAsEnum(parameters, self.YEAR_B, context)]
        if year_a == year_b:
            raise QgsProcessingException("Period A and period B must be different.")
        meta = c.RASTER_CATALOGUE[variable]
        if meta["periods"] == "annual_only" and period != "17":
            period = "17"

        import tempfile
        tmp_a = tempfile.NamedTemporaryFile(suffix=f"_{year_a}.asc", delete=False).name
        tmp_b = tempfile.NamedTemporaryFile(suffix=f"_{year_b}.asc", delete=False).name

        feedback.pushInfo(f"Downloading period A ({c.YEAR_RANGE_LABELS[year_a]})...")
        c.fetch_raster_grid(variable, period, year_a, tmp_a)
        feedback.pushInfo(f"Downloading period B ({c.YEAR_RANGE_LABELS[year_b]})...")
        c.fetch_raster_grid(variable, period, year_b, tmp_b)

        dest = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        feedback.pushInfo("Computing delta with the raster calculator...")
        rl.compute_delta_grid(tmp_a, tmp_b, dest)

        for tmp in (tmp_a, tmp_b):
            try:
                os.remove(tmp)
                os.remove(tmp + ".prj")
            except OSError:
                pass

        try:
            details = context.layerToLoadOnCompletionDetails(dest)
            processor = _StyleDeltaPostProcessor()
            details.setPostProcessor(processor)
            self._post_processor = processor
        except Exception:
            pass

        return {self.OUTPUT: dest}
