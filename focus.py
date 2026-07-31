"""Work out which window owns a Claude Code session: no UI dependencies.

A `claude` process never owns a window of its own — it runs inside a shell,
inside a terminal emulator. So the owning window is found by walking UP the
process tree from the session's pid until a process with a visible top-level
window is reached (claude.exe -> powershell.exe -> WindowsTerminal.exe).

Used by the alert layer to stay quiet about the session you're already looking
at. Everything here fails soft: when the answer can't be determined we report
"not focused", because a missed suppression is a stray toast while a wrong
suppression silently swallows the notification you needed.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

#: Depth cap when walking ancestors — a terminal is 2-3 hops up, and this stops
#: a cycle in bogus parent data from spinning forever.
MAX_ANCESTOR_HOPS = 8

_IS_WIN = sys.platform == "win32"

# Windows constants
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_char * _MAX_PATH),
    ]


def parent_map() -> dict:
    """{pid: parent_pid} for every process, or {} if it can't be read.

    One snapshot answers the whole ancestor walk, so this is taken once per
    query rather than per hop.
    """
    if not _IS_WIN:
        return {}
    try:
        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE_VALUE:
            return {}
        try:
            entry = _PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
            out = {}
            if not kernel32.Process32First(snap, ctypes.byref(entry)):
                return {}
            while True:
                out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    break
            return out
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return {}


def ancestors(pid, parents: Optional[dict] = None, limit: int = MAX_ANCESTOR_HOPS):
    """[pid, parent, grandparent, ...] up to *limit* hops.

    Stops on a repeat, so a self-referential or cyclic parent chain (which the
    snapshot can contain for pid 0) can't loop forever.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return []
    if pid <= 0:
        return []
    parents = parent_map() if parents is None else parents
    chain, seen = [], set()
    current = pid
    for _ in range(max(1, limit)):
        if current in seen or current <= 0:
            break
        seen.add(current)
        chain.append(current)
        nxt = parents.get(current)
        if not nxt:
            break
        current = int(nxt)
    return chain


def foreground_pid() -> Optional[int]:
    """PID owning the focused window, or None if it can't be determined."""
    if not _IS_WIN:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def is_session_foreground(pid, parents: Optional[dict] = None) -> bool:
    """True when the focused window belongs to *pid* or one of its ancestors.

    That covers the real shape of a session: the focused window is the terminal
    emulator, several hops above the `claude` process itself.

    NOTE this is per-window, not per-tab. Several sessions commonly live in one
    terminal window, and they will all report focused together — so callers
    should treat it as "you are looking at this terminal", not "at this exact
    session". Use :func:`exclusive_foreground_pid` when that distinction
    matters, which for alert suppression it does.
    """
    fg = foreground_pid()
    if fg is None:
        return False
    chain = ancestors(pid, parents)
    return bool(chain) and fg in chain


def exclusive_foreground_pid(pids, parents: Optional[dict] = None) -> Optional[int]:
    """The one session you are unambiguously looking at, else None.

    Tabs are invisible from here: three sessions in three tabs of one terminal
    all resolve to that terminal's window, so all three look focused. Measured
    on a real machine — three `claude` processes, three shells, one Warp
    window. Suppressing on that would silence every alert whenever the terminal
    had focus, including a session in another tab that needs you.

    So a session only counts as "the one you're looking at" when it is the
    ONLY live session under the focused window. With several sharing it we
    cannot tell them apart, and alerting is the safe answer.
    """
    fg = foreground_pid()
    if fg is None:
        return None
    parents = parent_map() if parents is None else parents
    matches = [p for p in pids if fg in ancestors(p, parents)]
    return matches[0] if len(matches) == 1 else None
