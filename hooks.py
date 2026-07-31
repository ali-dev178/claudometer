"""Instant session transitions via Claude Code hooks: no UI dependencies.

The registry file-watch in ``sessions_core`` already updates within a second at
no cost, so hooks are strictly the opt-in low-latency layer. What they add that
the registry cannot:

  * ``Notification.message`` — the actual prompt text Claude is waiting on,
    where the registry only has a coarse ``waitingFor`` category
  * ``Stop.last_assistant_message`` — what it said when it finished
  * events land the instant they happen rather than on the next poll

Only four events are registered — Notification, Stop, SessionStart, SessionEnd.
PreToolUse/PostToolUse fire constantly and each one costs a process spawn on
the critical path of the user's session, which is a bad trade for a widget.

This module edits ``~/.claude/settings.json``, which belongs to Claude Code and
not to us. So: nothing is written without the caller confirming :func:`preview`
first, the original is backed up on the first change, every unrelated key is
preserved byte-for-byte in value, and :func:`remove` puts the file back exactly
as it was found.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

#: The events we register. Order is the order they appear in the preview.
EVENTS = ("Notification", "Stop", "SessionStart", "SessionEnd")

#: Filename of the relay script; also how our entries are recognised for
#: clean removal, so it must stay distinctive.
RELAY_NAME = "hook_relay.py"

#: Claudometer's own directory. The relay is COPIED here at install time and
#: the hook points at the copy, never at the application directory. Pointing at
#: the app means the hook dies whenever the app moves — which it does on every
#: update, and permanently on uninstall — leaving Claude Code to spawn a
#: failing command on every event, forever, in a file the user never edited.
HOME_DIR = Path.home() / ".claudometer"

#: Refreshed while Claudometer runs. The relay uses it to notice that nobody is
#: draining the spool any more, which is what an uninstall looks like from
#: inside a hook.
HEARTBEAT_NAME = "heartbeat"

#: How long the heartbeat may go unrefreshed before the relay concludes
#: Claudometer is gone and uninstalls the hooks itself. Long enough that simply
#: not opening the app for a while is harmless — and if it does fire, the next
#: launch silently reinstalls.
STALE_DAYS = 7

#: Seconds Claude Code waits for the relay before giving up. It writes one
#: small file, so this is generous.
HOOK_TIMEOUT_S = 5

#: Spool files older than this are junk from a Claudometer that isn't running.
MAX_EVENT_AGE_S = 3600

#: Never read more than this many spool files in one pass.
MAX_EVENTS_PER_READ = 200

# Status values returned by status()
INSTALLED = "installed"
ABSENT = "absent"
STALE = "stale"            # our entries exist but point at a different install
UNAVAILABLE = "unavailable"  # no interpreter to run the relay with


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _config_dir(config_dir=None) -> Path:
    return Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
                or (Path.home() / ".claude"))


def settings_path(config_dir=None) -> Path:
    return _config_dir(config_dir) / "settings.json"


def spool_dir() -> Path:
    return Path(os.environ.get("CLAUDOMETER_EVENTS_DIR")
                or (Path.home() / ".claudometer-events"))


def home_dir() -> Path:
    return Path(os.environ.get("CLAUDOMETER_HOME") or HOME_DIR)


def relay_path() -> Path:
    """The relay shipped inside the application."""
    return Path(__file__).resolve().parent / RELAY_NAME


def installed_relay_path() -> Path:
    """The copy the hook actually runs — outside the application directory."""
    return home_dir() / RELAY_NAME


def heartbeat_path() -> Path:
    return home_dir() / HEARTBEAT_NAME


def touch_heartbeat() -> None:
    """Tell the relay we're alive. Cheap enough to call on a timer."""
    try:
        home_dir().mkdir(parents=True, exist_ok=True)
        heartbeat_path().write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def _install_relay() -> bool:
    """Put a copy of the relay somewhere that outlives this install."""
    try:
        home_dir().mkdir(parents=True, exist_ok=True)
        shutil.copyfile(relay_path(), installed_relay_path())
        return True
    except OSError:
        return False


def interpreter() -> Optional[str]:
    """A Python that can run the relay, or None if there isn't one.

    Running from source, this process's own interpreter is the right answer.
    In a PyInstaller build ``sys.executable`` is the packaged exe, which would
    re-launch the whole widget per hook event — so a real interpreter has to be
    found on PATH, and when there isn't one hooks are simply unavailable and
    the file-watch carries on alone.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable or None
    for name in ("pythonw", "python3", "python", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _quote(value: str) -> str:
    return '"%s"' % str(value).replace('"', r"\"")


def hook_command() -> Optional[str]:
    exe = interpreter()
    if not exe:
        return None
    return "%s %s" % (_quote(exe), _quote(str(installed_relay_path())))


# --------------------------------------------------------------------------- #
# Building and recognising our entries
# --------------------------------------------------------------------------- #
def _entry(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command,
                       "timeout": HOOK_TIMEOUT_S}]}


def build_entries(command: Optional[str] = None) -> dict:
    """{event: [entry]} for the events we register."""
    command = command or hook_command()
    if not command:
        return {}
    return {event: [_entry(command)] for event in EVENTS}


def _is_ours(entry) -> bool:
    """True for a hook group that runs our relay, whoever installed it."""
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks") or []:
        if isinstance(hook, dict) and RELAY_NAME in str(hook.get("command", "")):
            return True
    return False


def _is_current(entry, command: str) -> bool:
    for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
        if isinstance(hook, dict) and hook.get("command") == command:
            return True
    return False


# --------------------------------------------------------------------------- #
# Reading / writing settings.json
# --------------------------------------------------------------------------- #
def read_settings(config_dir=None) -> dict:
    """The current settings.json, or {} when absent/unreadable.

    A malformed file returns {} — callers must treat that as "do not write",
    since overwriting a file we failed to parse would destroy the user's
    configuration.
    """
    path = settings_path(config_dir)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def settings_readable(config_dir=None) -> bool:
    """False when the file exists but we can't parse it — never write then."""
    path = settings_path(config_dir)
    if not path.is_file():
        return True          # absent is fine; we'd create it
    try:
        json.loads(path.read_text(encoding="utf-8-sig"))
        return True
    except (OSError, ValueError):
        return False


