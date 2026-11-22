"""Token handling in the vendored low-level client (no Home Assistant)."""

import time
from unittest.mock import MagicMock

import pytest
import requests

from custom_components.netatmo_homesecurity.lib import (
    NetatmoAPI, NetatmoAuthError, NetatmoError, Token,
)

INVALID = {"error": {"code": 2, "message": "Invalid access token"}}


def resp(status, payload, url="https://app.netatmo.net/api/homesdata"):
    r = requests.Response()
    r.status_code = status
    r._content = __import__("json").dumps(payload).encode()
    r.headers["content-type"] = "application/json"
    r.request = requests.Request("POST", url).prepare()
    return r


def api_with(*responses):
    api = NetatmoAPI(client_secret="s")
    api.set_token(Token("old", "rt", time.time() + 3600))
    api._s = MagicMock()
    api._s.post.side_effect = list(responses)
    return api


def token_ok(access="new"):
    return resp(200, {"access_token": access, "refresh_token": "rt2",
                      "expires_in": 3600},
                "https://app.netatmo.net/oauth2/token")


def test_rejected_token_is_refreshed_and_request_retried():
    api = api_with(resp(403, INVALID), token_ok(), resp(200, {"body": {"ok": 1}}))
    seen = []
    api.on_token_change = seen.append
    assert api.homesdata() == {"body": {"ok": 1}}
    calls = api._s.post.call_args_list
    assert len(calls) == 3
    assert "/oauth2/token" in calls[1].args[0]
    assert calls[2].kwargs["headers"]["Authorization"] == "Bearer new"
    assert [t.access_token for t in seen] == ["new"]


def test_dead_refresh_token_raises_auth_error():
    api = api_with(resp(403, INVALID),
                   resp(400, {"error": "invalid_grant"},
                        "https://app.netatmo.net/oauth2/token"))
    with pytest.raises(NetatmoAuthError):
        api.homesdata()


def test_still_rejected_after_refresh_raises_auth_error():
    api = api_with(resp(403, INVALID), token_ok(), resp(403, INVALID))
    with pytest.raises(NetatmoAuthError):
        api.homesdata()
    assert api._s.post.call_count == 3  # one retry only, no loop


def test_unrelated_403_is_not_an_auth_error():
    api = api_with(resp(403, {"error": {"code": 13, "message": "Permission denied"}}))
    with pytest.raises(NetatmoError) as ei:
        api.homesdata()
    assert not isinstance(ei.value, NetatmoAuthError)
    assert api._s.post.call_count == 1


def test_network_error_is_plain_netatmo_error():
    api = api_with(requests.ConnectionError("boom"))
    with pytest.raises(NetatmoError) as ei:
        api.homesdata()
    assert not isinstance(ei.value, NetatmoAuthError)


def test_refresh_5xx_is_transient_not_auth():
    api = api_with(resp(403, INVALID),
                   resp(503, {"error": "unavailable"},
                        "https://app.netatmo.net/oauth2/token"))
    with pytest.raises(NetatmoError) as ei:
        api.homesdata()
    assert not isinstance(ei.value, NetatmoAuthError)
