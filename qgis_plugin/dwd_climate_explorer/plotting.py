"""
Matplotlib figure builders for the DWD Climate Explorer dock widget.

Pure matplotlib (no Qt / QGIS imports) so each function can be exercised
from a plain Python interpreter. Every function receives an already-created
Figure, clears it, and draws into it -- the caller (dock_widget.py) owns
the FigureCanvasQTAgg and calls canvas.draw() afterwards.
"""
import numpy as np

SOURCE_TXT = "Source: Deutscher Wetterdienst"

COL_TEMP = "#b2182b"
COL_PRECIP = "#4393c3"
COL_WIND_SPEED = "#1f4e8c"
COL_WIND_DIR = "#2ca02c"
COL_WARM = "#d62728"
COL_COOL = "#1f77b4"


def _station_subtitle(station_name, lat, lon, height):
    return f"{station_name}  ({lat:.2f}°N, {lon:.2f}°E, {height:.0f} m a.s.l.)"


def _attribute(ax):
    ax.annotate(SOURCE_TXT, xy=(1, 0), xycoords="axes fraction",
                xytext=(0, -32), textcoords="offset points",
                ha="right", va="top", fontsize=8, style="italic", color="#777777")


def plot_temperature_precip(fig, clim, var, year_range, station_name, lat, lon, height):
    fig.clear()
    ax = fig.add_subplot(111)
    if clim is None or len(clim) == 0 or "_date" not in clim.columns:
        ax.text(0.5, 0.5, "No daily climate data available.", ha="center", va="center", color="#888888")
        return
    years = clim["_date"].dt.year
    sub = clim[(years >= year_range[0]) & (years <= year_range[1])]
    if len(sub) == 0:
        ax.text(0.5, 0.5, "No data in the selected year range.", ha="center", va="center", color="#888888")
        return

    if var == "TMK":
        ax.plot(sub["_date"], sub["TMK"], color=COL_TEMP, lw=1)
        ax.axhline(0, color="#999999", lw=0.8)
        ax.set_ylabel("Air temperature [°C]")
        title = f"Daily Mean Temperature, {year_range[0]}-{year_range[1]}"
    else:
        ax.bar(sub["_date"], sub["RSK"], color=COL_PRECIP, width=1.2)
        ax.set_ylabel("Precipitation sum [mm]")
        title = f"Daily Precipitation Sum, {year_range[0]}-{year_range[1]}"

    ax.set_xlabel("Date")
    ax.set_title(_station_subtitle(station_name, lat, lon, height) + "\n" + title,
                 loc="left", fontsize=11)
    ax.grid(axis="y", color="#eeeeee")
    _attribute(ax)
    fig.tight_layout()


def _resolve_wind_col(df, kind):
    if kind == "F":
        for c in df.columns:
            if c == "F" or "Windgeschwindigkeit" in c:
                return c
    else:
        for c in df.columns:
            if c == "D" or "Windrichtung" in c:
                return c
    return None


