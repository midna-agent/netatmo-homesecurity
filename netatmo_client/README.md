# Netatmo / Legrand-BTicino "Home + Security" — Python client

A dependency-light (`requests` only) Python client that speaks the **exact**
protocol of the official Android app `com.netatmo.camera`
(Security v26.9.1.0 / build 500000026), reverse-engineered from decrypted TLS
captured on an authorized test device.

It can:

- **Log in** (OAuth2 password grant) and keep the session alive.
- **Discover entities** on first connect (homes, bridges, cameras, door locks, lights).
- **Open a door** (`setstate` / `lock:false`).
- **Capture stream frames** (timeline event snapshots; live camera snapshot where available).
- **Stay connected** like the real app: background token auto-refresh + a
  persistent realtime websocket with auto-reconnect.

## Wire transparency

Every request reproduces what the app sends, so the backend cannot tell this
client apart from the official app:

| Field | Value |
|---|---|
| API host | `https://app.netatmo.net` |
| Realtime WS | `wss://app-ws.netatmo.net/ws/` |
| Snapshot blobs | `https://netatmocameraimage.blob.core.windows.net/...` (pre-signed SAS) |
| `User-Agent` (API) | `NetatmoApp(Security/v26.9.1.0/500000026) Android(16/Google/Pixel 7)` |
| `User-Agent` (WS / blobs) | `okhttp/5.3.2` |
| OAuth `client_id` | `na_client_android_welcome` |
| OAuth `client_secret` | `8ab584d62ca2a77e37ccc6b2c7e4f29e` |
| OAuth `scope` | `security_scopes` |
| `app_type` | `app_camera` |

> Note: the app uses HTTP/2 to `app.netatmo.net`; this client uses HTTP/1.1
> (via `requests`), which the backend accepts. For byte-identical framing at
> the H2 layer, swap the transport for `httpx(http2=True)` — the headers/bodies
> here are already identical.

## Reverse-engineered endpoints

### Login — `POST /oauth2/token`
```
Content-Type: application/x-www-form-urlencoded

grant_type=password&username=<email>&password=<pw>
&client_id=na_client_android_welcome&client_secret=8ab584d62ca2a77e37ccc6b2c7e4f29e
&scope=security_scopes&app_version=26.9.1.0
```
Response: `{access_token, refresh_token, expires_in: 10800, scope:[...]}`.
Netatmo tokens look like `<user_id>|<hash>`.

### Refresh — `POST /oauth2/token`
`grant_type=refresh_token&refresh_token=...&client_id=...&client_secret=...&scope=security_scopes`

### Discovery
- `POST /api/homesdata` — topology (homes, modules, schedules).
- `POST /syncapi/v1/homestatus` — live module state (ids, `type`, `bridge`, `vpn_url`, lock state...).
- `POST /api/gethomeusers` — home users.

All take `{"app_type":"app_camera","app_version":"26.9.1.0", ...}` and the
`Authorization: Bearer <access_token>` header.

### Open door — `POST /syncapi/v1/setstate`
```json
{"app_type":"app_camera","app_version":"26.9.1.0",
 "home":{"timezone":"Europe/Madrid","id":"<home_id>",
   "modules":[{"bridge":"<bridge_mac>","lock":false,"id":"<module_id>"}]}}
```
`lock:false` unlocks/opens; this is exactly what "Slide to unlock" sends.
Verified response: `HTTP 200`.

> **Door-entry bridges need an active realtime session.** For BTicino/Legrand
> door-entry bridges, sending `setstate` as a lone HTTP call returns
> `status: ok` but with `body.errors:[{code:6, id:<bridge>}]` and the door does
> **not** move — the bridge only relays the command while a realtime websocket
> session is open, as the official app always keeps it. Open the websocket
> (`start_keepalive(realtime=True)`) and wait for it to connect **before**
> calling `open_door()`. With the session active the response is a clean
> `{"status":"ok"}` and the door opens. The CLI `open-door` does this
> automatically (disable with `--no-session`). Verified end-to-end on real
> hardware.

### Stream capture — `POST //api/getevents` → blob download
`getevents` body: `{"app_type":"app_camera","size":30,"app_version":"26.9.1.0","home_id":"<home_id>"}`
(note the literal double slash `//api/getevents`, as the app sends it).
Each event carries `snapshot`/`vignette` objects with **pre-signed Azure blob
URLs**; download them with `User-Agent: okhttp/5.3.2` and **no** auth header.

For Netatmo cameras exposing a `vpn_url` in homestatus, a live frame is
`GET {vpn_url}/live/snapshot_720.jpg`. BTicino entrance panels (e.g. this home's
`Classe 100X16E`) stream the live call over the realtime/UDP channel instead, so
use event snapshots for those.

## Usage

```bash
pip install requests      # the only dependency

# Log in once; the session (tokens) is stored and auto-refreshed afterwards.
python -m netatmo_client login --user you@example.com
# (password read from NETATMO_PASS env or prompted; never passed on argv)

python -m netatmo_client discover
python -m netatmo_client open-door --home <HOME_ID>          # opens the real door
python -m netatmo_client snapshots --home <HOME_ID> --out ./snapshots
python -m netatmo_client watch                                # keep-alive + realtime events
```

### Library

```python
from netatmo_client import NetatmoClient

c = NetatmoClient(token_store="~/.config/netatmo_client/session.json")
if not c.load_saved_session():
    c.login("you@example.com", "password")

homes = c.discover()                       # auto-discovery
home_id = next(iter(homes))

c.start_keepalive(realtime=True)           # stay connected like the app
c.on_event(print)

c.open_door(home_id)                       # first door lock found
c.capture_snapshots(home_id, "snapshots")  # grab recent frames
```

## Files
- `api.py` — faithful low-level HTTP endpoints + OAuth.
- `ws.py` — dependency-free persistent WebSocket (RFC6455 + permessage-deflate).
- `client.py` — discovery, actions, snapshots, keep-alive, token persistence.
- `__main__.py` — CLI.

## Provenance / authorization
Built from traffic captured on an authorized vulnit test device, using the
account owner's own credentials and their own devices, to produce an
interoperable client (as Netatmo's public API otherwise allows). The embedded
`client_secret` is extracted from the app binary's own traffic.
