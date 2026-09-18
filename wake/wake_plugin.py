"""Wake — main plugin class.

A dock panel to pick a provider and an area (current map view or a drawn box),
start/stop a live AIS stream, and watch vessels move. Streaming happens inside
the provider (off the GUI thread for socket I/O); the map is updated in batches
by a timer so QGIS stays responsive.
"""

import os

from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import (
    QAction, QDockWidget, QWidget, QVBoxLayout, QLabel, QComboBox, QLineEdit,
    QRadioButton, QButtonGroup, QPushButton, QGroupBox, QFormLayout,
)
from qgis.core import (
    QgsSettings, QgsProject, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsRectangle, QgsPointXY, QgsWkbTypes, QgsMessageLog, Qgis,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .providers import PROVIDERS
from .vessels import VesselStore
from ._debug import dbg

FLUSH_MS = 1000            # batch map updates once a second
STALE_SECONDS = 600        # drop vessels not heard from in 10 minutes
plugin_dir = os.path.dirname(__file__)


class RectangleMapTool(QgsMapTool):
    """Drag a rectangle on the canvas; emits it (in map CRS) on release."""

    rectangle_drawn = pyqtSignal(QgsRectangle)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rb.setColor(QColor(30, 120, 200, 60))
        self.rb.setStrokeColor(QColor(30, 120, 200))
        self.rb.setWidth(1)
        self._start = None

    def canvasPressEvent(self, event):
        self._start = self.toMapCoordinates(event.pos())
        self.rb.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if self._start is not None:
            self._draw(QgsRectangle(self._start, self.toMapCoordinates(event.pos())))

    def canvasReleaseEvent(self, event):
        if self._start is None:
            return
        rect = QgsRectangle(self._start, self.toMapCoordinates(event.pos()))
        self._start = None
        self._draw(rect)
        if not rect.isEmpty():
            self.rectangle_drawn.emit(rect)

    def _draw(self, rect):
        self.rb.reset(QgsWkbTypes.PolygonGeometry)
        corners = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
        ]
        for pt in corners:
            self.rb.addPoint(pt, False)
        self.rb.addPoint(corners[0], True)

    def clear(self):
        self.rb.reset(QgsWkbTypes.PolygonGeometry)


class WakePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.provider = None
        self.store = VesselStore()
        self.timer = None
        self.rect_tool = None
        self.drawn_rect = None      # QgsRectangle in map CRS
        self._last_status = "Idle"
        self._running = False

    # --- plugin lifecycle ------------------------------------------------
    def initGui(self):
        icon = QIcon(os.path.join(plugin_dir, "icon.svg"))
        self.action = QAction(icon, "Wake", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.toggled.connect(self._toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("Wake", self.action)

    def unload(self):
        self._stop()
        if self.rect_tool is not None:
            self.iface.mapCanvas().unsetMapTool(self.rect_tool)
            self.rect_tool = None
        self.store.remove_layer()
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("Wake", self.action)
            self.action = None

    def _toggle_dock(self, checked):
        if checked:
            if self.dock is None:
                self.dock = self._build_dock()
                self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.show()
        elif self.dock is not None:
            self.dock.hide()

    # --- UI --------------------------------------------------------------
    def _build_dock(self):
        dock = QDockWidget("Wake", self.iface.mainWindow())
        panel = QWidget()
        layout = QVBoxLayout(panel)

        intro = QLabel(
            "Watch live vessel traffic. Pick an area, then Start — ships stream "
            "onto the map and move in real time. Live only; no history."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # provider + key
        src = QGroupBox("Source")
        form = QFormLayout(src)
        self.cbo_provider = QComboBox()
        for pid, cls in PROVIDERS.items():
            self.cbo_provider.addItem(cls.label, pid)
        self.cbo_provider.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("Provider", self.cbo_provider)
        self.le_key = QLineEdit()
        self.le_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.le_key.setPlaceholderText("aisstream.io API key")
        form.addRow("API key", self.le_key)
        layout.addWidget(src)

        # area
        area = QGroupBox("Area to track")
        a_layout = QVBoxLayout(area)
        self.rb_view = QRadioButton("Track the current map view")
        self.rb_draw = QRadioButton("Draw an area on the map")
        self.rb_view.setChecked(True)
        self.area_group = QButtonGroup(panel)
        self.area_group.addButton(self.rb_view)
        self.area_group.addButton(self.rb_draw)
        self.rb_view.toggled.connect(self._on_area_mode_changed)
        a_layout.addWidget(self.rb_view)
        a_layout.addWidget(self.rb_draw)
        self.lbl_area = QLabel("")
        self.lbl_area.setStyleSheet("color: gray;")
        a_layout.addWidget(self.lbl_area)
        layout.addWidget(area)

        self.btn_start = QPushButton("Start tracking")
        self.btn_start.clicked.connect(self._on_start_clicked)
        layout.addWidget(self.btn_start)

        self.lbl_status = QLabel("Idle")
        layout.addWidget(self.lbl_status)
        layout.addStretch(1)

        dock.setWidget(panel)
        self._on_provider_changed()
        return dock

    def _on_provider_changed(self, *_):
        pid = self.cbo_provider.currentData()
        cls = PROVIDERS.get(pid)
        needs_key = bool(cls and cls.requires_api_key)
        self.le_key.setVisible(needs_key)
        if needs_key:
            saved = QgsSettings().value(f"wake/{pid}/api_key", "", type=str)
            self.le_key.setText(saved)

    def _on_area_mode_changed(self, *_):
        if self.rb_draw.isChecked():
            self._activate_draw_tool()
            self.lbl_area.setText("Drag a box on the map to set the area.")
        else:
            if self.rect_tool is not None:
                self.iface.mapCanvas().unsetMapTool(self.rect_tool)
                self.rect_tool.clear()
            self.lbl_area.setText("")

    def _activate_draw_tool(self):
        if self.rect_tool is None:
            self.rect_tool = RectangleMapTool(self.iface.mapCanvas())
            self.rect_tool.rectangle_drawn.connect(self._on_rectangle_drawn)
        self.iface.mapCanvas().setMapTool(self.rect_tool)

    def _on_rectangle_drawn(self, rect):
        self.drawn_rect = QgsRectangle(rect)
        self.lbl_area.setText("Area set. Press Start.")

    # --- start / stop ----------------------------------------------------
    def _on_start_clicked(self):
        if self._running:
            self._stop()
            return
        self._start()

    def _bbox_wgs84(self):
        """Return (lat_min, lon_min, lat_max, lon_max) for the chosen area, or None."""
        dst = QgsCoordinateReferenceSystem("EPSG:4326")
        canvas = self.iface.mapCanvas()
        if self.rb_draw.isChecked():
            if self.drawn_rect is None:
                return None
            rect = QgsRectangle(self.drawn_rect)
            src = canvas.mapSettings().destinationCrs()
        else:
            rect = canvas.extent()
            src = canvas.mapSettings().destinationCrs()
        if src != dst:
            rect = QgsCoordinateTransform(src, dst, QgsProject.instance()).transformBoundingBox(rect)
        return (rect.yMinimum(), rect.xMinimum(), rect.yMaximum(), rect.xMaximum())

    def _start(self):
        bar = self.iface.messageBar()
        pid = self.cbo_provider.currentData()
        cls = PROVIDERS.get(pid)
        if cls is None:
            return

        api_key = ""
        if cls.requires_api_key:
            api_key = self.le_key.text().strip()
            if not api_key:
                bar.pushWarning("Wake", "Enter your API key first.")
                return
            QgsSettings().setValue(f"wake/{pid}/api_key", api_key)

        bbox = self._bbox_wgs84()
        if bbox is None:
            bar.pushWarning("Wake", "Draw an area on the map first.")
            return

        dbg(f"_start: provider={pid} bbox(lat_min,lon_min,lat_max,lon_max)={bbox}")
        self.store.ensure_layer()
        self.provider = cls(api_key) if cls.requires_api_key else cls()
        self.provider.vessel_update.connect(self.store.ingest)
        self.provider.status_changed.connect(self._on_status)
        self.provider.error.connect(self._on_error)
        self.provider.start([bbox])

        self.timer = QTimer()
        self.timer.setInterval(FLUSH_MS)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

        self._running = True
        self.btn_start.setText("Stop tracking")
        self._last_status = "Starting…"
        self._update_status()

    def _stop(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        if self.provider is not None:
            try:
                self.provider.stop()
            except Exception as exc:  # noqa: BLE001
                QgsMessageLog.logMessage(f"stop: {exc}", "Wake", Qgis.MessageLevel.Warning)
            self.provider = None
        self.store.flush()
        self._running = False
        self._last_status = "Stopped"
        if self.btn_start is not None:
            self.btn_start.setText("Start tracking")
        self._update_status()

    # --- runtime ---------------------------------------------------------
    def _on_tick(self):
        try:
            self.store.flush()
            self.store.expire(STALE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            import traceback
            dbg(f"_on_tick ERROR: {exc}\n{traceback.format_exc()}")
        self._update_status()

    def _on_status(self, text):
        self._last_status = text
        self._update_status()

    def _on_error(self, text):
        self.iface.messageBar().pushWarning("Wake", text)
        self._last_status = f"Error: {text}"
        self._update_status()

    def _update_status(self):
        if self.lbl_status is not None:
            self.lbl_status.setText(f"{self._last_status} · {self.store.count()} vessels")
