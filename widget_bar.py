"""Always-on-top taskbar readout (Windows) with a premium details popover.

The strip and the popover are both rendered as high-quality Pillow images
(see render.py) and shown via ImageTk. The strip's background is sampled from
the taskbar so it looks like floating text yet stays fully clickable; clicking
it opens a polished popover with circular usage gauges.

Interactions: left-click = open/close popover · left-drag = move (remembered)
· right-click = Details / Open Settings / Refresh / Check for Updates / Quit.
"""

import ctypes
import dataclasses
from ctypes import wintypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk

import usage_core as core
import render
import settings
import sessions_core
import focus
import hooks as claude_hooks
import hotkey as hotkeys
import console_send
import cost
import resume
import config
import updates
import autostart

POS_FILE = Path.home() / ".claude_widget_bar.json"
TASKBAR_H = 48
DRAG_THRESHOLD = 4

# Windows-only native handles are set up lazily so this module imports on any OS
# (the floating widget is cross-platform; only the taskbar/DPI/fullscreen extras
# are Windows-specific and no-op elsewhere).
_IS_WIN = sys.platform == "win32"
if _IS_WIN:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _gdi32.GetPixel.restype = ctypes.c_uint
    # Multi-monitor: resolve the monitor (and its work area) under a screen point
    # so the popover and toasts land on whichever display the widget lives on.
    _user32.MonitorFromPoint.restype = ctypes.c_void_p
    _user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
else:
    _user32 = _gdi32 = None


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _set_dpi_aware():
    if not _IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _screen_w():
    if _IS_WIN:
        return _user32.GetSystemMetrics(0)
    r = tk._default_root
    return r.winfo_screenwidth() if r is not None else 1920


def _screen_size():
    if _IS_WIN:
        return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    r = tk._default_root
    if r is not None:
        return r.winfo_screenwidth(), r.winfo_screenheight()
    return 1920, 1080


#: MonitorFromPoint: return NULL rather than the nearest monitor.
_MONITOR_DEFAULTTONULL = 0


def _on_a_monitor(x, y):
    """True if (x, y) actually lands on a display.

    A remembered position outlives the display it was chosen on — unplug a
    second monitor, or let a drag fling the strip past an edge, and restoring
    it verbatim leaves the widget running somewhere nobody can see, with no way
    to get it back but deleting a file.
    """
    if not _IS_WIN:
        sw, sh = _screen_size()
        return -40 <= x <= sw - 20 and -40 <= y <= sh - 10
    try:
        pt = wintypes.POINT(int(x) + 8, int(y) + 8)   # just inside the strip
        return bool(ctypes.windll.user32.MonitorFromPoint(
            pt, _MONITOR_DEFAULTTONULL))
    except Exception:
        return True      # can't tell — trust what was saved


def _monitor_workarea(x, y):
    """(left, top, right, bottom) work area of the monitor under screen point
    (x, y) — used to place the popover/toasts on the widget's own display.
    rcWork already excludes the taskbar. Falls back to the primary screen
    off-Windows or if the query fails."""
    if _IS_WIN:
        try:
            hmon = _user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)  # NEAREST
            if hmon:
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    r = mi.rcWork
                    return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    sw, sh = _screen_size()
    return 0, 0, sw, sh


def _popover_xy(anchor_x, anchor_top, anchor_bottom, w, h, work):
    """Top-left for a w×h popover anchored to a strip (top-left at
    (anchor_x, anchor_top), bottom at anchor_bottom), kept inside monitor work
    area ``work`` = (left, top, right, bottom). Opens above the strip when there
    is room, else drops below; clamps inside the work area on every edge. Pure
    arithmetic (no Tk/ctypes) so it's unit-testable headlessly."""
    wl, wt, wr, wb = work
    px = min(max(wl + 8, anchor_x), wr - w - 8)
    above = anchor_top - h - 8
    if above >= wt + 8:                 # enough room above -> preferred
        py = above
    else:                               # top is tight -> drop below the strip
        py = anchor_bottom + 8
        if py + h > wb - 8:             # would overflow the bottom -> clamp up
            py = max(wt + 8, wb - h - 8)
    return px, py


def _get_pixel(x, y):
    if not _IS_WIN:
        return None  # no taskbar to sample off-Windows
    hdc = _user32.GetDC(0)
    if not hdc:
        return None
    try:
        c = _gdi32.GetPixel(hdc, int(x), int(y))
        if c == 0xFFFFFFFF:
            return None
        return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)
    finally:
        _user32.ReleaseDC(0, hdc)


def _lum(rgb):
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _rel_luminance(rgb):
    """WCAG relative luminance, for judging text contrast."""
    out = []
    for channel in rgb[:3]:
        c = min(max(channel, 0), 255) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _contrast(rgb_a, rgb_b):
    """WCAG contrast ratio between two colours (1.0 … 21.0)."""
    a, b = _rel_luminance(rgb_a), _rel_luminance(rgb_b)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


def _theme_for_bg(rgb):
    """Pick the theme whose text is actually readable on *rgb*.

    A plain luminance threshold gets this wrong on saturated colours: a mid
    blue sits above the cutoff, so the light theme's near-black text is chosen
    and the strip becomes hard to read. The strip samples whatever sits behind
    it — which is sometimes a window, not the taskbar — so the choice is made
    on measured contrast instead.
    """
    best, winner = 0.0, "light"
    for name, theme in render.THEMES.items():
        text = theme["neutral"].lstrip("#")
        ratio = _contrast(rgb, tuple(int(text[i:i + 2], 16) for i in (0, 2, 4)))
        if ratio > best:
            best, winner = ratio, name
    return winner


_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _quns_fullscreen():
    """True if Windows reports a fullscreen / presentation / D3D app active
    (the same signal used to suppress notifications)."""
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value in (2, 3, 4)  # BUSY, RUNNING_D3D_FULL_SCREEN, PRESENTATION
    except Exception:
        pass
    return False


def _foreground_fullscreen(sw, sh):
    """True if the foreground window covers the whole primary screen
    (borderless-fullscreen video/games), excluding the desktop/shell."""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, buf, 64)
        if buf.value in _SHELL_CLASSES:
            return False
        rect = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        return rect.left <= 0 and rect.top <= 0 and rect.right >= sw and rect.bottom >= sh
    except Exception:
        return False


def _fullscreen_active():
    if not _IS_WIN:
        return False  # fullscreen auto-hide is a Windows-only extra
    if _quns_fullscreen():
        return True
    sw, sh = _screen_size()
    return _foreground_fullscreen(sw, sh)


