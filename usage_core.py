"""Shared core for the Claude usage widget: no UI dependencies.

Responsible for reading the local OAuth credentials, calling the usage
endpoint, refreshing the token when needed, and formatting the numbers into
display strings the tray / menu-bar adapters can render verbatim.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

from config import (
    BETA_HEADER,
    DEFAULT_POLL,
    FALLBACK_VERSION,
    HTTP_TIMEOUT,
    OAUTH_CLIENT_ID,
    REFRESH_SKEW_MS,
    TOKEN_URLS,
    USAGE_URL,
)


# --------------------------------------------------------------------------- #
# Exceptions & status
# --------------------------------------------------------------------------- #
class CredentialsMissing(Exception):
    """No usable OAuth credentials were found on this machine."""


class RefreshRejected(Exception):
    """The refresh token was rejected (invalid_grant); re-login required."""


class RefreshFailed(Exception):
    """Refresh failed for a transient reason (network, host down)."""


class Status(Enum):
    """Non-usage outcomes a poll can produce."""

    RATE_LIMITED = "rate_limited"
    OFFLINE = "offline"
    NO_DATA = "no_data"
    NO_CREDS = "no_creds"
    ERROR = "error"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Limit:
    kind: str                       # "session" | "weekly_all" | "weekly_scoped" | ...
    group: str                      # "session" | "weekly"
    percent: float                  # 0-100
    severity: str                   # "normal" | "warning" | "critical" | ...
    resets_at: Optional[datetime]   # tz-aware UTC, or None
    scope_label: Optional[str]      # e.g. "Fable" for scoped weekly limits
    is_active: bool


@dataclass
class Usage:
    limits: list                    # list[Limit]
    plan: str = ""                  # subscriptionType, e.g. "max"
    rate_tier: str = ""             # rateLimitTier, e.g. "default_claude_max_5x"
    raw: dict = field(default_factory=dict)


@dataclass
class PollState:
    backoff: int = 0
    last_usage: Optional[Usage] = None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:  # always return tz-aware UTC (avoids naive/aware crashes)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _scope_label(scope) -> Optional[str]:
    if not scope:
        return None
    model = scope.get("model") or {}
    name = model.get("display_name")
    if name:
        return name
    return scope.get("surface")


def parse_usage(data: dict, plan: str = "", rate_tier: str = "") -> Usage:
    """Normalize the endpoint response into a Usage.

    Prefers the self-describing ``limits`` array (current API shape); falls
    back to the flat ``five_hour`` / ``seven_day`` / ``seven_day_*`` fields
    for older responses.
    """
    limits: list = []
    raw_limits = data.get("limits")

    if isinstance(raw_limits, list) and raw_limits:
        for item in raw_limits:
            limits.append(
                Limit(
                    kind=item.get("kind", ""),
                    group=item.get("group", ""),
                    percent=float(item.get("percent") or 0),
                    severity=item.get("severity", "normal"),
                    resets_at=_parse_dt(item.get("resets_at")),
                    scope_label=_scope_label(item.get("scope")),
                    is_active=bool(item.get("is_active")),
                )
            )
    else:
        def add(kind, group, block, scope_label=None):
            if not block:
                return
            limits.append(
                Limit(
                    kind=kind,
                    group=group,
                    percent=float(block.get("utilization") or 0),
                    severity="normal",
                    resets_at=_parse_dt(block.get("resets_at")),
                    scope_label=scope_label,
                    is_active=(group == "session"),
                )
            )

        add("session", "session", data.get("five_hour"))
        add("weekly_all", "weekly", data.get("seven_day"))
        add("weekly_scoped", "weekly", data.get("seven_day_opus"), "Opus")
        add("weekly_scoped", "weekly", data.get("seven_day_sonnet"), "Sonnet")
        add("weekly_scoped", "weekly", data.get("seven_day_fable"), "Fable")

    return Usage(limits=limits, plan=plan, rate_tier=rate_tier, raw=data)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
_from_keychain = False  # tracks where the current credentials came from


def _config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _cred_file() -> Path:
    return _config_dir() / ".credentials.json"


def _read_macos_keychain() -> Optional[dict]:
    for svc in ("Claude Code-credentials", "Claude Code"):
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", svc, "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout.strip())
        except Exception:
            continue
    return None


def read_credentials_full() -> dict:
    """Return the full credentials dict ({"claudeAiOauth": {...}}).

    Reads fresh on every call, so a token that Claude Code refreshed on disk
    is picked up automatically. On macOS, the Keychain is preferred with the
    JSON file as a fallback.
    """
    global _from_keychain
    _from_keychain = False
    cred_file = _cred_file()

    if sys.platform == "darwin":
        data = _read_macos_keychain()
        if data is not None:
            _from_keychain = True
            return data
        if cred_file.exists():
            return json.loads(cred_file.read_text(encoding="utf-8"))
        raise CredentialsMissing("No Claude credentials in Keychain or ~/.claude")

    if cred_file.exists():
        return json.loads(cred_file.read_text(encoding="utf-8"))
    raise CredentialsMissing(f"Not found: {cred_file}")


# --------------------------------------------------------------------------- #
# User-Agent (mandatory for the usage endpoint)
# --------------------------------------------------------------------------- #
_ua_cache: Optional[str] = None


def user_agent() -> str:
    global _ua_cache
    if _ua_cache:
        return _ua_cache
    version = FALLBACK_VERSION
    for cmd in (["claude", "--version"], ["claude.cmd", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            m = re.search(r"\d+\.\d+\.\d+", out.stdout or "")
            if m:
                version = m.group(0)
                break
        except Exception:
            continue
    _ua_cache = f"claude-code/{version}"
    return _ua_cache


# --------------------------------------------------------------------------- #
# Network: fetch + refresh
# --------------------------------------------------------------------------- #
def fetch_usage(token: str, plan: str = "", rate_tier: str = ""):
    """Return (status_str, Usage|None). status_str in {ok, unauthorized,
    rate_limited, offline, no_data, http_<code>}."""
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": user_agent(),
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(USAGE_URL, headers=headers, timeout=HTTP_TIMEOUT)
    except (requests.ConnectionError, requests.Timeout):
        return ("offline", None)

    if resp.status_code == 401:
        return ("unauthorized", None)
    if resp.status_code == 429:
        return ("rate_limited", None)
    if resp.status_code != 200:
        return (f"http_{resp.status_code}", None)

    try:
        data = resp.json()
    except ValueError:
        return ("no_data", None)

    usage = parse_usage(data, plan, rate_tier)
    if not usage.limits:
        return ("no_data", None)
    return ("ok", usage)


def refresh_token(refresh_tok: str) -> dict:
    """Exchange a refresh token for a new access token. Returns the raw token
    response {access_token, refresh_token, expires_in}."""
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": OAUTH_CLIENT_ID,
    }
    headers = {"Content-Type": "application/json", "User-Agent": user_agent()}
    last_err = None
    for url in TOKEN_URLS:
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (400, 401):
                # invalid_grant / invalid token: retrying the other host won't help.
                raise RefreshRejected(f"{resp.status_code}: {resp.text[:200]}")
            last_err = f"{resp.status_code}: {resp.text[:200]}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = str(exc)
    raise RefreshFailed(str(last_err))


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)  # atomic on Windows and POSIX
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _write_macos_keychain(full: dict) -> None:
    import getpass

    payload = json.dumps(full, separators=(",", ":"))
    subprocess.run(
        [
            "security", "add-generic-password", "-U",
            "-s", "Claude Code-credentials",
            "-a", getpass.getuser(),
            "-w", payload,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )


def persist_credentials(new: dict, full: dict) -> None:
    """Persist a refreshed token, preserving the file's other keys/shape.

    Only accessToken / refreshToken / expiresAt are updated. On macOS with a
    Keychain-sourced credential this is best-effort (write-back can fail on a
    locked Keychain or SSH session); the fresh in-memory token is still used
    for the running session.
    """
    oauth = full.get("claudeAiOauth", {})
    oauth["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        oauth["refreshToken"] = new["refresh_token"]  # refresh token rotates
    if new.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(new["expires_in"]) * 1000
    full["claudeAiOauth"] = oauth

    if sys.platform == "darwin" and _from_keychain:
        try:
            _write_macos_keychain(full)
        except Exception:
            pass  # keep the in-memory token; documented limitation
    else:
        _atomic_write_json(_cred_file(), full)


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
def poll_seconds() -> int:
    try:
        val = int(os.environ.get("CLAUDE_WIDGET_POLL", str(DEFAULT_POLL)))
    except ValueError:
        val = DEFAULT_POLL
    return max(60, min(300, val))


def _expiring_soon(creds: dict) -> bool:
    try:
        expires_at = int(creds.get("expiresAt") or 0)
    except (TypeError, ValueError):
        return False
    if not expires_at:
        return False
    return (expires_at - int(time.time() * 1000)) < REFRESH_SKEW_MS


def _refresh_and_persist(refresh_tok: str, full: dict) -> str:
    new = refresh_token(refresh_tok)
    persist_credentials(new, full)
    return new["access_token"]


def _fake_usage() -> Optional[Usage]:
    """Testing hook: CLAUDE_WIDGET_FAKE="session,weekly[,scoped]" short-circuits
    the network so colors/rendering can be exercised without real load."""
    spec = os.environ.get("CLAUDE_WIDGET_FAKE")
    if not spec:
        return None
    parts = [p.strip() for p in spec.split(",")]

    def num(idx):
        return float(parts[idx]) if idx < len(parts) and parts[idx] else 0.0

    now = datetime.now(timezone.utc)
    limits = [
        Limit("session", "session", num(0), "normal",
              now + timedelta(hours=2, minutes=55), None, True),
        Limit("weekly_all", "weekly", num(1), "normal",
              now + timedelta(days=5, hours=3), None, False),
        Limit("weekly_scoped", "weekly", num(2), "normal", None, "Fable", False),
    ]
    return Usage(limits=limits, plan="max", rate_tier="default_claude_max_5x")


def poll_once(state: PollState):
    """Perform one poll. Returns a Usage on success, or a Status otherwise.
    May raise CredentialsMissing."""
    fake = _fake_usage()
    if fake is not None:
        state.backoff = 0
        state.last_usage = fake
        return fake

    full = read_credentials_full()
    creds = full.get("claudeAiOauth", {})
    token = creds.get("accessToken")
    plan = creds.get("subscriptionType", "")
    rate_tier = creds.get("rateLimitTier", "")
    refresh_tok = creds.get("refreshToken")

    if not token:
        raise CredentialsMissing("credentials file missing accessToken")

    # Proactive refresh if the token is about to expire. A RefreshRejected
    # (invalid_grant) means re-login is required — track it to surface NO_CREDS.
    rejected = False
    if _expiring_soon(creds) and refresh_tok:
        try:
            token = _refresh_and_persist(refresh_tok, full)
            refresh_tok = full.get("claudeAiOauth", {}).get("refreshToken", refresh_tok)  # rotated
        except RefreshRejected:
            rejected = True
        except RefreshFailed:
            pass  # transient — try the existing token anyway

    status, usage = fetch_usage(token, plan, rate_tier)

    # Reactive refresh: exactly one retry on 401.
    if status == "unauthorized" and refresh_tok and not rejected:
        try:
            token = _refresh_and_persist(refresh_tok, full)
            status, usage = fetch_usage(token, plan, rate_tier)
        except RefreshRejected:
            rejected = True
        except RefreshFailed:
            pass

    if status == "ok":
        state.backoff = 0
        state.last_usage = usage
        return usage
    if status == "rate_limited":
        base = state.backoff or poll_seconds()
        state.backoff = min(base * 2, 600)
        return Status.RATE_LIMITED
    if status == "offline":
        return Status.OFFLINE
    if status == "no_data":
        return Status.NO_DATA
    if rejected or status == "unauthorized":
        return Status.NO_CREDS  # re-login required
    return Status.ERROR


# --------------------------------------------------------------------------- #
# Formatting for display
# --------------------------------------------------------------------------- #
def critical_limit(usage: Usage) -> Optional[Limit]:
    """The limit the user is closest to hitting (highest percent)."""
    if not usage.limits:
        return None
    return max(usage.limits, key=lambda limit: limit.percent)


def color_for(pct: float) -> str:
    if pct < 50:
        return "green"
    if pct <= 80:
        return "amber"
    return "red"


def _reset_in(resets_at: Optional[datetime]) -> str:
    if not resets_at:
        return ""
    secs = int((resets_at - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "resetting now"
    hours, mins = secs // 3600, (secs % 3600) // 60
    if hours >= 24:
        days, hours = hours // 24, hours % 24
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {mins:02d}m"
    return f"resets in {mins}m"


def _reset_at(resets_at: Optional[datetime]) -> str:
    if not resets_at:
        return ""
    local = resets_at.astimezone()  # convert UTC -> system local
    hour_fmt = "%#I" if os.name == "nt" else "%-I"  # no leading zero
    return "resets " + local.strftime(f"%a {hour_fmt}:%M %p")


def _find(usage: Usage, kind: str) -> Optional[Limit]:
    for limit in usage.limits:
        if limit.kind == kind:
            return limit
    return None


def _pct(value: float) -> str:
    return f"{int(round(value))}%"


def _plan_label(usage: Usage) -> str:
    names = {
        "max": "Max",
        "pro": "Pro",
        "team": "Team",
        "enterprise": "Enterprise",
        "free": "Free",
    }
    base = names.get((usage.plan or "").lower(), (usage.plan or "Claude").capitalize())
    m = re.search(r"(\d+)x", usage.rate_tier or "")
    if m:
        base += f" ({m.group(1)}x)"
    return f"Plan: {base}"


def format_breakdown(usage: Usage) -> dict:
    """Turn a Usage into the flat dict both UI adapters render verbatim."""
    crit = critical_limit(usage)
    pct = crit.percent if crit else 0.0

    session = _find(usage, "session")
    weekly = _find(usage, "weekly_all")
    scoped = [l for l in usage.limits if l.kind == "weekly_scoped"]

    session_line = None
    if session:
        session_line = f"Session   {_pct(session.percent)}"
        reset = _reset_in(session.resets_at)
        if reset:
            session_line += f"   ·   {reset}"

    weekly_line = None
    if weekly:
        weekly_line = f"Weekly (all)   {_pct(weekly.percent)}"
        reset = _reset_at(weekly.resets_at)
        if reset:
            weekly_line += f"   ·   {reset}"

    models = [f"{(l.scope_label or 'Scoped')}   {_pct(l.percent)}" for l in scoped]

    tip = []
    if session:
        tip.append(f"Session {_pct(session.percent)}")
    if weekly:
        tip.append(f"Weekly {_pct(weekly.percent)}")
    tooltip = "Claude · " + " · ".join(tip) if tip else "Claude usage"

    return {
        "face_pct": _pct(pct),
        "face_color": color_for(pct),
        "plan": _plan_label(usage),
        "session": session_line,
        "weekly": weekly_line,
        "models": models,
        "tooltip": tooltip,
        "critical_kind": crit.kind if crit else None,
        # per-window numbers for the taskbar readout
        "session_pct": int(round(session.percent)) if session else None,
        "session_color": color_for(session.percent) if session else "grey",
        "session_resets_at": session.resets_at if session else None,
        "weekly_pct": int(round(weekly.percent)) if weekly else None,
        "weekly_color": color_for(weekly.percent) if weekly else "grey",
        "weekly_resets_at": weekly.resets_at if weekly else None,
        "model_rows": [
            {"label": l.scope_label or "Scoped",
             "pct": int(round(l.percent)),
             "color": color_for(l.percent)}
            for l in scoped
        ],
    }


def status_display(status: Status) -> dict:
    """Display dict for the non-usage states."""
    # Faces are drawn into the icon bitmap, so keep them to glyphs the tray
    # font reliably has (no ellipsis / em dash).
    table = {
        Status.RATE_LIMITED: ("...", "amber", "rate limited, backing off"),
        Status.OFFLINE: ("-", "grey", "offline - showing last known"),
        Status.NO_DATA: ("-", "grey", "no usage data yet"),
        Status.NO_CREDS: ("!", "grey", "not logged in to Claude Code"),
        Status.ERROR: ("!", "grey", "error fetching usage"),
    }
    face, color, note = table.get(status, ("?", "grey", "usage"))
    return {
        "face_pct": face,
        "face_color": color,
        "plan": None,
        "session": note,
        "weekly": None,
        "models": [],
        "tooltip": f"Claude · {note}",
        "critical_kind": None,
        "session_pct": None,
        "session_color": "grey",
        "session_resets_at": None,
        "weekly_pct": None,
        "weekly_color": "grey",
        "weekly_resets_at": None,
        "model_rows": [],
    }
