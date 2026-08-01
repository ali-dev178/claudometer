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
    # What the session is actually blocked on, read from its transcript: the
    # tool call still awaiting a result. The registry only ever says "input
    # needed", which isn't enough to answer from.
    pending_tool: str = ""
    pending_question: str = ""
    pending_options: tuple = ()

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

#: Dots drawn on the taskbar strip before it falls back to "+N". The strip
#: shares a crowded taskbar, so this stays small.
DOT_CAP = 8

# Plural-safe wording for the one-line summary, in the order it reads best:
# what needs you first, then what is running, then what is finished.
_SUMMARY_ORDER = ((WAITING, "needs you"), (SHELL, "in shell"),
                  (BUSY, "working"), (IDLE, "done"))


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

#: Longest string handed to a renderer. Elision handles the visual width; this
#: only stops a pathological value from costing measurable time to measure.
_TEXT_CAP = 200


def oneline(text, limit: int = _TEXT_CAP) -> str:
    """Collapse a value to a single line of printable text.

    Everything rendered MUST be single-line: Pillow raises "can't measure
    length of multiline text" on a newline, and that exception escapes
    ``render_popover`` and takes down the whole widget. This is reachable —
    titles are model-generated, ``waitingFor`` comes from the CLI, and a POSIX
    directory name may legally contain a newline — so it is scrubbed once here,
    at the seam every adapter shares, rather than in each renderer.
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", _CTRL_RE.sub(" ", str(text))).strip()[:limit]


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


def format_recent(recent, at_ms: Optional[int] = None, limit: int = 5):
    """Rows for recently ended sessions, newest first.

    *recent* is what :attr:`SessionTracker.recent` returns: (ended_at_ms,
    Session) pairs.
    """
    at_ms = now_ms() if at_ms is None else at_ms
    rows = []
    for ended_at, session in list(recent)[:max(0, limit)]:
        ago = fmt_dwell(max(0, int((at_ms - ended_at) / 1000)))
        rows.append({
            "session_id": session.session_id,
            "label": oneline(session.label),
            "project": oneline(session.project),
            "cwd": session.cwd,
            "ago": ago,
            "detail": f"ended {ago}" if ago else "ended",
        })
    return rows


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
        status_text = oneline(session.status_text)
        rows.append({
            "session_id": session.session_id,
            "pid": session.pid,
            "label": oneline(session.label),
            "project": oneline(session.project),
            "cwd": session.cwd,
            "status": session.status,
            "status_text": status_text,
            "color": session.color,
            "emoji": status_emoji(session.status),
            "dwell": dwell,
            # "needs you · 4m" — the right-hand side of a row, prebuilt so the
            # renderer never has to decide how to join them.
            "detail": f"{status_text} · {dwell}" if dwell else status_text,
            # What it is actually waiting on, for the answer window. The
            # transcript's outstanding tool call is the real question; the
            # registry's "input needed" is only the fallback.
            "question": (oneline(session.pending_question)
                         or oneline(session.waiting_for)
                         if session.status == WAITING else ""),
            "options": list(session.pending_options)
                       if session.status == WAITING else [],
            "pending_tool": oneline(session.pending_tool),
            "tool": oneline(session.tool),
            "model": oneline(session.model),
            "branch": oneline(session.git_branch),
            "last_prompt": oneline(session.last_prompt),
            "is_background": session.is_background,
        })

    count = len(ordered)
    summary = summarize(ordered)
    blocked = [s for s in ordered if s.status == WAITING]
    return {
        "sessions_rows": rows,
        "sessions_count": count,
        "sessions_blocked": len(blocked),
        # One colour per session, already in display order, for the strip's
        # at-a-glance dot row. Blocked sessions sort first, so they lead.
        "sessions_dots": [s.color for s in ordered[:DOT_CAP]],
        "sessions_dots_overflow": max(0, count - DOT_CAP),
        # The session to jump to when you just want "the one that needs me".
        "sessions_blocked_pid": blocked[0].pid if blocked else 0,
        "sessions_blocked_label": oneline(blocked[0].label) if blocked else "",
        "sessions_overflow": max(0, count - len(shown)),
        # EVERY live pid, not just the ones that fitted. Liveness must not
        # depend on how many rows the user chose to display: a session pushed
        # past that limit is still running, and anything that reads "has this
        # ended?" off the visible rows would answer yes and refuse to talk
        # to it.
        "sessions_pids": [s.pid for s in ordered],
        "sessions_summary": summary,
        "sessions_color": overall_color(ordered),
        "sessions_empty": "No Claude sessions running",
        "sessions_tooltip": (f"Claude · {summary}" if count
                             else "Claude · no sessions running"),
    }


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
#: Transition kinds worth interrupting someone for, in settings order.
#:   waiting  it just became blocked on you
#:   idle     it just finished and is waiting for your next prompt
#:   stuck    it has been blocked on you for a while
#:   gone     the session ended
#: Deliberately absent: "appeared", "->busy" and "->shell". Those fire
#: constantly during normal work and would train you to ignore the toasts.
ALERT_KINDS = ("waiting", "idle", "stuck", "gone")

#: Sensible defaults: tell me when something needs me or has finished. Ending
#: is usually something you did yourself, so it's off unless asked for.
DEFAULT_ALERT_KINDS = ("waiting", "idle", "stuck")

_ALERT_COLORS = {"waiting": "red", "stuck": "red", "idle": "green",
                 "gone": "grey"}


def _alert(kind: str, session: Session, title: str, subtitle: str) -> dict:
    return {
        "kind": kind,
        "title": title,
        "subtitle": oneline(subtitle),
        "color": _ALERT_COLORS.get(kind, "grey"),
        "session_id": session.session_id,
        "pid": session.pid,
    }


def alert_for(transition: Transition) -> Optional[dict]:
    """Toast payload for a transition, or None when it isn't worth a toast."""
    session = transition.session
    where = oneline(session.label)
    project = oneline(session.project)

    if transition.kind == "status":
        if transition.after == WAITING:
            reason = oneline(session.waiting_for) or project
            return _alert("waiting", session, "Needs you",
                          f"{where} · {reason}" if reason else where)
        if transition.after == IDLE:
            return _alert("idle", session, "Finished",
                          f"{where} · {project}" if project else where)
        return None
    if transition.kind == "gone":
        return _alert("gone", session, "Session ended",
                      f"{where} · {project}" if project else where)
    return None


