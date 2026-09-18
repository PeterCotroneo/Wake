"""Temporary tracer for diagnosing the live path.

Writes to ~/wake_debug.log AND fans out to any registered sinks (the plugin's
in-panel log view). All callers are on the GUI thread, so sinks may touch
widgets directly. Remove once the plugin is confirmed working.
"""

import os
import time

_PATH = os.path.expanduser("~/wake_debug.log")
_sinks = []


def add_sink(fn):
    if fn not in _sinks:
        _sinks.append(fn)


def clear_sinks():
    _sinks.clear()


def dbg(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(_PATH, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    for fn in list(_sinks):
        try:
            fn(line)
        except Exception:
            pass
