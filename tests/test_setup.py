"""Setup / reauth behaviour of the config entry."""

import time
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.netatmo_homesecurity.const import DOMAIN
from custom_components.netatmo_homesecurity.lib import (
    NetatmoAuthError, NetatmoClient, NetatmoError, Token,
)

DATA = {
    "username": "me@example.com",
    "client_id": "cid",
    "client_secret": "sec",
    "token": {"access_token": "a", "refresh_token": "r",
              "expires_at": time.time() + 3600, "scope": []},
    "home_id": "home1",
    "home_name": "Albahaca",
}


def entry(hass):
    e = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="u:home1",
                        title="Albahaca")
    e.add_to_hass(hass)
    return e


async def test_network_error_keeps_setup_retry(hass):
    e = entry(hass)
    with patch.object(NetatmoClient, "discover", side_effect=NetatmoError("net down")):
        await hass.config_entries.async_setup(e.entry_id)
    assert e.state is ConfigEntryState.SETUP_RETRY


async def test_auth_failure_triggers_reauth_not_retry(hass):
    e = entry(hass)
    with patch.object(NetatmoClient, "discover",
                      side_effect=NetatmoAuthError("Invalid access token", status=403)):
        await hass.config_entries.async_setup(e.entry_id)
    assert e.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert flows[0]["step_id"] == "reauth_confirm"


async def test_rejected_token_is_renewed_during_setup(hass):
    """End to end: homesdata answers 403 once, the client refreshes with the
    stored refresh token and the entry loads (no setup_retry, no reauth)."""
    import json
    import requests

    e = entry(hass)
    state = {"homesdata": 0, "refreshes": 0}

    def mk(status, payload):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(payload).encode()
        r.headers["content-type"] = "application/json"
        r.request = requests.Request("POST", "https://app.netatmo.net/x").prepare()
        return r

    def post(self, url, **kw):
        if url.endswith("/oauth2/token"):
            assert kw["data"]["grant_type"] == "refresh_token"
            assert kw["data"]["refresh_token"] == "r"
            state["refreshes"] += 1
            return mk(200, {"access_token": "a2", "refresh_token": "r2",
                            "expires_in": 3600})
        if url.endswith("/api/homesdata"):
            state["homesdata"] += 1
            if state["homesdata"] == 1:
                return mk(403, {"error": {"code": 2,
                                          "message": "Invalid access token"}})
            assert kw["headers"]["Authorization"] == "Bearer a2"
            return mk(200, {"body": {"homes": []}})
        return mk(200, {"body": {"home": {"modules": []}, "events": []}})

    with patch("requests.Session.post", post), \
         patch.object(NetatmoClient, "start_keepalive"):
        await hass.config_entries.async_setup(e.entry_id)
    assert state == {"homesdata": 2, "refreshes": 1}
    assert e.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    await hass.config_entries.async_unload(e.entry_id)


async def test_reauth_with_password_updates_token_and_reloads(hass):
    e = entry(hass)
    with patch.object(NetatmoClient, "discover",
                      side_effect=NetatmoAuthError("Invalid access token")):
        await hass.config_entries.async_setup(e.entry_id)
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]

    new = Token("fresh", "r2", time.time() + 3600)
    with patch("netatmo_client.api.NetatmoAPI.login", return_value=new), \
         patch("custom_components.netatmo_homesecurity.lib.api.NetatmoAPI.login",
               return_value=new), \
         patch("homeassistant.config_entries.ConfigEntries.async_reload"):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"password": "pw"})
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert e.data["token"]["access_token"] == "fresh"
    assert e.data["home_id"] == "home1"


async def test_reauth_bad_password_shows_error(hass):
    e = entry(hass)
    with patch.object(NetatmoClient, "discover",
                      side_effect=NetatmoAuthError("x")):
        await hass.config_entries.async_setup(e.entry_id)
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    with patch("custom_components.netatmo_homesecurity.lib.api.NetatmoAPI.login",
               side_effect=NetatmoAuthError("bad credentials")):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"password": "nope"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "auth"}