def alert_for_stuck(session: Session, at_ms: Optional[int] = None) -> dict:
    dwell = fmt_dwell(dwell_seconds(session, at_ms))
    reason = oneline(session.waiting_for) or oneline(session.project)
    where = oneline(session.label)
    return _alert("stuck", session, f"Still waiting · {dwell}" if dwell
                  else "Still waiting",
                  f"{where} · {reason}" if reason else where)


def alerts_for(transitions, kinds=DEFAULT_ALERT_KINDS):
    """Alert payloads for a batch of transitions, filtered to *kinds*."""
    allowed = set(kinds or ())
    out = []
    for transition in transitions:
        payload = alert_for(transition)
        if payload is not None and payload["kind"] in allowed:
            out.append(payload)
    return out


#: Most-urgent-first. ALERT_KINDS is declaration/settings order, which is not
#: the same thing.
_URGENCY = (WAITING, "stuck", IDLE, "gone")

_ALERT_NOUNS = {WAITING: "need you", "stuck": "are still waiting",
                IDLE: "finished", "gone": "ended"}


def coalesce_alerts(alerts) -> Optional[dict]:
    """Collapse a batch of alerts into the single toast that will be shown.

    Only one toast exists at a time, and showing a second destroys the first —
    so firing one per session means a burst of three finishing together paints
    only the last, silently swallowing the other two. One summary toast is both
    honest and less noisy.
    """
    if not alerts:
        return None
    if len(alerts) == 1:
        return alerts[0]

    present = [k for k in _URGENCY if any(a["kind"] == k for a in alerts)]
    counts = {k: sum(1 for a in alerts if a["kind"] == k) for k in present}
    lead = present[0]

    if len(present) == 1:
        title = f"{counts[lead]} sessions {_ALERT_NOUNS[lead]}"
        # Each subtitle is "<label> · <detail>"; the labels alone identify them.
        subtitle = " · ".join(a["subtitle"].split(" · ")[0] for a in alerts)
    else:
        title = f"{len(alerts)} session updates"
        subtitle = " · ".join(f"{counts[k]} {_ALERT_NOUNS[k]}" for k in present)

    return {
        "kind": lead,
        "title": title,
        "subtitle": oneline(subtitle),
        "color": _ALERT_COLORS.get(lead, "grey"),
        "session_id": "",     # a rollup belongs to no single session
        "pid": 0,
    }


