"""In-app activity feed.

Messages are shown in the plugin's collapsible Activity panel via registered
sinks. No file is written. All callers are on the GUI thread, so sinks may
touch widgets directly.
"""

import logging
import time

_log = logging.getLogger("wake")
_sinks = []


def add_sink(fn):
    if fn not in _sinks:
        _sinks.append(fn)


def clear_sinks():
    _sinks.clear()


def dbg(msg):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    for fn in list(_sinks):
        try:
            fn(line)
        except Exception:
            _log.debug("activity sink failed", exc_info=True)
