"""Provider registry.

Adding a new AIS source is a two-line change: implement AisProvider in a new
module here, then register its class below. The plugin builds its provider
dropdown from PROVIDERS, so nothing else needs to change.
"""

from .base import AisProvider
from .aisstream import AisStreamProvider

# id -> provider class
PROVIDERS = {
    AisStreamProvider.id: AisStreamProvider,
}

__all__ = ["AisProvider", "AisStreamProvider", "PROVIDERS"]
