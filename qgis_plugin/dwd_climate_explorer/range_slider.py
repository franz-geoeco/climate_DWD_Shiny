"""A two-handle range slider (year-range picker), since PyQt5 has no
built-in one. Recreates the R app's dual-ended sliderInput() widgets
natively, with a live "YYYY - YYYY" label."""
from qgis.PyQt.QtCore import Qt, pyqtSignal, QRect
from qgis.PyQt.QtGui import QPainter, QColor, QPen
from qgis.PyQt.QtWidgets import QWidget, QStyle, QStyleOptionSlider, QSizePolicy


class RangeSlider(QWidget):
    rangeChanged = pyqtSignal(int, int)

    HANDLE_RADIUS = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min = 1900
        self._max = 2024
        self._lo = 1990
        self._hi = 2024
        self._pressed = None  # 'lo' | 'hi' | None
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    # -- public API -----------------------------------------------------
    def setBounds(self, lo, hi):
        if hi <= lo:
            hi = lo + 1
        self._min, self._max = lo, hi
        self._lo = max(self._lo, lo)
        self._hi = min(self._hi, hi)
        if self._hi <= self._lo:
            self._lo, self._hi = lo, hi
        self.update()

    def setValues(self, lo, hi):
        lo = max(self._min, min(lo, self._max))
        hi = max(self._min, min(hi, self._max))
        if hi < lo:
            lo, hi = hi, lo
        changed = (lo, hi) != (self._lo, self._hi)
        self._lo, self._hi = lo, hi
        self.update()
        if changed:
            self.rangeChanged.emit(self._lo, self._hi)

    def values(self):
        return self._lo, self._hi

    def bounds(self):
        return self._min, self._max

    # -- geometry helpers -------------------------------------------------
    def _track_rect(self):
        m = self.HANDLE_RADIUS + 2
        return QRect(m, self.height() // 2 - 2, max(1, self.width() - 2 * m), 4)

    def _value_to_x(self, value):
        track = self._track_rect()
        span = self._max - self._min
        if span <= 0:
            return track.left()
        frac = (value - self._min) / span
        return track.left() + int(frac * track.width())

    def _x_to_value(self, x):
        track = self._track_rect()
        span = self._max - self._min
        if track.width() <= 0:
            return self._min
        frac = (x - track.left()) / track.width()
        frac = min(1.0, max(0.0, frac))
        return round(self._min + frac * span)

    # -- painting -----------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = self._track_rect()

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#dddddd"))
        painter.drawRoundedRect(track, 2, 2)

        x_lo = self._value_to_x(self._lo)
        x_hi = self._value_to_x(self._hi)
        active = QRect(x_lo, track.top(), max(1, x_hi - x_lo), track.height())
        painter.setBrush(QColor("#2c7fb8"))
        painter.drawRoundedRect(active, 2, 2)

        for x in (x_lo, x_hi):
            painter.setPen(QPen(QColor("#2c7fb8"), 1.5))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(x - self.HANDLE_RADIUS, self.height() // 2 - self.HANDLE_RADIUS,
                                 2 * self.HANDLE_RADIUS, 2 * self.HANDLE_RADIUS)

    # -- interaction -----------------------------------------------------
    def mousePressEvent(self, event):
        x = event.pos().x()
        x_lo = self._value_to_x(self._lo)
        x_hi = self._value_to_x(self._hi)
        if abs(x - x_lo) <= abs(x - x_hi):
            self._pressed = "lo"
        else:
            self._pressed = "hi"
        self._drag_to(x)

    def mouseMoveEvent(self, event):
        if self._pressed:
            self._drag_to(event.pos().x())

    def mouseReleaseEvent(self, event):
        self._pressed = None

    def _drag_to(self, x):
        v = self._x_to_value(x)
        if self._pressed == "lo":
            self.setValues(min(v, self._hi), self._hi)
        elif self._pressed == "hi":
            self.setValues(self._lo, max(v, self._lo))
