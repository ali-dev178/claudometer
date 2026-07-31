"""Live Claude Code session discovery: no UI dependencies.

Claude Code keeps a registry of its running processes at
``~/.claude/sessions/<pid>.json`` — one small file per CLI process, rewritten
whenever that session changes state. This module turns that directory into a
list of :class:`Session` records the tray / menu-bar / popover adapters can
render verbatim, in the same spirit as ``usage_core``.

Status vocabulary (mirrors the CLI's own enum):

    busy      the model is generating, or a tool is running
    shell     a shell command is running specifically
    idle      it replied and is waiting for you to type
    waiting   it is blocked ON you right now (permission prompt, question)

``waiting`` carries a ``waitingFor`` reason such as "input needed" or
"sandbox request".

Reading strategy — the registry is the primary source because it is instant
(no subprocess) and carries the full field set. ``claude agents --json`` is the
documented, TTY-free scripting interface, so it is kept as a fallback for when
the directory is missing or its schema stops parsing: a CLI update degrades
this feature instead of breaking it. The fallback is rate-limited because it
costs a process spawn.

Everything here is read-only; the CLI prunes its own stale files.

Threading: safe to call from a background poll thread while the UI thread
reads the results. The two module-level caches (``_cli_cache``,
``_transcript_cache``) are plain dicts used idempotently — the worst a race
can cost is a redundant recompute, never a corrupt record. Verified under 10
concurrent threads and ~15k iterations. :class:`SessionTracker` is the one
exception: it owns per-instance mutable state, so give each consumer its own.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------- #
# Status vocabulary
# --------------------------------------------------------------------------- #
BUSY = "busy"
SHELL = "shell"
IDLE = "idle"
WAITING = "waiting"
UNKNOWN = "unknown"

#: The statuses Claude Code itself writes. Anything else becomes ``UNKNOWN``.
KNOWN_STATUSES = (BUSY, SHELL, IDLE, WAITING)

#: Session kinds Claude Code writes. ``claude agents --json`` renames bg ->
#: "background", which we normalize back.
KNOWN_KINDS = ("interactive", "bg", "daemon", "daemon-worker")

# Theme color keys (see render.THEMES) — there is no blue in the palette, so a
# running shell command borrows the Claude accent to stay distinct from "busy".
_COLORS = {
    WAITING: "red",
    BUSY: "amber",
    SHELL: "accent",
    IDLE: "green",
    UNKNOWN: "grey",
}

_LABELS = {
    WAITING: "needs you",
    BUSY: "working",
    SHELL: "running a command",
    IDLE: "done",
    UNKNOWN: "unknown",
}

# Native menus render plain text with no colour control, so the tray and macOS
# menu bar carry the status as an emoji dot instead — matching the colours the
# popover draws.
_EMOJI = {
    WAITING: "\U0001F534",   # red
    BUSY: "\U0001F7E1",      # yellow
    SHELL: "\U0001F7E0",     # orange
    IDLE: "\U0001F7E2",      # green
    UNKNOWN: "⚪",       # white
}

# Sort rank: blocked first, then anything actively running, then finished.
_RANK = {WAITING: 0, SHELL: 1, BUSY: 1, IDLE: 2, UNKNOWN: 3}

#: A live session's process should have been created within this many seconds
#: of the ``startedAt`` its file claims. A wider gap means the PID was recycled
#: and the file is stale, so the session is dropped. Only enforced where the
#: process creation time is actually readable (Windows today).
#:
#: Deliberately generous: ``startedAt`` is stamped after the CLI finishes
#: booting, and real sessions on this machine have shown gaps over 20s (cold
#: start, MCP servers, a large CLAUDE.md). PID reuse takes hours, so a wide
#: window still catches it while never hiding a slow-starting session.
PID_REUSE_TOLERANCE_S = 300

#: Minimum seconds between ``claude agents --json`` invocations. The fallback
#: costs a process spawn, so it must never run at watch-loop frequency.
CLI_MIN_INTERVAL_S = 10.0

#: Claude Code rewrites ``<pid>.json`` with a plain truncate-and-write — there
#: is no temp-file-plus-rename — so a reader can catch the file empty or half
#: written. The window is microseconds, so a couple of quick retries close it.
#: Without this a torn read silently drops a live session for one poll.
READ_RETRIES = 3
READ_RETRY_DELAY_S = 0.002

#: How many consecutive polls a session must stay missing before
#: :class:`SessionTracker` believes it. Guards the alert layer against the same
#: torn read: one dropped poll must never fire a "session ended" toast.
MISSING_CONFIRM_TICKS = 2

#: How much of a transcript's tail to read when enriching. Transcripts reach
#: tens of MB, so they are always read backwards from the end.
TRANSCRIPT_TAIL_BYTES = 96 * 1024

#: How much of history.jsonl to read for the per-session "last prompt".
HISTORY_TAIL_BYTES = 256 * 1024


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    """One live Claude Code process."""

    pid: int = 0
    session_id: str = ""
    cwd: str = ""
    name: str = ""                 # CLI-derived, e.g. "claude-widget-b0"
    kind: str = ""                 # interactive | bg | daemon | daemon-worker
    entrypoint: str = ""
    version: str = ""
    status: str = UNKNOWN
    waiting_for: str = ""          # only meaningful while status == WAITING
    started_at: int = 0            # epoch ms
    updated_at: int = 0            # epoch ms
    status_updated_at: int = 0     # epoch ms — drives the dwell timer
    agent: str = ""
    state: str = ""                # background agents: success | failure | stopped
    detail: str = ""
    tempo: str = ""                # active | idle | blocked
    needs: str = ""
    job_id: str = ""
    log_path: str = ""
    tmux: str = ""
    source: str = "file"           # file | cli

    # --- enrichment; empty until enrich() fills it in --------------------- #
    title: str = ""                # AI-generated session title
    tool: str = ""                 # most recent tool used
    model: str = ""
    last_prompt: str = ""
    git_branch: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    subagents: int = 0

    # --- derived ---------------------------------------------------------- #
    @property
    def project(self) -> str:
        """Basename of the working directory, e.g. "claude-widget"."""
        return os.path.basename(str(self.cwd).rstrip("\\/")) or str(self.cwd)

    @property
    def label(self) -> str:
        """Best human-readable name: AI title, else CLI name, else folder."""
        return self.title or self.name or self.project or "session"

    @property
    def is_background(self) -> bool:
        return self.kind in ("bg", "background")

    @property
    def color(self) -> str:
        return status_color(self.status)

    @property
    def status_text(self) -> str:
        """"needs you: input needed" / "working" / "done"."""
        base = status_label(self.status)
        if self.status == WAITING and self.waiting_for:
            return f"{base}: {self.waiting_for}"
        return base


@dataclass
class Transition:
    """A change between two snapshots, for alerting."""

    kind: str                      # appeared | gone | status
    session: Session
    before: str = ""               # previous status, for kind == "status"
    after: str = ""                # new status, for kind == "status"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def now_ms() -> int:
    return int(time.time() * 1000)


def status_color(status: str) -> str:
    return _COLORS.get(status, "grey")


def status_label(status: str) -> str:
    return _LABELS.get(status, "unknown")


def status_emoji(status: str) -> str:
    return _EMOJI.get(status, _EMOJI[UNKNOWN])


def dwell_seconds(session: Session, at_ms: Optional[int] = None) -> Optional[int]:
    """Seconds the session has held its current status, or None if unknown.

    Falls back to ``updated_at`` when the CLI has not written a
    ``statusUpdatedAt`` yet (the very first write of a fresh session).
    """
    stamp = session.status_updated_at or session.updated_at
    if not stamp:
        return None
    secs = int(((at_ms if at_ms is not None else now_ms()) - stamp) / 1000)
    return max(0, secs)


def fmt_dwell(secs: Optional[int]) -> str:
    """Compact age string: "just now", "45s", "12m", "2h 05m", "3d 4h"."""
    if secs is None:
        return ""
    if secs < 10:
        return "just now"
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours, mins = mins // 60, mins % 60
    if hours < 24:
        return f"{hours}h {mins:02d}m"
    days, hours = hours // 24, hours % 24
    return f"{days}d {hours}h"


def sort_key(session: Session, at_ms: Optional[int] = None):
    """Blocked first (longest-blocked leading), then running, then finished.

    Within the running/finished groups the most recently active session leads,
    which is the opposite ordering from ``waiting`` — there, the session that
    has been stuck longest is the one that needs attention.
    """
    rank = _RANK.get(session.status, 3)
    dwell = dwell_seconds(session, at_ms) or 0
    secondary = -dwell if session.status == WAITING else -(session.status_updated_at
                                                           or session.updated_at)
    return (rank, secondary, session.label.lower(), session.pid)


def sort_sessions(sessions, at_ms: Optional[int] = None):
    return sorted(sessions, key=lambda s: sort_key(s, at_ms))


def stuck_sessions(sessions, minutes: float, at_ms: Optional[int] = None):
    """Sessions blocked on the user for longer than *minutes*."""
    cutoff = minutes * 60
    out = []
    for s in sessions:
        if s.status != WAITING:
            continue
        secs = dwell_seconds(s, at_ms)
        if secs is not None and secs >= cutoff:
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Formatting for display
# --------------------------------------------------------------------------- #
#: Rows shown before the list collapses into a "+N more" line. The popover is a
#: fixed-width card, so an unbounded list would run off the screen.
DEFAULT_MAX_ROWS = 6

# Plural-safe wording for the one-line summary, in the order it reads best:
# what needs you first, then what is running, then what is finished.
_SUMMARY_ORDER = ((WAITING, "needs you"), (SHELL, "in shell"),
                  (BUSY, "working"), (IDLE, "done"))


def summarize(sessions) -> str:
    """One-line rollup, e.g. "1 needs you · 2 working"."""
    counts = {}
    for session in sessions:
        counts[session.status] = counts.get(session.status, 0) + 1
    parts = [f"{counts[status]} {word}"
             for status, word in _SUMMARY_ORDER if counts.get(status)]
    unknown = counts.get(UNKNOWN)
    if unknown:
        parts.append(f"{unknown} unknown")
    return "   ·   ".join(parts)


def overall_color(sessions) -> str:
    """The colour of the most urgent session, for a badge or tray dot."""
    statuses = {s.status for s in sessions}
    if WAITING in statuses:
        return "red"
    if BUSY in statuses or SHELL in statuses:
        return "amber"
    if IDLE in statuses:
        return "green"
    return "grey"


def format_sessions(sessions, at_ms: Optional[int] = None,
                    max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Turn Sessions into the flat dict every UI adapter renders verbatim.

    Mirrors ``usage_core.format_breakdown`` so the tray, menu bar and popover
    all consume one shape and none of them re-derive wording or colour.

    Keys are deliberately ``sessions_*`` (plural). ``usage_core`` already owns
    ``session_pct`` / ``session_color`` / ``session_resets_at`` for the 5-hour
    usage meter, and the two dicts get merged into one before rendering — a
    singular name here would silently repaint that meter.
    """
    ordered = sort_sessions(sessions, at_ms)
    limit = max(1, int(max_rows)) if max_rows else len(ordered)
    shown = ordered[:limit]

    rows = []
    for session in shown:
        dwell = fmt_dwell(dwell_seconds(session, at_ms))
        rows.append({
            "session_id": session.session_id,
            "pid": session.pid,
            "label": session.label,
            "project": session.project,
            "cwd": session.cwd,
            "status": session.status,
            "status_text": session.status_text,
            "color": session.color,
            "emoji": status_emoji(session.status),
            "dwell": dwell,
            # "needs you · 4m" — the right-hand side of a row, prebuilt so the
            # renderer never has to decide how to join them.
            "detail": f"{session.status_text} · {dwell}" if dwell
                      else session.status_text,
            "tool": session.tool,
            "model": session.model,
            "branch": session.git_branch,
            "last_prompt": session.last_prompt,
            "is_background": session.is_background,
        })

    count = len(ordered)
    summary = summarize(ordered)
    return {
        "sessions_rows": rows,
        "sessions_count": count,
        "sessions_blocked": sum(1 for s in ordered if s.status == WAITING),
        "sessions_overflow": max(0, count - len(shown)),
        "sessions_summary": summary,
        "sessions_color": overall_color(ordered),
        "sessions_empty": "No Claude sessions running",
        "sessions_tooltip": (f"Claude · {summary}" if count
                             else "Claude · no sessions running"),
    }


