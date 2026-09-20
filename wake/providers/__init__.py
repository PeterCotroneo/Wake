"""Provider registry.

Adding a new AIS source: implement AisProvider in a new module, declare its
``config_fields`` (what the configure dialog should ask for), and register its
class below. The plugin builds its provider dropdown and settings dialog from
PROVIDERS + each provider's config_fields — nothing else needs to change.
"""

from .base import AisProvider
from .aisstream import AisStreamProvider, OpenWatersProvider
from .digitraffic import DigitrafficProvider

# id -> provider class (order shown in the dropdown)
PROVIDERS = {
    AisStreamProvider.id: AisStreamProvider,
    OpenWatersProvider.id: OpenWatersProvider,
    DigitrafficProvider.id: DigitrafficProvider,
}

__all__ = [
    "AisProvider", "AisStreamProvider", "OpenWatersProvider",
    "DigitrafficProvider", "PROVIDERS",
]
