"""
CLI for the Netatmo Home + Security client.

Examples:
  # Log in (password prompted if not piped) and remember the session:
  python -m netatmo_client login --user you@example.com

  # Discover homes / modules:
  python -m netatmo_client discover

  # Open the (only) door lock in a home:
  python -m netatmo_client open-door --home <HOME_ID>

  # Capture recent snapshot frames into ./snapshots:
  python -m netatmo_client snapshots --home <HOME_ID> --out ./snapshots

  # Keep the session open and print realtime events (Ctrl-C to stop):
  python -m netatmo_client watch

Credentials can also come from env: NETATMO_USER / NETATMO_PASS.
The session (tokens) is stored at ~/.config/netatmo_client/session.json
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from .client import NetatmoClient
from .api import NetatmoError

DEFAULT_STORE = Path(os.path.expanduser("~/.config/netatmo_client/session.json"))


def _client(args) -> NetatmoClient:
    return NetatmoClient(token_store=args.store)


def _ensure_session(c: NetatmoClient, args) -> None:
    if c.load_saved_session():
        return
    user = args.user or os.environ.get("NETATMO_USER")
    pw = os.environ.get("NETATMO_PASS")
    if not user:
        user = input("Netatmo email: ").strip()
    if not pw:
        pw = getpass.getpass("Netatmo password: ")
    c.login(user, pw)


def cmd_login(args):
    c = _client(args)
    user = args.user or os.environ.get("NETATMO_USER") or input("Netatmo email: ").strip()
    pw = os.environ.get("NETATMO_PASS") or getpass.getpass("Netatmo password: ")
    c.login(user, pw)
    print(f"Logged in. Session saved to {args.store}")


def cmd_discover(args):
    c = _client(args)
    _ensure_session(c, args)
    homes = c.discover()
    if not homes and args.home:
        c.add_home(args.home)
        homes = c.homes
    for home in homes.values():
        print(f"\nHome: {home.name}  (id={home.id}, tz={home.timezone})")
        for m in home.modules.values():
            tags = []
            if m.is_doorlock:
                tags.append("DOORLOCK")
            if m.is_camera:
                tags.append("CAMERA")
            tag = f"  [{','.join(tags)}]" if tags else ""
            print(f"  - {m.name:<20} id={m.id} type={m.type or '?'} "
                  f"bridge={m.bridge or '-'}{tag}")
    if not homes:
        print("No homes discovered. If you know your home_id, pass --home <id>.")


def cmd_open_door(args):
    c = _client(args)
    _ensure_session(c, args)
    c.discover()
    if args.home not in c.homes:
        c.add_home(args.home)
    # Door-entry bridges (e.g. BTicino) only relay the command while a realtime
    # session is active, exactly like the official app. Bring the websocket up
    # and wait for it before sending, unless --no-session is given.
    if not args.no_session:
        c.start_keepalive(realtime=True)
        for _ in range(15):
            if c._ws and c._ws._sock is not None:
                break
            time.sleep(1)
        time.sleep(1)
    try:
        res = c.open_door(args.home, args.module)
    finally:
        if not args.no_session:
            c.close()
    errors = (res.get("body", {}) or {}).get("errors")
    print("Door command sent. Server response:",
          json.dumps(res, ensure_ascii=False)[:300])
    if errors:
        print("WARNING: device reported", errors,
              "(try again with the realtime session active).")


def cmd_snapshots(args):
    c = _client(args)
    _ensure_session(c, args)
    files = c.capture_snapshots(args.home, args.out, size=args.size)
    if files:
        print(f"Saved {len(files)} snapshot(s) to {args.out}:")
        for f in files:
            print("  ", f)
    else:
        print("No snapshot frames found in recent events.")


def cmd_watch(args):
    c = _client(args)
    _ensure_session(c, args)
    c.discover()
    c.on_event(lambda ev: print("EVENT:",
                                json.dumps(ev, ensure_ascii=False)[:500]
                                if not isinstance(ev, (bytes, bytearray))
                                else f"<{len(ev)} bytes>"))
    c.start_keepalive(realtime=True)
    print("Session kept alive (token auto-refresh + realtime websocket). "
          "Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        c.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="netatmo_client")
    p.add_argument("--store", type=Path, default=DEFAULT_STORE,
                   help="session/token file path")
    p.add_argument("--user", help="Netatmo account email")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("login"); sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("discover"); sp.add_argument("--home")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("open-door")
    sp.add_argument("--home", required=True)
    sp.add_argument("--module", help="door-lock module id (default: first found)")
    sp.add_argument("--no-session", action="store_true",
                    help="send the command without first opening the realtime "
                         "websocket session (may yield a device 'code 6' on "
                         "door-entry bridges)")
    sp.set_defaults(func=cmd_open_door)

    sp = sub.add_parser("snapshots")
    sp.add_argument("--home", required=True)
    sp.add_argument("--out", default="./snapshots")
    sp.add_argument("--size", type=int, default=30)
    sp.set_defaults(func=cmd_snapshots)

    sp = sub.add_parser("watch"); sp.set_defaults(func=cmd_watch)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except NetatmoError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