# --------------------------------------------------------------------------- #
# Process liveness
# --------------------------------------------------------------------------- #
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

#: PIDs are 32-bit on every platform we support. A larger value can only come
#: from a stray ``<digits>.json`` in the registry directory (a timestamp-named
#: file is 14 digits, which overflows), and handing it to ctypes raises
#: ArgumentError — which would take down the whole poll loop.
MAX_PID = 0xFFFFFFFF


def pid_alive(pid) -> bool:
    """True if *pid* names a running process.

    NOTE: never use ``os.kill(pid, 0)`` on Windows — CPython maps any signal
    other than CTRL_C/CTRL_BREAK onto ``TerminateProcess``, so the "harmless"
    liveness probe would kill the user's Claude session. The Windows branch
    opens the process for query-only access instead.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid > MAX_PID:
        return False

    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True      # can't tell — assume alive rather than hide it
                return code.value == _STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False             # a liveness probe must never break the loop

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                  # exists, owned by someone else
    except OSError:
        return False
    return True


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


# Seconds between 1601-01-01 (the Windows FILETIME epoch) and 1970-01-01.
_FILETIME_EPOCH_OFFSET_S = 11644473600


def proc_start_ms(pid) -> Optional[int]:
    """Process creation time in epoch ms, or None if it can't be read.

    Windows-only today. Used purely to reject a stale registry file whose PID
    has since been recycled; callers must treat None as "can't tell, keep it".
    """
    if sys.platform != "win32":
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or pid > MAX_PID:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created, exited = _FILETIME(), _FILETIME()
            kernel, user = _FILETIME(), _FILETIME()
            ok = kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            if not ticks:
                return None
            # FILETIME counts 100ns intervals since 1601-01-01 UTC.
            return int(ticks / 10_000 - _FILETIME_EPOCH_OFFSET_S * 1000)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def pid_matches_session(pid, started_at,
                        tolerance_s: int = PID_REUSE_TOLERANCE_S) -> bool:
    """False only when we can prove the PID was recycled.

    Compares the process creation time against the ``startedAt`` the registry
    file claims. Deliberately conservative: if either value is unavailable we
    return True, because hiding a real session is worse than briefly showing a
    stale one (which the CLI prunes on its own anyway).

    ``startedAt`` is used rather than the file's ``procStart`` field because
    ``procStart`` is written as .NET local-time ticks, whereas ``startedAt`` is
    unambiguous epoch-ms UTC.
    """
    if not started_at:
        return True
    actual = proc_start_ms(pid)
    if actual is None:
        return True
    return abs(actual - int(started_at)) <= tolerance_s * 1000


# --------------------------------------------------------------------------- #
# Reading the registry
# --------------------------------------------------------------------------- #
def _config_dir(config_dir=None) -> Path:
    return Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
                or (Path.home() / ".claude"))


def sessions_dir(config_dir=None) -> Path:
    return _config_dir(config_dir) / "sessions"


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_session(data: dict, pid: int = 0) -> Optional[Session]:
    """Turn one registry file's JSON into a Session, or None if unusable.

    A record without a session id is not something we can track or act on, so
    it is rejected — that rejection is also the signal that the on-disk schema
    has changed and the CLI fallback should take over.
    """
    if not isinstance(data, dict):
        return None
    session_id = _str(data.get("sessionId"))
    if not session_id:
        return None

    status = _str(data.get("status"))
    if status not in KNOWN_STATUSES:
        status = UNKNOWN

    kind = _str(data.get("kind"))
    if kind == "background":          # `claude agents --json` spelling
        kind = "bg"

    return Session(
        pid=_int(data.get("pid")) or _int(pid),
        session_id=session_id,
        cwd=_str(data.get("cwd")),
        name=_str(data.get("name")),
        kind=kind,
        entrypoint=_str(data.get("entrypoint")),
        version=_str(data.get("version")),
        status=status,
        waiting_for=_str(data.get("waitingFor")),
        started_at=_int(data.get("startedAt")),
        updated_at=_int(data.get("updatedAt")),
        status_updated_at=_int(data.get("statusUpdatedAt")),
        agent=_str(data.get("agent")),
        state=_str(data.get("state")),
        detail=_str(data.get("detail")),
        tempo=_str(data.get("tempo")),
        needs=_str(data.get("needs")),
        job_id=_str(data.get("jobId")),
        log_path=_str(data.get("logPath")),
        tmux=_str(data.get("tmux")),
        source="file",
    )


def _read_json(path, retries: int = READ_RETRIES,
               delay: float = READ_RETRY_DELAY_S):
    """Read a JSON file, retrying briefly on a torn read.

    Returns None if the file is unreadable or still not valid JSON after the
    retries — the caller cannot tell a torn read from real corruption, and
    both mean "skip this file".
    """
    for attempt in range(max(1, retries)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None            # genuinely unreadable; retrying won't help
        except UnicodeDecodeError:
            # Torn mid multi-byte character — a session cwd with non-ASCII in
            # it makes this reachable. Treat exactly like a partial read and
            # retry, rather than losing the session for this poll.
            text = ""
        if text.strip():
            try:
                return json.loads(text)
            except ValueError:
                pass
        if attempt + 1 < retries:
            time.sleep(delay)
    return None


def _dedupe(sessions):
    """Collapse records that name the same session, keeping the freshest.

    Resuming a session can briefly leave two registry files pointing at one
    ``sessionId``. Showing the same work twice is worse than picking one.
    """
    best = {}
    for session in sessions:
        key = session.session_id or f"pid:{session.pid}"
        current = best.get(key)
        if current is None or session.updated_at > current.updated_at:
            best[key] = session
    return list(best.values())


def scan(config_dir=None, alive=None):
    """Read the registry directory.

    Returns ``(sessions, health)`` where health is one of:

        ok           the directory was read and parsed normally
        no-dir       there is no sessions directory at all
        unparsable   files are present but none produced a usable record

    The health value exists so the caller can decide when to reach for the
    CLI fallback without spawning a process on every quiet tick.
    """
    alive = pid_alive if alive is None else alive
    sdir = sessions_dir(config_dir)
    if not sdir.is_dir():
        return [], "no-dir"

    try:
        files = sorted(sdir.glob("*.json"))
    except OSError:
        return [], "no-dir"

    sessions, saw_files, parsed_any = [], False, False
    for path in files:
        name = path.name[:-5]
        if not name.isdigit():        # only <pid>.json belongs to the registry
            continue
        saw_files = True
        try:
            data = _read_json(path)
            if data is None:
                continue
            session = parse_session(data, pid=int(name))
            if session is None:
                continue
            parsed_any = True
            if not alive(session.pid):
                continue
            if not pid_matches_session(session.pid, session.started_at):
                continue
            sessions.append(session)
        except Exception:
            # One malformed file must never take down the whole scan: this runs
            # in a watch loop and has to survive whatever lands in this
            # directory, including files no version of the CLI ever wrote.
            continue

    if saw_files and not parsed_any:
        return [], "unparsable"
    return _dedupe(sessions), "ok"


# --------------------------------------------------------------------------- #
# CLI fallback: `claude agents --json`
# --------------------------------------------------------------------------- #
_CLI_COMMANDS = (["claude"], ["claude.cmd"])
_cli_cache = {"at": 0.0, "sessions": []}


def _no_window_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def sessions_from_cli(timeout: int = 10, include_done: bool = False):
    """Ask the CLI for its session list. Returns [] on any failure.

    This is the documented scripting interface, but it collapses ``shell`` into
    ``busy`` and omits the dwell timestamps, so it is strictly a fallback.
    """
    args = ["agents", "--json"] + (["--all"] if include_done else [])
    for base in _CLI_COMMANDS:
        try:
            out = subprocess.run(base + args, capture_output=True, text=True,
                                 timeout=timeout, **_no_window_kwargs())
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0 or not (out.stdout or "").strip():
            continue
        try:
            payload = json.loads(out.stdout)
        except ValueError:
            continue
        if not isinstance(payload, list):
            continue
        sessions = []
        for item in payload:
            session = parse_session(item)
            if session is not None:
                sessions.append(replace(session, source="cli"))
        return sessions
    return []


def _cli_fallback(timeout: int, at: Optional[float] = None):
    """Rate-limited wrapper so a watch loop can't spawn a process per tick."""
    at = time.monotonic() if at is None else at
    if _cli_cache["sessions"] and (at - _cli_cache["at"]) < CLI_MIN_INTERVAL_S:
        return list(_cli_cache["sessions"])
    sessions = sessions_from_cli(timeout=timeout)
    _cli_cache["at"] = at
    _cli_cache["sessions"] = list(sessions)
    return sessions


