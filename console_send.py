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


if _IS_WIN:  # pragma: no cover - platform specific

    class _COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                    ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

    class _CSBI(ctypes.Structure):
        _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD),
                    ("wAttributes", wintypes.WORD), ("srWindow", _SMALL_RECT),
                    ("dwMaximumWindowSize", _COORD)]


def visible_rows(win_top: int, win_bottom: int, buffer_rows: int,
                 max_rows: int):
    """Which rows of the console buffer to read, as a half-open range.

    Split out of read_screen purely so it can be tested: it needs a real
    console otherwise, and the one thing worth getting right here is which
    END gets dropped when the window is taller than the cap. Everything that
    matters — the newest reply, the question, its menu — is at the BOTTOM, so
    the cap takes rows off the top.
    """
    top = max(0, win_top)
    bottom = max(top, min(buffer_rows, win_bottom))
    return max(top, bottom - max(1, max_rows)), bottom


def read_screen(pid, max_rows: int = 120):
    """What a session currently has on screen, as a list of lines.

    The transcript only gains a tool call once it has been answered, so while a
    session is actually waiting the question exists nowhere but its own screen.
    Read-only.
    """
    if not _IS_WIN:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    with _LOCK:
        try:
            kernel32 = ctypes.windll.kernel32
        except Exception:
            return None
        try:
            kernel32.FreeConsole()
        except Exception:
            pass
        try:
            if not kernel32.AttachConsole(pid):
                return None
        except Exception:
            return None
        try:
            handle = kernel32.CreateFileW(
                "CONOUT$", _GENERIC_READ | _GENERIC_WRITE,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
                0, None)
            if handle in (0, _INVALID_HANDLE):
                return None
            try:
                info = _CSBI()
                if not kernel32.GetConsoleScreenBufferInfo(handle,
                                                           ctypes.byref(info)):
                    return None
                width = int(info.dwSize.X)
                if width <= 0:
                    return None
                # Only the visible window, not the whole scrollback.
                top, bottom = visible_rows(
                    int(info.srWindow.Top), int(info.srWindow.Bottom) + 1,
                    int(info.dwSize.Y), max_rows)
                buf = ctypes.create_unicode_buffer(width)
                got = wintypes.DWORD()
                rows = []
                for y in range(top, bottom):
                    if not kernel32.ReadConsoleOutputCharacterW(
                            handle, buf, width, _COORD(0, y),
                            ctypes.byref(got)):
                        break
                    rows.append(buf.value[:got.value].rstrip())
                return rows
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
        finally:
            try:
                kernel32.FreeConsole()
            except Exception:
                pass


def _units(text: str):
    """Split into UTF-16 code units, which is what a console record holds.

    A KEY_EVENT_RECORD carries a single ``wchar_t``. Anything outside the
    basic plane — an emoji, most obviously — is two units, and handing the
    whole character to ctypes raises before a single key is delivered. Split
    into surrogates and the console reassembles them on the far side.
    """
    for ch in text:
        if ord(ch) <= 0xFFFF:
            yield ch
        else:
            point = ord(ch) - 0x10000
            yield chr(0xD800 + (point >> 10))
            yield chr(0xDC00 + (point & 0x3FF))


def _records(text: str):
    recs = []
    for ch in _units(text):
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
