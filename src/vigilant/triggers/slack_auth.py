# Copyright 2026 Timothy Long / Longitudinal Intelligence Technologies (LIT)
# SPDX-License-Identifier: Apache-2.0
"""Slack token acquisition with automatic refresh.

The poll-based monitor needs a Slack token. There are two ways to get one, both
app-free:

  1. **Static** - you export ``SLACK_TOKEN`` (+ ``SLACK_COOKIE_D`` for xoxc-)
     yourself. Simple, but an xoxc- session token expires every few hours and
     you'd have to re-set it by hand.
  2. **Auto (this module)** - Vigilant reads the token straight from your
     logged-in Slack session in a local Chromium profile (Playwright), caches
     it, and silently re-extracts it whenever Slack expires it. Nothing is
     installed into the workspace.

Playwright is an **optional** dependency (``pip install
'vigilant-pr[slack-refresh]'`` then ``python -m playwright install chromium``);
it is imported lazily so the core stays stdlib-only and static-token users never
need it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from .slack_client import SlackClient, SlackError


class TokenSource(Protocol):
    """Something that can hand out a usable ``(token, cookie_d)`` pair."""

    def get(self, force_refresh: bool = False) -> tuple[str, str | None]: ...

    @property
    def can_refresh(self) -> bool: ...


class EnvTokenSource:
    """A fixed token from the environment. Cannot self-refresh."""

    def __init__(self, token: str, cookie_d: str | None):
        self._token = token
        self._cookie_d = cookie_d

    def get(self, force_refresh: bool = False) -> tuple[str, str | None]:
        return self._token, self._cookie_d

    @property
    def can_refresh(self) -> bool:
        return False


def _cache_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "vigilant-pr" / "slack_tokens.json"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    try:
        path.chmod(0o600)  # tokens are secrets; keep them owner-only
    except OSError:
        pass


def extract_from_browser() -> dict[str, Any]:
    """Extract Slack ``xoxc-`` tokens + the ``d`` cookie from a local Chrome
    session via Playwright.

    Returns ``{"cookie_d": str, "teams": {team_id: {"name", "token"}}}``. Raises
    SlackError with actionable guidance if Playwright is missing or extraction
    fails. Copies the profile to a temp dir when Chrome is running (it locks the
    original), mirroring the proven YTB approach.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SlackError(
            "Automatic Slack token refresh needs Playwright. Install it with:\n"
            "  pip install 'vigilant-pr[slack-refresh]'\n"
            "  python -m playwright install chromium\n"
            "Or set SLACK_TOKEN (+ SLACK_COOKIE_D) yourself to skip auto-refresh."
        ) from e

    import shutil
    import subprocess
    import tempfile

    chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    if not chrome_root.exists():  # non-macOS default locations
        for candidate in (
            Path.home() / ".config" / "google-chrome",
            Path.home() / ".config" / "chromium",
        ):
            if candidate.exists():
                chrome_root = candidate
                break

    chrome_running = (
        subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True).returncode == 0
    )
    temp_profile: str | None = None
    user_data_dir = str(chrome_root)
    if chrome_running:
        temp_profile = tempfile.mkdtemp(prefix="vigilant_slack_")
        src_default = chrome_root / "Default"
        dst_default = Path(temp_profile) / "Default"
        dst_default.mkdir(parents=True, exist_ok=True)
        for item in ("Cookies", "Cookies-journal", "Local Storage", "Session Storage"):
            src = src_default / item
            if src.exists():
                if src.is_dir():
                    shutil.copytree(src, dst_default / item, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst_default / item)
        if (src_default / "Preferences").exists():
            shutil.copy2(src_default / "Preferences", dst_default / "Preferences")
        if (chrome_root / "Local State").exists():
            shutil.copy2(chrome_root / "Local State", Path(temp_profile) / "Local State")
        user_data_dir = temp_profile

    tokens: dict[str, Any] = {}
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=os.path.join(user_data_dir, "Default"),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()
            page.goto("https://app.slack.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            teams = page.evaluate(
                """() => {
                    try {
                        const cfg = JSON.parse(localStorage.getItem("localConfig_v2"));
                        if (!cfg || !cfg.teams) return null;
                        const out = {};
                        for (const [id, t] of Object.entries(cfg.teams)) {
                            out[id] = { name: t.name, token: t.token };
                        }
                        return out;
                    } catch (e) { return null; }
                }"""
            )
            if teams:
                tokens["teams"] = teams
            for cookie in context.cookies("https://app.slack.com"):
                if cookie["name"] == "d":
                    tokens["cookie_d"] = cookie["value"]
                    break
            context.close()
    except Exception as e:  # noqa: BLE001 - normalize into SlackError
        raise SlackError(f"Slack token extraction failed: {e}") from e
    finally:
        if temp_profile:
            shutil.rmtree(temp_profile, ignore_errors=True)

    if not tokens.get("teams") or not tokens.get("cookie_d"):
        raise SlackError(
            "Could not read a Slack session from Chrome. Open Slack in Chrome and "
            "sign in, or set SLACK_TOKEN (+ SLACK_COOKIE_D) manually."
        )
    return tokens


