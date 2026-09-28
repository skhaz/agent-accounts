#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "==3.13.*"
# dependencies = ["httpx>=0.28.1", "jinja2>=3.1.6"]
# ///

import argparse
import asyncio
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib

import httpx
import jinja2


ROOT = Path(__file__).resolve().parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text())
TEMPLATES = jinja2.Environment(
    loader=jinja2.FileSystemLoader(ROOT), undefined=jinja2.StrictUndefined,
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
)


def home(value, variable):
    path = Path(os.environ.get(variable, value)).expanduser()
    return path if path.is_absolute() else ROOT / path


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


async def request(client, url, headers=None, data=None):
    response = await client.request("GET" if data is None else "POST", url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()


def claims(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def remaining_text(duration, used, reset):
    remaining = max(0, min(100, 100 - used))
    text = f"{duration}: {remaining:g}% left"
    if remaining == 0 and reset is not None:
        text += f" · resets: {reset.astimezone():%Y-%m-%d %H:%M %Z (%z)}"
    return text


class Provider:
    def __init__(self, name):
        self.name = name
        self.config = CONFIG[name]
        self.accounts = self.home / self.config["accounts_dir"]

    def account_path(self, address):
        return self.accounts / (hashlib.sha256(address.casefold().encode()).hexdigest() + ".json")

    def save(self, auth):
        address = self.email(auth)
        path = self.account_path(address)
        if read(path) != auth:
            write(path, auth)
        return address

    async def fetch(self, client, path, auth):
        if self.expired(auth):
            auth = await self.refresh(client, path, auth)
        try:
            return await self.usage(client, auth)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
        return await self.usage(client, await self.refresh(client, path, auth))

    async def status(self, client, path, active):
        auth = read(path)
        address = self.email(auth)
        data = await self.fetch(client, path, auth)
        return {"address": address, "active": address == active, "lines": self.limits(data)}


class Codex(Provider):
    title = "Codex"

    def __init__(self):
        self.home = home(CONFIG["codex"]["home"], "CODEX_HOME")
        self.auth = self.home / CONFIG["codex"]["auth_file"]
        super().__init__("codex")

    def current(self):
        return read(self.auth)

    def activate(self, auth):
        write(self.auth, auth)

    def email(self, auth):
        values = claims(auth["tokens"]["id_token"])
        return (values.get("email") or values["https://api.openai.com/profile"]["email"]).casefold()

    def expired(self, auth):
        return claims(auth["tokens"]["access_token"]).get("exp", float("inf")) <= time.time()

    async def usage(self, client, auth):
        tokens = auth["tokens"]
        return await request(client, self.config["usage_url"], headers={
            "Authorization": "Bearer " + tokens["access_token"],
            "ChatGPT-Account-Id": tokens["account_id"],
        })

    async def refresh(self, client, path, auth):
        current = self.current()
        if current and self.email(current) == self.email(auth) and current != auth:
            self.save(current)
            return current
        result = await request(client, self.config["token_url"], data={
            "client_id": self.config["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": auth["tokens"]["refresh_token"],
        })
        original = auth["tokens"].copy()
        for key in ("access_token", "refresh_token", "id_token"):
            if result.get(key):
                auth["tokens"][key] = result[key]
        auth["last_refresh"] = datetime.now(timezone.utc).isoformat()
        write(path, auth)
        current = self.current()
        if current and current["tokens"] == original:
            self.activate(auth)
        return auth

    @staticmethod
    def window_text(window):
        seconds = window["limit_window_seconds"]
        duration = f"{seconds / 3600:g}h" if seconds < 86400 else f"{seconds / 86400:g}d"
        return remaining_text(duration, window["used_percent"], datetime.fromtimestamp(window["reset_at"], timezone.utc))

    def limit_text(self, name, limit):
        windows = [self.window_text(limit[key]) for key in ("primary_window", "secondary_window") if limit.get(key)]
        text = f"{name}: " + (" | ".join(windows) or "percentage not available")
        if limit.get("limit_reached") or limit.get("allowed") is False:
            text += " · limit reached"
        return text

    def limits(self, data):
        limits = [("Codex", data.get("rate_limit"))]
        limits.extend((item["limit_name"], item.get("rate_limit")) for item in data.get("additional_rate_limits") or [])
        limits.append(("Review", data.get("code_review_rate_limit")))
        lines = [self.limit_text(name, limit) for name, limit in limits if limit]
        count = (data.get("rate_limit_reset_credits") or {}).get("available_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            lines.append(f"Resets: {count} available")
        return lines

    def login(self, device_auth):
        with tempfile.TemporaryDirectory(dir=self.accounts) as directory:
            command = [self.config["command"], "-c", 'cli_auth_credentials_store="file"', "login"]
            if device_auth:
                command.append("--device-auth")
            subprocess.run(command, env={**os.environ, "CODEX_HOME": directory}, check=True)
            return self.save(read(Path(directory) / self.config["auth_file"]))


class Claude(Provider):
    title = "Claude"

    def __init__(self):
        self.custom = "CLAUDE_CONFIG_DIR" in os.environ
        self.home = home(CONFIG["claude"]["home"], "CLAUDE_CONFIG_DIR")
        super().__init__("claude")
        self.global_config = self.home / ".claude.json" if self.custom else Path(self.config["config_file"]).expanduser()
        self.service = self.keychain_service(self.home if self.custom else None)

    def keychain_service(self, directory):
        service = self.config["keychain_service"]
        if directory is None:
            return service
        return service + "-" + hashlib.sha256(str(directory).encode()).hexdigest()[:8]

    @staticmethod
    def keychain_read(service):
        result = subprocess.run(
            ["security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode == 44:
            return None
        result.check_returncode()
        return json.loads(result.stdout)

    @staticmethod
    def keychain_write(service, value):
        secret = json.dumps(value, separators=(",", ":")).encode().hex()
        account = getpass.getuser()
        line = f'add-generic-password -U -a "{account}" -s "{service}" -X {secret}\n'
        if len(line) <= 4000:
            subprocess.run(["security", "-i"], input=line, text=True, capture_output=True, check=True)
        else:
            subprocess.run(
                ["security", "add-generic-password", "-U", "-a", account, "-s", service, "-X", secret],
                capture_output=True, check=True,
            )

    @staticmethod
    def keychain_delete(service):
        subprocess.run(
            ["security", "delete-generic-password", "-a", getpass.getuser(), "-s", service],
            capture_output=True,
        )

    def credentials(self, directory, service):
        if sys.platform == "darwin":
            return self.keychain_read(service)
        return read(directory / self.config["credentials_file"])

    def current(self):
        credentials = self.credentials(self.home, self.service)
        account = (read(self.global_config) or {}).get("oauthAccount")
        if not credentials or not credentials.get("claudeAiOauth") or not account:
            return None
        return {"claudeAiOauth": credentials["claudeAiOauth"], "oauthAccount": account}

    def write_credentials(self, oauth):
        credentials = self.credentials(self.home, self.service) or {}
        credentials["claudeAiOauth"] = oauth
        if sys.platform == "darwin":
            self.keychain_write(self.service, credentials)
        else:
            self.home.mkdir(parents=True, exist_ok=True)
            write(self.home / self.config["credentials_file"], credentials)

    def activate(self, auth):
        self.write_credentials(auth["claudeAiOauth"])
        config = read(self.global_config) or {}
        config["oauthAccount"] = auth["oauthAccount"]
        write(self.global_config, config)

    def email(self, auth):
        return auth["oauthAccount"]["emailAddress"].casefold()

    def expired(self, auth):
        return auth["claudeAiOauth"].get("expiresAt", float("inf")) <= time.time() * 1000

    async def usage(self, client, auth):
        return await request(client, self.config["usage_url"], headers={
            "Authorization": "Bearer " + auth["claudeAiOauth"]["accessToken"],
            "anthropic-beta": self.config["beta"],
        })

    async def refresh(self, client, path, auth):
        current = await asyncio.to_thread(self.current)
        if current and self.email(current) == self.email(auth) and current != auth:
            self.save(current)
            return current
        original = auth["claudeAiOauth"].copy()
        result = await request(client, self.config["token_url"], data={
            "client_id": self.config["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": original["refreshToken"],
        })
        auth["claudeAiOauth"]["accessToken"] = result["access_token"]
        if result.get("refresh_token"):
            auth["claudeAiOauth"]["refreshToken"] = result["refresh_token"]
        if result.get("expires_in"):
            auth["claudeAiOauth"]["expiresAt"] = int((time.time() + result["expires_in"]) * 1000)
        write(path, auth)
        current = await asyncio.to_thread(self.current)
        if current and current["claudeAiOauth"] == original:
            await asyncio.to_thread(self.write_credentials, auth["claudeAiOauth"])
        return auth

    def limits(self, data):
        groups = {}
        for limit in data.get("limits") or []:
            scope = ((limit.get("scope") or {}).get("model") or {}).get("display_name") or "Claude"
            duration = "5h" if limit.get("group") == "session" else "7d"
            reset = limit.get("resets_at")
            reset = datetime.fromisoformat(reset) if reset else None
            groups.setdefault(scope, []).append(remaining_text(duration, limit.get("percent") or 0, reset))
        return [f"{name}: " + " | ".join(windows) for name, windows in groups.items()]

    def login(self, device_auth):
        with tempfile.TemporaryDirectory(dir=self.accounts) as directory:
            directory = Path(directory).resolve()
            service = self.keychain_service(directory)
            try:
                subprocess.run(
                    [self.config["command"], "auth", "login"],
                    env={**os.environ, "CLAUDE_CONFIG_DIR": str(directory)}, check=True,
                )
                credentials = self.credentials(directory, service)
                account = read(directory / ".claude.json")["oauthAccount"]
                return self.save({"claudeAiOauth": credentials["claudeAiOauth"], "oauthAccount": account})
            finally:
                if sys.platform == "darwin":
                    self.keychain_delete(service)


PROVIDERS = {"codex": Codex, "claude": Claude}


async def main():
    parser = argparse.ArgumentParser(description="Read limits and switch Codex and Claude Code accounts.")
    parser.add_argument("provider", nargs="?", metavar="codex|claude")
    parser.add_argument("account", nargs="?", metavar="email|login|add")
    parser.add_argument("--device-auth", action="store_true", help="use a device code with codex login")
    args = parser.parse_args()
    if args.provider not in (None, *PROVIDERS):
        if args.account is not None:
            parser.error(f"unknown provider: {args.provider}")
        args.provider, args.account = None, args.provider
    if args.account == "login" and args.provider is None:
        parser.error("login requires codex or claude")
    if args.device_auth and (args.account != "login" or args.provider != "codex"):
        parser.error("--device-auth requires codex login")
    providers = [PROVIDERS[args.provider]()] if args.provider else [cls() for cls in PROVIDERS.values()]
    found = False
    listing = []
    with ExitStack() as stack:
        for provider in providers:
            provider.accounts.mkdir(parents=True, exist_ok=True, mode=0o700)
            provider.accounts.chmod(0o700)
            lock = stack.enter_context((provider.accounts / ".lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = provider.current()
            active = provider.save(current) if current else None
            if args.account == "login":
                address = provider.login(args.device_auth)
                print(f"{provider.title} account saved: {address}\nTo activate: ax {provider.name} {address}")
            elif args.account == "add":
                print(f"{provider.title} account saved: {active}" if active else f"{provider.title}: no active account. Run ax {provider.name} login.")
            elif args.account:
                target = read(provider.account_path(args.account))
                if target is None:
                    continue
                found = True
                if target != current:
                    provider.activate(target)
                print(f"{provider.title} active account: {provider.email(target)}")
            else:
                listing.append((provider, active, list(provider.accounts.glob("*.json"))))
        if listing:
            async with httpx.AsyncClient(
                timeout=CONFIG["timeout_seconds"], limits=httpx.Limits(max_connections=CONFIG["workers"]),
            ) as client:
                results = await asyncio.gather(*(
                    asyncio.gather(*(provider.status(client, path, active) for path in paths))
                    for provider, active, paths in listing
                ))
            sections = [
                {"title": provider.title, "name": provider.name, "accounts": accounts}
                for (provider, _, _), accounts in zip(listing, results)
            ]
            print(TEMPLATES.get_template("ax.j2").render(providers=sections), end="")
    if args.account not in (None, "login", "add") and not found:
        print("Account not found. Run ax codex login or ax claude login.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