class StuckWatcher:
    """Nudges once when a session has been blocked on you for too long.

    Re-arms only when that session stops waiting, so a session blocked for an
    hour produces one nudge rather than one per poll.
    """

    def __init__(self, minutes: float = 10):
        self.minutes = minutes
        self._fired = set()

    def check(self, sessions, at_ms: Optional[int] = None):
        """Sessions that have *just* crossed the threshold."""
        if not self.minutes or self.minutes <= 0:
            self._fired.clear()
            return []
        waiting = {s.session_id: s for s in sessions if s.status == WAITING}
        self._fired &= set(waiting)      # anything that moved on can nudge again
        out = []
        for session_id, session in waiting.items():
            if session_id in self._fired:
                continue
            secs = dwell_seconds(session, at_ms)
            if secs is not None and secs >= self.minutes * 60:
                self._fired.add(session_id)
                out.append(session)
        return out

    def reset(self) -> None:
        self._fired.clear()


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


def parse_proc_stat_start(stat_text: str, btime: int,
                          hz: int = 100) -> Optional[int]:
    """Linux: process start time in epoch ms from /proc/<pid>/stat.

    Field 22 is the start time in clock ticks since boot. The comm field (2) is
    parenthesised and may itself contain spaces and parentheses, so everything
    up to the LAST ')' is skipped rather than split on whitespace.
    """
    try:
        tail = stat_text[stat_text.rindex(")") + 1:].split()
        # After comm, field 3 is state; starttime is field 22 overall, i.e.
        # index 19 of what follows comm.
        ticks = int(tail[19])
    except (ValueError, IndexError):
        return None
    if hz <= 0:
        return None
    return int((btime + ticks / hz) * 1000)


def parse_ps_lstart(text: str) -> Optional[int]:
    """macOS: epoch ms from `ps -o lstart=` output ("Fri Aug  1 10:23:45 2026").

    The stamp is read as UTC, because the caller runs ``ps`` with ``TZ=UTC``
    for it. Local time would be ambiguous for one hour every autumn, and
    resolving it the wrong way looks identical to a recycled pid — which would
    hide a perfectly live session until the widget restarted.
    """
    from datetime import datetime, timezone

    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    try:
        stamp = datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return int(stamp.replace(tzinfo=timezone.utc).timestamp() * 1000)


_proc_start_cache = {}


def _proc_start_posix(pid: int) -> Optional[int]:
    """Process start time on POSIX, or None when it can't be determined.

    A process's start time never changes, so it is cached for the life of the
    widget — on macOS this is a subprocess, which must not run per poll.
    """
    if pid in _proc_start_cache:
        return _proc_start_cache[pid]
    value = None
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/stat", encoding="utf-8") as fh:
                btime = next((int(line.split()[1]) for line in fh
                              if line.startswith("btime ")), None)
            if btime is not None:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    value = parse_proc_stat_start(
                        fh.read(), btime, os.sysconf("SC_CLK_TCK"))
        elif sys.platform == "darwin":
            # LC_ALL=C so the day and month names are the ones strptime knows,
            # and TZ=UTC so the stamp is unambiguous: local time repeats an
            # hour every autumn, and guessing the wrong side of it looks
            # exactly like a recycled pid — which hides a live session.
            env = dict(os.environ, LC_ALL="C", LANG="C", TZ="UTC")
            out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                 capture_output=True, text=True, timeout=5,
                                 env=env)
            if out.returncode == 0:
                value = parse_ps_lstart(out.stdout)
    except Exception:
        value = None
    if value is not None:
        _proc_start_cache[pid] = value
    return value


