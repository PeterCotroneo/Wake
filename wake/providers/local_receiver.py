"""Local AIS receiver provider (AIS-catcher / rtl-ais / dAISy).

Live AIS from your *own* antenna — no cloud, no API key. AIS-catcher decodes
AIS off an SDR and serves decoded JSON from its built-in web server
(``AIS-catcher -N 8100`` -> ``/api/ships.json``). We poll that endpoint and
normalise each ship to the base-class contract, exactly like the Digitraffic
REST provider.

The parser is deliberately forgiving: it accepts AIS-catcher's ships.json
(``{"ships": [...]}``), its HTTP-push payload (``{"msgs": [...]}``) and a bare
array, and maps AIS-catcher's field names (``speed``/``course``/``shipname``/
``shiptype``/``to_bow``…) onto Wake's normalised vessel dict. So it works with
anything that emits AIS-catcher-style JSON.
"""

import json
from datetime import datetime, timezone

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QUrl, QTimer
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from .base import AisProvider
from .._debug import dbg

POLL_MS = 5000
DEFAULT_URL = "http://127.0.0.1:8100/api/ships.json"
_CLASS_A_TYPES = (1, 2, 3, 5)
_CLASS_B_TYPES = (18, 19, 24)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(rxtime):
    """AIS-catcher rxtime is 'YYYYMMDDHHMMSS' on the UTC host clock."""
    if not rxtime:
        return ""
    try:
        return datetime.strptime(str(rxtime), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return ""


def _ships(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("ships", "msgs", "vessels"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def _ship_class(ship, rec):
    raw = str(ship.get("shipclass") or ship.get("class_type")
              or ship.get("mmsi_type") or "").upper()
    if "B" in raw:
        return "B"
    if raw.strip() in ("A",) or "CLASS A" in raw:
        return "A"
    mtype = ship.get("type")  # AIS message type, when the feed carries it
    if isinstance(mtype, int):
        if mtype in _CLASS_B_TYPES:
            return "B"
        if mtype in _CLASS_A_TYPES:
            return "A"
    return None


def parse_ships(data):
    """Normalise a decoded AIS-catcher JSON payload into vessel dicts."""
    out = []
    for s in _ships(data):
        if not isinstance(s, dict):
            continue
        mmsi = s.get("mmsi")
        if mmsi in (None, 0):
            continue
        rec = {"mmsi": str(mmsi)}
        lat, lon = _num(s.get("lat")), _num(s.get("lon"))
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            rec["lat"], rec["lon"] = lat, lon
        rec["sog"] = _num(s.get("speed", s.get("sog")))
        rec["cog"] = _num(s.get("course", s.get("cog")))
        heading = _num(s.get("heading"))
        rec["heading"] = None if heading in (None, 511) else heading
        nav = s.get("status", s.get("nav_status"))
        if nav is not None:
            rec["nav_status"] = nav
        name = (s.get("shipname") or s.get("name") or "").strip()
        if name:
            rec["name"] = name
        stype = s.get("shiptype", s.get("ship_type"))
        if stype not in (None, 0):
            rec["type_code"] = stype
        callsign = (s.get("callsign") or "").strip()
        if callsign:
            rec["callsign"] = callsign
        dest = (s.get("destination") or "").strip()
        if dest:
            rec["destination"] = dest
        imo = s.get("imo")
        if imo:
            rec["imo"] = imo
        draught = _num(s.get("draught", s.get("draft")))
        if draught:
            rec["draught"] = draught  # AIS-catcher scales to metres already
        a, b = _num(s.get("to_bow")), _num(s.get("to_stern"))
        c, d = _num(s.get("to_port")), _num(s.get("to_starboard"))
        if a is not None and b is not None and (a + b) > 0:
            rec["length"] = a + b
        if c is not None and d is not None and (c + d) > 0:
            rec["beam"] = c + d
        cls = _ship_class(s, rec)
        if cls:
            rec["ship_class"] = cls
        seen = _iso(s.get("rxtime"))
        if seen:
            rec["last_seen"] = seen
        out.append(rec)
    return out


class LocalReceiverProvider(AisProvider):
    id = "local"
    label = "Local receiver (AIS-catcher)"
    help_text = (
        'Live AIS from your own receiver — no key, no internet. Run AIS-catcher '
        'with its web server, e.g. <code>AIS-catcher -N 8100</code>, and point '
        'this at its JSON endpoint. Works with anything that serves '
        'AIS-catcher-style JSON. Needs a VHF AIS receiver + antenna (RTL-SDR, '
        'dAISy, etc.); coverage is whatever your antenna hears (~20–40 nm).')
    config_fields = [
        {"key": "url", "label": "Receiver JSON URL", "secret": False,
         "default": DEFAULT_URL, "placeholder": DEFAULT_URL},
    ]

    def __init__(self, settings=None, parent=None):
        super().__init__(settings, parent)
        self._url = (self.settings.get("url") or DEFAULT_URL).strip()
        self._want = False
        self._nam = QgsNetworkAccessManager.instance()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # --- AisProvider interface ------------------------------------------
    def start(self, bboxes):
        self._want = True
        self.status_changed.emit("Connecting…")
        dbg(f"Polling local receiver at {self._url}")
        self._fetch()
        self._timer.start()

    def stop(self):
        self._want = False
        self._timer.stop()

    # fetch-all from a local receiver; the store clips to the current view.

    # --- polling --------------------------------------------------------
    def _poll(self):
        if self._want:
            self._fetch()

    def _fetch(self):
        req = QNetworkRequest(QUrl(self._url))
        req.setRawHeader(b"Accept", b"application/json")
        reply = self._nam.get(req)
        reply.finished.connect(lambda: self._on_reply(reply))

    def _on_reply(self, reply):
        reply.deleteLater()
        if not self._want:
            return
        # NB: reply.error() returns an enum member that is truthy even for
        # NoError (value 0), so compare explicitly rather than `if reply.error()`.
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.error.emit(
                f"Local receiver unreachable at {self._url} "
                f"({reply.errorString()}). Is AIS-catcher running with -N?")
            return
        try:
            data = json.loads(bytes(reply.readAll()))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Local receiver returned bad JSON: {exc}")
            return
        vessels = parse_ships(data)
        for v in vessels:
            self.vessel_update.emit(v)
        self.status_changed.emit("Connected")
        dbg(f"Local receiver: {len(vessels)} vessels")
