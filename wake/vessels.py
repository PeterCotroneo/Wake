"""Live vessel layer: buffer incoming reports and flush to a memory layer in
batches so the map stays responsive under a busy feed.

Provider messages arrive as normalised vessel dicts (see providers/base.py).
Position and static messages for the same MMSI are merged. Markers are triangles
oriented to heading (falling back to course) and coloured by ship-type group.
Vessels not heard from for a while are expired.
"""

import time

from qgis.PyQt.QtCore import QVariant, QDateTime
from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsMarkerSymbol,
    QgsCategorizedSymbolRenderer,
    QgsRendererCategory,
    QgsProperty,
    QgsPalLayerSettings,
    QgsVectorLayerSimpleLabeling,
    QgsMessageLog,
    Qgis,
)

from ._debug import dbg
from .mid import country_for_mmsi

LAYER_NAME = "Wake — Live Vessels"

# ordered layer fields
_FIELDS = [
    ("mmsi", QVariant.String),
    ("name", QVariant.String),
    ("flag", QVariant.String),
    ("callsign", QVariant.String),
    ("type", QVariant.String),
    ("type_group", QVariant.String),
    ("status", QVariant.String),
    ("destination", QVariant.String),
    ("sog", QVariant.Double),
    ("cog", QVariant.Double),
    ("heading", QVariant.Double),
    ("length_m", QVariant.Int),
    ("beam_m", QVariant.Int),
    ("draught_m", QVariant.Double),
    ("imo", QVariant.Int),
    ("nav_status", QVariant.Int),
    ("rotation", QVariant.Double),
    ("ship_class", QVariant.String),
    ("last_seen", QVariant.String),
]
_FIELD_INDEX = {name: i for i, (name, _t) in enumerate(_FIELDS)}

_ALIASES = {
    "mmsi": "MMSI", "name": "Name", "flag": "Flag / Country",
    "callsign": "Call sign", "type": "Ship type",
    "type_group": "Category", "status": "Nav status", "destination": "Destination",
    "sog": "Speed (kn)", "cog": "Course (°)", "heading": "Heading (°)",
    "length_m": "Length (m)", "beam_m": "Beam (m)", "draught_m": "Draught (m)",
    "imo": "IMO", "nav_status": "Nav code", "ship_class": "AIS class",
    "last_seen": "Last seen (UTC)",
}

_MAP_TIP = (
    "<b>[% \"name\" %]</b> &nbsp;<span style='color:gray'>MMSI [% \"mmsi\" %]</span>"
    "[% CASE WHEN \"flag\" IS NOT NULL AND \"flag\" != '' THEN ' · ' || \"flag\" ELSE '' END %]<br/>"
    "[% coalesce(\"type\",'Vessel') %][% CASE WHEN \"status\" IS NOT NULL AND \"status\" != '' "
    "THEN ' · ' || \"status\" ELSE '' END %]<br/>"
    "Speed [% coalesce(\"sog\",'?') %] kn · Course [% coalesce(\"cog\",'?') %]° · "
    "Heading [% coalesce(\"heading\",'?') %]°<br/>"
    "[% CASE WHEN \"destination\" IS NOT NULL AND \"destination\" != '' "
    "THEN 'Destination: ' || \"destination\" || '<br/>' ELSE '' END %]"
    "[% CASE WHEN \"length_m\" IS NOT NULL THEN 'Size ' || \"length_m\" || '×' || "
    "coalesce(\"beam_m\",'?') || ' m' ELSE '' END %]"
    "[% CASE WHEN \"draught_m\" IS NOT NULL THEN ' · Draught ' || \"draught_m\" || ' m' ELSE '' END %]"
    "[% CASE WHEN \"callsign\" IS NOT NULL AND \"callsign\" != '' "
    "THEN '<br/>Call sign ' || \"callsign\" ELSE '' END %]"
    "[% CASE WHEN \"imo\" IS NOT NULL THEN ' · IMO ' || \"imo\" ELSE '' END %]"
    "<br/><span style='color:gray'>Last seen [% coalesce(\"last_seen\",'—') %]</span>"
)

