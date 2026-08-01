"""A system-wide hotkey: no UI dependencies beyond ctypes.

Used for one thing — "take me to the session that needs me" — from any
application, without touching the widget at all.

Windows only. ``RegisterHotKey`` posts ``WM_HOTKEY`` to the *thread* message
queue rather than to a window, so there is nothing for Tk to dispatch: the
owner has to drain the queue itself. :meth:`Hotkey.poll` does that, cheaply
enough to call from an existing timer, and everything fails soft — a hotkey
that can't be registered (another app already owns the combination) simply
doesn't work, and never stops the widget from starting.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Optional

_IS_WIN = sys.platform == "win32"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "cmd": MOD_WIN, "super": MOD_WIN}

#: Virtual-key codes for the keys worth binding. Letters and digits map to
#: their ASCII value, so only the named ones need listing.
_KEYS = {"f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
         "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
         "f11": 0x7A, "f12": 0x7B, "space": 0x20, "tab": 0x09,
         "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B}


def parse(spec: str):
    """"ctrl+alt+j" -> (modifiers, virtual key), or None if unparsable."""
    if not spec or not isinstance(spec, str):
        return None
    parts = [p.strip().lower() for p in spec.replace("-", "+").split("+")
             if p.strip()]
    if not parts:
        return None
    mods, key = 0, None
    for part in parts:
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS:
            key = _KEYS[part]
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            key = ord(part.upper())
        else:
            return None
    if key is None or mods == 0:
        return None       # a bare key would swallow it from every application
    return mods, key


WM_QUIT = 0x0012


class Hotkey:
    """One registered system-wide hotkey.

    Registration and the message loop live on their own daemon thread. They
    have to: ``RegisterHotKey`` is per-thread, and ``WM_HOTKEY`` is posted to
    the thread queue rather than to a window — on the Tk main thread, Tk's own
    message pump retrieves and discards it before we ever see it, so the
    shortcut registers successfully and then silently never fires.

    The thread only counts presses. :meth:`poll` drains that count from
    whichever thread owns the UI, so the callback still runs where it's safe to
    touch widgets.
    """

    def __init__(self, spec: str, on_press, hotkey_id: int = 0xC1AD,
                 timeout: float = 2.0):
        self.spec = spec
        self._on_press = on_press
        self._id = hotkey_id
        self.registered = False
        self.error: Optional[str] = None
        self._pending = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread_id = None

        combo = parse(spec)
        if combo is None:
            self.error = "unrecognised shortcut"
            return
        if not _IS_WIN:
            self.error = "only supported on Windows"
            return
        self._combo = combo
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="claudometer-hotkey")
        self._thread.start()
        # The caller wants to report a clash immediately, so wait briefly for
        # the registration result rather than leaving it indeterminate.
        self._ready.wait(timeout)

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        mods, key = self._combo
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            # MOD_NOREPEAT: holding the keys fires once, not continuously.
            ok = user32.RegisterHotKey(None, self._id, mods | MOD_NOREPEAT, key)
            self.registered = bool(ok)
            if not ok:
                self.error = "already taken by another application"
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._ready.set()
        if not self.registered:
            return
        try:
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self._id:
                    with self._lock:
                        self._pending += 1
        except Exception:
            pass
        finally:
            try:
                user32.UnregisterHotKey(None, self._id)  # same thread as register
            except Exception:
                pass
            self.registered = False

    def poll(self) -> int:
        """Run the callback if the hotkey was pressed. Returns presses seen."""
        with self._lock:
            fired, self._pending = self._pending, 0
        if fired:
            try:
                self._on_press()      # a burst collapses into one action
            except Exception:
                pass
        return fired

    def unregister(self):
        if not self.registered:
            return
        self.registered = False
        try:
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass


# ctypes.wintypes isn't imported by default on non-Windows builds of ctypes.
if _IS_WIN:  # pragma: no cover - platform specific
    import ctypes.wintypes  # noqa: F401
