"""Connector plugins. Importing this package registers every built-in connector."""

from . import arr, disk, jellyfin, silo  # noqa: F401
from .base import (
    Capability,
    ConnectionTest,
    Connector,
    ConnectorConfig,
    ConnectorError,
    connector_types,
    get_connector_class,
)

__all__ = [
    "Capability",
    "ConnectionTest",
    "Connector",
    "ConnectorConfig",
    "ConnectorError",
    "connector_types",
    "get_connector_class",
]
