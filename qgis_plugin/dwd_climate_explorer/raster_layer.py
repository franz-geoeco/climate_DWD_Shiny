"""Turns a downloaded DWD multi-annual ASCII grid into a properly styled,
properly georeferenced QGIS raster layer. QGIS reprojects on the fly for
display, so unlike the R app (which had to manually terra::project() to
WGS84 before plotting a static PNG), the grid can simply be dropped onto
the canvas in its native Gauss-Krueger CRS and it will line up with the
station layer and any basemap automatically."""
from qgis.core import (
    Qgis, QgsProject, QgsRasterLayer, QgsCoordinateReferenceSystem,
    QgsSingleBandPseudoColorRenderer, QgsColorRampShader, QgsRasterShader,
)
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
from qgis.PyQt.QtGui import QColor
import numpy as np

from . import dwd_client as c
from . import timeline as tl

GRID_CRS = "EPSG:31467"  # DHDN / Gauss-Kruger zone 3, used by all DWD multi_annual grids

# Diverging ramp for delta/change maps: blue (decrease) -> white (no change) -> red (increase).
DELTA_PALETTE = ["#2166ac", "#4393c3", "#92c5de", "#d1e5f0", "#f7f7f7",
                 "#fddbc7", "#f4a582", "#d6604d", "#b2182b"]


def write_prj_sidecar(asc_path):
    """Writes a .prj next to the .asc so the file is self-describing on
    disk -- needed because Processing raster-destination outputs are
    consumed as plain file paths, not QgsRasterLayer objects we control,
    so a runtime-only layer.setCrs() call wouldn't be picked up by them."""
    crs = QgsCoordinateReferenceSystem(GRID_CRS)
    with open(asc_path + ".prj", "w", encoding="ascii") as f:
        f.write(crs.toWkt())


def load_raster_grid(asc_path, variable, period, year_code):
    meta = c.RASTER_CATALOGUE[variable]
    layer_name = f"DWD {meta['label']} - {c.PERIOD_LABELS[period]} ({c.YEAR_RANGE_LABELS[year_code]})"
    write_prj_sidecar(asc_path)
    layer = QgsRasterLayer(asc_path, layer_name)
    if not layer.isValid():
        raise RuntimeError(f"GDAL could not read the downloaded grid: {asc_path}")
    layer.setCrs(QgsCoordinateReferenceSystem(GRID_CRS))
    _style_layer(layer, meta["palette"])
    return layer


def _style_layer(layer, palette, vmin=None, vmax=None):
    provider = layer.dataProvider()
    if vmin is None or vmax is None:
        stats = provider.bandStatistics(
            1, Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max)
        vmin, vmax = stats.minimumValue, stats.maximumValue

    ramp_colors = [QColor(hexcode) for hexcode in palette]
    n = len(ramp_colors)
    stops = []
    for i, colour in enumerate(ramp_colors):
        value = vmin + (vmax - vmin) * i / (n - 1) if n > 1 else vmin
        stops.append(QgsColorRampShader.ColorRampItem(value, colour, f"{value:.1f}"))

    shader_fn = QgsColorRampShader()
    shader_fn.setColorRampType(QgsColorRampShader.Interpolated)
    shader_fn.setColorRampItemList(stops)

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_fn)

    renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return vmin, vmax


def _style_delta_layer(layer):
    """Diverging ramp centred on zero, so 'no change' is always white
    regardless of the actual min/max of this particular delta."""
    provider = layer.dataProvider()
    stats = provider.bandStatistics(1, Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max)
    bound = max(abs(stats.minimumValue), abs(stats.maximumValue))
    _style_layer(layer, DELTA_PALETTE, vmin=-bound, vmax=bound)
    return -bound, bound


def add_or_replace_raster_layer(asc_path, variable, period, year_code):
    project = QgsProject.instance()
    for lyr in list(project.mapLayers().values()):
        if (isinstance(lyr, QgsRasterLayer) and lyr.name().startswith("DWD ")
                and not lyr.customProperty(tl.SERIES_PROP)):
            project.removeMapLayer(lyr.id())
    layer = load_raster_grid(asc_path, variable, period, year_code)
    project.addMapLayer(layer)
    return layer