def reset_proc_start_cache() -> None:
    _proc_start_cache.clear()


def proc_start_ms(pid) -> Optional[int]:
    """Process creation time in epoch ms, or None if it can't be read.

    Used purely to reject a stale registry file whose PID has since been
    recycled; callers must treat None as "can't tell, keep it". Every platform
    branch fails soft, so an unsupported system simply keeps its sessions.
    """
    if sys.platform != "win32":
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid <= 0 or pid > MAX_PID:
            return None
        return _proc_start_posix(pid)
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
    if abs(actual - int(started_at)) <= tolerance_s * 1000:
        return True
    # A mismatch is either a genuinely recycled pid or a stale cache entry
    # from the PREVIOUS holder of this pid — and on POSIX the start time is
    # cached for the life of the widget, so the second case would hide a real
    # session forever. Drop the entry and ask again; a mismatch is rare, so
    # this costs nothing in the normal case.
    if _proc_start_cache.pop(pid, None) is None:
        return False
    fresh = proc_start_ms(pid)
    return fresh is None or abs(fresh - int(started_at)) <= tolerance_s * 1000


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
            # utf-8-sig, not utf-8: a leading BOM would otherwise reach
            # json.loads and fail the parse, silently dropping a live session.
            # Node writes these files without one, but anything that rewrites
            # them on Windows (PowerShell's Set-Content defaults to a BOM) can
            # add one. Decoding this way is a no-op when there is no BOM.
            text = path.read_text(encoding="utf-8-sig")
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
    """Ask the CLI for its session list.

    Returns the list — possibly empty, which is a real answer — or None when
    the CLI could not be run or understood at all. The caller needs to tell
    those apart: "there are no sessions" can be cached like any other answer,
    while "I couldn't ask" must not be mistaken for it.

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
    return None         # no candidate ran and parsed — we simply don't know


def _cli_fallback(timeout: int, at: Optional[float] = None):
    """Rate-limited wrapper so a watch loop can't spawn a process per tick."""
    at = time.monotonic() if at is None else at
    # Keyed on WHEN we last asked, not on whether the answer was non-empty.
    # "No sessions" is a real answer and has to be rate-limited like any
    # other — testing the list for truthiness meant the quiet case, which is
    # most of the time, spawned `claude agents --json` on every single tick.
    if _cli_cache["at"] and (at - _cli_cache["at"]) < CLI_MIN_INTERVAL_S:
        return list(_cli_cache["sessions"])
    sessions = sessions_from_cli(timeout=timeout)
    _cli_cache["at"] = at
    if sessions is None:
        # The CLI didn't answer. Keep whatever it last said rather than
        # reporting nothing — a transient failure must not empty the list —
        # but still back off, or a CLI that never works is a process per tick.
        return list(_cli_cache["sessions"])
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

    #: How many ended sessions to remember. Enough to answer "what was I just
    #: doing?" without becoming a second, unbounded history feature.
    RECENT_LIMIT = 10

    def __init__(self, confirm_ticks: int = MISSING_CONFIRM_TICKS):
        self.confirm_ticks = max(1, int(confirm_ticks))
        self._known = {}       # key -> last seen Session
        self._missing = {}     # key -> consecutive polls absent
        self._sorted = []
        self._recent = []      # (ended_at_ms, Session), newest first

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
                ended = self._known.pop(key)
                self._missing.pop(key, None)
                events.append(Transition("gone", ended))
                self._recent.insert(0, (at_ms if at_ms is not None else now_ms(),
                                        ended))
                del self._recent[self.RECENT_LIMIT:]
            else:
                self._missing[key] = misses

        self._sorted = sort_sessions(self._known.values(), at_ms)
        return events

    @property
    def recent(self):
        """Recently ended sessions, newest first, as (ended_at_ms, Session)."""
        return list(self._recent)

    def reset(self) -> None:
        """Forget everything — used when the feature is toggled back on, so a
        restart doesn't announce every running session as newly appeared."""
        self._known.clear()
        self._missing.clear()
        self._recent.clear()
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
        line = line.strip().lstrip("﻿")   # a BOM is not whitespace
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


