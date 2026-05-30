"""trdump — dump, export, and monitor a Trade Republic account."""

from . import _ws_debug as _ws

# Honour TRDUMP_WS_LOG=… so debugging works on every subcommand without
# touching the per-command flags.
_ws.enable_from_env()

__version__ = "0.1.0"
