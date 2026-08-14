"""One-click PDF report: a QGIS Print Layout built entirely in code (no
.qpt template) showing a Germany-wide overview map with the station
highlighted, its data-availability facts, and one page per climate chart."""
import datetime

from qgis.core import (
    Qgis, QgsProject, QgsPrintLayout, QgsLayoutItemMap, QgsLayoutItemLabel,
    QgsLayoutItemPicture, QgsLayoutItemPage, QgsLayoutPoint, QgsLayoutSize,
    QgsLayoutExporter, QgsRectangle, QgsVectorLayer, QgsFeature,
    QgsGeometry, QgsPointXY, QgsField, QgsMarkerSymbol, QgsSingleSymbolRenderer,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QFont

from . import dwd_client as c

MM = Qgis.LayoutUnit.Millimeters
GERMANY_EXTENT = QgsRectangle(5.5, 47.0, 15.6, 55.3)  # WGS84 lon/lat bounding box
PAGE_W, PAGE_H = 210.0, 297.0  # A4, mm
MARGIN = 12.0


def _make_highlight_layer(station_row):
    layer = QgsVectorLayer("Point?crs=EPSG:4326", "Report Highlight", "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(
        QgsPointXY(float(station_row["geoLaenge"]), float(station_row["geoBreite"]))))
    feat.setAttributes([str(station_row["Stationsname"])])
    provider.addFeatures([feat])
    layer.updateExtents()
    symbol = QgsMarkerSymbol.createSimple({
        "name": "circle", "size": "7", "color": "#e31a1c",
        "outline_color": "black", "outline_width": "0.8",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    return layer


def _station_info_html(station_row):
    tier = station_row["tier"]
    tier_col = c.TIER_COLOUR.get(tier, "#aaaaaa")

    def row(has, label, frm, to):
        if has and frm:
            return (f"<tr><td>&#10004; {label}</td>"
                    f"<td style='padding-left:10px;color:#444'>{frm} to {to}</td></tr>")
        return (f"<tr><td style='color:#999'>&#10008; {label}</td>"
                f"<td style='padding-left:10px;color:#bbb'>n/a</td></tr>")

    return f"""
    <div style='font-family:sans-serif;'>
    <h2 style='margin:0 0 4px 0;'>{station_row['Stationsname']}</h2>
    <p style='color:#666;margin:0 0 10px 0;'>DWD Station ID {int(station_row['Stations_id'])}</p>
    <table style='font-size:11pt;'>
    <tr><td><b>State</b></td><td style='padding-left:10px;'>{station_row['Bundesland']}</td></tr>
    <tr><td><b>Elevation</b></td><td style='padding-left:10px;'>{station_row['Stationshoehe']} m a.s.l.</td></tr>
    <tr><td><b>Coordinates</b></td><td style='padding-left:10px;'>{station_row['geoBreite']:.4f}&deg;N, {station_row['geoLaenge']:.4f}&deg;E</td></tr>
    <tr><td><b>Data tier</b></td><td style='padding-left:10px;color:{tier_col};font-weight:bold;'>{station_row['tier_desc']}</td></tr>
    </table>
    <table style='font-size:10pt;margin-top:10px;'>
    {row(station_row['has_daily_kl'], 'Daily climate', station_row['from_daily_kl'], station_row['to_daily_kl'])}
    {row(station_row['has_monthly_kl'], 'Monthly climate', station_row['from_monthly_kl'], station_row['to_monthly_kl'])}
    {row(station_row['has_hourly_wind'], 'Hourly wind', station_row['from_hourly_wind'], station_row['to_hourly_wind'])}
    {row(station_row['has_annual_klindex'], 'Annual indices', station_row['from_annual_klindex'], station_row['to_annual_klindex'])}
    </table>
    </div>
    """


def _add_label(layout, page, x, y, w, h, text, font_size, bold=False, html=False):
    label = QgsLayoutItemLabel(layout)
    layout.addLayoutItem(label)
    if html:
        label.setMode(QgsLayoutItemLabel.ModeHtml)
    else:
        label.setFont(QFont("Sans Serif", font_size, QFont.Bold if bold else QFont.Normal))
    label.setText(text)
    label.attemptMove(QgsLayoutPoint(x, y, MM), page=page)
    label.attemptResize(QgsLayoutSize(w, h, MM))
    return label


def build_pdf_report(station_row, chart_pngs, station_layer, dest_pdf_path):
    """chart_pngs: list of (title, png_path) tuples, one per chart panel."""
    project = QgsProject.instance()
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName("DWD Climate Report")

    pc = layout.pageCollection()
    pc.page(0).setPageSize("A4")

    # ---- Page 0: title, map, station facts ----
    _add_label(layout, 0, MARGIN, 10, PAGE_W - 2 * MARGIN, 12,
               f"DWD Climate Report — {station_row['Stationsname']}", 18, bold=True)
    _add_label(layout, 0, MARGIN, 21, PAGE_W - 2 * MARGIN, 8,
               f"Generated {datetime.date.today().isoformat()} · "
               f"Source: Deutscher Wetterdienst (opendata.dwd.de)", 9)

    highlight_layer = _make_highlight_layer(station_row)
    map_layers = [highlight_layer]
    if station_layer is not None:
        map_layers.append(station_layer)
    map_layers.extend(project.layerTreeRoot().layerOrder())

    map_item = QgsLayoutItemMap(layout)
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(MARGIN, 32, MM), page=0)
    map_item.attemptResize(QgsLayoutSize(112, 130, MM))
    map_item.setLayers(map_layers)
    map_item.setExtent(GERMANY_EXTENT)
    map_item.setFrameEnabled(True)

    _add_label(layout, 0, MARGIN + 116, 32, PAGE_W - 2 * MARGIN - 116, 130,
               _station_info_html(station_row), 10, html=True)

    _add_label(layout, 0, MARGIN, PAGE_H - 14, PAGE_W - 2 * MARGIN, 8,
               "Every grey/coloured dot is a DWD weather station; the red marker is this report's station.", 8)

    # ---- One page per chart ----
    for i, (chart_title, png_path) in enumerate(chart_pngs):
        page_index = i + 1
        pc.addPage(QgsLayoutItemPage(layout))
        pc.page(page_index).setPageSize("A4")

        _add_label(layout, page_index, MARGIN, 12, PAGE_W - 2 * MARGIN, 10,
                   chart_title, 14, bold=True)

        pic = QgsLayoutItemPicture(layout)
        layout.addLayoutItem(pic)
        pic.setPicturePath(png_path)
        pic.attemptMove(QgsLayoutPoint(MARGIN, 26, MM), page=page_index)
        pic.attemptResize(QgsLayoutSize(PAGE_W - 2 * MARGIN, 180, MM))

    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.PdfExportSettings()
    result = exporter.exportToPdf(dest_pdf_path, settings)
    return result == QgsLayoutExporter.ExportResult.Success
