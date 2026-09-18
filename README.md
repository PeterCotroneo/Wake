# Wake

Watch **live marine vessel traffic** on your map. Wake streams real-time AIS
ship positions into QGIS and shows vessels moving — track your current map view,
or draw an area to watch.

Uses free AIS data from **aisstream.io** (a free account and API key are
required; you supply your own key). Wake shows *live* traffic only — it does not
replay history.

## Why Wake

The existing AIS options for QGIS are either receiver-only (need physical AIS
hardware) or track vessels one-by-one by MMSI number and aren't in the official
plugin repository. Wake is different:

- **Area-based, not MMSI-based** — watch *all* traffic in your map view or a drawn box, not just ships you already know.
- **No extra dependencies** — uses Qt's built-in WebSocket, so it installs cleanly from the official QGIS plugin repository.
- **Class A *and* Class B** — sees cargo/tankers *and* the smaller vessels (fishing, leisure) that Class-A-only tools miss.
- **Real vessels** — heading, speed, ship type, name and destination, styled on the map.
- **Extensible** — a provider abstraction so other AIS sources (AISHub, a local SDR/NMEA feed, …) can be added without touching the map or UI.

Pairs naturally with [SeaState US](https://github.com/PeterCotroneo/SeaState-US):
vessel movement and coastal conditions in one map.

## Getting a key

Create a free account at [aisstream.io](https://aisstream.io/), generate an API
key, and paste it into Wake's panel (stored in QGIS settings, never in the
project or the repo).

## Layout

```
wake/
  metadata.txt          QGIS plugin metadata
  __init__.py           classFactory entry point
  wake_plugin.py        plugin: dock panel, "Track this area", start/stop, status
  vessels.py            live vessel layer + batched updates + styling
  providers/
    base.py             AisProvider abstraction + normalised vessel contract
    aisstream.py        aisstream.io provider (Qt WebSocket)
    __init__.py         provider registry
```

## Status

Early scaffold. The provider layer (aisstream.io over Qt WebSockets) is in place
and the live feed is verified; the map layer and dock UI are being wired next.

## License

GPL-2.0-or-later.
