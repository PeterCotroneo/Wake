"""Temporary file tracer for diagnosing the live path. Writes to
~/wake_debug.log so it can be read without opening a QGIS panel. Remove once
the plugin is confirmed working."""

import os
import time

_PATH = os.path.expanduser("~/wake_debug.log")


def dbg(msg):
    try:
        with open(_PATH, "a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass
