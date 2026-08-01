"""Deliver an answer into one specific Claude session: no UI dependencies.

A running session publishes no socket to send into, so the only way to answer
one from outside is its console. ``AttachConsole(pid)`` attaches to the console
owned by a PROCESS, which is what makes this usable: it addresses the session
itself, not the window it happens to be drawn in. Several sessions share one
terminal and nothing in the process tree distinguishes their tabs, so anything
window-based would be a coin flip.

Verified against Claude Code's own TUI running under a Windows Terminal
pseudoconsole: injected keystrokes were received and acted on.

Windows only. ``AttachConsole`` is Win32; Linux's TIOCSTI is disabled on modern
kernels and macOS has no equivalent, so elsewhere :func:`can_send` is False and
callers fall back to opening the terminal.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Optional, Tuple

_IS_WIN = sys.platform == "win32"

#: Only one console can be attached to a process at a time, so sends are
#: serialised. They are user-initiated and rare, so this never contends.
_LOCK = threading.Lock()

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_KEY_EVENT = 0x0001
_VK_RETURN = 0x0D

#: Longest answer accepted. Each character costs two input records; this keeps
#: a pasted essay from flooding a session's input buffer.
MAX_LEN = 2000

if _IS_WIN:  # pragma: no cover - platform specific
    import ctypes.wintypes as wintypes

    class _CHAR_UNION(ctypes.Union):
        _fields_ = [("UnicodeChar", ctypes.c_wchar),
                    ("AsciiChar", ctypes.c_char)]

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL),
                    ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD),
                    ("wVirtualScanCode", wintypes.WORD),
                    ("uChar", _CHAR_UNION),
                    ("dwControlKeyState", wintypes.DWORD)]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]

    class _INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _INPUT_UNION)]


def can_send() -> bool:
    """True where an answer can be delivered without opening the terminal."""
    return _IS_WIN


def clean(text) -> str:
    """One line of printable text, capped.

    Newlines are stripped rather than sent: an embedded one would submit the
    answer half-typed, and the caller adds exactly one Enter at the end.
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in ("\r", "\n", "\t"):
            out.append(" ")
        elif ord(ch) < 32 or ord(ch) == 127:
            continue
        else:
            out.append(ch)
    return "".join(out).strip()[:MAX_LEN]


def _records(text: str):
    recs = []
    for ch in text:
        for down in (True, False):
            rec = _INPUT_RECORD()
            rec.EventType = _KEY_EVENT
            rec.Event.KeyEvent.bKeyDown = down
            rec.Event.KeyEvent.wRepeatCount = 1
            rec.Event.KeyEvent.wVirtualKeyCode = _VK_RETURN if ch == "\r" else 0
            rec.Event.KeyEvent.wVirtualScanCode = 0
            rec.Event.KeyEvent.uChar.UnicodeChar = ch
            rec.Event.KeyEvent.dwControlKeyState = 0
            recs.append(rec)
    return recs


def send_text(pid, text: str, submit: bool = True) -> Tuple[bool, Optional[str]]:
    """Type *text* into the session owned by *pid*.

    Returns ``(ok, error)``. Never raises: a failed send has to surface as a
    message, not take the widget down.

    ``submit`` appends the Enter that actually sends it — False types the text
    and leaves it sitting in the prompt.
    """
    if not _IS_WIN:
        return False, "only supported on Windows"
    body = clean(text)
    if not body and not submit:
        return False, "nothing to send"
    # An empty body WITH submit is a bare Enter — how you accept the
    # highlighted option in one of Claude Code's numbered menus.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "no session"
    if pid <= 0:
        return False, "no session"

    payload = body + ("\r" if submit else "")
    with _LOCK:
        try:
            kernel32 = ctypes.windll.kernel32
        except Exception as exc:
            return False, str(exc)
        # We must own no console before attaching to someone else's. The widget
        # is a windowed app and has none, but detaching first is what makes a
        # second send in the same run work.
        try:
            kernel32.FreeConsole()
        except Exception:
            pass
        try:
            if not kernel32.AttachConsole(pid):
                return False, "that session's console isn't reachable"
        except Exception as exc:
            return False, str(exc)
        try:
            handle = kernel32.CreateFileW(
                "CONIN$", _GENERIC_READ | _GENERIC_WRITE,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
                0, None)
            if handle in (0, _INVALID_HANDLE):
                return False, "couldn't open that session's input"
            try:
                recs = _records(payload)
                array = (_INPUT_RECORD * len(recs))(*recs)
                written = wintypes.DWORD()
                ok = kernel32.WriteConsoleInputW(handle, array, len(recs),
                                                 ctypes.byref(written))
                if not ok:
                    return False, "the session refused the input"
                if written.value != len(recs):
                    return False, "only part of the answer was delivered"
                return True, None
            finally:
                kernel32.CloseHandle(handle)
        except Exception as exc:
            return False, str(exc)
        finally:
            try:
                kernel32.FreeConsole()
            except Exception:
                pass