def _select_team_token(
    extracted: dict[str, Any], team_hint: str | None, probe_channel: str | None
) -> tuple[str, str]:
    """Pick the right workspace token from the extracted set.

    Preference: explicit ``team_hint`` (VIGILANT_SLACK_TEAM) -> the only team if
    there is exactly one -> probe each token against ``probe_channel`` and take
    the first that can read it. Returns ``(token, cookie_d)``.
    """
    cookie_d = str(extracted["cookie_d"])
    teams: dict[str, Any] = extracted["teams"]

    if team_hint and team_hint in teams:
        return str(teams[team_hint]["token"]), cookie_d
    if len(teams) == 1:
        only = next(iter(teams.values()))
        return str(only["token"]), cookie_d
    if probe_channel:
        for team in teams.values():
            token = str(team["token"])
            try:
                SlackClient(token, cookie_d).conversations_history(probe_channel, limit=1)
                return token, cookie_d
            except SlackError:
                continue
    raise SlackError(
        "Multiple Slack workspaces found in your session and none could read the "
        f"target channel. Set VIGILANT_SLACK_TEAM to one of: {', '.join(teams)}."
    )


class BrowserTokenSource:
    """Token from the local Chrome Slack session, cached and auto-refreshed."""

    def __init__(self, team_hint: str | None = None, probe_channel: str | None = None):
        self.team_hint = team_hint or os.environ.get("VIGILANT_SLACK_TEAM")
        self.probe_channel = probe_channel
        self.cache_path = _cache_path()

    @property
    def can_refresh(self) -> bool:
        return True

    def get(self, force_refresh: bool = False) -> tuple[str, str | None]:
        if not force_refresh:
            cached = _load_cache(self.cache_path)
            token = cached.get("token")
            cookie_d = cached.get("cookie_d")
            if token and self._is_valid(token, cookie_d):
                return str(token), (str(cookie_d) if cookie_d else None)
        extracted = extract_from_browser()
        token, cookie_d = _select_team_token(extracted, self.team_hint, self.probe_channel)
        _save_cache(
            self.cache_path,
            {"token": token, "cookie_d": cookie_d, "extracted_at": time.strftime("%FT%T")},
        )
        return token, cookie_d

    @staticmethod
    def _is_valid(token: str, cookie_d: str | None) -> bool:
        try:
            SlackClient(token, cookie_d).auth_test()
            return True
        except SlackError:
            return False


def build_token_source(
    auto: bool, probe_channel: str | None = None
) -> TokenSource:
    """Choose a token source from the environment and the ``--auto-token`` flag.

    A static ``SLACK_TOKEN`` always wins (explicit intent). Otherwise, if ``auto``
    (``--auto-token`` / ``VIGILANT_SLACK_AUTO_TOKEN``) is set, use the browser
    source. Raises SlackError with guidance if neither is available.
    """
    env_token = os.environ.get("SLACK_TOKEN")
    if env_token:
        return EnvTokenSource(env_token, os.environ.get("SLACK_COOKIE_D"))
    if auto or _truthy(os.environ.get("VIGILANT_SLACK_AUTO_TOKEN")):
        return BrowserTokenSource(probe_channel=probe_channel)
    raise SlackError(
        "No Slack token. Either export SLACK_TOKEN (+ SLACK_COOKIE_D for xoxc-), "
        "or pass --auto-token to read and auto-refresh it from your Chrome Slack "
        "session (needs the slack-refresh extra)."
    )


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")
