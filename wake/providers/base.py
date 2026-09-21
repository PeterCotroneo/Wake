"""Provider abstraction for Wake.

A provider is anything that can deliver live AIS vessel reports for one or more
geographic areas — aisstream.io today, but the layer and UI never need to know
which one. To add a provider, subclass :class:`AisProvider`, do your I/O off the
GUI thread, and emit ``vessel_update`` with the normalised vessel dict below.

Normalised vessel dict (keys a provider may emit; all optional except mmsi):
    mmsi (str)            - unique vessel id
    name (str)           - vessel name, trimmed
    lat, lon (float)     - position (WGS84)
    cog (float)          - course over ground, degrees
    sog (float)          - speed over ground, knots
    heading (float)      - true heading, degrees (511 = not available)
    nav_status (int)     - AIS navigational status code
    ship_class (str)     - "A" or "B"
    type_code (int)      - AIS ship-and-cargo type code (from static data)
    destination (str)    - reported destination (from static data)
    last_seen (str)      - provider timestamp, if any

A position message carries lat/lon; a static message carries type_code/
destination. The vessel store merges both by mmsi, so a provider can emit
whichever fields a given message contains.
"""

from qgis.PyQt.QtCore import QObject, pyqtSignal


class AisProvider(QObject):
    """Abstract live-AIS source. Subclasses implement start()/stop()."""

    vessel_update = pyqtSignal(dict)   # one normalised vessel dict per report
    status_changed = pyqtSignal(str)   # human-readable connection status
    error = pyqtSignal(str)            # human-readable error

    #: short identifier used in config/registry (e.g. "aisstream")
    id = "base"
    #: label shown in the provider dropdown
    label = "Abstract provider"
    #: one-line help / how-to shown in the configure dialog
    help_text = ""
    #: settings the provider needs, rendered by the configure dialog as fields.
    #: each is {"key", "label", "masked" (bool), "placeholder" (optional)}.
    #: an empty list means the provider needs no configuration.
    config_fields = []

    def __init__(self, settings=None, parent=None):
        """`settings` is a dict of {field key: value} gathered from
        ``config_fields`` (empty/absent for providers that need none)."""
        super().__init__(parent)
        self.settings = settings or {}

    @classmethod
    def needs_config(cls):
        return bool(cls.config_fields)

    def start(self, bboxes):
        """Begin streaming for ``bboxes`` — a list of (lat_min, lon_min,
        lat_max, lon_max) tuples. Must not block the GUI thread."""
        raise NotImplementedError

    def stop(self):
        """Stop streaming and release the connection."""
        raise NotImplementedError

    def update_area(self, bboxes):
        """Change the tracked area on a live connection, if the provider
        supports it (default: no-op). Same bbox format as start()."""
        pass