def _log_exc(_exc=None):
    """Append an unexpected exception to a log so real bugs are diagnosable."""
    try:
        log = Path.home() / ".claude" / "claudometer-error.log"
        with log.open("a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
    except Exception:
        pass


def _open_url(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        _log_exc()


def _make_transparent(win, theme):
    """Make a borderless window's background transparent (so rounded cards float),
    using the right mechanism per OS. Returns the bg 'key' color to also apply to
    the window's Canvas. Falls back to an opaque bg where unsupported."""
    if sys.platform == "darwin":
        try:
            win.attributes("-transparent", True)
            win.configure(bg="systemTransparent")
            return "systemTransparent"
        except Exception:
            pass  # fall through to opaque
    key = render.THEMES.get(theme, render.THEMES["light"])["key"]
    try:
        if _IS_WIN:
            win.attributes("-transparentcolor", key)
    except Exception:
        pass
    win.configure(bg=key)
    return key


def _round_alpha(img, radius):
    """RGBA copy of img with corners outside a rounded rect made transparent, so it
    composites cleanly on any transparent window (Win keys it out, mac shows through)."""
    img = img.convert("RGBA")
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.putalpha(mask)
    return img


# --------------------------------------------------------------------------- #
# Details popover (image-based)
# --------------------------------------------------------------------------- #
class Popover:
    def __init__(self, root, theme, get_disp, anchor_x, anchor_top, anchor_bottom, work,
                 on_refresh, on_quit, on_settings, on_close, on_session=None,
                 on_session_menu=None):
        self.theme = theme
        self.get_disp = get_disp
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.on_settings = on_settings
        self.on_close = on_close
        self.on_session = on_session            # left click a session row
        self.on_session_menu = on_session_menu  # right click a session row
        self._rows = []                         # rows as last rendered
        self._closed = False
        self._after = None
        self._sig = None
        self._hits = {}
        # Manual-refresh feedback: "Refreshing…" from the click until a newer
        # poll lands; otherwise the footer shows a live "Updated … ago · src"
        # where src is whether the last poll was a manual click or an auto tick.
        self._refresh_since = None
        self._refresh_base_seq = None
        self._last_seq = None
        self._last_source = None
        self.anchor_x = anchor_x
        self.anchor_top = anchor_top
        self.anchor_bottom = anchor_bottom
        self.work = work

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass
        key = _make_transparent(self.top, theme)
        self.canvas = tk.Canvas(self.top, bg=key, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Button-3>", self._right_click)
        self.canvas.bind("<Motion>", self._motion)
        self.top.bind("<Escape>", lambda e: self.close())

        self._render(force=True)
        self.top.after(120, self._arm)
        self._tick()

    def _arm(self):
        try:
            self.top.focus_force()
            self.top.bind("<FocusOut>", lambda e: self.close())
        except Exception:
            pass

    def _sig_of(self, disp):
        return (
            disp.get("session_pct"), disp.get("session_color"),
            render._fmt_left(disp.get("session_resets_at")),
            disp.get("weekly_pct"), disp.get("weekly_color"),
            render._fmt_at(disp.get("weekly_resets_at")),
            tuple((r["label"], r["pct"], r["color"]) for r in disp.get("model_rows") or []),
            disp.get("plan"),
            round(disp.get("cost_usd") or 0, 2),
            # Live sessions: the dwell text is part of the signature, so a row
            # that only ages ("4m" -> "5m") still triggers a redraw.
            tuple((r["label"], r["project"], r["color"], r["detail"])
                  for r in disp.get("sessions_rows") or []),
            disp.get("sessions_overflow"), disp.get("sessions_summary"),
            disp.get("sessions_rows") is not None,
        )

    @staticmethod
    def _fmt_age(secs):
        secs = int(max(0, secs))
        if secs < 5:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        return f"{secs // 60}m ago"

    def _foot_state(self, disp):
        """Footer status shown while the popover is open: 'Refreshing…' during a
        manual refresh, otherwise a live 'Updated … ago · manual|auto' so the
        data's freshness and its source are always visible. Returns a foot dict."""
        now = time.monotonic()
        cur = disp.get("_seq")
        if self._refresh_since is not None:
            done = cur is not None and (self._refresh_base_seq is None
                                        or cur > self._refresh_base_seq)
            if done:  # the manual click's poll landed
                self._refresh_since = None
                self._last_seq = cur
                self._last_source = "manual"
            elif now - self._refresh_since > 12:  # fetch stuck/offline — give up
                self._refresh_since = None
            else:
                return {"text": "Refreshing…", "dot": "amber"}
        if cur is not None and cur != self._last_seq:  # a background tick landed
            self._last_seq = cur
            self._last_source = "auto"
        mono = disp.get("_poll_mono")
        if mono is None:
            return {"text": "Auto-updating", "dot": "green"}
        src = self._last_source or "auto"
        return {"text": f"Updated {self._fmt_age(now - mono)} · {src}", "dot": "green"}

    def _render(self, force=False):
        disp = self.get_disp() or {}
        foot = self._foot_state(disp)
        sig = (self._sig_of(disp), foot and (foot["text"], foot["dot"]))
        if not force and sig == self._sig:
            return
        self._sig = sig
        if foot:
            disp = dict(disp, foot=foot)
        self._rows = list(disp.get("sessions_rows") or [])
        img, hits = render.render_popover(disp, self.theme)
        img = _round_alpha(img, 16)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self._hits = hits
        # Place on the SAME monitor as the strip (not the primary screen), and
        # open up or down depending on which side has room. See _popover_xy.
        px, py = _popover_xy(
            self.anchor_x, self.anchor_top, self.anchor_bottom, w, h, self.work)
        self.canvas.configure(width=w, height=h)
        self.top.geometry(f"{w}x{h}+{int(px)}+{int(py)}")
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _hit_at(self, x, y):
        for name, (x1, y1, x2, y2) in self._hits.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _motion(self, e):
        # A hand cursor over the footer buttons hints they're clickable.
        try:
            self.canvas.configure(cursor="hand2" if self._hit_at(e.x, e.y) else "")
        except Exception:
            pass

    def _row_at(self, x, y):
        """The session row under a point, or None."""
        name = self._hit_at(x, y)
        if not name or not name.startswith("session:"):
            return None
        try:
            return self._rows[int(name.split(":", 1)[1])]
        except (ValueError, IndexError):
            return None      # the list changed between render and click

    def _right_click(self, e):
        row = self._row_at(e.x, e.y)
        if row is not None and self.on_session_menu:
            self.on_session_menu(row, e.x_root, e.y_root)

    def _click(self, e):
        row = self._row_at(e.x, e.y)
        if row is not None:
            if self.on_session:
                self.on_session(row)
            return
        for name, (x1, y1, x2, y2) in self._hits.items():
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                if name == "refresh":
                    self._refresh_base_seq = (self.get_disp() or {}).get("_seq")
                    self._refresh_since = time.monotonic()
                    self.on_refresh()
                    self._render(force=True)  # show "Refreshing…" at once
                    self.top.after(400, self._render)  # catch a quick completion
                elif name == "quit":
                    self.on_quit()
                elif name == "settings":
                    self.close()
                    if self.on_settings:
                        self.on_settings()
                return

    def _tick(self):
        if self._closed:
            return
        self._render()
        self._after = self.top.after(1000, self._tick)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._after:
            try:
                self.top.after_cancel(self._after)
            except Exception:
                pass
        try:
            self.top.destroy()
        except Exception:
            pass
        if self.on_close:
            self.on_close()


# --------------------------------------------------------------------------- #
# Threshold alert toast
# --------------------------------------------------------------------------- #
class AnswerWindow:
    """Answer a blocked session without leaving what you're doing.

    Shows what the session is waiting on, one-click Yes / No, and a box for
    anything else. The answer goes to that session's console by pid, so it
    lands in the right session regardless of which terminal tab is in front —
    and without stealing your focus.
    """

    W = 380

    def __init__(self, root, theme, row, on_send, on_open_terminal,
                 on_close=None):
        self._on_send = on_send
        self._on_open = on_open_terminal
        self._on_close = on_close
        self._closed = False
        self.row = row
        T = render.THEMES.get(theme, render.THEMES["light"])
        bg, fg, dim = T["panel_bot"], T["neutral"], T["dim"]

        self.top = tk.Toplevel(root)
        self.top.title("Answer session")
        self.top.configure(bg=bg)
        self.top.resizable(False, False)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Escape>", lambda e: self.close())

        pad = tk.Frame(self.top, bg=bg)
        pad.pack(fill="both", expand=True, padx=18, pady=14)

        head = tk.Frame(pad, bg=bg)
        head.pack(fill="x")
        tk.Canvas(head, width=10, height=10, bg=bg, highlightthickness=0).pack(
            side="left", padx=(0, 8))
        dot = head.winfo_children()[0]
        dot.create_oval(1, 1, 9, 9, fill=render.sev_color(T, row.get("color", "red")),
                        outline="")
        tk.Label(head, text=(row.get("label") or "session")[:44], bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 11), anchor="w").pack(side="left")
        # Project and how long it's been waiting — NOT row["detail"], which
        # embeds the question and would repeat it directly above itself.
        dwell = row.get("dwell") or ""
        tk.Label(pad, text=f"{row.get('project', '')}"
                           + (f" · waiting {dwell}" if dwell else ""),
                 bg=bg, fg=dim, font=("Segoe UI", 9), anchor="w").pack(
                     fill="x", pady=(2, 10))

        question = row.get("question") or row.get("status_text") or ""
        if question:
            tk.Message(pad, text=question, bg=bg, fg=fg, font=("Segoe UI", 10),
                       width=self.W - 40, anchor="w", justify="left").pack(
                           fill="x", pady=(0, 12))

        options = list(row.get("options") or [])
        if options:
            # The session offered a numbered menu, so offer the same choices
            # rather than a Yes/No that means nothing here. Picking one sends
            # its number, which is how that menu is answered.
            selected = int(row.get("selected") or 0)
            for index, label in enumerate(options, start=1):
                here = index == selected      # what the session has highlighted
                tk.Button(pad, text=f"{index}.  {label}",
                          command=lambda n=index: self._send(str(n)),
                          bg=T["accent_soft"] if here else T["track"],
                          fg=fg, activebackground=T["accent"],
                          activeforeground="#ffffff", bd=0, relief="flat",
                          font=("Segoe UI Semibold" if here else "Segoe UI", 10),
                          anchor="w", padx=12, pady=6,
                          cursor="hand2").pack(fill="x", pady=(0, 4))
            tk.Label(pad, text="…or type an answer", bg=bg, fg=dim,
                     font=("Segoe UI", 9), anchor="w").pack(fill="x",
                                                            pady=(6, 2))
        else:
            quick = tk.Frame(pad, bg=bg)
            quick.pack(fill="x")
            # Yes/No answer a permission prompt; a bare Enter accepts whatever
            # option a menu already has highlighted.
            for label, text, primary in (("Yes", "yes", True),
                                         ("No", "no", False),
                                         ("⏎ Enter", "", False)):
                tk.Button(quick, text=label,
                          command=lambda t=text: self._send(t, submit=True),
                          bg=T["accent"] if primary else T["track"],
                          fg="#ffffff" if primary else fg,
                          activebackground=T["accent"] if primary else T["track"],
                          bd=0, relief="flat", font=("Segoe UI Semibold", 10),
                          padx=(22 if primary else 16), pady=5,
                          cursor="hand2").pack(side="left", padx=(0, 8))

        entry_row = tk.Frame(pad, bg=bg)
        entry_row.pack(fill="x", pady=(12, 0))
        self.var = tk.StringVar()
        entry = tk.Entry(entry_row, textvariable=self.var, bg=T["track"], fg=fg,
                         insertbackground=fg, bd=0, relief="flat",
                         font=("Segoe UI", 10))
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        entry.bind("<Return>", lambda e: self._send(self.var.get()))
        tk.Button(entry_row, text="Send",
                  command=lambda: self._send(self.var.get()),
                  bg=T["accent"], fg="#ffffff", activebackground=T["accent"],
                  bd=0, relief="flat", font=("Segoe UI Semibold", 10),
                  padx=16, pady=4, cursor="hand2").pack(side="right",
                                                        padx=(8, 0))

        foot = tk.Label(pad, text="Open the terminal  →", bg=bg, fg=T["accent"],
                        font=("Segoe UI", 9), cursor="hand2", anchor="e")
        foot.pack(fill="x", pady=(12, 0))
        foot.bind("<Button-1>", lambda e: self._open_terminal())

        self.status = tk.Label(pad, text="", bg=bg, fg=dim,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", pady=(6, 0))

        self._centre(root)
        try:
            self.top.lift()
            self.top.focus_force()
            entry.focus_set()
        except Exception:
            pass

    def _centre(self, root):
        try:
            self.top.update_idletasks()
            w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
            wl, wt_, wr, wb = _monitor_workarea(
                root.winfo_rootx() + root.winfo_width() // 2,
                root.winfo_rooty() + root.winfo_height() // 2)
            x = int(wr - w - 24)
            y = int(wb - h - 24)
            self.top.geometry(f"{max(w, self.W)}x{h}+{x}+{y}")
        except Exception:
            pass

    def _send(self, text, submit=True):
        ok, error = self._on_send(self.row, text, submit)
        if ok:
            self.close()
            return
        try:
            self.status.configure(text=error or "Couldn't send that.")
        except Exception:
            pass

    def _open_terminal(self):
        self.close()
        if self._on_open:
            self._on_open(self.row)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.top.destroy()
        except Exception:
            pass
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass


class Toast:
    """A small auto-dismissing alert card near the tray."""

    def __init__(self, root, theme, pct, title, subtitle, color_name, duration=6500,
                 on_close=None, on_click=None):
        self._on_close = on_close
        # A blocked session is a request, not an announcement — its toast stays
        # until it's dealt with (duration=None) instead of timing out.
        self._on_click = on_click
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass
        key = _make_transparent(self.top, theme)
        img = _round_alpha(render.render_toast(pct, title, subtitle, color_name, theme), 14)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        c = tk.Canvas(self.top, width=w, height=h, bg=key, highlightthickness=0, bd=0)
        c.pack()
        c.create_image(0, 0, anchor="nw", image=self._photo)
        c.bind("<Button-1>", self._clicked)
        try:
            c.configure(cursor="hand2" if on_click else "")
        except Exception:
            pass
        wl, wt, wr, wb = _monitor_workarea(root.winfo_rootx() + root.winfo_width() // 2,
                                           root.winfo_rooty() + root.winfo_height() // 2)
        self.top.geometry(f"{w}x{h}+{wr - w - 20}+{wb - h - 16}")
        self._closed = False
        self._after = self.top.after(duration, self.close) if duration else None

    def _clicked(self, _e=None):
        action = self._on_click
        self.close()
        if action:
            try:
                action()
            except Exception:
                _log_exc()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._after:
                self.top.after_cancel(self._after)
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass


class ResumeToast:
    """A clickable resume notification.

    Static mode: clicking runs the action; auto-closes after a timeout.
    Countdown mode: counts down and runs on_expire at zero; a click cancels it.
    """

    def __init__(self, root, theme, title, subtitle, action_label, on_click,
                 timeout_ms=120000, countdown_s=None, on_expire=None, on_close=None):
        self._on_click = on_click
        self._on_expire = on_expire
        self._on_close = on_close
        self._timeout_ms = timeout_ms
        self._remaining = countdown_s
        self._theme = theme
        self._title = title
        self._subtitle = subtitle
        self._action = action_label
        self._closed = False
        self._after = None
        self._root = root

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
        except Exception:
            pass
        key = _make_transparent(self.top, theme)
        self.canvas = tk.Canvas(self.top, bg=key, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", lambda e: self._click())

        self._render()
        if self._remaining is not None:
            self._tick()
        elif self._timeout_ms:
            self._after = self.top.after(self._timeout_ms, self.close)

    def _render(self):
        sub = self._subtitle
        if self._remaining is not None:
            sub = f"resuming in {self._remaining}s  ·  click to cancel"
        img = _round_alpha(render.render_action_toast(self._title, sub, self._action, self._theme), 14)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self.canvas.configure(width=w, height=h)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        r = self._root
        wl, wt, wr, wb = _monitor_workarea(r.winfo_rootx() + r.winfo_width() // 2,
                                           r.winfo_rooty() + r.winfo_height() // 2)
        self.top.geometry(f"{w}x{h}+{wr - w - 20}+{wb - h - 16}")

    def _tick(self):
        if self._closed:
            return
        if self._remaining <= 0:
            fn = self._on_expire
            self.close()
            if fn:
                fn()
            return
        self._render()
        self._remaining -= 1
        self._after = self.top.after(1000, self._tick)

    def _click(self):
        countdown = self._remaining is not None
        self.close()
        if not countdown and self._on_click:
            self._on_click()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._after:
            try:
                self.top.after_cancel(self._after)
            except Exception:
                pass
        try:
            self.top.destroy()
        except Exception:
            pass
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Settings panel (native, theme-matched) — writes the config + applies live
# --------------------------------------------------------------------------- #
class _ImgWidget:
    """Base for a control drawn as a Pillow image on a tk.Label, redrawn on change."""

    def __init__(self, parent, bg):
        self.lbl = tk.Label(parent, bd=0, bg=bg, cursor="hand2")

    def pack(self, **kw):
        self.lbl.pack(**kw)
        return self

    def _show(self, pil):
        self._img = ImageTk.PhotoImage(pil)
        self.lbl.configure(image=self._img)


class _ToggleW(_ImgWidget):
    def __init__(self, parent, var, theme, bg, command=None):
        super().__init__(parent, bg)
        self.var, self.theme, self.command = var, theme, command
        self.lbl.bind("<Button-1>", self._click)
        var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _draw(self):
        self._show(render.render_toggle(bool(self.var.get()), self.theme))

    def _click(self, _):
        self.var.set(not self.var.get())
        if self.command:
            self.command()


class _SegmentW(_ImgWidget):
    def __init__(self, parent, var, values, labels, theme, bg):
        super().__init__(parent, bg)
        self.var, self.values, self.labels, self.theme = var, values, labels, theme
        self._segw = 1
        self.lbl.bind("<Button-1>", self._click)
        var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _draw(self):
        sel = self.values.index(self.var.get()) if self.var.get() in self.values else 0
        img, self._segw = render.render_segment(self.labels, sel, self.theme)
        self._show(img)

    def _click(self, e):
        i = min(max(int(e.x // self._segw), 0), len(self.values) - 1)
        self.var.set(self.values[i])


class _SliderW(_ImgWidget):
    def __init__(self, parent, var, lo, hi, theme, bg, width=150):
        super().__init__(parent, bg)
        self.var, self.lo, self.hi, self.theme, self.width = var, lo, hi, theme, width
        self.lbl.bind("<Button-1>", self._set)
        self.lbl.bind("<B1-Motion>", self._set)
        var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _val(self):
        try:
            return int(self.var.get())
        except Exception:
            return self.lo

    def _draw(self):
        frac = (self._val() - self.lo) / float(self.hi - self.lo)
        self._show(render.render_slider(frac, self.theme, self.width))

    def _set(self, e):
        m = 10
        frac = (e.x - m) / float(max(1, self.width - 2 * m))
        self.var.set(int(round(min(max(frac, 0), 1) * (self.hi - self.lo) + self.lo)))


class _StepperW(_ImgWidget):
    def __init__(self, parent, var, lo, hi, theme, bg, width=94):
        super().__init__(parent, bg)
        self.var, self.lo, self.hi, self.theme, self.width = var, lo, hi, theme, width
        self.lbl.bind("<Button-1>", self._click)
        var.trace_add("write", lambda *a: self._draw())
        self._draw()

    def _val(self):
        try:
            return int(self.var.get())
        except Exception:
            return self.lo

    def _draw(self):
        self._show(render.render_stepper(self._val(), self.theme, width=self.width))

    def _click(self, e):
        v = self._val()
        if e.x < self.width / 3:
            v -= 1
        elif e.x > 2 * self.width / 3:
            v += 1
        self.var.set(min(max(v, self.lo), self.hi))


class SettingsWindow:
    """A premium, theme-matched native settings window opened from the popover
    gear. Controls are drawn through the same Pillow pipeline as the popover. It
    writes ~/.claudometer.toml via settings.save() and hands the new config to
    on_apply() so the running widget updates live (no restart needed)."""

    WIN_W = 366

    def __init__(self, root, theme, cfg, on_apply, on_close=None, on_demo=None, demo_active=False):
        self._on_apply = on_apply
        self._on_close = on_close
        self._on_demo = on_demo
        self._demo_active = demo_active
        self._cfg = dict(cfg)
        self._closed = False
        self._theme = theme
        T = render.THEMES.get(theme, render.THEMES["light"])
        self.T = T
        bg, fg, dim, field = T["panel_bot"], T["neutral"], T["dim"], T["track"]

        self.top = tk.Toplevel(root)
        self.top.title("Claudometer — Settings")
        self.top.configure(bg=bg)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.bind("<Escape>", lambda e: self.close())

        m = cfg.get("metrics") or ["session", "weekly"]
        thr = cfg.get("alert_thresholds") or [80, 90]
        self.v_theme = tk.StringVar(value=cfg.get("theme", "auto"))
        self.v_session = tk.BooleanVar(value="session" in m)
        self.v_weekly = tk.BooleanVar(value="weekly" in m)
        self.v_accent = tk.StringVar(value=cfg.get("accent") or "")
        self.v_poll = tk.IntVar(value=cfg.get("poll", 90))
        self.v_alerts = tk.BooleanVar(value=cfg.get("alerts", True))
        self.v_t1 = tk.IntVar(value=thr[0] if len(thr) > 0 else 80)
        self.v_t2 = tk.IntVar(value=thr[1] if len(thr) > 1 else 90)
        self.v_cost = tk.BooleanVar(value=cfg.get("show_cost", False))
        self.v_fs = tk.BooleanVar(value=cfg.get("hide_on_fullscreen", True))
        self.v_login = tk.BooleanVar(value=autostart.is_enabled())
        self.v_notify = tk.BooleanVar(value=cfg.get("resume_notify", True))
        self.v_auto = tk.BooleanVar(value=cfg.get("resume_auto", False))
        self.v_skip = tk.BooleanVar(value=cfg.get("resume_skip_permissions", False))
        self.v_prompt = tk.StringVar(value=cfg.get("resume_prompt", ""))
        self.v_maxturns = tk.IntVar(value=cfg.get("resume_max_turns", 30))
        self.v_sessions = tk.BooleanVar(value=cfg.get("sessions", True))
        self.v_sess_strip = tk.BooleanVar(value=cfg.get("sessions_on_strip", True))
        self.v_sess_rows = tk.IntVar(value=cfg.get("sessions_max_rows", 6))
        alert_on = cfg.get("sessions_alert_on") or []
        self.v_sess_alerts = tk.BooleanVar(value=cfg.get("sessions_alerts", True))
        self.v_a_waiting = tk.BooleanVar(value="waiting" in alert_on)
        self.v_a_idle = tk.BooleanVar(value="idle" in alert_on)
        self.v_a_stuck = tk.BooleanVar(value="stuck" in alert_on)
        self.v_a_gone = tk.BooleanVar(value="gone" in alert_on)
        self.v_sess_stuck_min = tk.IntVar(value=cfg.get("sessions_stuck_minutes", 10))
        self.v_sess_quiet = tk.BooleanVar(
            value=cfg.get("sessions_quiet_foreground", True))
        self.v_sess_hooks = tk.BooleanVar(value=cfg.get("sessions_hooks", False))
        self.v_sess_hotkey = tk.StringVar(value=cfg.get("sessions_hotkey", ""))
        self.v_sess_answer = tk.BooleanVar(value=cfg.get("sessions_answer", True))

        # rendered header banner (sparkle + title + subtitle)
        self._hdr = ImageTk.PhotoImage(render.render_settings_header(theme, self.WIN_W))
        tk.Label(self.top, image=self._hdr, bd=0, bg=bg).pack()

        # The panel grew past the height of the screen it configures, so the
        # content scrolls when it doesn't fit. The canvas sizes itself to the
        # content and only starts scrolling — and only then shows a scrollbar —
        # once that would run off the display.
        self._scroll_wrap = tk.Frame(self.top, bg=bg)
        self._scroll_wrap.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(self._scroll_wrap, bg=bg, highlightthickness=0,
                                 bd=0, width=self.WIN_W)
        self._vsb = tk.Scrollbar(self._scroll_wrap, orient="vertical",
                                 command=self._canvas.yview)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.configure(yscrollcommand=self._vsb.set)

        # An outer frame carries the canvas window (so it spans the full width);
        # `body` sits inside it and keeps the original 20px gutter.
        self._body_outer = tk.Frame(self._canvas, bg=bg)
        self._canvas.create_window((0, 0), window=self._body_outer, anchor="nw",
                                   width=self.WIN_W)
        body = tk.Frame(self._body_outer, bg=bg)
        body.pack(fill="x", padx=20)
        self._body_outer.bind("<Configure>", lambda _e: self._resize_scroll())
        self._canvas.bind("<Enter>", lambda _e: self._canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda _e: self._canvas.unbind_all(
            "<MouseWheel>"))
        LBL = ("Segoe UI", 9)

        def section(title, first=False):
            lbl = tk.Label(body, text=title.upper(), bg=bg, fg=T["accent"],
                           font=("Segoe UI Semibold", 8))
            lbl.pack(anchor="w", pady=(10 if first else 15, 5))
            return lbl   # an anchor for pack(before=...) on collapsible groups

        def row(parent=None):
            f = tk.Frame(parent or body, bg=bg)
            f.pack(fill="x", pady=4)
            return f

        def label(parent, text):
            return tk.Label(parent, text=text, bg=bg, fg=fg, font=LBL, anchor="w")

        def toggle_row(text, var, parent=None, cmd=None):
            r = row(parent)
            label(r, text).pack(side="left")
            _ToggleW(r, var, theme, bg, command=cmd).pack(side="right")

        # ----- Display -----
        section("Display", first=True)
        r = row()
        label(r, "Theme").pack(side="left")
        _SegmentW(r, self.v_theme, ["auto", "light", "dark"], ["Auto", "Light", "Dark"],
                  theme, bg).pack(side="right")
        toggle_row("Session meter", self.v_session)
        toggle_row("Weekly meter", self.v_weekly)
        toggle_row("Start on login", self.v_login, cmd=self._apply_login)

        r = row()
        label(r, "Accent").pack(side="left")
        self._swatch = tk.Label(r, width=2, bg=(cfg.get("accent") or T["accent"]), bd=0)
        self._swatch.pack(side="right", padx=(8, 0), ipady=6)
        tk.Entry(r, textvariable=self.v_accent, width=9, justify="center", bg=field, fg=fg,
                 insertbackground=fg, bd=0, relief="flat", font=("Consolas", 9)).pack(side="right", ipady=4)
        self.v_accent.trace_add("write", lambda *a: self._update_swatch())
        pr = row()
        tk.Label(pr, text="", bg=bg, font=LBL).pack(side="left")
        for val in ("", "#d97757", "#5b8def", "#12a150", "#8250df", "#e5484d"):
            sw = tk.Label(pr, width=2, bg=(val or T["accent"]), bd=0, cursor="hand2")
            sw.pack(side="left", padx=3, ipady=6)
            sw.bind("<Button-1>", lambda e, v=val: self.v_accent.set(v))

        r = row()
        label(r, "Poll interval").pack(side="left")
        self._poll_lbl = tk.Label(r, text=f"{self.v_poll.get()}s", bg=bg, fg=dim,
                                  width=5, anchor="e", font=LBL)
        self._poll_lbl.pack(side="right")
        _SliderW(r, self.v_poll, 60, 300, theme, bg, width=150).pack(side="right", padx=(0, 8))
        self.v_poll.trace_add("write", lambda *a: self._poll_lbl.configure(
            text=f"{self._safe_int(self.v_poll, 90)}s"))

        # ----- Live sessions -----
        section("Live sessions")
        toggle_row("Show running Claude sessions", self.v_sessions)
        # Rows are capped at 12 below: beyond that the popover is taller than a
        # small laptop screen (see settings.load).
        toggle_row("Alert me about sessions", self.v_sess_alerts)
        # The rest lives behind a disclosure. Shown inline these rows push the
        # window past 1080px tall — it has to fit the screen it configures.
        self._sess_open = False
        self._sess_btn = tk.Label(body, text="▸  Which alerts, and how many rows",
                                  bg=bg, fg=T["accent"], font=LBL, cursor="hand2")
        self._sess_btn.pack(anchor="w", pady=(8, 0))
        self._sess_btn.bind("<Button-1>", lambda e: self._toggle_sessions())
        self._sess_more = tk.Frame(body, bg=bg)
        toggle_row("When one needs me", self.v_a_waiting, parent=self._sess_more)
        toggle_row("When one finishes", self.v_a_idle, parent=self._sess_more)
        toggle_row("When one stays blocked", self.v_a_stuck, parent=self._sess_more)
        toggle_row("When one ends", self.v_a_gone, parent=self._sess_more)
        rs = row(self._sess_more)
        label(rs, "Blocked for").pack(side="left")
        tk.Label(rs, text="min", bg=bg, fg=dim, font=LBL).pack(side="right",
                                                               padx=(6, 0))
        _StepperW(rs, self.v_sess_stuck_min, 0, 600, theme, bg,
                  width=86).pack(side="right")
        toggle_row("Stay quiet for the terminal I'm using", self.v_sess_quiet,
                   parent=self._sess_more)
        toggle_row("Show count on the strip", self.v_sess_strip,
                   parent=self._sess_more)
        toggle_row("Answer blocked sessions from here", self.v_sess_answer,
                   parent=self._sess_more)
        toggle_row("Instant alerts (edits Claude's settings)", self.v_sess_hooks,
                   parent=self._sess_more, cmd=self._confirm_hooks)
        rk = row(self._sess_more)
        label(rk, "Jump shortcut").pack(side="left")
        tk.Entry(rk, textvariable=self.v_sess_hotkey, width=13, justify="center",
                 bg=field, fg=fg, insertbackground=fg, bd=0, relief="flat",
                 font=LBL).pack(side="right", ipady=4)
        rr = row(self._sess_more)
        label(rr, "Rows in popover").pack(side="left")
        _StepperW(rr, self.v_sess_rows, 1, 12, theme, bg,
                  width=86).pack(side="right")

        # ----- Alerts -----
        # Anchor for the collapsible group above, so expanding it inserts the
        # rows under their own button rather than at the bottom of the window.
        self._sess_anchor = section("Alerts")
        toggle_row("Desktop alert on threshold", self.v_alerts)
        r = row()
        label(r, "Alert at").pack(side="left")
        tk.Label(r, text="%", bg=bg, fg=dim, font=LBL).pack(side="right", padx=(6, 0))
        _StepperW(r, self.v_t2, 1, 100, theme, bg, width=86).pack(side="right", padx=(6, 0))
        tk.Label(r, text="and", bg=bg, fg=dim, font=LBL).pack(side="right", padx=6)
        _StepperW(r, self.v_t1, 1, 100, theme, bg, width=86).pack(side="right")
        toggle_row("Show estimated cost", self.v_cost)
        toggle_row("Hide over fullscreen apps", self.v_fs)

        # ----- Resume -----
        section("Resume on reset")
        toggle_row("Notify + one-click resume", self.v_notify)
        self._adv_open = False
        self._adv_btn = tk.Label(body, text="▸  Advanced — auto-resume ⚠",
                                 bg=bg, fg=T["accent"], font=LBL, cursor="hand2")
        self._adv_btn.pack(anchor="w", pady=(8, 0))
        self._adv_btn.bind("<Button-1>", lambda e: self._toggle_advanced())
        self._adv = tk.Frame(body, bg=bg)
        toggle_row("Auto-resume unattended (risky)", self.v_auto, parent=self._adv, cmd=self._confirm_auto)
        toggle_row("Skip permission prompts (dangerous)", self.v_skip, parent=self._adv, cmd=self._confirm_skip)
        rp = row(self._adv)
        label(rp, "Prompt").pack(side="left")
        tk.Entry(rp, textvariable=self.v_prompt, bg=field, fg=fg, insertbackground=fg,
                 bd=0, relief="flat", font=LBL).pack(side="right", fill="x", expand=True, padx=(10, 0), ipady=4)
        rm = row(self._adv)
        label(rm, "Max turns").pack(side="left")
        _StepperW(rm, self.v_maxturns, 1, 200, theme, bg, width=96).pack(side="right")

        self._fbar = tk.Frame(body, bg=bg)
        self._fbar.pack(fill="x", pady=(18, 6))
        tk.Button(self._fbar, text="Save", command=self._save, bg=T["accent"], fg="#ffffff",
                  activebackground=T["accent"], activeforeground="#ffffff", bd=0, relief="flat",
                  font=("Segoe UI Semibold", 10), padx=24, pady=6, cursor="hand2").pack(side="right")
        tk.Button(self._fbar, text="Cancel", command=self.close, bg=field, fg=fg,
                  activebackground=field, activeforeground=fg, bd=0, relief="flat",
                  font=("Segoe UI", 10), padx=18, pady=6, cursor="hand2").pack(side="right", padx=(0, 10))
        if self._on_demo:  # preview every feature in a safe, offline demo (toggles)
            tk.Button(self._fbar, text=("◼  Exit demo" if self._demo_active else "▶  Try a demo"),
                      command=self._demo, bg=field, fg=T["accent"],
                      activebackground=field, activeforeground=T["accent"], bd=0, relief="flat",
                      font=("Segoe UI", 10), padx=14, pady=6, cursor="hand2").pack(side="left")

        # footer: version + a link to the project (releases / news / star)
        foot = tk.Frame(body, bg=bg)
        foot.pack(fill="x", pady=(2, 12))
        tk.Label(foot, text=f"Claudometer v{config.APP_VERSION}", bg=bg, fg=T["faint"],
                 font=("Segoe UI", 8)).pack(side="left")
        gh = tk.Label(foot, text="View on GitHub  ↗", bg=bg, fg=T["accent"],
                      font=("Segoe UI", 8), cursor="hand2")
        gh.pack(side="right")
        gh.bind("<Button-1>", lambda e: _open_url(config.REPO_URL))

        if cfg.get("resume_auto") or cfg.get("resume_skip_permissions"):
            self._toggle_advanced()
        self._center(root)
        self.top.transient(root)
        self.top.lift()
        self.top.focus_force()

    @staticmethod
    def _safe_int(var, default):
        try:
            return int(var.get())
        except Exception:
            return default

    def _update_swatch(self):
        import re
        v = self.v_accent.get().strip()
        # only preview what _save() will accept (6-digit hex); else show default
        color = v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else self.T["accent"]
        try:
            self._swatch.configure(bg=color)
        except tk.TclError:
            pass

    def _max_body_h(self):
        """Tallest the scrolling area may be: the screen, less the header, the
        window chrome and a margin so the Save button is never off-screen."""
        try:
            screen_h = self.top.winfo_screenheight()
        except Exception:
            screen_h = 900
        header = self._hdr.height() if self._hdr else 0
        return max(240, screen_h - header - 140)

    def _resize_scroll(self):
        """Match the canvas to its content, scrolling only when it overflows."""
        try:
            needed = self._body_outer.winfo_reqheight()
            limit = self._max_body_h()
            self._canvas.configure(scrollregion=(0, 0, self.WIN_W, needed))
            self._canvas.configure(height=min(needed, limit))
            if needed > limit:
                self._vsb.pack(side="right", fill="y")
            else:
                self._vsb.pack_forget()
                self._canvas.yview_moveto(0)
        except Exception:
            pass

    def _on_wheel(self, event):
        try:
            if self._body_outer.winfo_reqheight() <= self._max_body_h():
                return          # nothing to scroll; don't swallow the event
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def _confirm_hooks(self):
        """Show the exact JSON before letting us edit Claude Code's settings.

        This is the one place Claudometer writes to a file that belongs to
        another application, so the user sees the literal change and can say
        no — and saying no puts the toggle straight back.
        """
        if not self.v_sess_hooks.get():
            return                       # switching OFF needs no permission
        if claude_hooks.interpreter() is None:
            messagebox.showinfo(
                "Instant alerts unavailable",
                "No Python interpreter was found to run the hook relay, so "
                "instant alerts aren't available in this build.\n\n"
                "Sessions still update about once a second on their own.",
                parent=self.top)
            self.v_sess_hooks.set(False)
            return
        if not claude_hooks.settings_readable():
            messagebox.showerror(
                "Can't read Claude's settings",
                f"{claude_hooks.settings_path()} exists but isn't valid JSON, "
                f"so Claudometer won't touch it.\n\nFix or move that file and "
                f"try again.",
                parent=self.top)
            self.v_sess_hooks.set(False)
            return
        ok = messagebox.askokcancel(
            "Edit Claude Code's settings?",
            "Claudometer will add these four hooks to\n"
            f"{claude_hooks.settings_path()}\n\n"
            f"{claude_hooks.preview()}\n\n"
            "They run a small relay that records when a session needs you or "
            "finishes. Your existing settings are backed up and left "
            "untouched, and turning this off removes exactly these entries.\n\n"
            "The relay is copied to ~/.claudometer, so updating or uninstalling "
            "Claudometer can't strand it — and if Claudometer stops running for "
            "a week it removes these hooks by itself.",
            parent=self.top)
        if not ok:
            self.v_sess_hooks.set(False)

    def _toggle_sessions(self):
        self._sess_open = not self._sess_open
        label = "Which alerts, and how many rows"
        if self._sess_open:
            self._sess_more.pack(fill="x", before=self._sess_anchor)
            self._sess_btn.configure(text=f"▾  {label}")
        else:
            self._sess_more.pack_forget()
            self._sess_btn.configure(text=f"▸  {label}")

    def _toggle_advanced(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            self._adv.pack(fill="x", before=self._fbar)
            self._adv_btn.configure(text="▾  Advanced — auto-resume ⚠")
        else:
            self._adv.pack_forget()
            self._adv_btn.configure(text="▸  Advanced — auto-resume ⚠")
        self.top.update_idletasks()
        self.top.geometry("")

    def _apply_login(self):
        # Start-on-login is an OS action, not a config value — apply it live and
        # reflect the real resulting state (revert the toggle if it didn't take).
        want = bool(self.v_login.get())
        actual = autostart.set_enabled(want)
        if actual != want:
            self.v_login.set(actual)
            messagebox.showwarning(
                "Start on login",
                "Couldn't update the login item.\nPlease try again.",
                parent=self.top)

    def _confirm_auto(self):
        if self.v_auto.get():
            ok = messagebox.askyesno(
                "Enable unattended auto-resume?",
                "Auto-resume runs Claude Code with NOBODY watching when your session "
                "resets — it can make changes on its own.\n\nEnable it?",
                parent=self.top, icon="warning")
            if not ok:
                self.v_auto.set(False)
        if not self.v_auto.get():
            self.v_skip.set(False)

    def _confirm_skip(self):
        if not self.v_skip.get():
            return
        if not self.v_auto.get():
            messagebox.showinfo("Auto-resume required",
                                "Turn on auto-resume first.", parent=self.top)
            self.v_skip.set(False)
            return
        ok = messagebox.askyesno(
            "Skip all permission prompts?",
            "This runs auto-resume with --dangerously-skip-permissions: Claude can "
            "edit files and run commands with NO approval.\n\nAre you sure?",
            parent=self.top, icon="warning")
        if not ok:
            self.v_skip.set(False)

    def _save(self):
        import re
        acc = self.v_accent.get().strip()
        if acc and not re.fullmatch(r"#[0-9a-fA-F]{6}", acc):
            messagebox.showerror("Invalid accent",
                                 "Accent must be a hex color like #d97757 (or blank).",
                                 parent=self.top)
            return
        metrics = [name for name, var in (("session", self.v_session), ("weekly", self.v_weekly))
                   if var.get()]
        if not metrics:
            messagebox.showerror("Pick a meter",
                                 "Choose at least one of Session / Weekly.", parent=self.top)
            return
        try:
            t1, t2 = int(self.v_t1.get()), int(self.v_t2.get())
        except Exception:
            t1, t2 = 80, 90
        thr = sorted({max(1, min(100, t)) for t in (t1, t2)})
        try:
            poll = max(60, min(300, int(self.v_poll.get())))
        except Exception:
            poll = 90
        try:
            maxturns = max(1, min(200, int(self.v_maxturns.get())))
        except Exception:
            maxturns = 30
        try:
            sess_rows = max(1, min(12, int(self.v_sess_rows.get())))
        except Exception:
            sess_rows = 6
        try:
            stuck_min = max(0, min(600, int(self.v_sess_stuck_min.get())))
        except Exception:
            stuck_min = 10
        cfg = dict(self._cfg)
        cfg.update({
            "poll": poll,
            "theme": self.v_theme.get() if self.v_theme.get() in ("auto", "light", "dark") else "auto",
            "metrics": metrics,
            "accent": acc or None,
            "alerts": bool(self.v_alerts.get()),
            "alert_thresholds": thr,
            "show_cost": bool(self.v_cost.get()),
            "hide_on_fullscreen": bool(self.v_fs.get()),
            "resume_notify": bool(self.v_notify.get()),
            "resume_auto": bool(self.v_auto.get()),
            "resume_skip_permissions": bool(self.v_skip.get() and self.v_auto.get()),
            "resume_prompt": self.v_prompt.get().strip() or self._cfg.get("resume_prompt")
                             or "Continue where you left off.",
            "resume_max_turns": maxturns,
            "sessions": bool(self.v_sessions.get()),
            "sessions_on_strip": bool(self.v_sess_strip.get()),
            "sessions_max_rows": sess_rows,
            "sessions_alerts": bool(self.v_sess_alerts.get()),
            "sessions_alert_on": [
                name for name, var in (("waiting", self.v_a_waiting),
                                       ("idle", self.v_a_idle),
                                       ("stuck", self.v_a_stuck),
                                       ("gone", self.v_a_gone)) if var.get()],
            "sessions_stuck_minutes": stuck_min,
            "sessions_quiet_foreground": bool(self.v_sess_quiet.get()),
            "sessions_hooks": bool(self.v_sess_hooks.get()),
            "sessions_hotkey": self.v_sess_hotkey.get().strip(),
            "sessions_answer": bool(self.v_sess_answer.get()),
        })
        try:
            self._on_apply(cfg)
        except Exception:
            _log_exc()
        self.close()

    def _center(self, root):
        self.top.update_idletasks()
        w, h = self.top.winfo_width(), self.top.winfo_height()
        try:
            # Center within the work area of the monitor the widget lives on, so
            # Settings opens on the same screen as the strip (not the primary).
            wl, wt, wr, wb = _monitor_workarea(
                root.winfo_rootx() + root.winfo_width() // 2,
                root.winfo_rooty() + root.winfo_height() // 2)
            x = wl + ((wr - wl) - w) // 2
            y = wt + max(20, ((wb - wt) - h) // 3)
            self.top.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _demo(self):
        self.close()
        if self._on_demo:
            self._on_demo()

    def focus(self):
        self.top.lift()
        self.top.focus_force()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.top.destroy()
        except Exception:
            pass
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Taskbar strip (image-based)
# --------------------------------------------------------------------------- #
class BarWidget:
    def __init__(self, demo=False):
        self._demo = False            # toggled on/off in place by _enter/_exit_demo
        self._start_in_demo = demo    # `app.py demo` → auto-enter after startup
        self._demo_after = None
        _set_dpi_aware()
        self.root = tk.Tk()
        self.root.title("Claudometer")
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self._card = not _IS_WIN   # off-Windows: float a rounded card (no taskbar to blend into)
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack()

        cfg = settings.load()
        self._poll = cfg["poll"]
        self._metrics = tuple(cfg["metrics"])
        self._forced_theme = cfg["theme"] if cfg["theme"] in ("light", "dark") else None
        self._alerts_on = cfg["alerts"]
        self._thresholds = cfg["alert_thresholds"]
        self._show_cost = cfg["show_cost"]
        self._hide_on_fullscreen = cfg["hide_on_fullscreen"]
        self._orig_accents = {k: render.THEMES[k]["accent"] for k in render.THEMES}
        self._accent = cfg["accent"]
        if cfg["accent"]:
            for _t in render.THEMES.values():
                _t["accent"] = cfg["accent"]
        self._settings_win = None
        self._alerted = {"session": set(), "weekly": set()}
        self._toast = None
        self._first_alert = {"session": True, "weekly": True}

        self._resume_notify = cfg["resume_notify"]
        self._resume_auto = cfg["resume_auto"]
        self._resume_prompt = cfg["resume_prompt"]
        self._resume_skip_perms = cfg["resume_skip_permissions"]
        self._resume_max_turns = cfg["resume_max_turns"]
        self._resume_state = "idle"  # idle | capped
        self._resume_toast = None
        self._resume_retry_after = None
        self._resume_fire_tries = 0
        self._resume_cooldown_until = 0.0  # monotonic time; blocks repeat auto-resume
        self._poll_seq = 0        # bumped by the poll thread on each new result
        self._processed_seq = 0   # last poll processed for alerts/resume (main thread)

        self._theme = self._forced_theme or "light"
        if self._card:  # transparent window so the rounded card floats (macOS/Linux)
            self.canvas.configure(bg=_make_transparent(self.root, self._theme))
        self._bg_hex = None
        self._state = core.PollState()
        self._disp = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._sig = None
        self._need_autoplace = self._load_pos() is None
        self._topmost_ticks = 0
        self._bg_ticks = 0
        self._popover = None
        self._pop_closed_at = 0.0
        self._photo = None
        self._hidden = False

        # Live Claude Code sessions. These change far faster than the usage
        # numbers (90s poll), so they refresh on the 1s UI tick instead —
        # a snapshot costs well under a millisecond. Enrichment (titles, tools)
        # reads transcript tails and is ~25x dearer, so it runs on its own
        # slower cadence and its results are merged in by session id.
        self._sessions_on = cfg["sessions"]
        self._sessions_on_strip = cfg["sessions_on_strip"]
        self._sessions_max_rows = cfg["sessions_max_rows"]
        self._sess_tracker = sessions_core.SessionTracker()
        self._sess_disp = {}
        self._sess_extra = {}          # session_id -> enrichment fields
        self._sess_enriched_at = 0.0
        self._sess_known_ids = frozenset()
        self._sess_alerts_on = cfg["sessions_alerts"]
        self._sess_alert_on = tuple(cfg["sessions_alert_on"])
        self._sess_quiet_fg = cfg["sessions_quiet_foreground"]
        self._sess_stuck = sessions_core.StuckWatcher(cfg["sessions_stuck_minutes"])
        # The very first tick sees every running session as newly appeared; that
        # is startup, not news, so alerting stays off until after it.
        self._sess_seeded = False
        self._flash = False            # strip attention pulse
        self._sess_sticky_pid = 0      # session the sticky toast belongs to
        self._sess_answer_on = cfg["sessions_answer"]
        self._answer_win = None
        self._hotkey = None
        if cfg["sessions_hotkey"]:
            self._hotkey = hotkeys.Hotkey(cfg["sessions_hotkey"],
                                          self._jump_to_blocked)
        # Hooks: opt-in, and only trusted while the config says they're on —
        # otherwise a stale settings.json entry could keep feeding us events.
        self._sess_hooks_on = cfg["sessions_hooks"] and self._sessions_on
        self._sess_hook_notes = {}     # session_id -> latest hook message
        self._sess_heartbeat_at = 0.0
        if self._sess_hooks_on:
            # Reconcile on every start. The config saying "on" IS the prior
            # consent, and an install can go stale on its own: after the app
            # updates, the recorded relay path may no longer exist, which would
            # leave Claude Code spawning a failing command on every event.
            self._apply_hooks(True)
        else:
            # Not merely "don't read them" — take any registration from a
            # previous run back out, or it keeps firing into a queue nobody
            # drains for as long as the setting stays off.
            self._apply_hooks(False)

        self._apply_bg((233, 238, 243))  # provisional; refined by sampling
        self._place_initial()
        self._bind_events()
        self._draw(core.status_display(core.Status.NO_DATA))
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self._hotkey is not None and self._hotkey.registered:
            self.root.after(self.HOTKEY_POLL_MS, self._hotkey_loop)
        self.root.after(400, self._refresh_ui)
        if self._start_in_demo:  # `app.py demo` — drop straight into the tour
            self.root.after(500, self._enter_demo)

    # -- background matching --------------------------------------------- #
    def _apply_bg(self, rgb):
        hexc = "#%02x%02x%02x" % tuple(rgb[:3])
        if hexc == self._bg_hex:
            return
        self._bg_hex = hexc
        self._theme = self._forced_theme or _theme_for_bg(rgb)
        if not self._card:  # card mode keeps its transparent bg (no taskbar to match)
            self.root.configure(bg=hexc)
            self.canvas.configure(bg=hexc)
        self._sig = None

    def _sample_bg(self):
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            w = self.root.winfo_width()
            sw, sh = _screen_size()
            sx = x + w + 14
            if sx > sw - 3:
                sx = x - 14
            sx = min(max(sx, 0), sw - 1)
            sy = min(max(y + self.root.winfo_height() // 2, 2), sh - 2)
            return _get_pixel(sx, sy)
        except Exception:
            return None

    # -- geometry --------------------------------------------------------- #
    def _place_initial(self):
        pos = self._load_pos()
        if pos and not _on_a_monitor(*pos):
            pos = None          # that display is gone; fall back to auto-place
            self._need_autoplace = True
        if pos:
            self.root.geometry(f"+{pos[0]}+{pos[1]}")
        else:
            sw, sh = _screen_size()
            self.root.geometry(f"+{sw - 250 - 200}+{sh - TASKBAR_H + 9}")

    def _load_pos(self):
        try:
            # utf-8-sig: anything that rewrites this file on Windows is liable
            # to leave a BOM, and a BOM makes json.loads fail — the remembered
            # position is then silently discarded and the widget jumps.
            d = json.loads(POS_FILE.read_text(encoding="utf-8-sig"))
            return int(d["x"]), int(d["y"])
        except Exception:
            return None

    def _save_pos(self):
        try:
            POS_FILE.write_text(
                json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}),
                encoding="utf-8",
            )
        except Exception:
            pass

    # -- events ----------------------------------------------------------- #
    def _bind_events(self):
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._popup_menu)

    def _press(self, e):
        self._dx, self._dy = e.x, e.y
        self._prx, self._pry = e.x_root, e.y_root
        self._moved = False
        self._need_autoplace = False

    def _motion(self, e):
        if not self._moved and (abs(e.x_root - self._prx) > DRAG_THRESHOLD
                                or abs(e.y_root - self._pry) > DRAG_THRESHOLD):
            self._moved = True
        if self._moved:
            self.root.geometry(f"+{self.root.winfo_x() + e.x - self._dx}"
                               f"+{self.root.winfo_y() + e.y - self._dy}")

    def _release(self, e):
        if self._moved:
            # Don't persist somewhere it can't be seen — a drag that ends past
            # an edge would otherwise be remembered and reapplied on restart.
            if not _on_a_monitor(self.root.winfo_x(), self.root.winfo_y()):
                self._need_autoplace = True
                self._sig = None
                return
            self._save_pos()
        else:
            self._toggle_popover()

    def _get_disp(self):
        with self._lock:
            disp = self._disp
        return self._with_sessions(disp)

    def _toggle_popover(self):
        if self._popover is not None:
            self._popover.close()
            return
        if time.monotonic() - self._pop_closed_at < 0.35:
            return
        # winfo_rootx/rooty = the strip's absolute screen coords; resolve the
        # monitor from the strip's CENTER so it's correct right at a boundary.
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        work = _monitor_workarea(rx + rw // 2, ry + rh // 2)
        self._popover = Popover(
            self.root, self._theme, self._get_disp,
            rx, ry, ry + rh, work,
            self._refresh_now, self._quit, self._open_settings, self._on_popover_closed,
            on_session=self._act_on_session, on_session_menu=self._session_menu,
        )

    def _on_popover_closed(self):
        self._popover = None
        self._pop_closed_at = time.monotonic()

    # -- session actions --------------------------------------------------- #
    def _row_for_pid(self, pid):
        for row in (self._sess_disp or {}).get("sessions_rows") or []:
            if row.get("pid") == pid:
                return row
        return None

    def _act_on_session(self, row):
        """The one thing to do about a session.

        Blocked and answerable — open the answer window, because replying is
        the point and it doesn't disturb what you're doing. Anything else —
        take you to its terminal.
        """
        if (self._sess_answer_on and row.get("status") == sessions_core.WAITING
                and console_send.can_send()):
            self._open_answer(row)
        else:
            self._focus_session(row)

    def _focus_pid(self, pid):
        """Raise a session's terminal by pid. Shared by the toast, the hotkey
        and the row menu so they can't drift apart."""
        return self._focus_session({"pid": pid})

    def _live_question(self, row):
        """Refresh a row with what the session is asking *right now*.

        Read at the moment the window opens rather than on every tick: it costs
        a console attach, and it is only ever needed here.
        """
        try:
            screen = console_send.read_screen(row.get("pid"))
            prompt = sessions_core.parse_console_prompt(screen)
            if prompt and (prompt["question"] or prompt["options"]):
                return dict(row,
                            question=prompt["question"] or row.get("question"),
                            options=list(prompt["options"]),
                            selected=prompt["selected"])
        except Exception:
            _log_exc()
        return row

    def _open_answer(self, row):
        row = self._live_question(row)
        if self._answer_win is not None:
            try:
                self._answer_win.close()
            except Exception:
                pass
        try:
            self._answer_win = AnswerWindow(
                self.root, self._theme, row, self._send_answer,
                self._focus_session, on_close=self._clear_answer)
        except Exception:
            _log_exc()
            self._focus_session(row)      # fall back to the old behaviour

    def _clear_answer(self):
        self._answer_win = None

    def _send_answer(self, row, text, submit=True):
        """Deliver an answer to that exact session. Returns (ok, error).

        Re-checks that the session is still live and still waiting: the answer
        was composed for a question that may have been dealt with in the
        meantime, and typing it into whatever the pid became would be worse
        than not sending it.
        """
        body = console_send.clean(text)
        if not body and not submit:
            return False, "Type something first."
        pid = row.get("pid")
        current = self._row_for_pid(pid)
        if current is None:
            return False, "That session has gone."
        if current.get("status") != sessions_core.WAITING:
            return False, "It isn't waiting any more."
        ok, error = console_send.send_text(pid, body, submit=submit)
        if ok:
            self._sess_sticky_pid = 0
            if self._toast is not None:
                try:
                    self._toast.close()
                except Exception:
                    pass
        return ok, error

    def _jump_to_blocked(self):
        """Answer, or go to, whichever session needs you — the hotkey's job."""
        pid = (self._sess_disp or {}).get("sessions_blocked_pid") or 0
        if not pid:
            self._notify_session("No session is waiting on you right now.")
            return
        row = self._row_for_pid(pid) or {"pid": pid}
        self._act_on_session(row)

    def _focus_session(self, row):
        """Bring a session's terminal to the front.

        This raises the WINDOW, not the tab — several sessions routinely share
        one terminal and nothing in the process tree tells their tabs apart. So
        it takes you to the right terminal and stops there rather than pretending
        to land on the exact session.
        """
        try:
            pid = row.get("pid")
            parents = focus.parent_map()
            if focus.window_for_pid(pid, parents) is None:
                self._notify_session("No terminal window found for that session.")
                return
            if focus.raise_window(pid, parents):
                if self._popover is not None:
                    self._popover.close()   # get out of the way of what we raised
                return
            # raise_window reports what actually happened rather than what it
            # asked for, so reaching here means Windows refused the switch.
            self._notify_session("Windows wouldn't bring that terminal forward.")
        except Exception:
            _log_exc()
            self._notify_session("Couldn't open that session's terminal.")

    def _notify_session(self, message):
        try:
            self._show_toast(None, "Live sessions", message, "grey")
        except Exception:
            _log_exc()

    def _copy(self, text, what):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(text))
            self.root.update_idletasks()     # survive our own exit
            self._notify_session(f"{what} copied to the clipboard.")
        except Exception:
            _log_exc()

    def _open_path(self, path, what):
        try:
            if not path or not Path(path).exists():
                self._notify_session(f"That {what} no longer exists.")
                return
            if _IS_WIN:
                os.startfile(str(path))          # noqa: S606 - user-initiated
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            _log_exc()
            self._notify_session(f"Couldn't open that {what}.")

    def _open_transcript(self, row):
        path = sessions_core.transcript_path(row.get("session_id"),
                                             row.get("cwd"))
        if path is None:
            self._notify_session("No transcript found for that session.")
            return
        self._open_path(path, "transcript")

    def _reply_and_go(self, row, text):
        """Put a reply on the clipboard, then jump to the terminal.

        Claudometer can't answer the prompt itself — a running session exposes
        no channel to send into, and typing blind would land in whatever tab
        happens to be in front. This gets you there with the answer ready to
        paste, which is the safe half of the job.
        """
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(text))
            self.root.update_idletasks()
        except Exception:
            _log_exc()
        self._focus_session(row)

    def _quick_answer(self, row, text):
        ok, error = self._send_answer(row, text)
        if not ok:
            self._notify_session(error or "Couldn't send that.")

    def _custom_reply(self, row):
        from tkinter import simpledialog

        text = simpledialog.askstring(
            "Reply and go",
            f"Copy a reply for {(row.get('label') or 'this session')[:40]}:",
            parent=self.root)
        if text:
            self._reply_and_go(row, text)

    def _session_menu(self, row, x_root, y_root):
        """Right-click actions for one session row."""
        menu = tk.Menu(self.root, tearoff=0)
        label = (row.get("label") or "session")[:44]
        menu.add_command(label=f"Go to {label}",
                         command=lambda: self._focus_session(row))
        if row.get("status") == sessions_core.WAITING:
            if self._sess_answer_on and console_send.can_send():
                menu.add_command(label="Answer without leaving here…",
                                 command=lambda: self._open_answer(row))
                reply = tk.Menu(menu, tearoff=0)
                for text in ("yes", "no", "continue"):
                    reply.add_command(
                        label=f'Send "{text}"',
                        command=lambda t=text: self._quick_answer(row, t))
                menu.add_cascade(label="Send", menu=reply)
            else:
                # No console channel here (or switched off) — the best we can
                # do is arrive with the answer ready to paste.
                reply = tk.Menu(menu, tearoff=0)
                for text in ("yes", "no", "continue"):
                    reply.add_command(
                        label=f'Copy "{text}" and go',
                        command=lambda t=text: self._reply_and_go(row, t))
                reply.add_separator()
                reply.add_command(label="Custom reply…",
                                  command=lambda: self._custom_reply(row))
                menu.add_cascade(label="Reply and go", menu=reply)
        menu.add_separator()
        menu.add_command(label="Open project folder",
                         command=lambda: self._open_path(row.get("cwd"), "folder"))
        menu.add_command(label="Open transcript",
                         command=lambda: self._open_transcript(row))
        menu.add_separator()
        menu.add_command(label="Copy session ID",
                         command=lambda: self._copy(row.get("session_id"),
                                                    "Session ID"))
        menu.add_command(label="Copy project path",
                         command=lambda: self._copy(row.get("cwd"), "Project path"))
        try:
            menu.tk_popup(int(x_root), int(y_root))
        finally:
            menu.grab_release()

    # -- threshold alerts ------------------------------------------------- #
    def _maybe_alert(self, disp):
        """Fire a toast when session/weekly crosses a configured threshold upward.
        Runs on the main thread. Skipped while hidden (fullscreen) so a crossing
        isn't marked-alerted-but-suppressed — it's re-detected on unhide."""
        if not (self._alerts_on or self._demo) or self._hidden:
            return
        thresholds = [80, 90] if self._demo else self._thresholds  # tour uses fixed marks
        for which in ("session", "weekly"):
            pct = disp.get(f"{which}_pct")
            if pct is None:
                continue
            reached = {t for t in thresholds if pct >= t}
            new = reached - self._alerted[which]
            # Sticky: keep a threshold "alerted" until pct falls well below it, so
            # boundary jitter can't re-fire; a real reset (big drop) clears it.
            self._alerted[which] = {t for t in (self._alerted[which] | reached) if pct >= t - 5}
            if self._first_alert[which]:  # seed per-window on its first data, no alert
                self._first_alert[which] = False
                continue
            if new:
                self._queue_toast(which, pct, disp)

    def _queue_toast(self, which, pct, disp):
        color = disp.get(f"{which}_color", "amber")
        if which == "session":
            label, reset = "Session", render._fmt_left(disp.get("session_resets_at"))
        else:
            label, reset = "Weekly", render._fmt_at(disp.get("weekly_resets_at"))
        title = f"{label} usage at {pct}%"
        sub = reset or "you're approaching your limit"
        self._show_toast(pct, title, sub, color)

    def _clear_toast(self):
        self._toast = None

    def _show_toast(self, pct, title, subtitle, color, duration=6500,
                    on_click=None):
        if self._hidden:
            return
        try:
            if self._toast is not None:
                self._toast.close()
            self._toast = Toast(self.root, self._theme, pct, title, subtitle, color,
                                duration=duration, on_close=self._clear_toast,
                                on_click=on_click)
        except Exception:
            _log_exc()

    # -- attention ---------------------------------------------------------- #
    #: Half-cycles of the strip pulse, and how long each lasts.
    PULSE_STEPS, PULSE_MS = 6, 180

    def _pulse_strip(self, step=0):
        """Flash the strip when a session starts needing you.

        The widget is overrideredirect, so it has no taskbar button to flash —
        pulsing the strip itself is the equivalent that's actually visible,
        since the strip IS what sits on the taskbar.
        """
        if self._hidden:
            return
        self._flash = (step % 2 == 0)
        self._sig = None                 # force the next tick to repaint
        try:
            with self._lock:
                disp = self._disp
            if disp is not None:
                merged = self._with_sessions(disp)
                # Only the status dot pulses. The strip's background is sampled
                # from the taskbar so it blends in — repainting that would make
                # the whole widget flash, which is not what it's for.
                self._draw(dict(merged, _pulse=self._flash))
        except Exception:
            _log_exc()
            return
        if step + 1 < self.PULSE_STEPS:
            self.root.after(self.PULSE_MS, self._pulse_strip, step + 1)
        else:
            self._flash = False
            self._sig = None

    # -- resume on reset -------------------------------------------------- #
    def _track_resume(self, disp):
        """Watch the 5-hour session: mark it capped at 100%, and fire the resume
        flow once utilization drops back below the cap (you've regained headroom).

        Utilization is the ground truth for 'can I use Claude again' — the moment
        it's under 100% you're no longer rate-limited. We deliberately DON'T gate
        on session_resets_at, which is only a projection that drifts as the
        rolling window slides. For a capped-and-waiting user, utilization only
        decreases, so the <=90 crossing is reliably observed. Main thread."""
        if not (self._resume_notify or self._resume_auto or self._demo):
            return
        if self._resume_toast is not None or self._resume_retry_after is not None:
            return  # a resume is already in flight — don't re-detect or re-fire
        sp = disp.get("session_pct")
        if sp is None:
            return
        if self._resume_state == "idle":
            if sp >= 100:  # session limit reached
                self._resume_state = "capped"
        elif self._resume_state == "capped" and sp <= 90:  # headroom regained
            self._resume_state = "idle"
            self._fire_resume()

    def _clear_resume_toast(self):
        self._resume_toast = None

    def _fire_resume(self):
        if self._demo:  # demo: show the notification, but never touch a real session
            self._show_demo_resume()
            return
        # Resolve the session to resume lazily, at fire time — the most recent
        # session reflects the user's latest work better than a hours-old capture.
        if self._resume_retry_after is not None:  # replace any pending retry
            try:
                self.root.after_cancel(self._resume_retry_after)
            except Exception:
                pass
            self._resume_retry_after = None
        if self._hidden:  # over fullscreen — retry shortly, don't lose it
            self._resume_retry_after = self.root.after(15000, self._fire_resume)
            return
        snap = resume.last_session()
        if not snap or not snap.get("cwd") or not Path(snap["cwd"]).exists():
            # transient (sessions not written yet / dir gone) — retry a few times
            self._resume_fire_tries += 1
            if self._resume_fire_tries <= 5:
                self._resume_retry_after = self.root.after(30000, self._fire_resume)
            else:
                self._resume_fire_tries = 0
            return
        self._resume_fire_tries = 0
        cwd, sid = snap["cwd"], snap["session_id"]
        if self._resume_auto and time.monotonic() < self._resume_cooldown_until:
            return  # recently auto-resumed — don't launch another unattended run
        try:
            if self._resume_toast is not None:
                self._resume_toast.close()
            if self._resume_auto:
                self._resume_toast = ResumeToast(
                    self.root, self._theme, "Session reset — auto-resuming", "",
                    "Cancel", on_click=None, countdown_s=20,
                    on_expire=lambda: self._do_auto_resume(cwd, sid),
                    on_close=self._clear_resume_toast)
            elif self._resume_notify:
                self._resume_toast = ResumeToast(
                    self.root, self._theme, "Session limit reset",
                    "Click to resume where you left off", "Resume",
                    on_click=lambda: resume.open_terminal(cwd, sid),
                    timeout_ms=180000, on_close=self._clear_resume_toast)
        except Exception:
            _log_exc()

    def _do_auto_resume(self, cwd, sid):
        try:
            log = resume.run_auto(cwd, sid, self._resume_prompt,
                                  skip_permissions=self._resume_skip_perms,
                                  max_turns=self._resume_max_turns)
            # cooldown: don't launch another unattended resume for a while, even if
            # the just-launched job pushes usage back to the cap and it resets.
            self._resume_cooldown_until = time.monotonic() + 1800
            if log:
                self._resume_toast = ResumeToast(
                    self.root, self._theme, "Auto-resumed session",
                    "running headless · check the log", "OK", on_click=None,
                    timeout_ms=9000, on_close=self._clear_resume_toast)
            else:  # run_auto failed — offer a supervised fallback
                self._resume_toast = ResumeToast(
                    self.root, self._theme, "Auto-resume failed",
                    "click to resume manually", "Resume",
                    on_click=lambda: resume.open_terminal(cwd, sid),
                    timeout_ms=30000, on_close=self._clear_resume_toast)
        except Exception:
            _log_exc()

    # -- demo / self-test tour ------------------------------------------- #
    # A scripted, fully offline sequence that drives the REAL alert/resume/render
    # code paths through every state — so you (and new users) can verify each
    # feature in ~50s instead of waiting for real usage to reach those conditions.
    #: Plausible work for the tour's session list. Enough of them to show the
    #: dot row overflowing.
    _DEMO_SESSIONS = [
        ("Ship the release pipeline", "claude-widget"),
        ("Refactor the payment retries", "checkout-api"),
        ("Draft the migration plan", "docs-site"),
        ("Explore the caching idea", "proxy-passer"),
        ("Chase the flaky login test", "web-app"),
        ("Port the parser to Rust", "tokenizer"),
        ("Write the upgrade notes", "handbook"),
        ("Trim the docker image", "infra"),
        ("Add the retry budget", "gateway"),
        ("Rename the metrics", "telemetry"),
        ("Tidy the changelog", "release-notes"),
    ]

    def _demo_session_set(self, spec):
        """Build synthetic Sessions for one scene.

        *spec* is a list of ``(index, status, waiting_for)``. Real Session
        objects rather than pre-baked dicts, so the tour runs through the same
        tracker, formatter and alert path as live data — if that path breaks,
        the demo breaks with it.
        """
        now = sessions_core.now_ms()
        out = []
        for slot, (index, status, reason) in enumerate(spec):
            title, project = self._DEMO_SESSIONS[index % len(self._DEMO_SESSIONS)]
            out.append(sessions_core.Session(
                session_id=f"demo-{index}", pid=900000 + index,
                cwd=f"/demo/{project}", name=f"{project}-{index}",
                title=title, status=status, waiting_for=reason,
                status_updated_at=now - (45 + slot * 173) * 1000))
        return out

    def _demo_timeline(self):
        now = datetime.now(timezone.utc)

        def step(sp, sc, wp, wc, mins, **extra):
            d = {"_demo": True, "plan": "DEMO",
                 "session_pct": sp, "session_color": sc,
                 "session_resets_at": now + timedelta(minutes=mins),
                 "weekly_pct": wp, "weekly_color": wc,
                 "weekly_resets_at": now + timedelta(days=3),
                 "model_rows": [{"label": "Fable", "pct": 4, "color": "green"}]}
            d.update(extra)
            return d

        def status(session, color, **extra):  # non-usage state (offline / 429)
            d = {"_demo": True, "plan": None, "session": session,
                 "session_pct": None, "weekly_pct": None, "face_color": color,
                 "session_color": color, "weekly_color": color, "model_rows": []}
            d.update(extra)
            return d

        W, B, S, I = (sessions_core.WAITING, sessions_core.BUSY,
                      sessions_core.SHELL, sessions_core.IDLE)

        def live(*spec):
            return {"_sessions": self._demo_session_set(list(spec))}

        # The scripted list only ever GROWS, apart from one deliberate ending.
        # Shrinking it would report a mass exodus the viewer never caused —
        # jumping from ten sessions back to two once produced a bewildering
        # "9 session updates" instead of the "Needs you" the scene was for.
        working = live((0, B, ""), (1, S, ""), (2, I, ""))
        blocked = live((0, W, "input needed"), (1, B, ""), (2, I, ""))
        answered = live((0, B, ""), (1, B, ""), (2, I, ""))
        finished = live((0, I, ""), (1, I, ""), (2, I, ""))
        ended = live((0, I, ""), (1, B, ""))          # session 2 has ended
        crowd_spec = [(0, I, ""), (1, B, "")] + [(i, B, "") for i in range(3, 11)]
        crowd = live(*crowd_spec)
        crowd_blocked = live(*[(i, W, "permission needed") if i == 5 else (i, s, r)
                               for i, s, r in crowd_spec])
        crowd_answered = live(*[(i, S, "") if i == 5 else (i, s, r)
                                for i, s, r in crowd_spec])

        return [
            # The scenes carry a session list as well as usage, so the dot row
            # on the strip is live throughout the tour rather than a separate
            # act tacked on the end.
            # Session moments are deliberately placed on scenes where no usage
            # threshold is crossed. Only one toast exists at a time, so pairing
            # them buries the session alert under the usage one — which is
            # exactly what happened the first time this was built.
            (step(20, "green", 8, "green", 180, **working), 4.0),   # 1  comfortable — green
            (step(62, "amber", 15, "green", 120, **blocked), 5.5),  # 2  a session BLOCKS (no usage alert)
            (step(84, "red", 22, "green", 40, **answered), 4.5),    # 3  session crosses 80% → alert
            (step(93, "red", 24, "green", 16, **answered), 4.5),    # 4  session crosses 90% → alert
            (step(48, "amber", 88, "red", 90, **answered), 4.5),    # 5  WEEKLY crosses 80% → alert
            (step(100, "red", 30, "amber", 6, **answered), 4.5),    # 6  100% → "limit reached"
            (step(85, "red", 30, "amber", 300, **finished), 6.0),   # 7  resume (Tier 1) + two finish
            (step(40, "green", 30, "amber", 240, cost_tokens=2_450_000,
                  cost_usd=8.74, **ended), 5.0),                    # 8  cost line + one session ends
            (status("usage limit reached", "red", **crowd), 4.5),   # 9  rate-limited; the list grows
            (step(100, "red", 30, "amber", 5, **crowd), 4.5),       # 10 limit reached again (capped)
            (step(88, "red", 30, "amber", 260, **crowd_blocked), 6.0),   # 11 resume (Tier 2) + one blocks
            (status("offline (demo)", "grey", **crowd_answered), 4.0),   # 12 offline; the block is answered
        ]

    def _toggle_demo(self):
        self._exit_demo() if self._demo else self._enter_demo()

    def _enter_demo(self):
        # Switch THIS widget into the tour in place — no second window. The poll
        # thread stops publishing while _demo is set; the scripted driver owns the
        # display until you exit.
        if self._demo:
            return
        self._demo = True
        self._demo_resume_n = 0
        self._reset_alert_resume_state()
        self._reset_demo_sessions()
        self._demo_seq = self._demo_timeline()
        self._demo_i = 0
        self._demo_tick()

    def _exit_demo(self):
        if not self._demo:
            return
        self._demo = False
        if self._demo_after is not None:
            try:
                self.root.after_cancel(self._demo_after)
            except Exception:
                pass
            self._demo_after = None
        for t in (self._toast, self._resume_toast):
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
        self._toast = self._resume_toast = None
        self._reset_alert_resume_state()
        # Hand the session list back to real data, with no scripted history
        # left to mistake for it.
        self._reset_demo_sessions()
        self._sig = None    # force a redraw once real data arrives
        self._wake.set()    # wake the poll thread to fetch + publish now

    def _reset_demo_sessions(self):
        """Clear the session view on the way into and out of the tour."""
        self._sess_tracker.reset()
        self._sess_stuck.reset()
        self._sess_disp = {}
        self._sess_extra = {}
        self._sess_known_ids = frozenset()
        self._sess_sticky_pid = 0
        self._flash = False
        # The tour's first scene is a baseline, not news — same rule as startup.
        self._sess_seeded = False

    def _reset_alert_resume_state(self):
        self._alerted = {"session": set(), "weekly": set()}
        self._first_alert = {"session": True, "weekly": True}
        self._resume_state = "idle"
        if self._resume_retry_after is not None:
            try:
                self.root.after_cancel(self._resume_retry_after)
            except Exception:
                pass
            self._resume_retry_after = None

    def _demo_sessions_tick(self, live):
        """Run one scene's sessions through the real pipeline.

        Deliberately the same tracker, formatter and alert path the live data
        uses — so the tour shows the actual behaviour (blocked-first ordering,
        a sticky toast, a coalesced summary, the strip pulse) rather than a
        staged imitation that could drift away from it.
        """
        try:
            events = self._sess_tracker.update(live)
            self._sess_disp = sessions_core.format_sessions(
                live, max_rows=self._sessions_max_rows)
            self._sess_disp["sessions_recent"] = sessions_core.format_recent(
                self._sess_tracker.recent)
            self._retire_sticky_toast(live)
            self._session_alerts(events, live)
        except Exception:
            _log_exc()

    def _demo_tick(self):
        if not self._demo:
            return  # exited — stop the loop
        scene, hold = self._demo_seq[self._demo_i % len(self._demo_seq)]
        live = scene.get("_sessions")
        disp = {k: v for k, v in scene.items() if k != "_sessions"}
        with self._lock:
            self._disp = dict(disp)
            self._poll_seq += 1
        if live is not None:
            self._demo_sessions_tick(live)
        else:
            self._sess_disp = {}      # scenes with no session list show none
        self._demo_i += 1
        try:
            self._demo_after = self.root.after(int(hold * 1000), self._demo_tick)
        except Exception:
            pass  # window closed

    def _show_demo_resume(self):
        # Alternate the two resume tiers so the tour shows both over its run.
        self._demo_resume_n = getattr(self, "_demo_resume_n", 0) + 1
        try:
            if self._resume_toast is not None:
                self._resume_toast.close()
            if self._demo_resume_n % 2 == 0:   # Tier 2 — unattended auto-resume
                self._resume_toast = ResumeToast(
                    self.root, self._theme, "Session reset — auto-resuming (demo)",
                    "resuming in 6s · click to cancel", "Cancel",
                    on_click=None, countdown_s=6, on_expire=lambda: None,
                    on_close=self._clear_resume_toast)
            else:                               # Tier 1 — notify + one click
                self._resume_toast = ResumeToast(
                    self.root, self._theme, "Session limit reset (demo)",
                    "This is where you'd click Resume to continue", "Resume",
                    on_click=lambda: None, timeout_ms=8000, on_close=self._clear_resume_toast)
        except Exception:
            _log_exc()

    def _popup_menu(self, e):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Details…", command=self._toggle_popover)
        menu.add_command(label="Open Settings…", command=self._open_settings)
        menu.add_command(label="Refresh now", command=self._refresh_now)
        menu.add_command(label=("◼  Exit demo" if self._demo else "▶  Try a demo"),
                         command=self._toggle_demo)
        menu.add_separator()
        menu.add_command(label="Check for Updates…", command=self._check_updates)
        menu.add_command(label="View on GitHub", command=lambda: _open_url(config.REPO_URL))
        menu.add_command(label="Quit", command=self._quit)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _refresh_now(self):
        self._wake.set()

    def _check_updates(self):
        # Network call off the UI thread; present the result back on main.
        def worker():
            res = updates.check()
            try:
                self.root.after(0, lambda: self._show_update_result(res))
            except Exception:
                pass  # window closed mid-check
        threading.Thread(target=worker, daemon=True).start()

    def _show_update_result(self, res):
        if res["status"] == "update":
            if messagebox.askyesno(
                "Claudometer",
                f"A new version is available: {res['latest']}\n"
                f"You have {res['current']}.\n\nOpen the download page?"):
                _open_url(res["url"])
        elif res["status"] == "current":
            messagebox.showinfo(
                "Claudometer", f"You're on the latest version ({res['current']}).")
        else:
            messagebox.showwarning(
                "Claudometer",
                "Couldn't check for updates right now.\nPlease try again later.")

    def _quit(self):
        self._save_pos()
        self.root.destroy()

    # -- settings --------------------------------------------------------- #
    def _open_settings(self):
        if self._settings_win is not None:
            try:
                self._settings_win.focus()
                return
            except Exception:
                self._settings_win = None
        try:
            self._settings_win = SettingsWindow(
                self.root, self._theme, self._current_cfg(),
                on_apply=self._apply_settings, on_close=self._on_settings_closed,
                on_demo=self._toggle_demo, demo_active=self._demo)
        except Exception:
            _log_exc()
            self._settings_win = None

    def _on_settings_closed(self):
        self._settings_win = None

    def _current_cfg(self):
        return {
            "poll": self._poll,
            "theme": self._forced_theme or "auto",
            "metrics": list(self._metrics),
            "hide_on_fullscreen": self._hide_on_fullscreen,
            "alerts": self._alerts_on,
            "alert_thresholds": list(self._thresholds),
            "show_cost": self._show_cost,
            "accent": self._accent,
            "resume_notify": self._resume_notify,
            "resume_auto": self._resume_auto,
            "resume_prompt": self._resume_prompt,
            "resume_skip_permissions": self._resume_skip_perms,
            "resume_max_turns": self._resume_max_turns,
            "sessions": self._sessions_on,
            "sessions_max_rows": self._sessions_max_rows,
            "sessions_on_strip": self._sessions_on_strip,
        }

    def _apply_settings(self, cfg):
        """Apply new settings live (no restart) and persist them to disk."""
        alerts_changed = (self._alerts_on != cfg["alerts"]
                          or self._thresholds != cfg["alert_thresholds"])
        auto_was_on = self._resume_auto
        self._poll = cfg["poll"]
        self._metrics = tuple(cfg["metrics"])
        self._alerts_on = cfg["alerts"]
        self._thresholds = cfg["alert_thresholds"]
        self._show_cost = cfg["show_cost"]
        self._hide_on_fullscreen = cfg["hide_on_fullscreen"]
        self._resume_notify = cfg["resume_notify"]
        self._resume_auto = cfg["resume_auto"]
        self._resume_prompt = cfg["resume_prompt"]
        self._resume_skip_perms = cfg["resume_skip_permissions"]
        self._resume_max_turns = cfg["resume_max_turns"]
        sessions_was_on = self._sessions_on
        self._sessions_on = cfg.get("sessions", self._sessions_on)
        self._sessions_on_strip = cfg.get("sessions_on_strip", self._sessions_on_strip)
        self._sessions_max_rows = cfg.get("sessions_max_rows", self._sessions_max_rows)
        self._sess_alerts_on = cfg.get("sessions_alerts", self._sess_alerts_on)
        self._sess_alert_on = tuple(cfg.get("sessions_alert_on",
                                            self._sess_alert_on))
        self._sess_quiet_fg = cfg.get("sessions_quiet_foreground",
                                      self._sess_quiet_fg)
        self._sess_stuck.minutes = cfg.get("sessions_stuck_minutes",
                                           self._sess_stuck.minutes)
        self._apply_hooks(bool(cfg.get("sessions_hooks", self._sess_hooks_on)))
        self._apply_hotkey(cfg.get("sessions_hotkey", ""))
        self._sess_answer_on = bool(cfg.get("sessions_answer",
                                            self._sess_answer_on))
        if self._sessions_on and not sessions_was_on:
            # Re-enabled: forget the old history so the next tick doesn't
            # announce every already-running session as newly appeared.
            self._sess_tracker.reset()
            self._sess_stuck.reset()
            self._sess_known_ids = frozenset()
            self._sess_seeded = False
        # accent: apply, or restore the theme's original when cleared
        self._accent = cfg["accent"]
        for k in render.THEMES:
            render.THEMES[k]["accent"] = cfg["accent"] or self._orig_accents[k]
        # theme: live. Clearing _bg_hex stops the next taskbar sample from
        # early-returning without recomputing _theme (needed for forced -> auto).
        self._forced_theme = cfg["theme"] if cfg["theme"] in ("light", "dark") else None
        if self._forced_theme:
            self._theme = self._forced_theme
        self._bg_hex = None
        self._bg_ticks = 3   # re-sample the taskbar on the next tick
        self._sig = None     # force a strip re-render on the next tick
        # Only reseed alert state when the alert config actually changed, so a
        # crossing landing on the poll after an unrelated save isn't swallowed.
        if alerts_changed:
            self._alerted = {"session": set(), "weekly": set()}
            self._first_alert = {"session": True, "weekly": True}
        # If unattended auto-resume was just turned off, stand down any live
        # countdown / pending retry so a just-disabled run can't still fire.
        if auto_was_on and not self._resume_auto:
            if self._resume_toast is not None:
                try:
                    self._resume_toast.close()
                except Exception:
                    pass
                self._resume_toast = None
            if self._resume_retry_after is not None:
                try:
                    self.root.after_cancel(self._resume_retry_after)
                except Exception:
                    pass
                self._resume_retry_after = None
            self._resume_state = "idle"
        try:
            settings.save(cfg)
        except Exception:
            _log_exc()
        self._wake.set()  # nudge the poll thread so poll/cost apply immediately

    # -- drawing ---------------------------------------------------------- #
    def _strip_metrics(self):
        """Strip groups to draw — the usage meters plus, optionally, a live
        session count."""
        metrics = tuple(self._metrics)
        if self._sessions_on and self._sessions_on_strip:
            metrics += ("sessions",)
        return metrics

    def _strip_sig(self, disp):
        return (
            self._bg_hex, self._theme,
            disp.get("session_pct"), disp.get("session_color"),
            render._fmt_left(disp.get("session_resets_at")),
            disp.get("weekly_pct"), disp.get("weekly_color"),
            disp.get("face_pct"),
            self._strip_metrics(),
            disp.get("sessions_count"), disp.get("sessions_blocked"),
        )

    def _draw(self, disp):
        if self._card:  # off-Windows: a rounded floating pill (its own bg + alpha corners)
            T = render.THEMES.get(self._theme, render.THEMES["light"])
            strip = render.render_strip(disp, T["panel_bot"], self._theme, scale=3, metrics=self._strip_metrics())
            img = _round_alpha(strip, min(strip.size[1] // 2, 15))
        else:  # Windows: opaque strip painted in the sampled taskbar color (blends in)
            img = render.render_strip(disp, self._bg_hex, self._theme, scale=3,
                                      metrics=self._strip_metrics())
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self.canvas.configure(width=w, height=h)
        if self._need_autoplace:
            sw, sh = _screen_size()
            if self._card:
                self.root.geometry(f"{w}x{h}+{sw - w - 24}+40")  # top-right on mac/linux
            else:
                self.root.geometry(f"{w}x{h}+{sw - 250 - w}+{sh - TASKBAR_H + (TASKBAR_H - h) // 2}")
            self._need_autoplace = False
        else:
            self.root.geometry(f"{w}x{h}")
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    # -- live sessions ---------------------------------------------------- #
    #: Seconds between transcript-tail enrichments. A snapshot is ~0.5ms so it
    #: runs every tick; enrichment is ~5ms per session, so it doesn't.
    SESS_ENRICH_EVERY = 5.0

    #: Seconds between heartbeat writes while hooks are on.
    HEARTBEAT_EVERY = 300.0

    #: WM_HOTKEY lands on this thread's queue and nothing dispatches it for us.
    #: It's drained on its own fast timer rather than the 1s UI tick: Windows
    #: grants the hotkey's owner permission to change the foreground window only
    #: for a moment after the press, and a second's delay loses it — the jump
    #: then silently fails to raise anything.
    HOTKEY_POLL_MS = 120

    def _hotkey_loop(self):
        if self._hotkey is not None:
            self._hotkey.poll()
        try:
            self.root.after(self.HOTKEY_POLL_MS, self._hotkey_loop)
        except Exception:
            pass    # window closed

    def _sessions_tick(self):
        """Refresh the live-session list. Runs on the main thread every second.

        Never raises: a failure here must not take down the usage widget, which
        is the app's actual job.
        """
        if self._demo:
            return          # the tour drives the session list itself
        if not self._sessions_on:
            self._sess_disp = {}
            # A "needs you" toast has no timeout, so switching the monitor off
            # would otherwise leave it on screen forever with nothing left
            # running to take it down.
            self._retire_sticky_toast([])
            return
        try:
            self._drain_hook_events()
            live = sessions_core.snapshot()
            events = self._sess_tracker.update(live)
            live = self._sess_tracker.sessions

            # Re-enrich on a slow cadence, or immediately when the set of
            # sessions changes so a new row isn't nameless for five seconds.
            ids = frozenset(s.session_id for s in live)
            now = time.monotonic()
            if ids != self._sess_known_ids or \
                    now - self._sess_enriched_at >= self.SESS_ENRICH_EVERY:
                self._sess_known_ids = ids
                self._sess_enriched_at = now
                self._sess_extra = {
                    s.session_id: {
                        "title": s.title, "tool": s.tool, "model": s.model,
                        "git_branch": s.git_branch, "last_prompt": s.last_prompt,
                    }
                    for s in sessions_core.enrich_all(live)
                }
            merged = []
            for s in live:
                fields = dict(self._sess_extra.get(s.session_id) or {})
                note = self._sess_hook_notes.get(s.session_id)
                if note and s.status == sessions_core.WAITING:
                    # The hook knows the actual prompt; the registry only has a
                    # category like "input needed".
                    fields["waiting_for"] = note
                merged.append(dataclasses.replace(s, **fields) if fields else s)
            self._sess_disp = sessions_core.format_sessions(
                merged, max_rows=self._sessions_max_rows)
            self._sess_disp["sessions_recent"] = sessions_core.format_recent(
                self._sess_tracker.recent)
            self._retire_sticky_toast(merged)
            self._session_alerts(events, merged)
        except Exception:
            _log_exc()
            self._sess_disp = {}

    def _apply_hotkey(self, spec):
        """Re-register the global shortcut, reporting a clash rather than
        failing silently — a shortcut that quietly does nothing is worse than
        none at all."""
        spec = (spec or "").strip()
        current = self._hotkey.spec if self._hotkey is not None else ""
        if spec == current and (self._hotkey is None or self._hotkey.registered):
            return
        if self._hotkey is not None:
            self._hotkey.unregister()
            self._hotkey = None
        if not spec:
            return
        self._hotkey = hotkeys.Hotkey(spec, self._jump_to_blocked)
        if not self._hotkey.registered:
            messagebox.showwarning(
                "Shortcut unavailable",
                f"Couldn't register {spec} — {self._hotkey.error}.\n\n"
                f"Pick a different combination, or clear the box to turn the "
                f"shortcut off.",
                parent=self.root)

    def _apply_hooks(self, want):
        """Install or remove the hook entries to match the setting.

        Runs on every save, not just on change: an install can fail (a
        read-only file, a settings.json that stopped parsing), and re-checking
        each time lets it recover on the next save instead of staying silently
        broken.

        Hooks exist only to feed the session monitor, so turning that off takes
        them with it. Left registered they would run in every Claude session
        for a reader that never drains them — the queue would grow unbounded
        and, with no heartbeat, the relay would eventually uninstall itself
        while the setting still claimed to be on.
        """
        want = bool(want) and self._sessions_on
        try:
            if want:
                if claude_hooks.status() != claude_hooks.INSTALLED:
                    if not claude_hooks.install():
                        want = False     # couldn't write — don't claim it's on
                if want:
                    claude_hooks.prune()
            else:
                claude_hooks.remove()
                claude_hooks.clear_spool()
                self._sess_hook_notes.clear()
        except Exception:
            _log_exc()
            want = False
        self._sess_hooks_on = want

    def _drain_hook_events(self):
        """Consume queued hook events into per-session notes.

        Hooks are NOT a second source of alerts — the tracker stays the single
        place a transition is decided, so an event arriving here can never
        double-toast something the registry is about to report anyway. What
        they add is text the registry doesn't have: the actual prompt a session
        is blocked on, rather than a coarse category.
        """
        if not self._sess_hooks_on:
            return
        # Tell the relay we're still here. Throttled — the relay only checks
        # for staleness in days, so a write per tick would be pure churn.
        now = time.monotonic()
        if now - self._sess_heartbeat_at >= self.HEARTBEAT_EVERY:
            self._sess_heartbeat_at = now
            claude_hooks.touch_heartbeat()
        for raw in claude_hooks.read_events():
            note = claude_hooks.summarize(raw)
            session_id = note.get("session_id")
            if not session_id:
                continue
            if note["event"] == "Notification":
                text = note["message"] or note["title"]
                if text:
                    self._sess_hook_notes[session_id] = sessions_core.oneline(text)
            elif note["event"] in ("Stop", "SessionEnd"):
                self._sess_hook_notes.pop(session_id, None)

    def _retire_sticky_toast(self, live):
        """Drop a "needs you" toast once that session stops needing you.

        It has no timeout, so nothing else would ever take it off the screen.
        """
        if not self._sess_sticky_pid:
            return
        still_blocked = any(s.pid == self._sess_sticky_pid
                            and s.status == sessions_core.WAITING for s in live)
        if not still_blocked:
            self._sess_sticky_pid = 0
            if self._toast is not None:
                try:
                    self._toast.close()
                except Exception:
                    pass

    def _session_alerts(self, events, live):
        """Toast the transitions worth interrupting for. Main thread."""
        if not self._sess_seeded:
            # First tick after start (or after re-enabling): everything already
            # running looks brand new. That's startup, not news.
            self._sess_seeded = True
            return
        # The tour alerts regardless of the user's settings — it exists to show
        # what the feature does — and shows every kind, same as it overrides the
        # usage thresholds.
        if not (self._sess_alerts_on or self._demo) or self._hidden:
            # The stuck watcher is deliberately NOT run here. Consuming a
            # crossing while we're suppressed would mean a session that got
            # blocked during a fullscreen app is never nudged — not even once
            # you come back to the desktop.
            return
        # Re-point each transition at the enriched copy of its session, so an
        # alert names the session the same way the popover row does (its AI
        # title, not the raw CLI name) and picks up any hook message.
        by_id = {s.session_id: s for s in live}
        events = [sessions_core.Transition(
            e.kind, by_id.get(e.session.session_id, e.session), e.before, e.after)
            for e in events]
        kinds = sessions_core.ALERT_KINDS if self._demo else self._sess_alert_on
        alerts = sessions_core.alerts_for(events, kinds)
        if "stuck" in kinds:
            alerts += [sessions_core.alert_for_stuck(s)
                       for s in self._sess_stuck.check(live)]
        if not alerts:
            return
        if self._sess_quiet_fg:
            # Only suppress when exactly one session sits under the focused
            # window — sessions in tabs of a shared terminal are
            # indistinguishable from here, and silencing all of them would hide
            # the one that actually needs you.
            quiet_pid = focus.exclusive_foreground_pid(
                [s.pid for s in live], focus.parent_map())
            if quiet_pid is not None:
                alerts = [a for a in alerts if a["pid"] != quiet_pid]
        # One toast at a time: a second destroys the first before it paints, so
        # a batch has to be summarised rather than fired one by one.
        merged = sessions_core.coalesce_alerts(alerts)
        if merged is None:
            return
        needs_you = merged["kind"] in ("waiting", "stuck")
        jump_pid = merged["pid"] or (self._sess_disp.get("sessions_blocked_pid") or 0)
        self._sess_sticky_pid = jump_pid if needs_you else 0
        self._show_toast(
            None, merged["title"], merged["subtitle"], merged["color"],
            # A request waits for you; an announcement doesn't need to.
            duration=None if needs_you else 6500,
            on_click=(lambda pid=jump_pid: self._act_on_session(
                self._row_for_pid(pid) or {"pid": pid}))
            if needs_you and jump_pid else None)
        if needs_you:
            self._pulse_strip()

    def _with_sessions(self, disp):
        """Overlay the session fields onto a usage disp dict for rendering."""
        if not disp or not self._sess_disp:
            return disp
        out = dict(disp)
        out.update(self._sess_disp)
        return out

    # -- loops ------------------------------------------------------------ #
    def _poll_loop(self):
        while True:
            self._wake.clear()  # a refresh requested during the poll forces a re-poll
            if self._demo:      # the tour owns the display — don't poll or publish
                self._wake.wait(timeout=1.0)
                continue
            result = None
            try:
                result = core.poll_once(self._state)
                if isinstance(result, core.Usage):
                    disp = core.format_breakdown(result)
                else:
                    disp = core.status_display(result)
            except core.CredentialsMissing:
                disp = core.status_display(core.Status.NO_CREDS)
            except Exception:
                _log_exc()
                disp = core.status_display(core.Status.ERROR)
            if self._show_cost and isinstance(result, core.Usage):
                try:
                    c = cost.compute_today()
                    if c:
                        disp = dict(disp)
                        disp["cost_tokens"] = c["tokens"]
                        disp["cost_usd"] = c["cost"]
                        # Which project is actually burning it. Rendered in the
                        # menus, which have no height limit, not the popover.
                        disp["cost_projects"] = cost.compute_today_by_project(
                            limit=5) or []
                except Exception:
                    pass
            # Hand off to the main thread. Tkinter isn't thread-safe, so alerts
            # and resume (which build Toplevels/timers) run in _refresh_ui on the
            # main thread; here we only publish data + bump the sequence.
            with self._lock:
                if not self._demo:  # a demo may have started mid-poll — don't clobber it
                    self._poll_seq += 1
                    disp = dict(disp)
                    disp["_seq"] = self._poll_seq  # lets the popover detect a fresh poll
                    disp["_poll_mono"] = time.monotonic()  # for the "updated … ago" footer
                    self._disp = disp
            wait = self._state.backoff or self._poll
            self._wake.wait(timeout=wait)

    def _refresh_ui(self):
        # Hide over fullscreen apps (movies, games, presentations) like the
        # taskbar does; show again when they exit. Disabled via hide_on_fullscreen.
        fs = self._hide_on_fullscreen and not self._demo and _fullscreen_active()
        if fs and not self._hidden:
            self._hidden = True
            # a resume is pending if a toast is up OR a retry is already scheduled
            resume_pending = self._resume_toast is not None or self._resume_retry_after is not None
            if self._resume_retry_after is not None:
                try:
                    self.root.after_cancel(self._resume_retry_after)
                except Exception:
                    pass
                self._resume_retry_after = None
            for t in (self._popover, self._toast, self._resume_toast):
                if t is not None:
                    try:
                        t.close()
                    except Exception:
                        pass
            self._popover = self._toast = self._resume_toast = None
            try:
                self.root.withdraw()
            except Exception:
                pass
            if resume_pending:  # don't drop a pending/deferred resume — re-arm it
                self._resume_retry_after = self.root.after(15000, self._fire_resume)
        elif not fs and self._hidden:
            self._hidden = False
            self._processed_seq = -1  # re-process the latest poll (alerts) on unhide
            try:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            except Exception:
                pass

        # Process each new poll for alerts/resume state on the main thread — even
        # while hidden, so crossings/caps during fullscreen aren't lost. The toast
        # creation itself is deferred/suppressed via self._hidden.
        # Live sessions refresh every tick regardless of visibility, so the
        # tracker keeps its history across a fullscreen hide and alerts can't
        # miss a transition that happened while we were away.
        self._sessions_tick()

        with self._lock:
            disp = self._disp
            seq = self._poll_seq
        if disp is not None and seq != self._processed_seq:
            self._processed_seq = seq
            try:
                self._maybe_alert(disp)
                self._track_resume(disp)
            except Exception:
                _log_exc()

        if self._hidden:
            self.root.after(1000, self._refresh_ui)
            return

        # visible-only work
        self._bg_ticks += 1
        if self._bg_ticks >= 3:
            self._bg_ticks = 0
            rgb = self._sample_bg()
            if rgb:
                self._apply_bg(rgb)
        if disp is not None:
            merged = self._with_sessions(disp)
            sig = self._strip_sig(merged)
            if sig != self._sig:
                self._draw(merged)
                self._sig = sig
        self._topmost_ticks += 1
        if self._topmost_ticks >= 6 and self._popover is None:  # don't steal popover focus
            self._topmost_ticks = 0
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass
        self.root.after(1000, self._refresh_ui)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    BarWidget().run()
