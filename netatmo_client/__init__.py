"""Faithful Python client for the Netatmo / Legrand-BTicino "Home + Security"
backend (reverse-engineered from com.netatmo.camera)."""

from .api import NetatmoAPI, NetatmoError, Token
from .client import NetatmoClient, Home, Module

__all__ = [
    "NetatmoAPI", "NetatmoError", "Token",
    "NetatmoClient", "Home", "Module",
]