def _write_settings(data: dict, config_dir=None) -> bool:
    path = settings_path(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = path.with_suffix(".json.claudometer-bak")
            if not backup.exists():   # keep the pristine original, not the last write
                shutil.copy2(path, backup)
        tmp = path.with_suffix(".json.claudometer-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def status(config_dir=None) -> str:
    command = hook_command()
    if not command:
        return UNAVAILABLE
    hooks = read_settings(config_dir).get("hooks")
    if not isinstance(hooks, dict):
        return ABSENT
    found = current = 0
    for event in EVENTS:
        for entry in hooks.get(event) or []:
            if _is_ours(entry):
                found += 1
                if _is_current(entry, command):
                    current += 1
                break
    if found == 0:
        return ABSENT
    if current == len(EVENTS):
        return INSTALLED
    return STALE      # ours, but a different interpreter or relay path


def preview(config_dir=None) -> str:
    """Exactly what :func:`install` will add, for the confirm dialog.

    The user is being asked to let one app edit another app's config file, so
    they get to see the literal JSON first rather than a description of it.
    """
    entries = build_entries()
    if not entries:
        return ("No Python interpreter was found to run the hook relay, so "
                "instant alerts aren't available in this build. Sessions still "
                "update about once a second from the registry.")
    return json.dumps({"hooks": entries}, indent=2)


def install(config_dir=None) -> bool:
    """Merge our entries into settings.json. Returns False without writing if
    anything looks unsafe."""
    command = hook_command()
    if not command or not settings_readable(config_dir):
        return False
    if not _install_relay():
        return False
    touch_heartbeat()      # before the hooks exist, so the relay never sees a
                           # fresh install as an abandoned one
    data = read_settings(config_dir)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in EVENTS:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        # Drop any previous entry of ours (a stale path) but keep everyone
        # else's hooks exactly where they were.
        kept = [e for e in existing if not _is_ours(e)]
        kept.append(_entry(command))
        hooks[event] = kept
    data["hooks"] = hooks
    return _write_settings(data, config_dir)


def remove(config_dir=None) -> bool:
    """Take our entries back out, leaving the file as we found it."""
    if not settings_readable(config_dir):
        return False
    data = read_settings(config_dir)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return True                      # nothing of ours to remove
    changed = False
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _is_ours(e)]
        if len(kept) != len(entries):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]             # don't leave an empty list behind
    if not hooks:
        data.pop("hooks", None)          # nor an empty hooks object
    else:
        data["hooks"] = hooks
    ok = _write_settings(data, config_dir) if changed else True
    # Take the relay copy and heartbeat with it, so nothing of ours is left
    # running or lying around once the hooks are gone.
    for path in (installed_relay_path(), heartbeat_path()):
        try:
            path.unlink()
        except OSError:
            pass
    return ok


# --------------------------------------------------------------------------- #
# The spool
# --------------------------------------------------------------------------- #
def read_events(max_files: int = MAX_EVENTS_PER_READ):
    """Consume queued hook events, oldest first, deleting as we go.

    Files are deleted whether or not they parsed: a file we can't read will
    never become readable, and leaving it would make us re-read it forever.
    """
    directory = spool_dir()
    try:
        paths = sorted(directory.glob("*.json"))[:max_files]
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                out.append(payload)
        except (OSError, ValueError):
            pass
        try:
            path.unlink()
        except OSError:
            pass
    return out


def prune(max_age_s: int = MAX_EVENT_AGE_S, now: Optional[float] = None) -> int:
    """Delete spool files left behind while Claudometer wasn't running."""
    import time

    directory = spool_dir()
    now = time.time() if now is None else now
    removed = 0
    try:
        paths = list(directory.glob("*"))
    except OSError:
        return 0
    for path in paths:
        try:
            if now - path.stat().st_mtime > max_age_s:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def clear_spool() -> None:
    """Drop everything queued — used when hooks are switched off, so stale
    events can't surface later as if they were live."""
    try:
        for path in spool_dir().glob("*"):
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def summarize(event: dict) -> dict:
    """Normalise a raw hook payload into the fields Claudometer uses."""
    if not isinstance(event, dict):
        return {}
    return {
        "event": str(event.get("hook_event_name") or ""),
        "session_id": str(event.get("session_id") or ""),
        "cwd": str(event.get("cwd") or ""),
        # Notification carries the text the session is actually waiting on;
        # Stop carries what it said as it finished.
        "message": str(event.get("message") or ""),
        "title": str(event.get("title") or ""),
        "last_message": str(event.get("last_assistant_message") or ""),
        "reason": str(event.get("reason") or ""),
    }
