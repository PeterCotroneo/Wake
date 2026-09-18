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
    QgsSingleSymbolRenderer,
    QgsProperty,
    QgsSymbolLayer,
    QgsPalLayerSettings,
    QgsVectorLayerSimpleLabeling,
    QgsMessageLog,
    Qgis,
)

LAYER_NAME = "Wake — Live Vessels"

# ordered layer fields
_FIELDS = [
    ("mmsi", QVariant.String),
    ("name", QVariant.String),
    ("ship_class", QVariant.String),
    ("type_group", QVariant.String),
    ("destination", QVariant.String),
    ("cog", QVariant.Double),
    ("sog", QVariant.Double),
    ("heading", QVariant.Double),
    ("rotation", QVariant.Double),
    ("nav_status", QVariant.Int),
    ("last_seen", QVariant.String),
]
_FIELD_INDEX = {name: i for i, (name, _t) in enumerate(_FIELDS)}


def _type_group(type_code):
    try:
        code = int(type_code)
    except (TypeError, ValueError):
        return "Other"
    if 60 <= code <= 69:
        return "Passenger"
    if 70 <= code <= 79:
        return "Cargo"
    if 80 <= code <= 89:
        return "Tanker"
    if code == 30:
        return "Fishing"
    return "Other"


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
        self._style(layer)
        QgsProject.instance().addMapLayer(layer)
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
            symbol = QgsMarkerSymbol.createSimple(
                {"name": "triangle", "size": "4", "color": "gray",
                 "outline_color": "black", "outline_width": "0.2"})
            symbol.setDataDefinedAngle(QgsProperty.fromField("rotation"))
            color_expr = (
                "CASE"
                " WHEN \"type_group\"='Tanker' THEN '#d62728'"
                " WHEN \"type_group\"='Cargo' THEN '#2ca02c'"
                " WHEN \"type_group\"='Passenger' THEN '#1f77b4'"
                " WHEN \"type_group\"='Fishing' THEN '#ff7f0e'"
                " ELSE '#7f7f7f' END")
            symbol.symbolLayer(0).setDataDefinedProperty(
                QgsSymbolLayer.PropertyFillColor, QgsProperty.fromExpression(color_expr))
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
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
            _FIELD_INDEX["ship_class"]: rec.get("ship_class", ""),
            _FIELD_INDEX["type_group"]: _type_group(rec.get("type_code")),
            _FIELD_INDEX["destination"]: rec.get("destination", ""),
            _FIELD_INDEX["cog"]: rec.get("cog"),
            _FIELD_INDEX["sog"]: rec.get("sog"),
            _FIELD_INDEX["heading"]: rec.get("heading"),
            _FIELD_INDEX["rotation"]: _rotation(rec),
            _FIELD_INDEX["nav_status"]: rec.get("nav_status"),
            _FIELD_INDEX["last_seen"]: rec.get("last_seen", ""),
        }

    def flush(self):
        """Apply buffered adds/updates to the layer in one pass. Main thread."""
        if not self._layer_valid() or (not self._pending_new and not self._pending_upd):
            return
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
                if ok:
                    for mmsi, f in zip(mmsis, added):
                        self._fid[mmsi] = f.id()
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
        for mmsi in stale:
            self._records.pop(mmsi, None)
            self._fid.pop(mmsi, None)

    def count(self):
        return len(self._fid)