def compute_delta_grid(asc_path_a, asc_path_b, dest_tif_path):
    """Writes dest = B - A (GeoTIFF) via QGIS's own raster calculator. DWD's
    multi-annual grids for a given variable share one fixed national grid
    geometry across reference periods, so a plain cell-by-cell subtraction
    (no resampling) is valid."""
    write_prj_sidecar(asc_path_a)
    write_prj_sidecar(asc_path_b)
    layer_a = QgsRasterLayer(asc_path_a, "A")
    layer_b = QgsRasterLayer(asc_path_b, "B")
    if not layer_a.isValid() or not layer_b.isValid():
        raise RuntimeError("Could not read one of the input grids for the delta calculation.")
    crs = QgsCoordinateReferenceSystem(GRID_CRS)
    layer_a.setCrs(crs)
    layer_b.setCrs(crs)

    if (layer_a.width() != layer_b.width() or layer_a.height() != layer_b.height()
            or layer_a.extent() != layer_b.extent()):
        raise RuntimeError("The two grids do not share the same geometry -- cannot compute a delta.")

    entry_a = QgsRasterCalculatorEntry()
    entry_a.ref, entry_a.raster, entry_a.bandNumber = "A@1", layer_a, 1
    entry_b = QgsRasterCalculatorEntry()
    entry_b.ref, entry_b.raster, entry_b.bandNumber = "B@1", layer_b, 1

    calc = QgsRasterCalculator(
        "B@1 - A@1", dest_tif_path, "GTiff",
        layer_a.extent(), crs, layer_a.width(), layer_a.height(),
        [entry_a, entry_b], QgsProject.instance().transformContext(),
    )
    result = calc.processCalculation()
    if result != QgsRasterCalculator.Result.Success:
        raise RuntimeError(f"Raster calculation failed: {calc.lastError() or result}")
    return dest_tif_path


def series_value_range(layers):
    """Common min/max across a timeline series, so every step shares one
    colour scale and the steps are visually comparable."""
    lo, hi = None, None
    for layer in layers:
        stats = layer.dataProvider().bandStatistics(
            1, Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max)
        lo = stats.minimumValue if lo is None else min(lo, stats.minimumValue)
        hi = stats.maximumValue if hi is None else max(hi, stats.maximumValue)
    return lo, hi


def add_or_replace_delta_layer(tif_path, variable, period, year_code_a, year_code_b):
    meta = c.RASTER_CATALOGUE[variable]
    layer_name = (f"DWD {meta['label']} change - {c.PERIOD_LABELS[period]} "
                  f"({c.YEAR_RANGE_LABELS[year_code_a]} -> {c.YEAR_RANGE_LABELS[year_code_b]})")
    layer = QgsRasterLayer(tif_path, layer_name)
    if not layer.isValid():
        raise RuntimeError(f"Could not load the computed delta raster: {tif_path}")
    _style_delta_layer(layer)

    project = QgsProject.instance()
    for lyr in list(project.mapLayers().values()):
        if isinstance(lyr, QgsRasterLayer) and lyr.name().startswith("DWD ") and "change" in lyr.name():
            project.removeMapLayer(lyr.id())
    project.addMapLayer(layer)
    return layer


# ---------------------------------------------------------------------------
# RADOLAN
# ---------------------------------------------------------------------------
RADOLAN_NODATA = -1.0


def radolan_crs():
    return QgsCoordinateReferenceSystem.fromProj(c.RADOLAN_PROJ4)


def write_radolan_geotiff(path, values):
    """Writes one north-up RADOLAN array (mm, NaN = no radar coverage) as a
    compressed single-band GeoTIFF with the RADOLAN stereographic CRS
    embedded, so the file is self-describing for Processing outputs too."""
    from osgeo import gdal, osr
    nrows, ncols = values.shape
    ds = gdal.GetDriverByName("GTiff").Create(
        path, ncols, nrows, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"])
    ds.SetGeoTransform(c.RADOLAN_GEOTRANSFORM)
    srs = osr.SpatialReference()
    srs.ImportFromProj4(c.RADOLAN_PROJ4)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(RADOLAN_NODATA)
    band.WriteArray(np.where(np.isnan(values), RADOLAN_NODATA, values).astype(np.float32))
    band.FlushCache()
    ds = None  # closes + flushes the file
    return path


def radolan_colour_max(values, floor=1.0):
    """Upper end of the colour ramp: the 99.9th percentile rather than the
    max, so a single extreme cell doesn't wash out the rest of the map."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return floor
    return max(floor, float(np.percentile(finite, 99.9)))


def load_radolan_layer(tif_path, layer_name, vmax):
    layer = QgsRasterLayer(tif_path, layer_name)
    if not layer.isValid():
        raise RuntimeError(f"GDAL could not read the RADOLAN grid: {tif_path}")
    layer.setCrs(radolan_crs())
    # 0 mm maps to the palette's fully transparent first colour, so dry
    # areas show the basemap through, as on DWD's own radar maps.
    _style_layer(layer, c.RADOLAN_PALETTE, vmin=0.0, vmax=vmax)
    return layer


def add_or_replace_radolan_layer(layer):
    project = QgsProject.instance()
    for lyr in list(project.mapLayers().values()):
        if (isinstance(lyr, QgsRasterLayer) and lyr.name().startswith("RADOLAN ")
                and not lyr.customProperty(tl.SERIES_PROP)):
            project.removeMapLayer(lyr.id())
    project.addMapLayer(layer)
    return layer
