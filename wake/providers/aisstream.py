"""aisstream.io provider — the first Wake AIS source.

Uses Qt's own WebSocket (``QWebSocket``), so there is no external dependency to
install into QGIS. Message shapes verified live 2026-09-18 against
wss://stream.aisstream.io/v0/stream. Bounding boxes are [[lat_min, lon_min],
[lat_max, lon_max]] (confirmed with real vessels off Felixstowe and Singapore).

Free service; the user supplies their own aisstream.io API key.
"""

import json

from qgis.PyQt.QtCore import QUrl, QTimer
from qgis.core import QgsMessageLog, Qgis

from .._debug import dbg

# qgis.PyQt does not forward QtWebSockets, so import it from the Qt binding
# directly (PyQt6 on QGIS 4 / Qt6, PyQt5 on QGIS 3 / Qt5).
try:
    from PyQt6.QtWebSockets import QWebSocket
except ImportError:  # pragma: no cover - QGIS 3.x
    try:
        from PyQt5.QtWebSockets import QWebSocket
    except ImportError:
        QWebSocket = None

from .base import AisProvider

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
RECONNECT_MS = 3000
_POSITION_TYPES = ("PositionReport", "StandardClassBPositionReport")


class AisStreamProvider(AisProvider):
    id = "aisstream"
    label = "aisstream.io (free — account & API key required)"
    requires_api_key = True

    def __init__(self, api_key, parent=None):
        super().__init__(parent)
        self._key = api_key
        self._bboxes = []
        self._want = False  # whether we should be connected (drives reconnect)
        self._ws = None
        self._reconnect = None
        if QWebSocket is None:
            return  # start() will report the missing-binding error

        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        # aisstream sends JSON as binary frames; handle both text and binary.
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.binaryMessageReceived.connect(self._on_binary)
        self._ws.errorOccurred.connect(
            lambda _err: dbg(f"errorOccurred: {self._ws.errorString()}"))

        self._reconnect = QTimer(self)
        self._reconnect.setSingleShot(True)
        self._reconnect.timeout.connect(self._open)

    # --- AisProvider interface ------------------------------------------
    def start(self, bboxes):
        if self._ws is None:
            self.error.emit(
                "QtWebSockets is not available in this QGIS build; cannot stream AIS.")
            return
        self._bboxes = list(bboxes)
        self._want = True
        self._open()

    def stop(self):
        self._want = False
        if self._reconnect is not None:
            self._reconnect.stop()
        if self._ws is not None:
            self._ws.close()

    # --- socket lifecycle -----------------------------------------------
    def _open(self):
        if not self._want:
            return
        self.status_changed.emit("Connecting…")
        self._ws.open(QUrl(STREAM_URL))

    def _on_connected(self):
        subscription = {
            "APIKey": self._key,
            "BoundingBoxes": [[[b[0], b[1]], [b[2], b[3]]] for b in self._bboxes],
            "FilterMessageTypes": [
                "PositionReport", "StandardClassBPositionReport", "ShipStaticData"],
        }
        payload = json.dumps(subscription)
        sent = self._ws.sendTextMessage(payload)
        self._ws.flush()
        self._msg_count = 0
        dbg(f"_on_connected: sent {sent} bytes (payload {len(payload)}); "
            f"state={self._ws.state()}; sub={subscription['BoundingBoxes']}")
        self.status_changed.emit("Connected")

    def _on_disconnected(self):
        dbg(f"_on_disconnected (close code={self._ws.closeCode()} reason={self._ws.closeReason()!r})")
        self.status_changed.emit("Disconnected")
        if self._want:
            self._reconnect.start(RECONNECT_MS)  # auto-reconnect with backoff

    # --- message decoding (normalises to the base-class contract) --------
    def _on_text(self, text):
        self._handle(text)

    def _on_binary(self, data):
        try:
            self._handle(bytes(data).decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            dbg(f"binary decode error: {exc}")

    def _handle(self, text):
        try:
            message = json.loads(text)
        except (ValueError, TypeError):
            return
        kind = message.get("MessageType")
        self._msg_count = getattr(self, "_msg_count", 0) + 1
        if self._msg_count <= 3 or self._msg_count % 50 == 0:
            dbg(f"_on_message #{self._msg_count}: {kind}")
        if kind == "ErrorMessage":
            self.error.emit(str(message.get("Message")))
            return
        meta = message.get("MetaData", {})

        if kind in _POSITION_TYPES:
            body = message.get("Message", {}).get(kind, {})
            self.vessel_update.emit({
                "mmsi": str(meta.get("MMSI") or body.get("UserID")),
                "name": (meta.get("ShipName") or "").strip(),
                "lat": meta.get("latitude", body.get("Latitude")),
                "lon": meta.get("longitude", body.get("Longitude")),
                "cog": body.get("Cog"),
                "sog": body.get("Sog"),
                "heading": body.get("TrueHeading"),
                "nav_status": body.get("NavigationalStatus"),
                "ship_class": "A" if kind == "PositionReport" else "B",
                "last_seen": meta.get("time_utc"),
            })
        elif kind == "ShipStaticData":
            body = message.get("Message", {}).get("ShipStaticData", {})
            self.vessel_update.emit({
                "mmsi": str(meta.get("MMSI") or body.get("UserID")),
                "name": (meta.get("ShipName") or body.get("Name") or "").strip(),
                "type_code": body.get("Type"),
                "destination": (body.get("Destination") or "").strip(),
            })