def plot_wind(fig, wind, var, year_range, station_name, lat, lon, height):
    fig.clear()
    ax = fig.add_subplot(111)
    if wind is None or len(wind) == 0 or "_date" not in wind.columns:
        ax.text(0.5, 0.5, "No hourly wind data available for this station.",
                ha="center", va="center", color="#888888")
        return
    col = _resolve_wind_col(wind, var)
    if col is None:
        label = "Wind Speed" if var == "F" else "Wind Direction"
        ax.text(0.5, 0.5, f"Wind variable not available ({label}).", ha="center", va="center", color="#888888")
        return

    years = wind["_date"].dt.year
    sub = wind[(years >= year_range[0]) & (years <= year_range[1])].copy()
    if len(sub) == 0:
        ax.text(0.5, 0.5, "No data in the selected year range.", ha="center", va="center", color="#888888")
        return
    sub["day"] = sub["_date"].dt.floor("D")
    vals = sub[col].astype(float)

    if var == "F":
        daily = sub.groupby("day")[col].mean()
        ax.plot(daily.index, daily.values, color=COL_WIND_SPEED, lw=1)
        ax.set_ylabel("Wind speed [m/s]")
        title = f"Daily Mean Wind Speed, {year_range[0]}-{year_range[1]}"
    else:
        rad = np.radians(vals)
        sub["_u"] = np.sin(rad)
        sub["_v"] = np.cos(rad)
        daily = sub.groupby("day")[["_u", "_v"]].mean()
        direction = np.degrees(np.arctan2(daily["_u"], daily["_v"])) % 360
        ax.plot(daily.index, direction.values, ".", color=COL_WIND_DIR, ms=3)
        ax.set_ylim(0, 360)
        ax.set_yticks([0, 90, 180, 270, 360])
        ax.set_yticklabels(["N", "E", "S", "W", "N"])
        ax.set_ylabel("Wind direction")
        title = f"Daily Mean Wind Direction, {year_range[0]}-{year_range[1]}"

    ax.set_xlabel("Date")
    ax.set_title(_station_subtitle(station_name, lat, lon, height) + "\n" + title,
                 loc="left", fontsize=11)
    ax.grid(axis="y", color="#eeeeee")
    _attribute(ax)
    fig.tight_layout()


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def plot_climate_chart(fig, chart, year_range, station_name, lat, lon, height):
    fig.clear()
    ax1 = fig.add_subplot(111)
    if chart is None or len(chart) == 0 or "MESS_DATUM_BEGINN" not in chart.columns:
        ax1.text(0.5, 0.5, "No monthly climate data available.", ha="center", va="center", color="#888888")
        return
    dt = chart["MESS_DATUM_BEGINN"].astype("int64")
    year = dt // 10000
    month = (dt // 100) % 100
    sub = chart[(year >= year_range[0]) & (year <= year_range[1])].copy()
    sub["_year"] = year[sub.index]
    sub["_month"] = month[sub.index]
    if len(sub) == 0:
        ax1.text(0.5, 0.5, "No data in the selected year range.", ha="center", va="center", color="#888888")
        return

    temp_by_month = sub.groupby("_month")["MO_TT"].mean().reindex(range(1, 13))
    precip_by_month = sub.groupby("_month")["MO_RR"].mean().reindex(range(1, 13))

    ax2 = ax1.twinx()
    ax2.bar(range(1, 13), precip_by_month.values, color=COL_PRECIP, alpha=0.55, width=0.7, zorder=1)
    ax1.plot(range(1, 13), temp_by_month.values, color=COL_TEMP, lw=2.2, marker="o", ms=4, zorder=2)

    # Classic climate-diagram scaling: 1 degC of the left axis == 2 mm of
    # the right axis. Both axes share one combined range so neither the
    # temperature line nor the precipitation bars get clipped.
    tmin, tmax = np.nanmin(temp_by_month), np.nanmax(temp_by_month)
    pmax = np.nanmax(precip_by_month)
    lo = min(tmin - 2.0, 0.0)
    hi = max(tmax + 2.0, pmax / 2 * 1.1)
    ax1.set_ylim(lo, hi)
    ax2.set_ylim(lo * 2, hi * 2)

    ax1.set_xticks(range(1, 13))
    ax1.set_xticklabels(MONTH_NAMES)
    ax1.set_ylabel("Mean temperature [°C]", color=COL_TEMP)
    ax2.set_ylabel("Mean precipitation [mm]", color=COL_PRECIP)
    ax1.tick_params(axis="y", colors=COL_TEMP)
    ax2.tick_params(axis="y", colors=COL_PRECIP)
    ax1.axhline(0, color="#999999", lw=0.8, zorder=0)
    ax1.set_zorder(ax2.get_zorder() + 1)
    ax1.patch.set_visible(False)

    yrs = sub["_year"]
    ax1.set_title(f"30-Year Climate Chart, {int(yrs.min())}-{int(yrs.max())}", loc="left", fontsize=11)
    ax1.text(0.0, 1.08, _station_subtitle(station_name, lat, lon, height),
            transform=ax1.transAxes, ha="left", va="bottom", fontsize=9, style="italic", color="#555555")
    _attribute(ax1)
    fig.tight_layout()


def plot_trend_anomaly(fig, clim, year_range, station_name):
    fig.clear()
    ax = fig.add_subplot(111)
    if clim is None or len(clim) == 0 or "_date" not in clim.columns:
        ax.text(0.5, 0.5, "No daily climate data available.", ha="center", va="center", color="#888888")
        return
    clim = clim.copy()
    clim["_year"] = clim["_date"].dt.year
    ann = clim.groupby("_year")["TMK"].mean().dropna()
    ann = ann[(ann.index >= year_range[0]) & (ann.index <= year_range[1])]
    if len(ann) < 3:
        ax.text(0.5, 0.5, "Not enough data for trend analysis.", ha="center", va="center", color="#888888")
        return

    baseline_vals = ann[(ann.index >= 1961) & (ann.index <= 1990)]
    if len(baseline_vals) >= 5:
        baseline, base_label = baseline_vals.mean(), "1961-1990 baseline"
    else:
        baseline, base_label = ann.mean(), "period mean"

    years = ann.index.values.astype(float)
    anomaly = ann.values - baseline
    colors = np.where(anomaly >= 0, COL_WARM, COL_COOL)

    ax.axhline(0, color="#999999", lw=0.8)
    ax.bar(years, anomaly, color=colors, width=0.8)

    slope, intercept = np.polyfit(years, anomaly, 1)
    trend_y = intercept + slope * years
    ax.plot(years, trend_y, color="black", lw=2)

    per_decade = slope * 10
    ax.set_ylabel(f"Temperature anomaly [°C] vs {base_label}")
    ax.set_xlabel("Year")
    ax.legend(
        handles=[
            _dummy_line("black", f"Trend: {per_decade:+.2f} °C / decade"),
            _dummy_patch(COL_WARM, f"Warm year (above {base_label})"),
            _dummy_patch(COL_COOL, f"Cool year (below {base_label})"),
        ],
        loc="upper left", fontsize=8, frameon=False,
    )
    ax.set_title(f"Annual Temperature Anomaly -- {station_name}", loc="left", fontsize=11)
    ax.grid(axis="y", color="#eeeeee")
    _attribute(ax)
    fig.tight_layout()


INDEX_LABELS = {
    "JA_FROSTTAGE": "Frost days per year (Tmin < 0°C)",
    "JA_EISTAGE": "Ice days per year (Tmax < 0°C)",
    "JA_HEISSE_TAGE": "Hot days per year (Tmax ≥ 30°C)",
    "JA_SOMMERTAGE": "Summer days per year (Tmax ≥ 25°C)",
    "JA_TROPENNAECHTE": "Tropical nights per year (Tmin ≥ 20°C)",
}
WARM_INDICES = {"JA_HEISSE_TAGE", "JA_SOMMERTAGE", "JA_TROPENNAECHTE"}


def plot_indices(fig, indices, var_col, year_range, station_name):
    fig.clear()
    ax = fig.add_subplot(111)
    if indices is None or len(indices) == 0:
        ax.text(0.5, 0.5, "No annual climate index data available for this station.",
                ha="center", va="center", color="#888888")
        return
    if var_col not in indices.columns:
        ax.text(0.5, 0.5, f"Index '{var_col}' not available for this station.",
                ha="center", va="center", color="#888888")
        return

    dt = indices["MESS_DATUM_BEGINN"].astype("int64")
    year = dt // 10000
    sub = indices.copy()
    sub["_year"] = year
    sub = sub[(sub["_year"] >= year_range[0]) & (sub["_year"] <= year_range[1])]
    sub = sub.dropna(subset=[var_col])
    if len(sub) == 0:
        ax.text(0.5, 0.5, "No valid values in the selected year range.", ha="center", va="center", color="#888888")
        return

    label = INDEX_LABELS.get(var_col, var_col)
    color = COL_WARM if var_col in WARM_INDICES else COL_COOL
    ax.bar(sub["_year"], sub[var_col], color=color)
    ax.set_ylabel(label)
    ax.set_xlabel("Year")

    if len(sub) >= 5:
        slope, intercept = np.polyfit(sub["_year"], sub[var_col], 1)
        trend_y = intercept + slope * sub["_year"]
        ax.plot(sub["_year"], trend_y, color="black", lw=2)
        per_decade = slope * 10
        ax.legend(handles=[_dummy_line("black", f"Trend: {per_decade:+.1f} days / decade")],
                  loc="upper left", fontsize=8, frameon=False)

    ax.set_title(f"{label} -- {station_name}", loc="left", fontsize=11)
    ax.grid(axis="y", color="#eeeeee")
    _attribute(ax)
    fig.tight_layout()


def plot_phenology_trend(fig, df, phase_id, year_range, station_name):
    """Entry day-of-year (Jultag) vs. year for one phenological phase --
    scatter + linear trend, same visual language as plot_trend_anomaly. A
    negative slope means the phase is happening earlier over time, the
    standard phenology climate-change signal."""
    fig.clear()
    ax = fig.add_subplot(111)
    if df is None or len(df) == 0:
        ax.text(0.5, 0.5, "No phenology data available for this station/species.",
                ha="center", va="center", color="#888888")
        return

    sub = df[df["Phase_id"] == phase_id].dropna(subset=["Jultag"])
    sub = sub[(sub["Referenzjahr"] >= year_range[0]) & (sub["Referenzjahr"] <= year_range[1])]
    if len(sub) == 0:
        ax.text(0.5, 0.5, "No data for this phase in the selected year range.",
                ha="center", va="center", color="#888888")
        return

    years = sub["Referenzjahr"].values.astype(float)
    doy = sub["Jultag"].values.astype(float)

    ax.scatter(years, doy, color=COL_TEMP, s=22, zorder=3)

    if len(sub) >= 5:
        slope, intercept = np.polyfit(years, doy, 1)
        trend_y = intercept + slope * years
        ax.plot(years, trend_y, color="black", lw=2, zorder=2)
        per_decade = slope * 10
        ax.legend(handles=[_dummy_line("black", f"Trend: {per_decade:+.1f} days / decade")],
                  loc="upper left", fontsize=8, frameon=False)

    phase_label = sub["Phase_de"].iloc[0] if "Phase_de" in sub.columns else str(phase_id)
    plant_label = sub["Objekt_de"].iloc[0] if "Objekt_de" in sub.columns else ""

    ax.set_xlabel("Year")
    ax.set_ylabel("Day of year")
    ax.set_title(f"{plant_label} – {phase_label} – {station_name}", loc="left", fontsize=11)
    ax.grid(axis="y", color="#eeeeee")
    _attribute(ax)
    fig.tight_layout()


def plot_raster_timeline(fig, xs, values, title, location, ylabel, kind="line",
                         bar_width=None, total_unit=None):
    """Values of every grid in a timeline series at one picked map point.
    `xs` are datetimes (RADOLAN) or step labels (climatology series, drawn
    as a categorical axis); None in `values` = no data there (outside the
    grid / no radar coverage) and is skipped, not drawn as zero. Datetimes
    mark the END of each accumulation interval (DWD convention), so time
    bars are drawn leftwards from x (negative width, edge-aligned)."""
    fig.clear()
    ax = fig.add_subplot(111)
    pts = [(x, v) for x, v in zip(xs, values) if v is not None]
    if not pts:
        ax.text(0.5, 0.5, "No data at this location (outside the grid or no radar coverage).",
                ha="center", va="center", color="#888888")
        ax.set_axis_off()
        return
    is_time = not isinstance(xs[0], str)
    px = [x for x, _ in pts] if is_time else list(range(len(xs)))
    py = [v for _, v in pts] if is_time else [np.nan if v is None else v for v in values]

    if kind == "bar":
        width = -bar_width if (is_time and bar_width) else 0.8
        ax.bar(px, py, width=width, align="edge" if is_time else "center",
               color=COL_PRECIP, edgecolor="none")
    else:
        ax.plot(px, py, color=COL_TEMP, lw=1.8, marker="o", ms=5)

    if not is_time:
        ax.set_xticks(range(len(xs)))
        long_labels = max(len(x) for x in xs) > 4
        ax.set_xticklabels(xs, rotation=30 if long_labels else 0,
                           ha="right" if long_labels else "center")
    else:
        fig.autofmt_xdate()

    if total_unit:
        total = float(np.nansum(py))
        ax.legend(handles=[_dummy_patch(COL_PRECIP, f"Total: {total:.1f} {total_unit}  |  "
                                                    f"max: {np.nanmax(py):.1f} {total_unit}")],
                  loc="upper left", fontsize=8, frameon=False)

    ax.set_ylabel(ylabel)
    ax.set_title(f"{title}\n{location}", loc="left", fontsize=11)
    ax.grid(axis="y", color="#eeeeee")
    _attribute(ax)
    fig.tight_layout()


def _dummy_line(color, label):
    from matplotlib.lines import Line2D
    return Line2D([0], [0], color=color, lw=2, label=label)


def _dummy_patch(color, label):
    from matplotlib.patches import Patch
    return Patch(color=color, label=label)