def reset_cli_cache() -> None:
    """Drop the fallback's rate-limit cache (tests, and manual refresh)."""
    _cli_cache["at"] = 0.0
    _cli_cache["sessions"] = []


def snapshot(config_dir=None, cli_fallback: bool = True, timeout: int = 10,
             at_ms: Optional[int] = None, alive=None):
    """The current live sessions, sorted for display.

    Reads the registry directory first and only shells out to the CLI when the
    registry is missing or has stopped parsing — an empty-but-healthy directory
    simply means nothing is running.
    """
    sessions, health = scan(config_dir, alive=alive)
    if cli_fallback and health in ("no-dir", "unparsable"):
        sessions = _cli_fallback(timeout)
    return sort_sessions(sessions, at_ms)


# --------------------------------------------------------------------------- #
# Transitions (for alerting)
# --------------------------------------------------------------------------- #
def _by_id(sessions) -> dict:
    return {s.session_id or str(s.pid): s for s in sessions}


def diff(previous, current):
    """Transitions from *previous* to *current*.

    A session whose status is UNKNOWN in either snapshot produces no status
    transition — an unreadable write should not fire a "done!" toast.
    """
    before, after = _by_id(previous), _by_id(current)
    out = []

    for key, session in after.items():
        old = before.get(key)
        if old is None:
            out.append(Transition("appeared", session))
        elif (old.status != session.status
              and UNKNOWN not in (old.status, session.status)):
            out.append(Transition("status", session,
                                  before=old.status, after=session.status))

    for key, session in before.items():
        if key not in after:
            out.append(Transition("gone", session))

    return out


