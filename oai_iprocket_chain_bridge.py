"""Compatibility entry point for the shared IPRocket/1024proxy bridge."""

from iprocket_chain_bridge import ensure_background_server, stop_background_server

__all__ = ["ensure_background_server", "stop_background_server"]