_TYPE_SPECIALS = {
    30: "Fishing", 31: "Towing", 32: "Towing (long)", 33: "Dredging", 34: "Diving",
    35: "Military", 36: "Sailing", 37: "Pleasure craft", 50: "Pilot",
    51: "Search & rescue", 52: "Tug", 53: "Port tender", 55: "Law enforcement",
    58: "Medical",
}
_NAV_STATUS = {
    0: "Under way (engine)", 1: "At anchor", 2: "Not under command",
    3: "Restricted manoeuvrability", 4: "Constrained by draught", 5: "Moored",
    6: "Aground", 7: "Fishing", 8: "Under way (sailing)", 11: "Towing astern",
    12: "Pushing ahead", 14: "AIS-SART",
}


# category -> colour (also the order shown in the legend)
CATEGORY_COLORS = [
    ("Passenger", "#1f77b4"),
    ("Cargo", "#2ca02c"),
    ("Tanker", "#d62728"),
    ("Fishing", "#ff7f0e"),
    ("Sailing", "#9467bd"),
    ("High-speed", "#17becf"),
    ("Special", "#8c564b"),
    ("Other", "#7f7f7f"),
    ("Unknown", "#b0b0b0"),
]


def _type_group(type_code):
    if type_code is None:
        return "Unknown"
    try:
        code = int(type_code)
    except (TypeError, ValueError):
        return "Unknown"
    if 60 <= code <= 69:
        return "Passenger"
    if 70 <= code <= 79:
        return "Cargo"
    if 80 <= code <= 89:
        return "Tanker"
    if code == 30:
        return "Fishing"
    if code in (36, 37):
        return "Sailing"
    if 40 <= code <= 49:
        return "High-speed"
    if 50 <= code <= 59:
        return "Special"
    return "Other"


def _type_label(type_code):
    try:
        code = int(type_code)
    except (TypeError, ValueError):
        return None
    if code in _TYPE_SPECIALS:
        return _TYPE_SPECIALS[code]
    if 20 <= code <= 29:
        return "Wing-in-ground"
    if 40 <= code <= 49:
        return "High-speed craft"
    if 60 <= code <= 69:
        return "Passenger"
    if 70 <= code <= 79:
        return "Cargo"
    if 80 <= code <= 89:
        return "Tanker"
    if 90 <= code <= 99:
        return "Other"
    return None


def _nav_label(nav_code):
    try:
        return _NAV_STATUS.get(int(nav_code))
    except (TypeError, ValueError):
        return None


def _rotation(rec):
    """Heading if valid (0..359), else course, else 0."""
    for key in ("heading", "cog"):
        val = rec.get(key)
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if 0 <= f < 360:
            return f
    return 0.0


