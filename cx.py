#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["requests>=2.34.2"]
# ///

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

import requests


ROOT = Path(__file__).resolve().parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text())
HOME = Path(os.environ.get("CODEX_HOME", CONFIG["codex_home"])).expanduser()
if not HOME.is_absolute():
    HOME = ROOT / HOME
AUTH = HOME / CONFIG["auth_file"]
ACCOUNTS = HOME / CONFIG["accounts_dir"]


def read(path):
    return json.loads(path.read_bytes()) if path.exists() else None


def write(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def email(auth):
    payload = auth["tokens"]["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return (claims.get("email") or claims["https://api.openai.com/profile"]["email"]).casefold()


def account_path(address):
    return ACCOUNTS / (hashlib.sha256(address.casefold().encode()).hexdigest() + ".json")


def save(auth):
    address = email(auth)
    path = account_path(address)
    if read(path) != auth:
        write(path, auth)
    return address


def request(url, headers=None, data=None):
    with requests.request(
        "GET" if data is None else "POST", url,
        headers=headers, json=data, timeout=CONFIG["timeout_seconds"],
        allow_redirects=False,
    ) as response:
        response.raise_for_status()
        return response.json()


def usage(auth):
    tokens = auth["tokens"]
    return request(CONFIG["usage_url"], headers={
        "Authorization": "Bearer " + tokens["access_token"],
        "ChatGPT-Account-Id": tokens["account_id"],
    })


def refresh(path, auth):
    current = read(AUTH)
    if current and email(current) == email(auth) and current != auth:
        save(current)
        return current
    result = request(CONFIG["token_url"], data={
        "client_id": CONFIG["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": auth["tokens"]["refresh_token"],
    })
    original = auth["tokens"].copy()
    for key in ("access_token", "refresh_token", "id_token"):
        if result.get(key):
            auth["tokens"][key] = result[key]
    auth["last_refresh"] = datetime.now(timezone.utc).isoformat()
    write(path, auth)
    current = read(AUTH)
    if current and current["tokens"] == original:
        write(AUTH, auth)
    return auth


def window_text(window):
    seconds = window["limit_window_seconds"]
    duration = f"{seconds / 3600:g}h" if seconds < 86400 else f"{seconds / 86400:g}d"
    remaining = max(0, min(100, 100 - window["used_percent"]))
    text = f"{duration}: {remaining:g}% left"
    if remaining == 0:
        reset = datetime.fromtimestamp(window["reset_at"]).astimezone()
        text += f" · resets: {reset:%Y-%m-%d %H:%M %Z (%z)}"
    return text


def limit_text(name, limit):
    windows = [window_text(limit[key]) for key in ("primary_window", "secondary_window") if limit.get(key)]
    text = f"{name}: " + (" | ".join(windows) or "percentage not available")
    if limit.get("limit_reached") or limit.get("allowed") is False:
        text += " · limit reached"
    return text


def resets_text(data):
    credits = (data or {}).get("rate_limit_reset_credits") or {}
    count = credits.get("available_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return f"Resets: {count} available"


def status(path):
    address = path.stem
    try:
        auth = read(path)
        address = email(auth)
        try:
            data = usage(auth)
        except requests.HTTPError as exc:
            if exc.response.status_code != 401:
                raise
            data = usage(refresh(path, auth))
        limits = [("Codex", data.get("rate_limit"))]
        limits.extend((item["limit_name"], item.get("rate_limit")) for item in data.get("additional_rate_limits") or [])
        limits.append(("Review", data.get("code_review_rate_limit")))
        text = "\n    ".join(limit_text(name, limit) for name, limit in limits if limit)
        resets = resets_text(data)
        if resets:
            text = f"{text}\n    {resets}" if text else resets
        return address, text or "Limits not available.", True
    except requests.HTTPError as exc:
        return address, f"HTTP {exc.response.status_code}. Run cx login if the session expired.", False
    except requests.RequestException:
        return address, "Request failed. Check the connection and try again.", False
    except (KeyError, ValueError, TypeError, OSError) as exc:
        return address, f"Cannot read account status: {type(exc).__name__}.", False


def login(device_auth):
    with tempfile.TemporaryDirectory(dir=ACCOUNTS) as directory:
        command = [CONFIG["codex_command"], "-c", 'cli_auth_credentials_store="file"', "login"]
        if device_auth:
            command.append("--device-auth")
        subprocess.run(command, env={**os.environ, "CODEX_HOME": directory}, check=True)
        address = save(read(Path(directory) / CONFIG["auth_file"]))
    print(f"Account saved: {address}\nTo activate: cx {address}")


def main():
    parser = argparse.ArgumentParser(description="Read limits and switch Codex accounts.")
    parser.add_argument("account", nargs="?", metavar="email|login|add")
    parser.add_argument("--device-auth", action="store_true", help="use a device code with login")
    args = parser.parse_args()
    if args.device_auth and args.account != "login":
        parser.error("--device-auth requires login")
    ACCOUNTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    ACCOUNTS.chmod(0o700)
    with (ACCOUNTS / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = read(AUTH)
        active = save(current) if current else None
        if args.account == "login":
            login(args.device_auth)
        elif args.account == "add":
            print(f"Account saved: {active}" if active else "No active account. Run cx login.")
        elif args.account:
            target = read(account_path(args.account))
            if target is None:
                print("Account not found. Run cx login.", file=sys.stderr)
                return 1
            if target != current:
                write(AUTH, target)
            print(f"Active account: {email(target)}")
        else:
            paths = list(ACCOUNTS.glob("*.json"))
            if not paths:
                print("No saved accounts. Run cx login.")
                return 0
            failed = False
            with ThreadPoolExecutor(max_workers=min(CONFIG["workers"], len(paths))) as pool:
                for address, text, ok in pool.map(status, paths, buffersize=CONFIG["workers"]):
                    print(f"{'*' if address == active else ' '} {address}\n    {text}", flush=True)
                    failed |= not ok
            return int(failed)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"cx: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
