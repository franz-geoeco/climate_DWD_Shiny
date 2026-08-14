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

import requests
from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.PyQt.QtGui import QColor, QPixmap, QPainter
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QTabWidget, QPushButton, QLabel, QLineEdit, QDoubleSpinBox, QSpinBox,
    QRadioButton, QButtonGroup, QComboBox, QTextBrowser, QGroupBox,
    QFileDialog, QApplication, QStackedWidget,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure

from . import dwd_client as c
from . import plotting as p
from . import station_layer as sl
from . import raster_layer as rl
from . import report
from .range_slider import RangeSlider

CURRENT_YEAR = 2026


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

        self._build_ui()

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
        if checked:
            self._prev_map_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.pick_tool)
        else:
            if self._prev_map_tool is not None:
                self.canvas.setMapTool(self._prev_map_tool)

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

    def _save_csv(self, df, kind):
        station = self.selected_station["Stationsname"] if self.selected_station is not None else "station"
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

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self.canvas.mapTool() is self.pick_tool:
            self.canvas.unsetMapTool(self.pick_tool)
        super().closeEvent(event)
