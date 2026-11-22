"""Vendored copy of the netatmo_client library (api/ws/client), so this Home
Assistant integration is self-contained. Kept in sync with the top-level
netatmo_client package in this repository."""

from .api import NetatmoAPI, NetatmoAuthError, NetatmoError, Token
from .client import NetatmoClient, Home, Module

__all__ = ["NetatmoAPI", "NetatmoAuthError", "NetatmoError", "Token",
           "NetatmoClient", "Home", "Module"]
