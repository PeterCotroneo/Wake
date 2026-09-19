"""aisstream.io-compatible WebSocket AIS providers.

aisstream.io and Open Waters (aiscast) speak the same protocol — connect to a
WebSocket, send a JSON subscription with BoundingBoxes + FilterMessageTypes,
and receive AIS messages (as binary frames). So both share one implementation
and differ only by URL and the label of their key field. Uses Qt's built-in
QWebSocket — no external dependency.
"""

import json
import time

from qgis.PyQt.QtCore import QUrl, QTimer

# qgis.PyQt does not forward QtWebSockets; import it from the binding directly.
try:
    from PyQt6.QtWebSockets import QWebSocket
except ImportError:  # pragma: no cover - QGIS 3.x
    try:
        from PyQt5.QtWebSockets import QWebSocket
    except ImportError:
        QWebSocket = None

from .base import AisProvider
from .._debug import dbg

RECONNECT_MS = 3000
WATCHDOG_MS = 30000        # how often to check the stream is still alive
SILENCE_LIMIT_S = 120      # no message for this long => assume a silent death
_POSITION_TYPES = ("PositionReport", "StandardClassBPositionReport")


def _fmt_area(bboxes):
    return "; ".join(
        f"lat {b[0]:.2f}…{b[2]:.2f}, lon {b[1]:.2f}…{b[3]:.2f}" for b in bboxes)


