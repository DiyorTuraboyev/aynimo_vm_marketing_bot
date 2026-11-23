"""Deploy the bot to a Pterodactyl panel (bot-hosting.net) and restart it.

Used by GitHub Actions; can also be run by hand:
    set PANEL_URL=https://control.bot-hosting.net
    set PANEL_API_KEY=ptlc_xxx
    set SERVER_ID=abcd1234
    python scripts/deploy_pterodactyl.py

Only the files below are uploaded. `.env` and `vm_marketing_data/` on the server
(token, database, uploads) are never touched.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEPLOY_FILES = [
    "app.py",
    "requirements.txt",
    ".env.example",
    "VM_MARKETING TEMPLATE.xlsx",
]
DEPLOY_DIRS = ["vm_marketing_bot"]
SKIP_PARTS = {"__pycache__"}


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"::error::{name} is not set (add it in GitHub → Settings → Secrets and variables → Actions)")
    return value


PANEL_URL = env("PANEL_URL").rstrip("/")
API_KEY = env("PANEL_API_KEY")
SERVER_ID = env("SERVER_ID")
BASE = f"{PANEL_URL}/api/client/servers/{SERVER_ID}"


def request(method: str, path: str, body: bytes | None = None, content_type: str = "application/json",
            attempts: int = 4) -> bytes:
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json",
            "Content-Type": content_type,
            "User-Agent": "aynimo-bot-deploy/1.0",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(3 * attempt)
                continue
            sys.exit(f"::error::{method} {path} -> HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            if attempt < attempts:
                time.sleep(3 * attempt)
                continue
            sys.exit(f"::error::{method} {path} -> {exc.reason}")
    raise RuntimeError("unreachable")


def files_to_deploy() -> list[Path]:
    files = [ROOT / f for f in DEPLOY_FILES if (ROOT / f).exists()]
    for folder in DEPLOY_DIRS:
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and not SKIP_PARTS & set(path.parts) and path.suffix != ".pyc":
                files.append(path)
    return files


def upload(path: Path) -> None:
    remote = "/" + path.relative_to(ROOT).as_posix()
    query = urllib.parse.urlencode({"file": remote})
    request("POST", f"/files/write?{query}", path.read_bytes(), content_type="application/octet-stream")
    print(f"  uploaded {remote}")


def main() -> None:
    info = json.loads(request("GET", ""))
    name = info.get("attributes", {}).get("name", SERVER_ID)
    print(f"Deploying to server: {name}")

    files = files_to_deploy()
    for path in files:
        upload(path)
    print(f"{len(files)} files uploaded")

    request("POST", "/power", json.dumps({"signal": "restart"}).encode())
    print("Restart signal sent")

    for _ in range(24):  # wait up to ~2 minutes for the server to come back
        time.sleep(5)
        state = json.loads(request("GET", "/resources")).get("attributes", {}).get("current_state")
        print(f"  state: {state}")
        if state == "running":
            print("✅ Deployed and running")
            return
    sys.exit("::error::Server did not reach 'running' state — check the console in the panel")


if __name__ == "__main__":
    main()