class SessionTracker:
    """Turns a stream of snapshots into *confirmed* transitions.

    ``diff`` is pure and reports a disappearance the instant a session is
    absent. That is too eager to drive alerts from: a single poll can miss a
    live session for reasons that have nothing to do with it ending — the CLI
    rewrites ``<pid>.json`` with a non-atomic truncate-and-write, an antivirus
    scanner can hold the file briefly, and pruning can race a rewrite. Any of
    those would otherwise fire a "session ended" toast for a session that is
    still running.

    So a disappearance must persist for ``confirm_ticks`` consecutive polls
    before it counts. :attr:`sessions` likewise keeps a briefly-missing session
    in the list, so a row never blinks out and back.
    """

    def __init__(self, confirm_ticks: int = MISSING_CONFIRM_TICKS):
        self.confirm_ticks = max(1, int(confirm_ticks))
        self._known = {}       # key -> last seen Session
        self._missing = {}     # key -> consecutive polls absent
        self._sorted = []

    @staticmethod
    def _key(session: Session) -> str:
        return session.session_id or f"pid:{session.pid}"

    @property
    def sessions(self):
        """Current sessions, including any still inside the grace window."""
        return list(self._sorted)

    def update(self, sessions, at_ms: Optional[int] = None):
        """Feed one snapshot in; get the confirmed transitions back."""
        current = {self._key(s): s for s in sessions}
        events = []

        for key, session in current.items():
            self._missing.pop(key, None)      # it's back (or never left)
            old = self._known.get(key)
            if old is None:
                events.append(Transition("appeared", session))
            elif (old.status != session.status
                  and UNKNOWN not in (old.status, session.status)):
                events.append(Transition("status", session,
                                         before=old.status, after=session.status))
            self._known[key] = session

        for key in list(self._known):
            if key in current:
                continue
            misses = self._missing.get(key, 0) + 1
            if misses >= self.confirm_ticks:
                events.append(Transition("gone", self._known.pop(key)))
                self._missing.pop(key, None)
            else:
                self._missing[key] = misses

        self._sorted = sort_sessions(self._known.values(), at_ms)
        return events

    def reset(self) -> None:
        """Forget everything — used when the feature is toggled back on, so a
        restart doesn't announce every running session as newly appeared."""
        self._known.clear()
        self._missing.clear()
        self._sorted = []


