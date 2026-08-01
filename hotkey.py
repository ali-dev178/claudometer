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


class Hotkey:
    """One registered system-wide hotkey."""

    def __init__(self, spec: str, on_press, hotkey_id: int = 0xC1AD):
        self.spec = spec
        self._on_press = on_press
        self._id = hotkey_id
        self.registered = False
        self.error: Optional[str] = None
        self._register()

    def _register(self):
        combo = parse(self.spec)
        if combo is None:
            self.error = "unrecognised shortcut"
            return
        if not _IS_WIN:
            self.error = "only supported on Windows"
            return
        mods, key = combo
        try:
            user32 = ctypes.windll.user32
            # MOD_NOREPEAT: holding the keys fires once, not continuously.
            ok = user32.RegisterHotKey(None, self._id, mods | MOD_NOREPEAT, key)
            if ok:
                self.registered = True
            else:
                self.error = "already taken by another application"
        except Exception as exc:
            self.error = str(exc)

    def poll(self) -> int:
        """Drain any pending presses; returns how many fired.

        WM_HOTKEY goes to the thread queue, so this must run on the same thread
        that registered it — the Tk main thread.
        """
        if not self.registered:
            return 0
        fired = 0
        try:
            user32 = ctypes.windll.user32
            msg = ctypes.wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY,
                                      WM_HOTKEY, PM_REMOVE):
                if msg.wParam == self._id:
                    fired += 1
        except Exception:
            return 0
        for _ in range(min(fired, 1)):   # collapse a burst into one action
            try:
                self._on_press()
            except Exception:
                pass
        return fired

    def unregister(self):
        if not self.registered:
            return
        try:
            ctypes.windll.user32.UnregisterHotKey(None, self._id)
        except Exception:
            pass
        self.registered = False


# ctypes.wintypes isn't imported by default on non-Windows builds of ctypes.
if _IS_WIN:  # pragma: no cover - platform specific
    import ctypes.wintypes  # noqa: F401