#: Fields worth quoting back when the pending tool isn't a question — the one
#: that says what the tool is about to do, in the order we'd prefer them.
_TOOL_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url",
                      "prompt", "description", "query")


def _tool_summary(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _TOOL_SUMMARY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return oneline(value, 160)
    return ""


#: A numbered choice on a session's screen: "> 1. Label" or "  2) Label".
_SCREEN_OPTION_RE = re.compile(r"^\s*([>❯»❯]?)\s*(\d{1,2})[.)]\s+(\S.*)$")

#: Claude Code prints this under a menu. Its presence is what tells us the
#: numbers above really are a choice and not, say, a numbered list in prose.
_MENU_HINTS = ("to select", "to navigate", "to cancel")

#: Box drawing, spinners and status glyphs that are never the question.
_SCREEN_NOISE = "─━│┃┄┅┈┉╌╍═�texttt▪▫◦·✻✽⎿⏵❯>"


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    letters = [c for c in stripped if c.isalnum()]
    if not letters:
        return True
    # Mostly rule characters — a separator, not a sentence.
    return len(letters) / len(stripped) < 0.35


#: The TUI's input box, with or without something typed in it.
_PROMPT_RE = re.compile(r"^\s*[>❯⏵]\s*")

#: Claude Code's own bookkeeping: how long it thought, what a tool returned,
#: which topic a question belongs to. None of it is the session talking.
_ASIDE_RE = re.compile(r"^\s*(?:[✻✽✢✶*]\s|[⎿└]|\[.?\]\s|\d+\s*(?:tokens|lines))")

#: The bullet Claude Code puts in front of everything it says. It is BLACK
#: CIRCLE (U+25CF) — read off a live session's screen, not guessed. The
#: lookalikes are kept because ⏺ appears in transcripts and documentation, and
#: a bullet that is nearly right leaves the reply blank with nothing to show
#: for it.
_SAID_RE = re.compile(r"^\s*[●⏺•⧉]\s*(\S.*)$")
_SAID_CHARS = " ●⏺•⧉\t"

#: How far up from the bottom the input box can be. It is the last thing on
#: screen bar the status bar, which is one or two lines.
PROMPT_TAIL = 8


def latest_reply(lines, upto=None, max_chars: int = 600):
    """The last thing the session said, as one piece of prose.

    Terminal output is wrapped to the terminal's width and studded with glyphs
    a UI font doesn't have, so it is rejoined rather than mirrored — what
    matters is the sentence, not the column it happened to break at.

    Two things below it have to go first. The input box lives at the BOTTOM of
    the TUI, so anything keyed off "the last thing typed" runs off the end of
    the reply and into the status bar underneath it — which is how a window
    meant to show an answer ends up showing "ctx 5% · manual mode on".
    """
    rows = list(lines or [])
    if upto is not None and 0 < upto <= len(rows):
        rows = rows[:upto]
    # Everything from the input box down is the TUI's own furniture. Only the
    # bottom of the screen is searched for it: a reply that quotes a shell
    # prompt would otherwise be mistaken for one and thrown away.
    for index in range(len(rows) - 1, max(-1, len(rows) - PROMPT_TAIL), -1):
        if _PROMPT_RE.match(rows[index]):
            rows = rows[:index]
            break
    # Claude Code marks everything it says with a bullet, so the last one is
    # where the newest thing it said begins.
    start = None
    for index, row in enumerate(rows):
        if _SAID_RE.match(row):
            start = index
    if start is None:
        return ""
    said = []
    for row in rows[start:]:
        if (_is_noise(row) or _ASIDE_RE.match(row) or _PROMPT_RE.match(row)
                or _SCREEN_OPTION_RE.match(row)):
            continue
        said.append(row.strip().lstrip(_SAID_CHARS).strip())
    return oneline(" ".join(part for part in said if part), max_chars)


def parse_console_prompt(lines):
    """Pull the question and its choices off a session's visible screen.

    Returns ``{"question", "options", "selected"}`` or None. The transcript is
    no help here — a tool call only lands in it once it has been answered — so
    the screen is the one place a live question exists.
    """
    if not lines:
        return None
    rows = list(lines)
    hint_at = None
    for index in range(len(rows) - 1, -1, -1):
        low = rows[index].lower()
        if any(h in low for h in _MENU_HINTS):
            hint_at = index
            break
    if hint_at is None:
        return None

    options, selected, first_at = [], 0, None
    for index in range(hint_at - 1, max(-1, hint_at - 40), -1):
        match = _SCREEN_OPTION_RE.match(rows[index])
        if not match:
            continue
        marker, number, label = match.groups()
        number = int(number)
        if number < 1 or number > 20:
            continue
        options.append((number, oneline(label, 80), bool(marker.strip())))
        first_at = index
        if number == 1:
            break
    if not options:
        return None
    options.reverse()
    # Only trust a clean 1..N run; anything else is prose that happens to be
    # numbered.
    if [n for n, _l, _m in options] != list(range(1, len(options) + 1)):
        return None
    for position, (_n, _label, marked) in enumerate(options, start=1):
        if marked:
            selected = position

    question, start = "", first_at
    for index in range(first_at - 1, max(-1, first_at - 8), -1):
        candidate = rows[index]
        if _is_noise(candidate) or _SCREEN_OPTION_RE.match(candidate):
            continue
        question = oneline(candidate, 400)
        start = index
        break
    # ``start`` is where the prompt begins, so a caller showing the screen can
    # stop there instead of repeating the menu it is already drawing as buttons.
    return {"question": question,
            "options": tuple(label for _n, label, _m in options),
            "selected": selected,
            "start": start}


def pending_prompt(path, max_bytes: int = TRANSCRIPT_TAIL_BYTES):
    """What a session is currently waiting on, or None.

    A tool call with no matching ``tool_result`` is the one still outstanding,
    so that is the question being asked. ``AskUserQuestion`` carries its
    options as structured data, which is what lets them be offered as buttons
    rather than a sentence telling you to go and look.
    """
    text = _tail_text(path, max_bytes)
    if not text:
        return None
    uses, answered = {}, set()
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use" and block.get("id"):
                uses[block["id"]] = (_str(block.get("name")),
                                     block.get("input") or {})
            elif kind == "tool_result" and block.get("tool_use_id"):
                answered.add(block["tool_use_id"])

    outstanding = [pair for tid, pair in uses.items() if tid not in answered]
    if not outstanding:
        return None
    name, payload = outstanding[-1]     # dicts keep insertion order

    if name == "AskUserQuestion":
        questions = payload.get("questions") if isinstance(payload, dict) else None
        first = questions[0] if isinstance(questions, list) and questions else None
        if isinstance(first, dict):
            options = []
            for option in first.get("options") or []:
                if isinstance(option, dict):
                    label = oneline(option.get("label"), 60)
                    if label:
                        options.append(label)
            return {"tool": name,
                    "question": oneline(first.get("question"), 400),
                    "options": tuple(options)}

    summary = _tool_summary(payload)
    return {"tool": name,
            "question": f"{name}: {summary}" if summary else
                        (f"Waiting on {name}" if name else ""),
            "options": ()}


def last_prompts(config_dir=None, max_bytes: int = HISTORY_TAIL_BYTES) -> dict:
    """Map session id -> the most recent prompt text, from history.jsonl."""
    path = _config_dir(config_dir) / "history.jsonl"
    text = _tail_text(path, max_bytes)
    if not text:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")   # a BOM is not whitespace
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
            # Only a blocked session has anything outstanding, and this is a
            # second pass over the tail — don't pay for it otherwise.
            if session.status == WAITING:
                pending = pending_prompt(path, max_bytes)
                if pending:
                    fields["pending_tool"] = pending["tool"]
                    fields["pending_question"] = pending["question"]
                    fields["pending_options"] = pending["options"]
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
