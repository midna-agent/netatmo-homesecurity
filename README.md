# Netatmo / Legrand-BTicino "Home + Security"

Two things live here:

1. **`netatmo_client/`** — a dependency-light Python client (just `requests`)
   that speaks the exact protocol of the official Android app `com.netatmo.camera`
   (Security v26.9.1.0): login, entity discovery, open door, snapshot capture,
   and a persistent realtime session (token auto-refresh + websocket).
2. **`custom_components/netatmo_homesecurity/`** — a Home Assistant integration
   built on that client: a **lock** (open door), an **Open** button, and a
   **camera** (latest event snapshot), with a UI config flow.

Reverse-engineered from decrypted TLS captured on an authorized test device,
using the account owner's own account and devices, to build an interoperable
client — as Netatmo's public API otherwise allows.

## Python client

```bash
pip install requests
python -m netatmo_client login --user you@example.com
python -m netatmo_client discover
python -m netatmo_client open-door --home <HOME_ID>
python -m netatmo_client snapshots --home <HOME_ID> --out ./snapshots
python -m netatmo_client watch     # keep-alive + realtime events
```

See [`netatmo_client/README.md`](netatmo_client/README.md) for the full
reverse-engineered API (endpoints, headers, OAuth, door command, snapshots).

## Home Assistant integration

Install via HACS (custom repository) or copy
`custom_components/netatmo_homesecurity/` into your HA `config/custom_components/`
and restart. Then **Settings → Devices & Services → Add Integration →
"Netatmo Home + Security"** and sign in (email/password, or a refresh token).

Entities created per home:
- `lock.<door>` — open/unlock the door (the app's "Slide to unlock").
- `button.<door>_open` — momentary open.
- `camera.<panel>` — most recent event snapshot.

### Notes
- Door-entry bridges (BTicino) only relay the open command while a realtime
  websocket session is active; the integration keeps that session open, so the
  door opens reliably (no `code 6`).
- The OAuth `client_id`/`client_secret` are the app's own embedded values
  (identical across installs, extractable from the APK). They ship as defaults
  and can be overridden via the config flow or `NETATMO_CLIENT_ID` /
  `NETATMO_CLIENT_SECRET`.

## Layout
```
netatmo_client/                     # standalone library + CLI
custom_components/netatmo_homesecurity/
  __init__.py  manifest.json  const.py
  config_flow.py  coordinator.py  entity.py
  lock.py  button.py  camera.py
  strings.json  translations/{en,es}.json
  lib/                              # vendored copy of netatmo_client (self-contained)
```
