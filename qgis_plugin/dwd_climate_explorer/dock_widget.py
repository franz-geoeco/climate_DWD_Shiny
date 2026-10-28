"""The DWD Climate Explorer dock widget: one tabbed panel that drives the
real QGIS map canvas (station layer + raster layers) and hosts embedded
matplotlib charts for everything the original R/Shiny dashboard plotted.

Network calls in here run synchronously on the UI thread (wrapped in a
wait-cursor + QgsMessageBar busy message + processEvents() ticks). Given
each call is a handful of small HTTP requests worth a few seconds, a full
QThread worker would add real complexity for little benefit -- but it does
mean QGIS's UI is briefly non-interactive while a fetch is in flight.
"""
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from qgis.core import QgsProject
from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QCoreApplication, QDate
from qgis.PyQt.QtGui import QColor, QPixmap, QPainter
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QTabWidget, QPushButton, QLabel, QLineEdit, QDoubleSpinBox, QSpinBox,
    QRadioButton, QButtonGroup, QComboBox, QTextBrowser, QGroupBox,
    QFileDialog, QApplication, QStackedWidget, QCheckBox, QDateEdit, QScrollArea,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure

from . import dwd_client as c
from . import plotting as p
from . import station_layer as sl
from . import raster_layer as rl
from . import pheno_layer as pl
from . import timeline as tl
from . import report
from .range_slider import RangeSlider

CURRENT_YEAR = 2026
PHENO_REPORTER_KEYS = ["annual", "immediate"]
RADOLAN_CACHE_DIR = os.path.join(tempfile.gettempdir(), "dwd_radolan")


def _swatch(colour):
    pix = QPixmap(12, 12)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(QColor("#666666"))
    painter.drawEllipse(1, 1, 10, 10)
    painter.end()
    return pix


