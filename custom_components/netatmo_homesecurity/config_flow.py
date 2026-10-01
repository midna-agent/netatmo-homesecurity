"""Config flow for Netatmo Home + Security.

Two ways to authenticate:
  * username + password (normal), or
  * an existing refresh_token (handy for migrating a session).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_USERNAME,
    DEFAULT_CLIENT_ID,
    DOMAIN,
)
from .lib import NetatmoClient, NetatmoError, Token

_LOGGER = logging.getLogger(__name__)


class NetatmoHSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await self.hass.async_add_executor_job(
                    self._authenticate, user_input
                )
            except NetatmoError as err:
                _LOGGER.warning("auth failed: %s", err)
                errors["base"] = "auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error during setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(data["unique_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=data[CONF_HOME_NAME], data=data["entry"]
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_USERNAME): str,
                vol.Optional(CONF_PASSWORD): str,
                vol.Optional(CONF_REFRESH_TOKEN): str,
                vol.Optional(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): str,
                vol.Optional(CONF_CLIENT_SECRET): str,
                vol.Optional(CONF_HOME_ID): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    def _authenticate(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Blocking: log in (or adopt a refresh token), discover a home."""
        client_id = user_input.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID
        client_secret = user_input.get(CONF_CLIENT_SECRET) or None
        client = NetatmoClient(client_id=client_id, client_secret=client_secret)

        refresh_token = user_input.get(CONF_REFRESH_TOKEN)
        if refresh_token:
            # Adopt the token and force a refresh to validate + get an access token.
            client.api.set_token(Token(access_token="", refresh_token=refresh_token,
                                       expires_at=0))
            client.api.refresh()
        else:
            username = user_input.get(CONF_USERNAME)
            password = user_input.get(CONF_PASSWORD)
            if not username or not password:
                raise NetatmoError("username and password (or a refresh_token) "
                                   "are required")
            client.login(username, password)

        homes = client.discover()
        home_id = user_input.get(CONF_HOME_ID)
        if home_id and home_id not in homes:
            client.add_home(home_id)
            homes = client.homes
        if not homes:
            raise NetatmoError("no homes discovered; pass a home_id")
        if not home_id:
            home_id = next(iter(homes))
        home = client.homes[home_id]

        token = client.api.token
        # unique_id: the Netatmo user id is the prefix of the token "<uid>|<hash>".
        uid = token.access_token.split("|", 1)[0] if token.access_token else home_id
        entry = {
            CONF_USERNAME: user_input.get(CONF_USERNAME, ""),
            CONF_CLIENT_ID: client_id,
            CONF_CLIENT_SECRET: client_secret,
            CONF_TOKEN: token.to_dict(),
            CONF_HOME_ID: home_id,
            CONF_HOME_NAME: home.name,
        }
        return {"unique_id": f"{uid}:{home_id}", "entry": entry,
                CONF_HOME_NAME: home.name}
