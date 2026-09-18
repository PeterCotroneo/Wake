"""Digitraffic (Finnish Transport Infrastructure Agency) AIS provider.

Unlike the aisstream-style feeds, Digitraffic has no streaming socket that QGIS
can consume (its push feed is MQTT, which QGIS doesn't bundle). Instead it offers
a keyless REST API that we poll:

    /api/ais/v1/locations  - GeoJSON positions (mmsi, lon/lat, sog, cog, heading)
    /api/ais/v1/vessels    - static metadata (name, shipType, dimensions, imo…)

Positions and static data live in separate endpoints, so we poll locations often
and refresh the static table occasionally, emitting each as a partial vessel dict
that the vessel store merges by MMSI — exactly like a position/static AIS split.

Coverage is the Baltic / Finnish waters only (~1000 vessels), so no server-side
bounding box is needed; the vessel store clips to the current map view.

The API requires ``Accept-Encoding: gzip`` and a ``Digitraffic-User`` header. We
decompress defensively in case the network stack hands back the raw gzip bytes.
"""

import gzip
import json
from datetime import datetime, timezone

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QUrl, QTimer
from qgis.PyQt.QtNetwork import QNetworkRequest

from .base import AisProvider
from .._debug import dbg

BASE = "https://meri.digitraffic.fi/api/ais/v1"
LOCATIONS_URL = f"{BASE}/locations"
VESSELS_URL = f"{BASE}/vessels"
USER_AGENT = "WakeQGISPlugin"

POLL_MS = 5000            # position refresh cadence
STATIC_EVERY = 60         # refetch static metadata every N position polls (~5 min)


def _decode(reply):
    """Return the parsed JSON body of a finished reply, gunzipping if needed."""
    raw = bytes(reply.readAll())
    if raw[:2] == b"\x1f\x8b":  # gzip magic — stack didn't decompress for us
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _iso(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return ""


class DigitrafficProvider(AisProvider):
    id = "digitraffic"
    label = "Digitraffic (Finland/Baltic)"
    help_text = ('Free, keyless AIS from Finland’s Digitraffic service. No '
                 'account needed. Coverage is the Baltic Sea and Finnish waters '
                 'only — pan the map there to see vessels.')
    config_fields = []  # keyless

    def __init__(self, settings=None, parent=None):
        super().__init__(settings, parent)
        self._want = False
        self._tick = 0
        self._nam = QgsNetworkAccessManager.instance()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # --- AisProvider interface ------------------------------------------
    def start(self, bboxes):
        self._want = True
        self._tick = 0
        self.status_changed.emit("Connecting…")
        dbg("Connecting to Digitraffic")
        self._fetch_statics()
        self._fetch_locations()
        self._timer.start()

    def stop(self):
        self._want = False
        self._timer.stop()

    # Digitraffic is fetch-all (regional); the store clips to the view, so
    # there is nothing to re-subscribe when the map moves.

    # --- polling --------------------------------------------------------
    def _poll(self):
        if not self._want:
            return
        self._tick += 1
        self._fetch_locations()
        if self._tick % STATIC_EVERY == 0:
            self._fetch_statics()

    def _request(self, url):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"Digitraffic-User", USER_AGENT.encode())
        req.setRawHeader(b"Accept-Encoding", b"gzip")
        return req

    def _fetch_locations(self):
        reply = self._nam.get(self._request(LOCATIONS_URL))
        reply.finished.connect(lambda: self._on_locations(reply))

    def _fetch_statics(self):
        reply = self._nam.get(self._request(VESSELS_URL))
        reply.finished.connect(lambda: self._on_statics(reply))

    # --- reply handlers -------------------------------------------------
    def _on_locations(self, reply):
        reply.deleteLater()
        if not self._want:
            return
        try:
            data = _decode(reply)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Digitraffic locations error: {exc}")
            return
        features = data.get("features", [])
        for feat in features:
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or [None, None]
            props = feat.get("properties") or {}
            heading = props.get("heading")
            self.vessel_update.emit({
                "mmsi": str(feat.get("mmsi") or props.get("mmsi")),
                "lat": coords[1],
                "lon": coords[0],
                "cog": props.get("cog"),
                "sog": props.get("sog"),
                "heading": None if heading in (None, 511) else heading,
                "nav_status": props.get("navStat"),
                "last_seen": _iso(props.get("timestampExternal")),
            })
        self.status_changed.emit("Connected")
        dbg(f"Digitraffic: {len(features)} positions")

    def _on_statics(self, reply):
        reply.deleteLater()
        if not self._want:
            return
        try:
            vessels = _decode(reply)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Digitraffic vessels error: {exc}")
            return
        for v in vessels:
            length = (v.get("referencePointA", 0) or 0) + \
                (v.get("referencePointB", 0) or 0)
            beam = (v.get("referencePointC", 0) or 0) + \
                (v.get("referencePointD", 0) or 0)
            draught = v.get("draught")
            imo = v.get("imo") or 0
            self.vessel_update.emit({
                "mmsi": str(v.get("mmsi")),
                "name": (v.get("name") or "").strip(),
                "type_code": v.get("shipType"),
                "callsign": (v.get("callSign") or "").strip(),
                "destination": (v.get("destination") or "").strip(),
                "imo": imo if imo else None,
                "length": length or None,
                "beam": beam or None,
                "draught": (draught / 10.0) if draught else None,
            })
        dbg(f"Digitraffic: {len(vessels)} static records")