# --------------------------------------------------------------------------- #
# Enrichment: transcripts and prompt history
# --------------------------------------------------------------------------- #
def project_slug(cwd) -> str:
    """The ``~/.claude/projects`` directory name for a working directory.

    ``C:\\Personal Space\\claude-widget`` -> ``C--Personal-Space-claude-widget``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


_transcript_cache = {}


def transcript_path(session_id: str, cwd=None, config_dir=None) -> Optional[Path]:
    """Locate a session's transcript JSONL.

    Tries the slug-derived path first, then falls back to scanning the project
    directories for ``<session_id>.jsonl``. The slug transform is inferred, not
    documented, so the scan is what makes this correct rather than lucky.
    """
    if not session_id:
        return None
    cached = _transcript_cache.get(session_id)
    if cached is not None and cached.is_file():
        return cached

    base = _config_dir(config_dir) / "projects"
    if cwd:
        direct = base / project_slug(cwd) / f"{session_id}.jsonl"
        if direct.is_file():
            _transcript_cache[session_id] = direct
            return direct
    try:
        for entry in base.iterdir():
            candidate = entry / f"{session_id}.jsonl"
            if candidate.is_file():
                _transcript_cache[session_id] = candidate
                return candidate
    except OSError:
        pass
    return None


def reset_transcript_cache() -> None:
    _transcript_cache.clear()


def _tail_text(path, max_bytes: int) -> str:
    """Last *max_bytes* of a file, starting at a line boundary."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return ""
    if start > 0:                     # drop the partial line we landed inside
        _, sep, rest = blob.partition(b"\n")
        blob = rest if sep else b""
    return blob.decode("utf-8", "replace")


