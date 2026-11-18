"""Start Option Hub (india-s-best-option-hub-main) on the Dhan session OpenAlgo is logged in with.

  openalgo-main\\.venv\\Scripts\\python.exe tools\\run_option_hub.py                # page http://localhost:4001
  openalgo-main\\.venv\\Scripts\\python.exe tools\\run_option_hub.py --proxy-only   # data proxy only, 127.0.0.1:4002

Option Hub talks to Dhan itself (its verified path: option chains with Dhan's Greeks, the live binary feed, futures,
history). Instead of pasting a daily token into its Broker Settings, this reads the Dhan client id (OpenAlgo's
BROKER_API_KEY) and today's access token (OpenAlgo's encrypted login store) and hands them to Option Hub's proxy as
DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in the child process environment only: never printed, never written to disk.
Log in to OpenAlgo and connect Dhan first; restart this after each daily login (the token changes).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENALGO = ROOT / "openalgo-main"
HUB = ROOT / "india-s-best-option-hub-main"
IST = timezone(timedelta(hours=5, minutes=30))


def _jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def dhan_session(user: str | None) -> tuple[str, str, str]:
    """(client id, access token, OpenAlgo user) of the live Dhan login in OpenAlgo's database."""
    cwd = os.getcwd()
    os.chdir(OPENALGO)
    sys.path.insert(0, str(OPENALGO))
    try:
        from dotenv import load_dotenv

        load_dotenv(OPENALGO / ".env")
        from database.auth_db import Auth, get_auth_token

        q = Auth.query.filter_by(is_revoked=False, broker="dhan")
        row = (q.filter_by(name=user) if user else q).first()
        if row is None:
            raise SystemExit("No active Dhan login in OpenAlgo. Start OpenAlgo, log in and connect Dhan, then rerun.")
        name, token = row.name, get_auth_token(row.name) or ""
        client = (os.environ.get("BROKER_API_KEY") or "").split(":::")[0].strip()
    finally:
        os.chdir(cwd)
    client = client or str(_jwt(token).get("dhanClientId") or "")
    if not client or not token:
        raise SystemExit("OpenAlgo has no Dhan client id or token; reconnect Dhan in OpenAlgo and rerun.")
    return client, token, name


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--user", help="OpenAlgo login name (default: the one connected to Dhan)")
    p.add_argument("--proxy-only", action="store_true", help="start only Option Hub's data proxy (127.0.0.1:4002)")
    args = p.parse_args()
    if not (HUB / "proxy-server.mjs").exists():
        raise SystemExit(f"Option Hub not found at {HUB}")
    if not (HUB / "node_modules").exists():
        raise SystemExit(f"Run `npm install` in {HUB} first.")
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or (not npm and not args.proxy_only):
        raise SystemExit("Node.js (node and npm) is not on PATH.")

    client, token, user = dhan_session(args.user)
    exp = _jwt(token).get("exp")
    until = datetime.fromtimestamp(int(exp), IST) if exp else None
    if until and until <= datetime.now(IST):
        raise SystemExit(f"OpenAlgo's Dhan token expired at {until:%d %b %H:%M} IST: log in to OpenAlgo again, then rerun.")

    env = {**os.environ, "DHAN_CLIENT_ID": client, "DHAN_ACCESS_TOKEN": token,
           "NO_PROXY": ",".join(filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))}
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        env["NODE_USE_ENV_PROXY"] = "1"  # Node's fetch ignores HTTPS_PROXY unless asked (office network)
    cmd = [node, "proxy-server.mjs"] if args.proxy_only else [npm, "run", "dev"]
    print(f"Option Hub on Dhan client {client[:2]}...{client[-2:]} from OpenAlgo login '{user}'"
          + (f", token valid until {until:%d %b %H:%M} IST" if until else ""))
    print("Proxy http://127.0.0.1:4002/health" + ("" if args.proxy_only else " · page http://localhost:4001")
          + " · Ctrl+C stops it", flush=True)
    proc = subprocess.Popen(cmd, cwd=HUB, env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