class AisStreamLikeProvider(AisProvider):
    """Shared logic for aisstream.io-compatible feeds. Subclasses set
    ``stream_url`` (and their own id/label/config_fields)."""

    stream_url = ""

    def __init__(self, settings=None, parent=None):
        super().__init__(settings, parent)
        self._key = (self.settings.get("api_key") or "").strip()
        self._bboxes = []
        self._want = False
        self._connected = False
        self._ws = None
        self._reconnect = None
        self._watchdog = None
        self._last_rx = 0.0  # monotonic time of the last message received
        if QWebSocket is None:
            return

        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.binaryMessageReceived.connect(self._on_binary)
        self._ws.errorOccurred.connect(self._on_error)

        self._reconnect = QTimer(self)
        self._reconnect.setSingleShot(True)
        self._reconnect.timeout.connect(self._open)

        # Watchdog: sockets often die *silently* after a laptop sleep or network
        # change — no disconnected/error signal fires, so we'd never reconnect and
        # the map would slowly empty. If no message arrives for SILENCE_LIMIT_S
        # while we still want data, force a fresh connection.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self._check_alive)

    # --- AisProvider interface ------------------------------------------
    def start(self, bboxes):
        if self._ws is None:
            self.error.emit("QtWebSockets is not available in this QGIS build.")
            return
        if not self._key:
            self.error.emit("No key/token configured — use Configure provider.")
            return
        self._bboxes = list(bboxes)
        self._want = True
        self._open()
        if self._watchdog is not None:
            self._watchdog.start()

    def stop(self):
        self._want = False
        if self._reconnect is not None:
            self._reconnect.stop()
        if self._watchdog is not None:
            self._watchdog.stop()
        if self._ws is not None:
            self._ws.close()

    def update_area(self, bboxes):
        self._bboxes = list(bboxes)
        if self._connected and self._ws is not None:
            dbg("Map moved — updating watch area")
            self._send_subscription()

    # --- socket lifecycle -----------------------------------------------
    def _open(self):
        if not self._want:
            return
        # abandon any half-open socket before reopening (silent-death recovery)
        if self._ws is not None:
            self._ws.abort()
        self._last_rx = time.monotonic()  # grace period before the watchdog bites
        self.status_changed.emit("Connecting…")
        self._ws.open(QUrl(self.stream_url))

    def _check_alive(self):
        if not self._want or self._reconnect.isActive():
            return
        if time.monotonic() - self._last_rx > SILENCE_LIMIT_S:
            dbg("No data for a while — reconnecting…")
            self._connected = False
            self.status_changed.emit("Reconnecting…")
            self._open()

    def _on_error(self, _err):
        dbg(f"Connection error: {self._ws.errorString()}")
        # errorOccurred can fire without a following disconnected signal, so
        # schedule our own reconnect rather than waiting for one that may not come.
        if self._want and not self._reconnect.isActive():
            self._reconnect.start(RECONNECT_MS)

    def _send_subscription(self):
        subscription = {
            "APIKey": self._key,
            "BoundingBoxes": [[[b[0], b[1]], [b[2], b[3]]] for b in self._bboxes],
            "FilterMessageTypes": [
                "PositionReport", "StandardClassBPositionReport",
                "ShipStaticData", "StaticDataReport"],
        }
        self._ws.sendTextMessage(json.dumps(subscription))
        self._ws.flush()
        dbg(f"Watching area — {_fmt_area(self._bboxes)}")

    def _on_connected(self):
        self._connected = True
        self._last_rx = time.monotonic()
        dbg(f"Connected to {self.label}")
        self._send_subscription()
        self.status_changed.emit("Connected")

    def _on_disconnected(self):
        self._connected = False
        dbg("Connection dropped — reconnecting…")
        self.status_changed.emit("Disconnected")
        if self._want:
            self._reconnect.start(RECONNECT_MS)

    # --- message decoding (normalises to the base-class contract) --------
    def _on_text(self, text):
        self._handle(text)

    def _on_binary(self, data):
        try:
            self._handle(bytes(data).decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            dbg(f"decode error: {exc}")

    def _handle(self, text):
        self._last_rx = time.monotonic()  # proof the stream is alive
        try:
            message = json.loads(text)
        except (ValueError, TypeError):
            return
        kind = message.get("MessageType")
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
            dim = body.get("Dimension") or {}
            imo = body.get("ImoNumber") or 0
            length = (dim.get("A", 0) or 0) + (dim.get("B", 0) or 0)
            beam = (dim.get("C", 0) or 0) + (dim.get("D", 0) or 0)
            self.vessel_update.emit({
                "mmsi": str(meta.get("MMSI") or body.get("UserID")),
                "name": (meta.get("ShipName") or body.get("Name") or "").strip(),
                "type_code": body.get("Type"),
                "destination": (body.get("Destination") or "").strip(),
                "callsign": (body.get("CallSign") or "").strip(),
                "imo": imo if imo else None,
                "length": length or None,
                "beam": beam or None,
                "draught": body.get("MaximumStaticDraught"),
            })
        elif kind == "StaticDataReport":
            body = message.get("Message", {}).get("StaticDataReport", {})
            report_a = body.get("ReportA") or {}
            report_b = body.get("ReportB") or {}
            dim = report_b.get("Dimension") or {}
            length = (dim.get("A", 0) or 0) + (dim.get("B", 0) or 0)
            beam = (dim.get("C", 0) or 0) + (dim.get("D", 0) or 0)
            update = {
                "mmsi": str(meta.get("MMSI") or body.get("UserID")),
                "ship_class": "B",
            }
            name = (report_a.get("Name") or meta.get("ShipName") or "").strip()
            if name:
                update["name"] = name
            if report_b.get("ShipType"):
                update["type_code"] = report_b.get("ShipType")
            callsign = (report_b.get("CallSign") or "").strip()
            if callsign:
                update["callsign"] = callsign
            if length:
                update["length"] = length
            if beam:
                update["beam"] = beam
            self.vessel_update.emit(update)


class AisStreamProvider(AisStreamLikeProvider):
    id = "aisstream"
    label = "aisstream.io"
    stream_url = "wss://stream.aisstream.io/v0/stream"
    help_text = ('Free global AIS. Create a free account at '
                 '<a href="https://aisstream.io">aisstream.io</a>, generate an '
                 'API key, and paste it here.')
    config_fields = [
        {"key": "api_key", "label": "API key", "secret": True,
         "placeholder": "aisstream.io API key"},
    ]


class OpenWatersProvider(AisStreamLikeProvider):
    id = "openwaters"
    label = "Open Waters (aiscast)"
    stream_url = "wss://ais.openwaters.io/v0/stream"
    help_text = ('Open, volunteer-fed AIS network. Get a free token at '
                 '<a href="https://openwatersio.github.io/aiscast/token.html">'
                 'openwatersio.github.io/aiscast/token.html</a> '
                 '(personal tier: 20°×20° area). This feed carries positions '
                 'and names but not ship types, so vessels show as Unknown.')
    config_fields = [
        {"key": "api_key", "label": "Token", "secret": True,
         "placeholder": "aiscast token"},
    ]