def read_transcript_tail(path, max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> dict:
    """Pull display details out of the tail of a transcript.

    Returns a dict with any of: title, tool, model, git_branch, input_tokens,
    output_tokens, subagents. Missing keys simply mean the tail didn't contain
    that information.

    ``subagents`` counts distinct subagents *within the tail window*, not for
    the whole session — reading a multi-megabyte transcript end to end on every
    refresh would cost far more than the number is worth.
    """
    text = _tail_text(path, max_bytes)
    if not text:
        return {}

    out = {}
    agent_ids = set()      # subagents that stamped an explicit agentId
    rootless = 0           # sidechain roots that didn't (older/other shapes)
    lines = text.splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue

        kind = entry.get("type")
        if kind == "ai-title" and "title" not in out:
            title = _str(entry.get("aiTitle"))
            if title:
                out["title"] = title
            continue

        if entry.get("isSidechain"):
            agent_id = _str(entry.get("agentId"))
            if agent_id:
                agent_ids.add(agent_id)
            elif not entry.get("parentUuid"):
                rootless += 1     # a chain root is one subagent

        if "git_branch" not in out:
            branch = _str(entry.get("gitBranch"))
            if branch:
                out["git_branch"] = branch

        if kind == "assistant":
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if "model" not in out:
                model = _str(message.get("model"))
                if model:
                    out["model"] = model
            usage = message.get("usage")
            if isinstance(usage, dict) and "output_tokens" not in out:
                out["input_tokens"] = _int(usage.get("input_tokens"))
                out["output_tokens"] = _int(usage.get("output_tokens"))
            if "tool" not in out:
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"):
                            name = _str(block.get("name"))
                            if name:
                                out["tool"] = name
                                break

    subagents = len(agent_ids) + rootless
    if subagents:
        out["subagents"] = subagents
    return out


def last_prompts(config_dir=None, max_bytes: int = HISTORY_TAIL_BYTES) -> dict:
    """Map session id -> the most recent prompt text, from history.jsonl."""
    path = _config_dir(config_dir) / "history.jsonl"
    text = _tail_text(path, max_bytes)
    if not text:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        session_id = _str(entry.get("sessionId"))
        display = _str(entry.get("display"))
        if session_id and display:
            out[session_id] = display     # later lines win
    return out


def enrich(session: Session, config_dir=None, prompts: Optional[dict] = None,
           max_bytes: int = TRANSCRIPT_TAIL_BYTES) -> Session:
    """Return a copy of *session* with transcript/history detail filled in.

    Never raises and never returns None: on any failure the original session is
    returned unchanged, because a missing subtitle must not cost you the row.
    """
    fields = {}
    try:
        path = transcript_path(session.session_id, session.cwd, config_dir)
        if path is not None:
            fields.update(read_transcript_tail(path, max_bytes))
    except Exception:
        pass

    try:
        if prompts is None:
            prompts = last_prompts(config_dir)
        prompt = prompts.get(session.session_id)
        if prompt:
            fields["last_prompt"] = prompt
    except Exception:
        pass

    if not fields:
        return session
    return replace(session, **fields)


def enrich_all(sessions, config_dir=None, max_bytes: int = TRANSCRIPT_TAIL_BYTES):
    """Enrich a list of sessions, reading history.jsonl only once."""
    try:
        prompts = last_prompts(config_dir)
    except Exception:
        prompts = {}
    return [enrich(s, config_dir, prompts, max_bytes) for s in sessions]
