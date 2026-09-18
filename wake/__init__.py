def classFactory(iface):
    """Entry point required by QGIS to load the plugin."""
    from .wake_plugin import WakePlugin
    return WakePlugin(iface)