class VesselStore:
    def __init__(self):
        self._layer = None
        self._records = {}   # mmsi -> merged field dict (+ _last_update wall clock)
        self._fid = {}       # mmsi -> feature id in the layer
        self._pending_new = set()
        self._pending_upd = set()

    # --- layer lifecycle -------------------------------------------------
    def ensure_layer(self):
        if self._layer is not None and self._layer_valid():
            return self._layer
        fields = QgsFields()
        for name, qtype in _FIELDS:
            fields.append(QgsField(name, qtype))
        layer = QgsVectorLayer("Point?crs=EPSG:4326", LAYER_NAME, "memory")
        layer.dataProvider().addAttributes(fields.toList())
        layer.updateFields()
        for name, alias in _ALIASES.items():
            idx = layer.fields().indexOf(name)
            if idx >= 0:
                layer.setFieldAlias(idx, alias)
        layer.setMapTipTemplate(_MAP_TIP)
        self._add_actions(layer)
        self._style(layer)
        QgsProject.instance().addMapLayer(layer)

    def _add_actions(self, layer):
        """Right-click / Identify actions that open the vessel's page (photo +
        full details) in the browser, keyed by MMSI. No scraping — just links."""
        try:
            from qgis.core import QgsAction
            try:
                url_type = Qgis.AttributeActionType.OpenUrl
            except AttributeError:
                url_type = QgsAction.OpenUrl
            targets = [
                ("Look up on MarineTraffic",
                 'https://www.marinetraffic.com/en/ais/details/ships/mmsi:[% "mmsi" %]'),
                ("Look up on VesselFinder",
                 'https://www.vesselfinder.com/?mmsi=[% "mmsi" %]'),
            ]
            for label, url in targets:
                layer.actions().addAction(url_type, label, url)
        except Exception as exc:  # noqa: BLE001 - actions are a nicety
            QgsMessageLog.logMessage(f"actions skipped: {exc}", "Wake",
                                     Qgis.MessageLevel.Warning)
        self._layer = layer
        self._records.clear()
        self._fid.clear()
        self._pending_new.clear()
        self._pending_upd.clear()
        return layer

    def _layer_valid(self):
        try:
            return self._layer is not None and self._layer.isValid()
        except RuntimeError:
            return False

    def remove_layer(self):
        if self._layer_valid():
            QgsProject.instance().removeMapLayer(self._layer.id())
        self._layer = None
        self._records.clear()
        self._fid.clear()

    def _style(self, layer):
        try:
            categories = []
            for group, color in CATEGORY_COLORS:
                sym = QgsMarkerSymbol.createSimple(
                    {"name": "triangle", "size": "4", "color": color,
                     "outline_color": "black", "outline_width": "0.2"})
                sym.setDataDefinedAngle(QgsProperty.fromField("rotation"))
                categories.append(QgsRendererCategory(group, sym, group))
            layer.setRenderer(QgsCategorizedSymbolRenderer("type_group", categories))
            pal = QgsPalLayerSettings()
            pal.fieldName = "name"
            layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
            layer.setLabelsEnabled(True)
        except Exception as exc:  # noqa: BLE001 - styling must never block data
            QgsMessageLog.logMessage(f"styling skipped: {exc}", "Wake", Qgis.MessageLevel.Warning)

    # --- ingest / flush --------------------------------------------------
    def ingest(self, vessel):
        mmsi = vessel.get("mmsi")
        if not mmsi:
            return
        rec = self._records.setdefault(mmsi, {})
        for key, val in vessel.items():
            if val is not None:
                rec[key] = val
        rec["_last_update"] = time.time()
        has_pos = rec.get("lat") is not None and rec.get("lon") is not None
        if mmsi in self._fid:
            self._pending_upd.add(mmsi)
        elif has_pos:
            self._pending_new.add(mmsi)

    def _attrs(self, rec):
        return {
            _FIELD_INDEX["name"]: rec.get("name", ""),
            _FIELD_INDEX["flag"]: country_for_mmsi(rec.get("mmsi")),
            _FIELD_INDEX["callsign"]: rec.get("callsign", ""),
            _FIELD_INDEX["type"]: _type_label(rec.get("type_code")),
            _FIELD_INDEX["type_group"]: _type_group(rec.get("type_code")),
            _FIELD_INDEX["status"]: _nav_label(rec.get("nav_status")),
            _FIELD_INDEX["destination"]: rec.get("destination", ""),
            _FIELD_INDEX["sog"]: rec.get("sog"),
            _FIELD_INDEX["cog"]: rec.get("cog"),
            _FIELD_INDEX["heading"]: rec.get("heading"),
            _FIELD_INDEX["length_m"]: rec.get("length"),
            _FIELD_INDEX["beam_m"]: rec.get("beam"),
            _FIELD_INDEX["draught_m"]: rec.get("draught"),
            _FIELD_INDEX["imo"]: rec.get("imo"),
            _FIELD_INDEX["nav_status"]: rec.get("nav_status"),
            _FIELD_INDEX["rotation"]: _rotation(rec),
            _FIELD_INDEX["ship_class"]: rec.get("ship_class", ""),
            _FIELD_INDEX["last_seen"]: rec.get("last_seen", ""),
        }

    def flush(self):
        """Apply buffered adds/updates to the layer in one pass. Main thread."""
        if not self._layer_valid() or (not self._pending_new and not self._pending_upd):
            return
        n_new = len(self._pending_new)
        dp = self._layer.dataProvider()

        # additions
        if self._pending_new:
            feats, mmsis = [], []
            for mmsi in self._pending_new:
                rec = self._records.get(mmsi)
                if not rec:
                    continue
                feat = QgsFeature(self._layer.fields())
                feat.setGeometry(QgsGeometry.fromPointXY(
                    QgsPointXY(float(rec["lon"]), float(rec["lat"]))))
                attrs = [None] * len(_FIELDS)
                attrs[_FIELD_INDEX["mmsi"]] = mmsi
                for idx, val in self._attrs(rec).items():
                    attrs[idx] = val
                feat.setAttributes(attrs)
                feats.append(feat)
                mmsis.append(mmsi)
            if feats:
                ok, added = dp.addFeatures(feats)
                if added:
                    # map by each feature's own MMSI (order-independent) so we
                    # never lose an id and duplicate the vessel next message
                    for f in added:
                        m = f["mmsi"]
                        if m:
                            self._fid[m] = f.id()
                else:
                    # provider returned no feature list; recover ids by scanning
                    want = set(mmsis)
                    for feat in self._layer.getFeatures():
                        m = feat["mmsi"]
                        if m in want:
                            self._fid[m] = feat.id()
            self._pending_new.clear()

        # updates (geometry + attributes)
        if self._pending_upd:
            geom_changes, attr_changes = {}, {}
            for mmsi in self._pending_upd:
                fid = self._fid.get(mmsi)
                rec = self._records.get(mmsi)
                if fid is None or not rec:
                    continue
                if rec.get("lat") is not None and rec.get("lon") is not None:
                    geom_changes[fid] = QgsGeometry.fromPointXY(
                        QgsPointXY(float(rec["lon"]), float(rec["lat"])))
                attr_changes[fid] = self._attrs(rec)
            if geom_changes:
                dp.changeGeometryValues(geom_changes)
            if attr_changes:
                dp.changeAttributeValues(attr_changes)
            self._pending_upd.clear()

        self._layer.updateExtents()
        self._layer.triggerRepaint()
        if n_new:
            dbg(f"+{n_new} new vessel(s) · {self._layer.featureCount()} on the map")

    def expire(self, max_age_seconds):
        if not self._layer_valid():
            return
        now = time.time()
        stale = [mmsi for mmsi, rec in self._records.items()
                 if now - rec.get("_last_update", now) > max_age_seconds]
        fids = [self._fid[mmsi] for mmsi in stale if mmsi in self._fid]
        if fids:
            self._layer.dataProvider().deleteFeatures(fids)
            self._layer.triggerRepaint()
            dbg(f"removed {len(fids)} vessel(s) not seen recently")
        for mmsi in stale:
            self._records.pop(mmsi, None)
            self._fid.pop(mmsi, None)

    def count(self):
        return len(self._fid)

    def retain_within(self, bbox):
        """Drop vessels outside bbox (lat_min, lon_min, lat_max, lon_max) so the
        map reflects the area currently being watched after a pan/zoom."""
        if not self._layer_valid():
            return
        lat_min, lon_min, lat_max, lon_max = bbox
        outside = []
        for mmsi, rec in self._records.items():
            try:
                lat, lon = float(rec.get("lat")), float(rec.get("lon"))
            except (TypeError, ValueError):
                # No position yet (e.g. a static/type message arrived before the
                # first position). Keep it — it isn't on the map, and dropping it
                # would discard the cached type before the position lands.
                continue
            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                outside.append(mmsi)
        fids = [self._fid[m] for m in outside if m in self._fid]
        if fids:
            self._layer.dataProvider().deleteFeatures(fids)
            self._layer.triggerRepaint()
        for mmsi in outside:
            self._records.pop(mmsi, None)
            self._fid.pop(mmsi, None)