def _scrollable(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


def _qdate(d):
    return QDate(d.year, d.month, d.day)


def _pydate(qd):
    return date(qd.year(), qd.month(), qd.day())


class PlotPanel(QWidget):
    """A matplotlib canvas + interactive navigation toolbar (pan / zoom /
    save-as-PNG on every chart -- the R app's plots were static images)."""

    def __init__(self, figsize=(7, 4.2), parent=None):
        super().__init__(parent)
        self.figure = Figure(figsize=figsize, dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)

    def redraw(self):
        self.canvas.draw_idle()


class DWDDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("DWD Climate Explorer", parent)
        self.setObjectName("DWDClimateExplorerDock")
        self.iface = iface
        self.canvas = iface.mapCanvas()

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": c.USER_AGENT})

        self.index = None                 # dwd_client.DWDStationIndex
        self.station_layer_obj = None     # QgsVectorLayer
        self.selected_station = None      # pandas Series (one row of index.stations)
        self.bundle = None                # dict from fetch_station_bundle
        self._prev_map_tool = None

        self.pick_tool = sl.StationPickTool(self.canvas, lambda: self.station_layer_obj)
        self.pick_tool.stationPicked.connect(self.on_station_picked)

        # -- Phenology tab state --------------------------------------------
        self.pheno_stations_df = None       # DataFrame, current reporter type
        self.pheno_station_layer_obj = None
        self.pheno_selected_station = None  # pandas Series
        self.pheno_species_index = None     # dwd_client.PhenoSpeciesIndex, current reporter+category
        self.pheno_data = None              # DataFrame from fetch_phenology_species_data

        self.pheno_pick_tool = sl.StationPickTool(self.canvas, lambda: self.pheno_station_layer_obj)
        self.pheno_pick_tool.stationPicked.connect(self.on_pheno_station_picked)
        # All-station records of the current species, shared by the per-station
        # chart and the observation map so switching between them never
        # re-downloads the (multi-MB) species file.
        self.pheno_records = None
        self.pheno_records_key = None       # (reporter, category, species_token)

        # -- Timeline state ---------------------------------------------------
        self.timeline_series = {}           # series_id -> dict, see _register_timeline_series
        self.timeline_point = None          # (QgsPointXY, canvas CRS) of the last pick
        self.timeline_last = None           # DataFrame of the last chart, for CSV export
        self.timeline_marker = None         # QgsVertexMarker at the picked point
        self.timeline_pick_tool = tl.PointPickTool(self.canvas)
        self.timeline_pick_tool.pointPicked.connect(self.on_timeline_point_picked)

        self._build_ui()

        for tool, button in ((self.pick_tool, self.btn_pick_tool),
                             (self.pheno_pick_tool, self.btn_pheno_pick_tool),
                             (self.timeline_pick_tool, self.btn_timeline_pick)):
            tool.deactivated.connect(lambda b=button: self._uncheck_silently(b))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root_layout = QVBoxLayout(root)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)

        self.tabs.addTab(self._build_stations_tab(), "Stations")
        self.tabs.addTab(self._build_climate_tab(), "Climate")
        self.tabs.addTab(self._build_raster_tab(), "Raster Grids")
        self.tabs.addTab(self._build_radolan_tab(), "RADOLAN")
        self.pheno_tab = _scrollable(self._build_phenology_tab())
        self.tabs.addTab(self.pheno_tab, "Phenology")
        self.timeline_tab = self._build_timeline_tab()
        self.tabs.addTab(self.timeline_tab, "Timeline")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.setWidget(root)

    def _build_stations_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        intro = QLabel(
            "Loads every DWD weather station as a real layer on the QGIS map canvas, "
            "colour-coded by which products are available."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        btn_row = QHBoxLayout()
        self.btn_load_stations = QPushButton("Load / refresh stations")
        self.btn_load_stations.clicked.connect(self.on_load_stations)
        btn_row.addWidget(self.btn_load_stations)

        self.btn_pick_tool = QPushButton("Pick station on map")
        self.btn_pick_tool.setCheckable(True)
        self.btn_pick_tool.setEnabled(False)
        self.btn_pick_tool.toggled.connect(self.on_toggle_pick_tool)
        btn_row.addWidget(self.btn_pick_tool)
        layout.addLayout(btn_row)

        self.station_status_label = QLabel("No stations loaded yet.")
        self.station_status_label.setWordWrap(True)
        layout.addWidget(self.station_status_label)

        legend_box = QGroupBox("Data availability legend")
        legend_layout = QGridLayout(legend_box)
        for i, tier in enumerate(("full", "climate", "basic", "none")):
            icon = QLabel()
            icon.setPixmap(_swatch(c.TIER_COLOUR[tier]))
            legend_layout.addWidget(icon, i, 0)
            legend_layout.addWidget(QLabel(c.TIER_LABEL[tier]), i, 1)
        layout.addWidget(legend_box)

        self.station_info = QTextBrowser()
        self.station_info.setOpenExternalLinks(False)
        self.station_info.setHtml("<i style='color:#888'>Click a station marker on the map "
                                   "(after enabling 'Pick station on map') to see details.</i>")
        self.station_info.setMinimumHeight(160)
        layout.addWidget(self.station_info)

        self.btn_fetch_selected = QPushButton("Fetch data for selected station → Climate tab")
        self.btn_fetch_selected.setEnabled(False)
        self.btn_fetch_selected.clicked.connect(lambda: self.fetch_and_show(self.selected_station))
        layout.addWidget(self.btn_fetch_selected)

        layout.addStretch(1)
        return w

    def _build_climate_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        loc_box = QGroupBox("Location")
        loc_layout = QVBoxLayout(loc_box)

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(w)
        self.radio_city = QRadioButton("City name")
        self.radio_coords = QRadioButton("Coordinates")
        self.radio_map = QRadioButton("Selected station (Stations tab)")
        self.radio_city.setChecked(True)
        for i, rb in enumerate((self.radio_city, self.radio_coords, self.radio_map)):
            self.mode_group.addButton(rb, i)
            mode_row.addWidget(rb)
        loc_layout.addLayout(mode_row)

        self.location_stack = QStackedWidget()
        loc_layout.addWidget(self.location_stack)

        city_w = QWidget()
        city_form = QFormLayout(city_w)
        self.city_edit = QLineEdit()
        self.city_edit.setPlaceholderText("e.g. Freiburg im Breisgau")
        self.radius_spin = QSpinBox()
        self.radius_spin.setRange(1, 200)
        self.radius_spin.setValue(20)
        self.radius_spin.setSuffix(" km")
        city_form.addRow("City:", self.city_edit)
        city_form.addRow("Search radius:", self.radius_spin)
        self.location_stack.addWidget(city_w)

        coords_w = QWidget()
        coords_form = QFormLayout(coords_w)
        self.lat_spin = QDoubleSpinBox()
        self.lat_spin.setRange(47.0, 56.0)
        self.lat_spin.setDecimals(4)
        self.lat_spin.setValue(51.34)
        self.lon_spin = QDoubleSpinBox()
        self.lon_spin.setRange(5.0, 16.0)
        self.lon_spin.setDecimals(4)
        self.lon_spin.setValue(12.38)
        coords_form.addRow("Latitude (°N):", self.lat_spin)
        coords_form.addRow("Longitude (°E):", self.lon_spin)
        self.location_stack.addWidget(coords_w)

        map_w = QWidget()
        map_layout = QVBoxLayout(map_w)
        self.map_pick_label = QLabel("Go to the Stations tab, enable 'Pick station on map', "
                                      "click a station, then come back here and press Search.")
        self.map_pick_label.setWordWrap(True)
        map_layout.addWidget(self.map_pick_label)
        self.location_stack.addWidget(map_w)

        self.mode_group.idClicked.connect(self.location_stack.setCurrentIndex)

        self.btn_search = QPushButton("Fetch climate data")
        self.btn_search.clicked.connect(self.on_search_location)
        loc_layout.addWidget(self.btn_search)

        self.climate_status_label = QLabel("")
        self.climate_status_label.setWordWrap(True)
        loc_layout.addWidget(self.climate_status_label)

        layout.addWidget(loc_box)

        self.btn_report = QPushButton("Generate PDF report for this station")
        self.btn_report.setEnabled(False)
        self.btn_report.clicked.connect(self.on_generate_report)
        layout.addWidget(self.btn_report)

        self.sub_tabs = QTabWidget()
        layout.addWidget(self.sub_tabs)

        self.sub_tabs.addTab(self._build_temp_precip_tab(), "Temperature / Precip.")
        self.sub_tabs.addTab(self._build_wind_tab(), "Wind")
        self.sub_tabs.addTab(self._build_climate_chart_tab(), "30-Year Chart")
        self.sub_tabs.addTab(self._build_trend_tab(), "Trend && Anomaly")
        self.sub_tabs.addTab(self._build_indices_tab(), "Annual Indices")

        return w

    def _build_temp_precip_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        self.temp_var_group = QButtonGroup(w)
        self.radio_tmk = QRadioButton("Temperature")
        self.radio_rsk = QRadioButton("Precipitation")
        self.radio_tmk.setChecked(True)
        self.temp_var_group.addButton(self.radio_tmk, 0)
        self.temp_var_group.addButton(self.radio_rsk, 1)
        row.addWidget(self.radio_tmk)
        row.addWidget(self.radio_rsk)
        row.addStretch(1)
        self.btn_download_climate = QPushButton("Download CSV")
        self.btn_download_climate.clicked.connect(self.on_download_climate)
        row.addWidget(self.btn_download_climate)
        layout.addLayout(row)

        self.temp_range_label = QLabel()
        layout.addWidget(self.temp_range_label)
        self.temp_range = RangeSlider()
        self.temp_range.setBounds(1900, CURRENT_YEAR)
        self.temp_range.setValues(CURRENT_YEAR - 5, CURRENT_YEAR)
        layout.addWidget(self.temp_range)

        self.temp_panel = PlotPanel()
        layout.addWidget(self.temp_panel)

        self.radio_tmk.toggled.connect(self._redraw_temp)
        self.temp_range.rangeChanged.connect(self._redraw_temp)
        self._update_range_label(self.temp_range_label, self.temp_range)
        self.temp_range.rangeChanged.connect(lambda *_: self._update_range_label(self.temp_range_label, self.temp_range))
        return w

    def _build_wind_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        self.wind_var_group = QButtonGroup(w)
        self.radio_wind_speed = QRadioButton("Wind speed")
        self.radio_wind_dir = QRadioButton("Wind direction")
        self.radio_wind_speed.setChecked(True)
        self.wind_var_group.addButton(self.radio_wind_speed, 0)
        self.wind_var_group.addButton(self.radio_wind_dir, 1)
        row.addWidget(self.radio_wind_speed)
        row.addWidget(self.radio_wind_dir)
        row.addStretch(1)
        self.btn_download_wind = QPushButton("Download CSV")
        self.btn_download_wind.clicked.connect(self.on_download_wind)
        row.addWidget(self.btn_download_wind)
        layout.addLayout(row)

        self.wind_range_label = QLabel()
        layout.addWidget(self.wind_range_label)
        self.wind_range = RangeSlider()
        self.wind_range.setBounds(1900, CURRENT_YEAR)
        self.wind_range.setValues(CURRENT_YEAR - 5, CURRENT_YEAR)
        layout.addWidget(self.wind_range)

        self.wind_panel = PlotPanel()
        layout.addWidget(self.wind_panel)

        self.radio_wind_speed.toggled.connect(self._redraw_wind)
        self.wind_range.rangeChanged.connect(self._redraw_wind)
        self._update_range_label(self.wind_range_label, self.wind_range)
        self.wind_range.rangeChanged.connect(lambda *_: self._update_range_label(self.wind_range_label, self.wind_range))
        return w

    def _build_climate_chart_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        info = QLabel("Classic climate diagram: 30-year monthly temperature & precipitation normals.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.chart_range_label = QLabel()
        layout.addWidget(self.chart_range_label)
        self.chart_range = RangeSlider()
        self.chart_range.setBounds(1900, CURRENT_YEAR - 1)
        self.chart_range.setValues(1970, 2000)
        layout.addWidget(self.chart_range)

        self.chart_panel = PlotPanel()
        layout.addWidget(self.chart_panel)

        self.chart_range.rangeChanged.connect(self._redraw_chart)
        self._update_range_label(self.chart_range_label, self.chart_range)
        self.chart_range.rangeChanged.connect(lambda *_: self._update_range_label(self.chart_range_label, self.chart_range))
        return w

    def _build_trend_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        info = QLabel("Annual mean temperature, linear trend, and anomaly vs. the 1961-1990 baseline.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.trend_range_label = QLabel()
        layout.addWidget(self.trend_range_label)
        self.trend_range = RangeSlider()
        self.trend_range.setBounds(1900, CURRENT_YEAR)
        self.trend_range.setValues(1960, CURRENT_YEAR)
        layout.addWidget(self.trend_range)

        self.trend_panel = PlotPanel()
        layout.addWidget(self.trend_panel)

        self.trend_range.rangeChanged.connect(self._redraw_trend)
        self._update_range_label(self.trend_range_label, self.trend_range)
        self.trend_range.rangeChanged.connect(lambda *_: self._update_range_label(self.trend_range_label, self.trend_range))
        return w

    def _build_indices_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        row.addWidget(QLabel("Index:"))
        self.indices_combo = QComboBox()
        for key, label in p.INDEX_LABELS.items():
            self.indices_combo.addItem(label, key)
        row.addWidget(self.indices_combo, 1)
        layout.addLayout(row)

        self.indices_range_label = QLabel()
        layout.addWidget(self.indices_range_label)
        self.indices_range = RangeSlider()
        self.indices_range.setBounds(1900, CURRENT_YEAR)
        self.indices_range.setValues(1960, CURRENT_YEAR)
        layout.addWidget(self.indices_range)

        self.indices_panel = PlotPanel()
        layout.addWidget(self.indices_panel)

        self.indices_combo.currentIndexChanged.connect(self._redraw_indices)
        self.indices_range.rangeChanged.connect(self._redraw_indices)
        self._update_range_label(self.indices_range_label, self.indices_range)
        self.indices_range.rangeChanged.connect(lambda *_: self._update_range_label(self.indices_range_label, self.indices_range))
        return w

    def _build_raster_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Downloads a Germany-wide 1km x 1km interpolated DWD climate grid and adds it to the "
            "map canvas as a styled raster layer, lined up with the station layer automatically."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.raster_var_combo = QComboBox()
        for key, meta in c.RASTER_CATALOGUE.items():
            self.raster_var_combo.addItem(meta["label"], key)
        form.addRow("Variable:", self.raster_var_combo)

        self.raster_period_combo = QComboBox()
        form.addRow("Period:", self.raster_period_combo)

        self.raster_year_combo = QComboBox()
        for key, label in c.YEAR_RANGE_LABELS.items():
            self.raster_year_combo.addItem(label, key)
        self.raster_year_combo.setCurrentIndex(len(c.YEAR_RANGE_LABELS) - 1)
        form.addRow("Reference period:", self.raster_year_combo)

        self.raster_timeline_check = QCheckBox(
            "Timeline: load a series of grids, then click the map for a chart")
        form.addRow(self.raster_timeline_check)
        self.raster_series_combo = QComboBox()
        self.raster_series_combo.addItem("All reference periods (for the period above)", "periods")
        self.raster_series_combo.addItem("12 months (of the reference period above)", "months")
        self.raster_series_combo.setEnabled(False)
        form.addRow("Series over:", self.raster_series_combo)
        self.raster_timeline_check.toggled.connect(self.raster_series_combo.setEnabled)
        layout.addLayout(form)

        self.raster_var_combo.currentIndexChanged.connect(self._update_raster_periods)
        self._update_raster_periods()

        self.btn_load_raster = QPushButton("Load raster map")
        self.btn_load_raster.clicked.connect(self.on_load_raster)
        layout.addWidget(self.btn_load_raster)

        self.raster_status_label = QLabel("")
        self.raster_status_label.setWordWrap(True)
        layout.addWidget(self.raster_status_label)

        delta_box = QGroupBox("Change map (delta)")
        delta_layout = QVBoxLayout(delta_box)
        delta_info = QLabel(
            "Downloads the variable/period above for two reference periods and maps "
            "period B minus period A -- e.g. how much warmer/wetter Germany got."
        )
        delta_info.setWordWrap(True)
        delta_layout.addWidget(delta_info)

        delta_form = QFormLayout()
        self.delta_year_a_combo = QComboBox()
        self.delta_year_b_combo = QComboBox()
        for key, label in c.YEAR_RANGE_LABELS.items():
            self.delta_year_a_combo.addItem(label, key)
            self.delta_year_b_combo.addItem(label, key)
        self.delta_year_a_combo.setCurrentIndex(0)
        self.delta_year_b_combo.setCurrentIndex(len(c.YEAR_RANGE_LABELS) - 1)
        delta_form.addRow("Period A (subtracted):", self.delta_year_a_combo)
        delta_form.addRow("Period B (minus A):", self.delta_year_b_combo)
        delta_layout.addLayout(delta_form)

        self.btn_compute_delta = QPushButton("Compute change map")
        self.btn_compute_delta.clicked.connect(self.on_compute_delta)
        delta_layout.addWidget(self.btn_compute_delta)

        self.delta_status_label = QLabel("")
        self.delta_status_label.setWordWrap(True)
        delta_layout.addWidget(self.delta_status_label)

        layout.addWidget(delta_box)

        layout.addStretch(1)
        return w

    def _build_radolan_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "RADOLAN: radar-based, rain-gauge-adjusted precipitation for all of Germany on a "
            "1km x 1km grid (2005 onwards). Times are UTC and mark the end of each "
            "accumulation interval. Recent days download quickly; dates before the current "
            "'recent' window come from monthly archives (~40 MB each, cached in the temp folder)."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.radolan_product_combo = QComboBox()
        for key, meta in c.RADOLAN_PRODUCTS.items():
            self.radolan_product_combo.addItem(meta["label"], key)
        self.radolan_product_combo.setCurrentIndex(list(c.RADOLAN_PRODUCTS).index("SF"))
        form.addRow("Product:", self.radolan_product_combo)

        self.radolan_timeline_check = QCheckBox(
            "Timeline: date range as time-aware layers, click the map for a chart")
        form.addRow(self.radolan_timeline_check)

        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        self.radolan_start_label = QLabel("Date (UTC):")
        self.radolan_start_edit = QDateEdit(_qdate(yesterday))
        self.radolan_end_label = QLabel("End date (UTC):")
        self.radolan_end_edit = QDateEdit(_qdate(yesterday))
        for edit in (self.radolan_start_edit, self.radolan_end_edit):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setDateRange(QDate(2005, 1, 1), _qdate(yesterday))
        form.addRow(self.radolan_start_label, self.radolan_start_edit)
        form.addRow(self.radolan_end_label, self.radolan_end_edit)

        self.radolan_hour_label = QLabel("Hour ending (UTC):")
        self.radolan_hour_spin = QSpinBox()
        self.radolan_hour_spin.setRange(0, 23)
        self.radolan_hour_spin.setValue(12)
        self.radolan_hour_spin.setSuffix(":50")
        form.addRow(self.radolan_hour_label, self.radolan_hour_spin)
        layout.addLayout(form)

        self.radolan_product_combo.currentIndexChanged.connect(self._update_radolan_inputs)
        self.radolan_timeline_check.toggled.connect(self._update_radolan_inputs)
        self._update_radolan_inputs()

        self.btn_load_radolan = QPushButton("Load RADOLAN")
        self.btn_load_radolan.clicked.connect(self.on_load_radolan)
        layout.addWidget(self.btn_load_radolan)

        self.radolan_status_label = QLabel("")
        self.radolan_status_label.setWordWrap(True)
        layout.addWidget(self.radolan_status_label)

        layout.addStretch(1)
        return w

    def _update_radolan_inputs(self, *_):
        product = self.radolan_product_combo.currentData()
        timeline = self.radolan_timeline_check.isChecked()
        self.radolan_start_label.setText("Start date (UTC):" if timeline else "Date (UTC):")
        for wdg in (self.radolan_end_label, self.radolan_end_edit):
            wdg.setVisible(timeline)
        for wdg in (self.radolan_hour_label, self.radolan_hour_spin):
            wdg.setVisible(product == "RW" and not timeline)

    def _build_timeline_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Raster series loaded with 'Timeline' ticked (Raster Grids or RADOLAN tab) are "
            "listed here. With 'Pick point on map' on, click anywhere on the canvas to chart "
            "the value of every grid in the series at that spot. RADOLAN timelines are also "
            "time-aware layers: View > Panels > Temporal Controller animates them."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        row = QHBoxLayout()
        row.addWidget(QLabel("Series:"))
        self.timeline_combo = QComboBox()
        row.addWidget(self.timeline_combo, 1)
        layout.addLayout(row)

        btn_row = QHBoxLayout()
        self.btn_timeline_pick = QPushButton("Pick point on map")
        self.btn_timeline_pick.setCheckable(True)
        self.btn_timeline_pick.setEnabled(False)
        self.btn_timeline_pick.toggled.connect(
            lambda checked: self._toggle_map_tool(self.btn_timeline_pick, self.timeline_pick_tool, checked))
        btn_row.addWidget(self.btn_timeline_pick)
        self.btn_download_timeline = QPushButton("Download CSV")
        self.btn_download_timeline.setEnabled(False)
        self.btn_download_timeline.clicked.connect(self.on_download_timeline)
        btn_row.addWidget(self.btn_download_timeline)
        layout.addLayout(btn_row)

        self.timeline_status_label = QLabel("No timeline series loaded yet.")
        self.timeline_status_label.setWordWrap(True)
        layout.addWidget(self.timeline_status_label)

        self.timeline_panel = PlotPanel()
        layout.addWidget(self.timeline_panel)

        self.timeline_combo.currentIndexChanged.connect(self._redraw_timeline)
        return w

    def _build_phenology_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        info = QLabel(
            "Plant development-stage observations (sowing, flowering, ripening, leaf colouring, "
            "...) from DWD's phenology network -- a separate set of stations from the climate "
            "network on the other tabs."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        source_box = QGroupBox("Data source")
        source_layout = QFormLayout(source_box)

        reporter_row = QHBoxLayout()
        self.pheno_reporter_group = QButtonGroup(w)
        self.radio_pheno_annual = QRadioButton("Annual reporters")
        self.radio_pheno_immediate = QRadioButton("Immediate reporters")
        self.radio_pheno_annual.setChecked(True)
        self.pheno_reporter_group.addButton(self.radio_pheno_annual, 0)
        self.pheno_reporter_group.addButton(self.radio_pheno_immediate, 1)
        reporter_row.addWidget(self.radio_pheno_annual)
        reporter_row.addWidget(self.radio_pheno_immediate)
        source_layout.addRow("Reporter type:", reporter_row)

        self.pheno_category_combo = QComboBox()
        source_layout.addRow("Category:", self.pheno_category_combo)

        self.pheno_species_combo = QComboBox()
        source_layout.addRow("Species:", self.pheno_species_combo)

        layout.addWidget(source_box)

        self.pheno_reporter_group.idClicked.connect(self.on_pheno_reporter_changed)
        self.pheno_category_combo.currentIndexChanged.connect(self.on_pheno_category_changed)
        self.pheno_species_combo.currentIndexChanged.connect(self._reset_pheno_obs_phases)

        obs_box = QGroupBox("Observation map (all stations)")
        obs_layout = QVBoxLayout(obs_box)
        obs_info = QLabel(
            "Maps one phase's entry date at every station as a point layer coloured by day of "
            "year. The layer is time-aware: in the Temporal Controller each observation appears "
            "on its entry date and stays until the end of that year, so animating a spring "
            "shows e.g. flowering spreading across Germany."
        )
        obs_info.setWordWrap(True)
        obs_layout.addWidget(obs_info)

        obs_form = QFormLayout()
        phase_row = QHBoxLayout()
        self.pheno_obs_phase_combo = QComboBox()
        phase_row.addWidget(self.pheno_obs_phase_combo, 1)
        self.btn_pheno_obs_phases = QPushButton("Load phases")
        self.btn_pheno_obs_phases.clicked.connect(self.on_load_pheno_obs_phases)
        phase_row.addWidget(self.btn_pheno_obs_phases)
        obs_form.addRow("Phase:", phase_row)

        years_row = QHBoxLayout()
        self.pheno_obs_from_spin = QSpinBox()
        self.pheno_obs_to_spin = QSpinBox()
        for spin, value in ((self.pheno_obs_from_spin, CURRENT_YEAR - 29),
                            (self.pheno_obs_to_spin, CURRENT_YEAR)):
            spin.setRange(1900, CURRENT_YEAR)
            spin.setValue(value)
        years_row.addWidget(self.pheno_obs_from_spin)
        years_row.addWidget(QLabel("to"))
        years_row.addWidget(self.pheno_obs_to_spin)
        years_row.addStretch(1)
        obs_form.addRow("Years:", years_row)
        obs_layout.addLayout(obs_form)

        self.btn_pheno_obs_map = QPushButton("Add observation layer to map")
        self.btn_pheno_obs_map.clicked.connect(self.on_add_pheno_obs_layer)
        obs_layout.addWidget(self.btn_pheno_obs_map)

        self.pheno_obs_status_label = QLabel("")
        self.pheno_obs_status_label.setWordWrap(True)
        obs_layout.addWidget(self.pheno_obs_status_label)

        layout.addWidget(obs_box)

        loc_box = QGroupBox("Station")
        loc_layout = QVBoxLayout(loc_box)

        mode_row = QHBoxLayout()
        self.pheno_mode_group = QButtonGroup(w)
        self.radio_pheno_city = QRadioButton("City name")
        self.radio_pheno_coords = QRadioButton("Coordinates")
        self.radio_pheno_map = QRadioButton("Pick on map")
        self.radio_pheno_city.setChecked(True)
        for i, rb in enumerate((self.radio_pheno_city, self.radio_pheno_coords, self.radio_pheno_map)):
            self.pheno_mode_group.addButton(rb, i)
            mode_row.addWidget(rb)
        loc_layout.addLayout(mode_row)

        self.pheno_location_stack = QStackedWidget()
        loc_layout.addWidget(self.pheno_location_stack)

        pcity_w = QWidget()
        pcity_form = QFormLayout(pcity_w)
        self.pheno_city_edit = QLineEdit()
        self.pheno_city_edit.setPlaceholderText("e.g. Freiburg im Breisgau")
        self.pheno_radius_spin = QSpinBox()
        self.pheno_radius_spin.setRange(1, 300)
        self.pheno_radius_spin.setValue(30)
        self.pheno_radius_spin.setSuffix(" km")
        pcity_form.addRow("City:", self.pheno_city_edit)
        pcity_form.addRow("Search radius:", self.pheno_radius_spin)
        self.pheno_location_stack.addWidget(pcity_w)

        pcoords_w = QWidget()
        pcoords_form = QFormLayout(pcoords_w)
        self.pheno_lat_spin = QDoubleSpinBox()
        self.pheno_lat_spin.setRange(47.0, 56.0)
        self.pheno_lat_spin.setDecimals(4)
        self.pheno_lat_spin.setValue(51.34)
        self.pheno_lon_spin = QDoubleSpinBox()
        self.pheno_lon_spin.setRange(5.0, 16.0)
        self.pheno_lon_spin.setDecimals(4)
        self.pheno_lon_spin.setValue(12.38)
        pcoords_form.addRow("Latitude (°N):", self.pheno_lat_spin)
        pcoords_form.addRow("Longitude (°E):", self.pheno_lon_spin)
        self.pheno_location_stack.addWidget(pcoords_w)

        pmap_w = QWidget()
        pmap_layout = QVBoxLayout(pmap_w)
        pmap_btn_row = QHBoxLayout()
        self.btn_load_pheno_stations = QPushButton("Load / refresh phenology stations")
        self.btn_load_pheno_stations.clicked.connect(self.on_load_pheno_stations)
        pmap_btn_row.addWidget(self.btn_load_pheno_stations)
        self.btn_pheno_pick_tool = QPushButton("Pick station on map")
        self.btn_pheno_pick_tool.setCheckable(True)
        self.btn_pheno_pick_tool.setEnabled(False)
        self.btn_pheno_pick_tool.toggled.connect(self.on_toggle_pheno_pick_tool)
        pmap_btn_row.addWidget(self.btn_pheno_pick_tool)
        pmap_layout.addLayout(pmap_btn_row)
        self.pheno_map_pick_label = QLabel("Load the phenology stations, enable 'Pick station on "
                                            "map', then click a station marker on the canvas.")
        self.pheno_map_pick_label.setWordWrap(True)
        pmap_layout.addWidget(self.pheno_map_pick_label)
        self.pheno_location_stack.addWidget(pmap_w)

        self.pheno_mode_group.idClicked.connect(self.pheno_location_stack.setCurrentIndex)

        self.btn_pheno_fetch = QPushButton("Fetch phenology data")
        self.btn_pheno_fetch.clicked.connect(self.on_fetch_phenology)
        loc_layout.addWidget(self.btn_pheno_fetch)

        self.pheno_status_label = QLabel("")
        self.pheno_status_label.setWordWrap(True)
        loc_layout.addWidget(self.pheno_status_label)

        layout.addWidget(loc_box)

        chart_row = QHBoxLayout()
        chart_row.addWidget(QLabel("Phase:"))
        self.pheno_phase_combo = QComboBox()
        chart_row.addWidget(self.pheno_phase_combo, 1)
        self.btn_download_pheno = QPushButton("Download CSV")
        self.btn_download_pheno.setEnabled(False)
        self.btn_download_pheno.clicked.connect(self.on_download_phenology)
        chart_row.addWidget(self.btn_download_pheno)
        layout.addLayout(chart_row)

        self.pheno_range_label = QLabel()
        layout.addWidget(self.pheno_range_label)
        self.pheno_range = RangeSlider()
        self.pheno_range.setBounds(1900, CURRENT_YEAR)
        self.pheno_range.setValues(1960, CURRENT_YEAR)
        layout.addWidget(self.pheno_range)

        self.pheno_panel = PlotPanel()
        self.pheno_panel.setMinimumHeight(360)  # the tab scrolls, so give the chart real height
        layout.addWidget(self.pheno_panel)

        self.pheno_phase_combo.currentIndexChanged.connect(self._redraw_pheno)
        self.pheno_range.rangeChanged.connect(self._redraw_pheno)
        self._update_range_label(self.pheno_range_label, self.pheno_range)
        self.pheno_range.rangeChanged.connect(lambda *_: self._update_range_label(self.pheno_range_label, self.pheno_range))

        self._populate_pheno_categories()
        return w

    def _update_raster_periods(self):
        variable = self.raster_var_combo.currentData()
        meta = c.RASTER_CATALOGUE[variable]
        self.raster_period_combo.clear()
        if meta["periods"] == "annual_only":
            self.raster_period_combo.addItem("Annual mean", "17")
        else:
            for key, label in c.PERIOD_LABELS.items():
                self.raster_period_combo.addItem(label, key)
            self.raster_period_combo.setCurrentIndex(list(c.PERIOD_LABELS).index("17"))

    def _update_range_label(self, label, slider):
        lo, hi = slider.values()
        label.setText(f"Years: {lo} - {hi}")

    # ------------------------------------------------------------------
    # Busy-cursor / message-bar helpers
    # ------------------------------------------------------------------
    def _run_busy(self, fn, *args, **kwargs):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return fn(*args, **kwargs)
        finally:
            QApplication.restoreOverrideCursor()

    def _progress(self, i, n, msg):
        self.station_status_label.setText(f"{msg} ({i + 1}/{n})")
        QCoreApplication.processEvents()

    def _warn(self, msg):
        self.iface.messageBar().pushWarning("DWD Climate Explorer", msg)

    def _error(self, msg):
        self.iface.messageBar().pushCritical("DWD Climate Explorer", msg)

    # ------------------------------------------------------------------
    # Stations tab
    # ------------------------------------------------------------------
    def on_load_stations(self):
        self.btn_load_stations.setEnabled(False)
        try:
            self.index = self._run_busy(c.build_station_index, progress_cb=self._progress)
        except Exception as e:
            self._error(f"Could not load station list: {e}")
            return
        finally:
            self.btn_load_stations.setEnabled(True)

        self.station_layer_obj = sl.add_or_replace_station_layer(self.index.stations)
        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            self.station_layer_obj, self.station_layer_obj.extent()))
        self.canvas.refresh()
        n = len(self.index.stations)
        self.station_status_label.setText(
            f"{n} stations loaded. Click 'Pick station on map' and then click a marker."
        )
        self.btn_pick_tool.setEnabled(True)

    def on_toggle_pick_tool(self, checked):
        self._toggle_map_tool(self.btn_pick_tool, self.pick_tool, checked)

    def _toggle_map_tool(self, button, tool, checked):
        """Shared by all three pick buttons: only one can be active, and
        unchecking restores whatever map tool was active before -- unless
        the user has meanwhile switched to another tool themselves."""
        if checked:
            for other in (self.btn_pick_tool, self.btn_pheno_pick_tool, self.btn_timeline_pick):
                if other is not button and other.isChecked():
                    other.setChecked(False)
            self._prev_map_tool = self.canvas.mapTool()
            self.canvas.setMapTool(tool)
        elif self.canvas.mapTool() is tool and self._prev_map_tool is not None:
            self.canvas.setMapTool(self._prev_map_tool)

    def _uncheck_silently(self, button):
        """A pick tool was replaced by another map tool (e.g. the user
        clicked Pan): reflect that on its button without restoring anything."""
        button.blockSignals(True)
        button.setChecked(False)
        button.blockSignals(False)

    def on_station_picked(self, station_id):
        if self.index is None:
            return
        rows = self.index.stations[self.index.stations["Stations_id"] == station_id]
        if len(rows) == 0:
            return
        self.selected_station = rows.iloc[0]
        sl.highlight_feature(self.station_layer_obj, self.canvas, station_id)
        self._render_station_info(self.selected_station)
        self.btn_fetch_selected.setEnabled(True)
        self.map_pick_label.setText(
            f"Using station: {self.selected_station['Stationsname']} (ID {station_id})")

    def _render_station_info(self, st):
        def row(has, label, frm, to):
            if has and frm:
                return f"<tr><td style='color:#2ca02c;'>&#10004; {label}</td><td style='padding-left:12px;color:#555;'>{frm} to {to}</td></tr>"
            return f"<tr><td style='color:#d62728;'>&#10008; {label}</td><td style='padding-left:12px;color:#aaa;'>not available</td></tr>"

        tier_col = c.TIER_COLOUR[st["tier"]]
        html = f"""
        <div style='border-left:5px solid {tier_col}; padding-left:8px;'>
        <h4 style='margin:2px 0;'>{st['Stationsname']} <small style='color:#666'>(ID {st['Stations_id']})</small></h4>
        <p><b>State:</b> {st['Bundesland']} &nbsp; <b>Elevation:</b> {st['Stationshoehe']} m a.s.l.<br>
        <b>Coords:</b> {st['geoBreite']:.4f}°N, {st['geoLaenge']:.4f}°E</p>
        <p><b>Overall tier:</b> <span style='color:{tier_col}; font-weight:bold;'>{st['tier_desc']}</span></p>
        <table style='font-size:12px;'>
        {row(st['has_daily_kl'], 'Daily climate', st['from_daily_kl'], st['to_daily_kl'])}
        {row(st['has_monthly_kl'], 'Monthly climate chart', st['from_monthly_kl'], st['to_monthly_kl'])}
        {row(st['has_hourly_wind'], 'Hourly wind', st['from_hourly_wind'], st['to_hourly_wind'])}
        {row(st['has_annual_klindex'], 'Annual climate indices', st['from_annual_klindex'], st['to_annual_klindex'])}
        </table>
        </div>
        """
        self.station_info.setHtml(html)

    # ------------------------------------------------------------------
    # Climate tab
    # ------------------------------------------------------------------
    def on_search_location(self):
        mode = self.mode_group.checkedId()
        try:
            if mode == 2:  # map pick
                if self.selected_station is None:
                    self._warn("Pick a station on the map first (Stations tab).")
                    return
                station_row = self.selected_station
            elif mode == 0:  # city
                city = self.city_edit.text().strip()
                if not city:
                    self._warn("Enter a city name.")
                    return
                geocoded = self._run_busy(c.geocode_city, city, session=self.session)
                if geocoded is None:
                    self._error(f"Could not geocode '{city}'.")
                    return
                lat, lon, _display = geocoded
                station_row = self._nearest_station(lat, lon, self.radius_spin.value())
                if station_row is None:
                    return
            else:  # coords
                lat, lon = self.lat_spin.value(), self.lon_spin.value()
                station_row = self._nearest_station(lat, lon, self.radius_spin.value())
                if station_row is None:
                    return
        except Exception as e:
            self._error(f"Location lookup failed: {e}")
            return

        self.fetch_and_show(station_row)

    def _nearest_station(self, lat, lon, radius_km):
        if self.index is None:
            self._run_busy(self._ensure_index)
        near = c.nearby_stations(self.index.stations, lat, lon, radius_km)
        if len(near) == 0:
            self._warn(f"No station with daily climate data within {radius_km} km.")
            return None
        return near.iloc[0]

    def _ensure_index(self):
        self.index = c.build_station_index(progress_cb=self._progress)

    def fetch_and_show(self, station_row):
        if station_row is None:
            return
        self.tabs.setCurrentIndex(1)
        self.climate_status_label.setText(f"Fetching data for {station_row['Stationsname']}...")
        QCoreApplication.processEvents()
        try:
            self.bundle = self._run_busy(
                c.fetch_station_bundle, self.index, station_row,
                session=self.session, progress_cb=self._climate_progress,
            )
        except Exception as e:
            self._error(f"Could not fetch station data: {e}")
            return
        self.selected_station = station_row
        self._sync_slider_bounds()
        self.climate_status_label.setText(f"Showing {station_row['Stationsname']} "
                                           f"(ID {int(station_row['Stations_id'])})")
        self._redraw_all()
        self.btn_report.setEnabled(True)

    def _climate_progress(self, i, n, msg):
        self.climate_status_label.setText(f"{msg} ({i + 1}/{n})")
        QCoreApplication.processEvents()

    def _sync_slider_bounds(self):
        clim = self.bundle.get("data")
        if clim is not None and len(clim) and "_date" in clim.columns:
            years = clim["_date"].dt.year.dropna()
            if len(years):
                lo, hi = int(years.min()), int(years.max())
                self.temp_range.setBounds(lo, hi)
                self.temp_range.setValues(max(lo, hi - 4), hi)
                self.trend_range.setBounds(lo, hi)
                self.trend_range.setValues(lo, hi)

        wind = self.bundle.get("data_wind")
        if wind is not None and len(wind) and "_date" in wind.columns:
            years = wind["_date"].dt.year.dropna()
            if len(years):
                lo, hi = int(years.min()), int(years.max())
                self.wind_range.setBounds(lo, hi)
                self.wind_range.setValues(max(lo, hi - 4), hi)

        chart = self.bundle.get("data_chart")
        if chart is not None and len(chart) and "MESS_DATUM_BEGINN" in chart.columns:
            yrs = (chart["MESS_DATUM_BEGINN"].astype("int64") // 10000)
            if len(yrs):
                lo, hi = int(yrs.min()), int(yrs.max())
                self.chart_range.setBounds(lo, hi)
                end = hi
                start = max(lo, end - 29)
                self.chart_range.setValues(start, end)

        indices = self.bundle.get("data_indices")
        if indices is not None and len(indices) and "MESS_DATUM_BEGINN" in indices.columns:
            yrs = (indices["MESS_DATUM_BEGINN"].astype("int64") // 10000)
            if len(yrs):
                lo, hi = int(yrs.min()), int(yrs.max())
                self.indices_range.setBounds(lo, hi)
                self.indices_range.setValues(max(lo, hi - 29), hi)

    def _redraw_all(self):
        self._redraw_temp()
        self._redraw_wind()
        self._redraw_chart()
        self._redraw_trend()
        self._redraw_indices()

    def _station_geo(self):
        st = self.selected_station
        return st["Stationsname"], st["geoBreite"], st["geoLaenge"], st["Stationshoehe"]

    def _redraw_temp(self, *_):
        if self.bundle is None:
            return
        var = "TMK" if self.radio_tmk.isChecked() else "RSK"
        name, lat, lon, height = self._station_geo()
        p.plot_temperature_precip(self.temp_panel.figure, self.bundle["data"], var,
                                   self.temp_range.values(), name, lat, lon, height)
        self.temp_panel.redraw()

    def _redraw_wind(self, *_):
        if self.bundle is None:
            return
        var = "F" if self.radio_wind_speed.isChecked() else "D"
        name, lat, lon, height = self._station_geo()
        p.plot_wind(self.wind_panel.figure, self.bundle["data_wind"], var,
                    self.wind_range.values(), name, lat, lon, height)
        self.wind_panel.redraw()

    def _redraw_chart(self, *_):
        if self.bundle is None:
            return
        name, lat, lon, height = self._station_geo()
        p.plot_climate_chart(self.chart_panel.figure, self.bundle["data_chart"],
                              self.chart_range.values(), name, lat, lon, height)
        self.chart_panel.redraw()

    def _redraw_trend(self, *_):
        if self.bundle is None:
            return
        name, *_rest = self._station_geo()
        p.plot_trend_anomaly(self.trend_panel.figure, self.bundle["data"],
                              self.trend_range.values(), name)
        self.trend_panel.redraw()

    def _redraw_indices(self, *_):
        if self.bundle is None:
            return
        name, *_rest = self._station_geo()
        var_col = self.indices_combo.currentData()
        p.plot_indices(self.indices_panel.figure, self.bundle["data_indices"], var_col,
                        self.indices_range.values(), name)
        self.indices_panel.redraw()

    def on_download_climate(self):
        if self.bundle is None or self.bundle.get("data") is None:
            self._warn("No climate data loaded yet.")
            return
        self._save_csv(self.bundle["data"], "climate")

    def on_download_wind(self):
        if self.bundle is None or self.bundle.get("data_wind") is None:
            self._warn("No wind data loaded yet.")
            return
        self._save_csv(self.bundle["data_wind"], "wind")

    def _save_csv(self, df, kind, station_name=None):
        if station_name is None:
            station_name = self.selected_station["Stationsname"] if self.selected_station is not None else None
        station = station_name if station_name is not None else "station"
        safe_name = "".join(ch if ch.isalnum() else "_" for ch in station)
        default = f"{kind}_data_{safe_name}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", default, "CSV files (*.csv)")
        if not path:
            return
        df.to_csv(path, index=False, encoding="utf-8")
        self.iface.messageBar().pushSuccess("DWD Climate Explorer", f"Saved {path}")

    # ------------------------------------------------------------------
    # PDF report
    # ------------------------------------------------------------------
    def on_generate_report(self):
        if self.bundle is None or self.selected_station is None:
            self._warn("Fetch data for a station first.")
            return

        safe_name = "".join(ch if ch.isalnum() else "_" for ch in self.selected_station["Stationsname"])
        default = f"DWD_report_{safe_name}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF report", default, "PDF files (*.pdf)")
        if not path:
            return

        # Ensures the report's Germany-wide overview map has station-dot
        # context even if the user never visited the Stations tab.
        if self.station_layer_obj is None:
            try:
                self._run_busy(self._ensure_index)
                self.station_layer_obj = sl.add_or_replace_station_layer(self.index.stations)
            except Exception:
                pass  # report still works without it, just with less context

        charts = [
            ("Temperature / Precipitation", self.temp_panel.figure),
            ("Wind Speed / Direction", self.wind_panel.figure),
            ("30-Year Climate Chart", self.chart_panel.figure),
            ("Temperature Trend & Anomaly", self.trend_panel.figure),
            ("Annual Climate Indices", self.indices_panel.figure),
        ]
        tmp_pngs = []
        try:
            chart_entries = []
            for title, fig in charts:
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.close()
                fig.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor="white")
                tmp_pngs.append(tmp.name)
                chart_entries.append((title, tmp.name))

            ok = self._run_busy(
                report.build_pdf_report, self.selected_station, chart_entries,
                self.station_layer_obj, path,
            )
            if ok:
                self.iface.messageBar().pushSuccess("DWD Climate Explorer", f"Report saved to {path}")
            else:
                self._error("PDF export failed.")
        except Exception as e:
            self._error(f"Could not generate report: {e}")
        finally:
            for f in tmp_pngs:
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Raster tab
    # ------------------------------------------------------------------
    def on_load_raster(self):
        variable = self.raster_var_combo.currentData()
        period = self.raster_period_combo.currentData()
        year_code = self.raster_year_combo.currentData()
        if not period:
            return
        if self.raster_timeline_check.isChecked():
            self._load_raster_timeline(variable, period, year_code)
            return

        self.raster_status_label.setText("Downloading raster grid...")
        QCoreApplication.processEvents()
        dest = os.path.join(tempfile.gettempdir(),
                             f"dwd_{variable}_{period}_{year_code}.asc")
        try:
            self._run_busy(c.fetch_raster_grid, variable, period, year_code, dest,
                            session=self.session)
        except Exception as e:
            self._error(f"Could not download raster grid: {e}")
            self.raster_status_label.setText("")
            return

        try:
            layer = self._run_busy(rl.add_or_replace_raster_layer, dest, variable, period, year_code)
        except Exception as e:
            self._error(f"Could not load raster layer: {e}")
            return

        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            layer, layer.extent()))
        self.canvas.refresh()

        stats = layer.dataProvider().bandStatistics(1)
        meta = c.RASTER_CATALOGUE[variable]
        self.raster_status_label.setText(
            f"{meta['label']} | {c.PERIOD_LABELS.get(period, period)} | "
            f"{c.YEAR_RANGE_LABELS.get(year_code, year_code)} reference period | "
            f"Range: {stats.minimumValue:.1f} to {stats.maximumValue:.1f} {meta['unit']}"
        )

    def on_compute_delta(self):
        variable = self.raster_var_combo.currentData()
        period = self.raster_period_combo.currentData()
        year_a = self.delta_year_a_combo.currentData()
        year_b = self.delta_year_b_combo.currentData()
        if year_a == year_b:
            self._warn("Choose two different reference periods.")
            return

        meta = c.RASTER_CATALOGUE[variable]
        self.delta_status_label.setText(f"Downloading {c.YEAR_RANGE_LABELS[year_a]}...")
        QCoreApplication.processEvents()
        tmp_a = os.path.join(tempfile.gettempdir(), f"dwd_delta_a_{variable}_{period}_{year_a}.asc")
        tmp_b = os.path.join(tempfile.gettempdir(), f"dwd_delta_b_{variable}_{period}_{year_b}.asc")
        tif_path = os.path.join(
            tempfile.gettempdir(), f"dwd_delta_{variable}_{period}_{year_a}_{year_b}.tif")
        try:
            self._run_busy(c.fetch_raster_grid, variable, period, year_a, tmp_a, session=self.session)
            self.delta_status_label.setText(f"Downloading {c.YEAR_RANGE_LABELS[year_b]}...")
            QCoreApplication.processEvents()
            self._run_busy(c.fetch_raster_grid, variable, period, year_b, tmp_b, session=self.session)
        except Exception as e:
            self._error(f"Could not download raster grids: {e}")
            self.delta_status_label.setText("")
            return

        self.delta_status_label.setText("Computing delta...")
        QCoreApplication.processEvents()
        try:
            self._run_busy(rl.compute_delta_grid, tmp_a, tmp_b, tif_path)
            layer = self._run_busy(rl.add_or_replace_delta_layer, tif_path, variable, period, year_a, year_b)
        except Exception as e:
            self._error(f"Could not compute delta map: {e}")
            self.delta_status_label.setText("")
            return

        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            layer, layer.extent()))
        self.canvas.refresh()

        stats = layer.dataProvider().bandStatistics(1)
        self.delta_status_label.setText(
            f"{meta['label']} change, {c.YEAR_RANGE_LABELS[year_a]} -> {c.YEAR_RANGE_LABELS[year_b]} | "
            f"Range: {stats.minimumValue:+.1f} to {stats.maximumValue:+.1f} {meta['unit']}"
        )

    def _load_raster_timeline(self, variable, period, year_code):
        """Multi-annual grids have no real dates, so this series is not made
        time-aware -- it is a layer group (last step visible) whose steps
        are point-sampled from the Timeline tab."""
        meta = c.RASTER_CATALOGUE[variable]
        kind = self.raster_series_combo.currentData()
        if kind == "months":
            if meta["periods"] == "annual_only":
                self._warn(f"{meta['label']} only exists as an annual grid -- choose "
                           f"'All reference periods' instead.")
                return
            steps = [(f"{m:02d}", year_code, c.PERIOD_LABELS[f"{m:02d}"][:3]) for m in range(1, 13)]
            title = f"{meta['label']} - monthly cycle {c.YEAR_RANGE_LABELS[year_code]}"
        else:
            steps = [(period, yc, label) for yc, label in c.YEAR_RANGE_LABELS.items()]
            title = f"{meta['label']} - {c.PERIOD_LABELS[period]}, all reference periods"

        layers = []
        for i, (per, yc, label) in enumerate(steps):
            self.raster_status_label.setText(f"Downloading {label} ({i + 1}/{len(steps)})...")
            QCoreApplication.processEvents()
            dest = os.path.join(tempfile.gettempdir(), f"dwd_{variable}_{per}_{yc}.asc")
            try:
                self._run_busy(c.fetch_raster_grid, variable, per, yc, dest, session=self.session)
                layers.append(rl.load_raster_grid(dest, variable, per, yc))
            except Exception as e:
                self._error(f"Could not download raster grid ({label}): {e}")
                self.raster_status_label.setText("")
                return

        vmin, vmax = rl.series_value_range(layers)
        for layer in layers:
            rl._style_layer(layer, meta["palette"], vmin=vmin, vmax=vmax)
        series_id = f"grid:{variable}:{kind}:{year_code if kind == 'months' else period}"
        tl.add_series_group(layers, f"DWD timeline: {title}", series_id, only_last_visible=True)
        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            layers[-1], layers[-1].extent()))
        self.canvas.refresh()

        self.raster_status_label.setText(
            f"{title}: {len(layers)} grids loaded, common scale {vmin:.1f} to {vmax:.1f} "
            f"{meta['unit']}. Click the map to chart a point (Timeline tab).")
        self._register_timeline_series(
            series_id, title, layers, [label for _, _, label in steps],
            ylabel=f"{meta['label']} [{meta['unit']}]",
            kind="line" if variable in c.TEMPERATURE_VARIABLES else "bar",
            total_unit=meta["unit"] if kind == "months" and meta["unit"] in ("mm", "h") else None)

    # ------------------------------------------------------------------
    # RADOLAN tab
    # ------------------------------------------------------------------
    def _radolan_progress(self, i, n, msg):
        self.radolan_status_label.setText(f"{msg} ({i + 1}/{n})")
        QCoreApplication.processEvents()

    def on_load_radolan(self):
        product = self.radolan_product_combo.currentData()
        meta = c.RADOLAN_PRODUCTS[product]
        timeline = self.radolan_timeline_check.isChecked()
        start = _pydate(self.radolan_start_edit.date())
        if timeline:
            end = _pydate(self.radolan_end_edit.date())
            n_days = (end - start).days + 1
            if n_days < 1:
                self._warn("The end date is before the start date.")
                return
            if n_days > meta["max_days"]:
                self._warn(f"{meta['label']}: a timeline can span at most {meta['max_days']} days "
                           f"({n_days} selected).")
                return
            hour = None
        else:
            end = start
            hour = self.radolan_hour_spin.value() if product == "RW" else None

        os.makedirs(RADOLAN_CACHE_DIR, exist_ok=True)
        grids = []  # (start_dt, end_dt, tif_path)
        vmax = 0.0

        def download():
            nonlocal vmax
            for g in c.iter_radolan_grids(product, start, end, hour=hour, session=self.session,
                                          cache_dir=RADOLAN_CACHE_DIR,
                                          progress_cb=self._radolan_progress):
                path = os.path.join(RADOLAN_CACHE_DIR, f"radolan_{product}_{g['end']:%Y%m%d%H%M}.tif")
                rl.write_radolan_geotiff(path, g["data"])
                vmax = max(vmax, rl.radolan_colour_max(g["data"]))
                grids.append((g["start"], g["end"], path))

        self.btn_load_radolan.setEnabled(False)
        try:
            self._run_busy(download)
        except Exception as e:
            self._error(f"Could not load RADOLAN data: {e}")
            self.radolan_status_label.setText("")
            return
        finally:
            self.btn_load_radolan.setEnabled(True)
        if not grids:
            self._warn("No RADOLAN grids found for that selection.")
            self.radolan_status_label.setText("")
            return

        def label(end_dt):
            if product == "SF":
                return f"{end_dt:%Y-%m-%d} (24h to 23:50 UTC)"
            return f"{end_dt:%Y-%m-%d %H:%M} UTC"

        layers = [rl.load_radolan_layer(path, f"RADOLAN {product} {label(e)}", vmax)
                  for _s, e, path in grids]

        if not timeline:
            layer = rl.add_or_replace_radolan_layer(layers[0])
            self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
                layer, layer.extent()))
            self.canvas.refresh()
            self.radolan_status_label.setText(
                f"{meta['label']}, {label(grids[0][1])}. Colour scale 0 to {vmax:.1f} mm.")
            return

        for layer, (s, e, _path) in zip(layers, grids):
            tl.set_raster_time_range(layer, s, e)
        title = f"RADOLAN {product} {start:%Y-%m-%d} to {end:%Y-%m-%d}"
        series_id = f"radolan:{product}:{start:%Y%m%d}:{end:%Y%m%d}"
        tl.add_series_group(layers, f"RADOLAN timeline: {meta['label']}, "
                                    f"{start:%Y-%m-%d} to {end:%Y-%m-%d}", series_id)
        animated = tl.configure_temporal_controller(
            self.canvas, grids[0][0], grids[-1][1], meta["step_hours"] * 3600)
        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            layers[0], layers[0].extent()))
        self.canvas.refresh()

        self.radolan_status_label.setText(
            f"{title}: {len(layers)} time-aware grids, common colour scale 0 to {vmax:.1f} mm. "
            + ("Temporal Controller set to animate them (View > Panels > Temporal Controller). "
               if animated else "")
            + "Click the map to chart a point (Timeline tab).")
        self._register_timeline_series(
            series_id, f"{meta['label']}, {start:%Y-%m-%d} to {end:%Y-%m-%d}", layers,
            [e for _s, e, _p in grids], ylabel="Precipitation [mm]", kind="bar",
            bar_width=meta["step_hours"] / 24.0, total_unit="mm")

    # ------------------------------------------------------------------
    # Timeline tab
    # ------------------------------------------------------------------
    def _register_timeline_series(self, series_id, title, layers, xs, ylabel, kind,
                                  bar_width=None, total_unit=None):
        """Makes a freshly loaded series pickable: lists it in the Timeline
        tab, switches there and turns the point picker on -- ticking
        'Timeline' is the user's signal that they want to click the map."""
        self.timeline_series[series_id] = dict(
            title=title, layer_ids=[layer.id() for layer in layers], xs=xs, ylabel=ylabel,
            kind=kind, bar_width=bar_width, total_unit=total_unit)
        idx = self.timeline_combo.findData(series_id)
        if idx < 0:
            self.timeline_combo.addItem(title, series_id)
            idx = self.timeline_combo.count() - 1
        if idx == self.timeline_combo.currentIndex():
            self._redraw_timeline()  # same entry reloaded: refresh with the new layer ids
        else:
            self.timeline_combo.setCurrentIndex(idx)
        self.btn_timeline_pick.setEnabled(True)
        if self.timeline_point is None:
            self.timeline_status_label.setText("Click anywhere on the map to chart this series.")
        self.tabs.setCurrentWidget(self.timeline_tab)
        if not self.btn_timeline_pick.isChecked():
            self.btn_timeline_pick.setChecked(True)

    def on_timeline_point_picked(self, point):
        self.timeline_point = (point, self.canvas.mapSettings().destinationCrs())
        if self.timeline_marker is None:
            self.timeline_marker = QgsVertexMarker(self.canvas)
            self.timeline_marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.timeline_marker.setColor(QColor("#d62728"))
            self.timeline_marker.setIconSize(16)
            self.timeline_marker.setPenWidth(3)
        self.timeline_marker.setCenter(point)
        self._redraw_timeline()

    def _redraw_timeline(self, *_):
        series_id = self.timeline_combo.currentData()
        series = self.timeline_series.get(series_id)
        if series is None or self.timeline_point is None:
            return
        project = QgsProject.instance()
        if all(project.mapLayer(lid) is None for lid in series["layer_ids"]):
            # The user deleted the group from the layer tree.
            del self.timeline_series[series_id]
            self.timeline_combo.removeItem(self.timeline_combo.currentIndex())
            self._warn(f"The layers of '{series['title']}' were removed from the project.")
            if self.timeline_combo.count() == 0:
                self.btn_timeline_pick.setChecked(False)
                self.btn_timeline_pick.setEnabled(False)
            return

        point, crs = self.timeline_point
        values = self._run_busy(tl.sample_layers, series["layer_ids"], point, crs)
        lat, lon = tl.to_wgs84(point, crs)
        p.plot_raster_timeline(
            self.timeline_panel.figure, series["xs"], values, series["title"],
            f"Point {lat:.4f}°N, {lon:.4f}°E", series["ylabel"], kind=series["kind"],
            bar_width=series["bar_width"], total_unit=series["total_unit"])
        self.timeline_panel.redraw()

        n_valid = sum(v is not None for v in values)
        self.timeline_status_label.setText(
            f"{n_valid} of {len(values)} grids have data at {lat:.4f}°N, {lon:.4f}°E.")
        self.timeline_last = pd.DataFrame({
            "step": series["xs"], "value": values, "lat": lat, "lon": lon,
        })
        self.btn_download_timeline.setEnabled(n_valid > 0)

    def on_download_timeline(self):
        if self.timeline_last is None:
            self._warn("Pick a point on the map first.")
            return
        lat, lon = self.timeline_last["lat"].iloc[0], self.timeline_last["lon"].iloc[0]
        self._save_csv(self.timeline_last, "timeline", station_name=f"{lat:.3f}N_{lon:.3f}E")

    # ------------------------------------------------------------------
    # Phenology tab
    # ------------------------------------------------------------------
    def _populate_pheno_categories(self):
        """Populates the category combo only (pure config, no network call).
        The species combo is loaded lazily -- either when the user actually
        changes reporter/category, or on first use of 'Fetch phenology
        data' -- so simply opening the dock widget never hits the network."""
        reporter = PHENO_REPORTER_KEYS[self.pheno_reporter_group.checkedId()]
        self.pheno_category_combo.blockSignals(True)
        self.pheno_category_combo.clear()
        for key, label in c.pheno_categories_for(reporter):
            self.pheno_category_combo.addItem(label, key)
        self.pheno_category_combo.blockSignals(False)
        self.pheno_species_combo.clear()
        self.pheno_species_index = None

    def _on_tab_changed(self, _index):
        # The first category is pre-selected without a change signal, so its
        # species would otherwise never load (selecting the already-current
        # entry fires nothing). Load it the first time the tab is shown.
        if self.tabs.currentWidget() is self.pheno_tab and self.pheno_species_combo.count() == 0:
            self.on_pheno_category_changed()

    def on_pheno_reporter_changed(self, _id):
        self.pheno_stations_df = None
        self.pheno_selected_station = None
        self.btn_pheno_pick_tool.setEnabled(False)
        self._populate_pheno_categories()
        self.on_pheno_category_changed()

    def on_pheno_category_changed(self, *_):
        reporter = PHENO_REPORTER_KEYS[self.pheno_reporter_group.checkedId()]
        category = self.pheno_category_combo.currentData()
        self.pheno_species_combo.clear()
        if category is None:
            return
        self.pheno_status_label.setText("Loading species list...")
        QCoreApplication.processEvents()
        try:
            self.pheno_species_index = self._run_busy(c.list_phenology_species, reporter, category,
                                                        session=self.session)
        except Exception as e:
            self._error(f"Could not load phenology species list: {e}")
            self.pheno_status_label.setText("")
            return
        for token, entry in self.pheno_species_index.sorted_items():
            self.pheno_species_combo.addItem(entry["label"], token)
        self.pheno_status_label.setText(
            f"{self.pheno_species_combo.count()} species available for this category.")

    def on_load_pheno_stations(self):
        reporter = PHENO_REPORTER_KEYS[self.pheno_reporter_group.checkedId()]
        self.btn_load_pheno_stations.setEnabled(False)
        try:
            self.pheno_stations_df = self._run_busy(c.fetch_phenology_stations, reporter,
                                                      session=self.session)
        except Exception as e:
            self._error(f"Could not load phenology station list: {e}")
            return
        finally:
            self.btn_load_pheno_stations.setEnabled(True)

        reporter_label = self.radio_pheno_annual.text() if reporter == "annual" else self.radio_pheno_immediate.text()
        self.pheno_station_layer_obj = pl.add_or_replace_pheno_station_layer(
            self.pheno_stations_df, reporter_label)
        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            self.pheno_station_layer_obj, self.pheno_station_layer_obj.extent()))
        self.canvas.refresh()
        n = len(self.pheno_stations_df)
        self.pheno_map_pick_label.setText(
            f"{n} phenology stations loaded. Click 'Pick station on map' and then click a marker.")
        self.btn_pheno_pick_tool.setEnabled(True)

    def on_toggle_pheno_pick_tool(self, checked):
        self._toggle_map_tool(self.btn_pheno_pick_tool, self.pheno_pick_tool, checked)

    def on_pheno_station_picked(self, station_id):
        if self.pheno_stations_df is None:
            return
        rows = self.pheno_stations_df[self.pheno_stations_df["Stations_id"] == station_id]
        if len(rows) == 0:
            return
        self.pheno_selected_station = rows.iloc[0]
        sl.highlight_feature(self.pheno_station_layer_obj, self.canvas, station_id)
        self.pheno_map_pick_label.setText(
            f"Using station: {self.pheno_selected_station['Stationsname']} (ID {station_id})")

    def _pheno_nearest_station(self, lat, lon, radius_km):
        if self.pheno_stations_df is None:
            reporter = PHENO_REPORTER_KEYS[self.pheno_reporter_group.checkedId()]
            self.pheno_stations_df = self._run_busy(c.fetch_phenology_stations, reporter,
                                                      session=self.session)
        near = c.nearby_stations(self.pheno_stations_df, lat, lon, radius_km, require=None)
        if len(near) == 0:
            self._warn(f"No phenology station within {radius_km} km.")
            return None
        return near.iloc[0]

    def on_fetch_phenology(self):
        if self.pheno_species_combo.count() == 0:
            self.on_pheno_category_changed()
        species_token = self.pheno_species_combo.currentData()
        if species_token is None:
            self._warn("No species selected.")
            return

        mode = self.pheno_mode_group.checkedId()
        try:
            if mode == 2:  # map pick
                if self.pheno_selected_station is None:
                    self._warn("Pick a phenology station on the map first.")
                    return
                station_row = self.pheno_selected_station
            elif mode == 0:  # city
                city = self.pheno_city_edit.text().strip()
                if not city:
                    self._warn("Enter a city name.")
                    return
                geocoded = self._run_busy(c.geocode_city, city, session=self.session)
                if geocoded is None:
                    self._error(f"Could not geocode '{city}'.")
                    return
                lat, lon, _display = geocoded
                station_row = self._pheno_nearest_station(lat, lon, self.pheno_radius_spin.value())
                if station_row is None:
                    return
            else:  # coords
                lat, lon = self.pheno_lat_spin.value(), self.pheno_lon_spin.value()
                station_row = self._pheno_nearest_station(lat, lon, self.pheno_radius_spin.value())
                if station_row is None:
                    return
        except Exception as e:
            self._error(f"Location lookup failed: {e}")
            return

        self.pheno_status_label.setText(
            f"Fetching {self.pheno_species_combo.currentText()} data for {station_row['Stationsname']}...")
        QCoreApplication.processEvents()
        try:
            records = self._run_busy(self._pheno_species_records)
        except Exception as e:
            self._error(f"Could not fetch phenology data: {e}")
            self.pheno_status_label.setText("")
            return
        if records is None:
            df = None
        else:
            df = records[records["Stations_id"] == int(station_row["Stations_id"])].reset_index(drop=True)

        self.pheno_selected_station = station_row
        self.pheno_data = df

        if df is None or len(df) == 0:
            self.pheno_status_label.setText(
                f"No phenology records for {station_row['Stationsname']} / "
                f"{self.pheno_species_combo.currentText()}.")
            self.pheno_phase_combo.clear()
            self.btn_download_pheno.setEnabled(False)
            self._redraw_pheno()
            return

        self.pheno_phase_combo.blockSignals(True)
        self.pheno_phase_combo.clear()
        phases = df[["Phase_id", "Phase_de"]].drop_duplicates().sort_values("Phase_id")
        for phase_id, phase_de in phases.itertuples(index=False):
            self.pheno_phase_combo.addItem(phase_de, int(phase_id))
        self.pheno_phase_combo.blockSignals(False)

        years = df["Referenzjahr"].dropna()
        if len(years):
            lo, hi = int(years.min()), int(years.max())
            self.pheno_range.setBounds(lo, hi)
            self.pheno_range.setValues(lo, hi)

        self.pheno_status_label.setText(
            f"Showing {station_row['Stationsname']} (ID {int(station_row['Stations_id'])}) -- "
            f"{len(df)} records across {len(phases)} phase(s).")
        self.btn_download_pheno.setEnabled(True)
        self._redraw_pheno()

    def _redraw_pheno(self, *_):
        if self.pheno_selected_station is None:
            return
        phase_id = self.pheno_phase_combo.currentData()
        p.plot_phenology_trend(self.pheno_panel.figure, self.pheno_data, phase_id,
                                self.pheno_range.values(), self.pheno_selected_station["Stationsname"])
        self.pheno_panel.redraw()

    def _pheno_species_records(self):
        """All-station records of the selected species (lookups joined),
        downloaded once per (reporter, category, species) and then reused."""
        reporter = PHENO_REPORTER_KEYS[self.pheno_reporter_group.checkedId()]
        category = self.pheno_category_combo.currentData()
        token = self.pheno_species_combo.currentData()
        key = (reporter, category, token)
        if self.pheno_records_key != key:
            self.pheno_records = c.fetch_phenology_species_data(
                reporter, category, token, None, session=self.session,
                species_index=self.pheno_species_index)
            self.pheno_records_key = key
        return self.pheno_records

    def _reset_pheno_obs_phases(self, *_):
        self.pheno_obs_phase_combo.clear()
        self.pheno_obs_status_label.setText("")

    def on_load_pheno_obs_phases(self):
        if self.pheno_species_combo.count() == 0:
            self.on_pheno_category_changed()
        if self.pheno_species_combo.currentData() is None:
            self._warn("No species selected.")
            return False
        self.pheno_obs_status_label.setText(
            f"Downloading all {self.pheno_species_combo.currentText()} observations...")
        QCoreApplication.processEvents()
        try:
            records = self._run_busy(self._pheno_species_records)
        except Exception as e:
            self._error(f"Could not fetch phenology data: {e}")
            self.pheno_obs_status_label.setText("")
            return False
        self.pheno_obs_phase_combo.clear()
        if records is None or len(records) == 0:
            self.pheno_obs_status_label.setText("No observations for this species.")
            return False

        summary = (records.groupby(["Phase_id", "Phase_de"])
                   .agg(n=("Stations_id", "size"), stations=("Stations_id", "nunique"))
                   .reset_index().sort_values("Phase_id"))
        for row in summary.itertuples(index=False):
            self.pheno_obs_phase_combo.addItem(
                f"{row.Phase_de} ({row.n} obs., {row.stations} stations)", int(row.Phase_id))

        lo, hi = int(records["Referenzjahr"].min()), int(records["Referenzjahr"].max())
        for spin in (self.pheno_obs_from_spin, self.pheno_obs_to_spin):
            spin.setRange(lo, hi)
        self.pheno_obs_from_spin.setValue(max(lo, hi - 29))
        self.pheno_obs_to_spin.setValue(hi)
        self.pheno_obs_status_label.setText(
            f"{len(records)} observations, {lo}-{hi}. Choose a phase and add the layer.")
        return True

    def on_add_pheno_obs_layer(self):
        if self.pheno_obs_phase_combo.count() == 0 and not self.on_load_pheno_obs_phases():
            return
        phase_id = self.pheno_obs_phase_combo.currentData()
        y0, y1 = self.pheno_obs_from_spin.value(), self.pheno_obs_to_spin.value()
        if y1 < y0:
            self._warn("The end year is before the start year.")
            return
        try:
            if self.pheno_stations_df is None:
                reporter = PHENO_REPORTER_KEYS[self.pheno_reporter_group.checkedId()]
                self.pheno_stations_df = self._run_busy(c.fetch_phenology_stations, reporter,
                                                          session=self.session)
            records = self._run_busy(self._pheno_species_records)
        except Exception as e:
            self._error(f"Could not fetch phenology data: {e}")
            return
        df = c.attach_pheno_station_coords(records, self.pheno_stations_df, phase_id, (y0, y1))
        if df is None or len(df) == 0:
            self.pheno_obs_status_label.setText(f"No observations of this phase in {y0}-{y1}.")
            return

        species = self.pheno_species_combo.currentText()
        phase = df["Phase_de"].iloc[0]
        layer = self._run_busy(pl.add_or_replace_pheno_obs_layer, df, f"{species} - {phase}, {y0}-{y1}")
        self.canvas.setExtent(self.canvas.mapSettings().layerExtentToOutputExtent(
            layer, layer.extent()))
        # Extent + a weekly step for the Temporal Controller, but no forced
        # animation: with the controller off, the layer is a static map of
        # every observation, coloured by day of year.
        tl.configure_temporal_controller(self.canvas, datetime(y0, 1, 1), datetime(y1, 12, 31, 23, 59),
                                         7 * 86400, animate=False)
        self.canvas.refresh()
        median_doy = df["_date"].dt.dayofyear.median()
        self.pheno_obs_status_label.setText(
            f"{len(df)} observations at {df['Stations_id'].nunique()} stations, {y0}-{y1}. "
            f"Median entry: day {median_doy:.0f} of the year. Animate via View > Panels > "
            f"Temporal Controller.")

    def on_download_phenology(self):
        if self.pheno_data is None or len(self.pheno_data) == 0:
            self._warn("No phenology data loaded yet.")
            return
        station_name = (self.pheno_selected_station["Stationsname"]
                         if self.pheno_selected_station is not None else None)
        self._save_csv(self.pheno_data, "phenology", station_name=station_name)

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self.canvas.mapTool() is self.pick_tool:
            self.canvas.unsetMapTool(self.pick_tool)
        if self.canvas.mapTool() is self.pheno_pick_tool:
            self.canvas.unsetMapTool(self.pheno_pick_tool)
        if self.canvas.mapTool() is self.timeline_pick_tool:
            self.canvas.unsetMapTool(self.timeline_pick_tool)
        if self.timeline_marker is not None:
            self.canvas.scene().removeItem(self.timeline_marker)
            self.timeline_marker = None
        super().closeEvent(event)
