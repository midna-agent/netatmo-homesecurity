"""Constants for the Netatmo Home + Security integration."""

DOMAIN = "netatmo_homesecurity"

# Config entry data keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN = "token"          # stored dict {access_token, refresh_token, expires_at, scope}
CONF_HOME_ID = "home_id"
CONF_HOME_NAME = "home_name"

DEFAULT_CLIENT_ID = "na_client_android_welcome"

# How often to poll homestatus / latest event snapshot.
UPDATE_INTERVAL_SECONDS = 60

PLATFORMS = ["lock", "camera", "button"]
