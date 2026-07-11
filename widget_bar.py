"""Always-on-top taskbar readout (Windows) with a premium details popover.

The strip and the popover are both rendered as high-quality Pillow images
(see render.py) and shown via ImageTk. The strip's background is sampled from
the taskbar so it looks like floating text yet stays fully clickable; clicking
it opens a polished popover with circular usage gauges.

Interactions: left-click = open/close popover · left-drag = move (remembered)
· right-click = Details / Open Settings / Refresh / Check for Updates / Quit.
"""

import ctypes
from ctypes import wintypes
import json
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
                 on_refresh, on_quit, on_settings, on_close):
        self.theme = theme
        self.get_disp = get_disp
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.on_settings = on_settings
        self.on_close = on_close
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

    def _click(self, e):
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
class Toast:
    """A small auto-dismissing alert card near the tray."""

    def __init__(self, root, theme, pct, title, subtitle, color_name, duration=6500,
                 on_close=None):
        self._on_close = on_close
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
        c.bind("<Button-1>", lambda e: self.close())
        wl, wt, wr, wb = _monitor_workarea(root.winfo_rootx() + root.winfo_width() // 2,
                                           root.winfo_rooty() + root.winfo_height() // 2)
        self.top.geometry(f"{w}x{h}+{wr - w - 20}+{wb - h - 16}")
        self._closed = False
        self._after = self.top.after(duration, self.close)

    def close(self):
        if self._closed:
            return
        self._closed = True
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

        # rendered header banner (sparkle + title + subtitle)
        self._hdr = ImageTk.PhotoImage(render.render_settings_header(theme, self.WIN_W))
        tk.Label(self.top, image=self._hdr, bd=0, bg=bg).pack()

        body = tk.Frame(self.top, bg=bg)
        body.pack(fill="x", padx=20)
        LBL = ("Segoe UI", 9)

        def section(title, first=False):
            tk.Label(body, text=title.upper(), bg=bg, fg=T["accent"],
                     font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(10 if first else 15, 5))

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

        # ----- Alerts -----
        section("Alerts")
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

        self._apply_bg((233, 238, 243))  # provisional; refined by sampling
        self._place_initial()
        self._bind_events()
        self._draw(core.status_display(core.Status.NO_DATA))
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(400, self._refresh_ui)
        if self._start_in_demo:  # `app.py demo` — drop straight into the tour
            self.root.after(500, self._enter_demo)

    # -- background matching --------------------------------------------- #
    def _apply_bg(self, rgb):
        hexc = "#%02x%02x%02x" % tuple(rgb[:3])
        if hexc == self._bg_hex:
            return
        self._bg_hex = hexc
        self._theme = self._forced_theme or ("light" if _lum(rgb) > 140 else "dark")
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
        if pos:
            self.root.geometry(f"+{pos[0]}+{pos[1]}")
        else:
            sw, sh = _screen_size()
            self.root.geometry(f"+{sw - 250 - 200}+{sh - TASKBAR_H + 9}")

    def _load_pos(self):
        try:
            d = json.loads(POS_FILE.read_text(encoding="utf-8"))
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
            self._save_pos()
        else:
            self._toggle_popover()

    def _get_disp(self):
        with self._lock:
            return self._disp

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
        )

    def _on_popover_closed(self):
        self._popover = None
        self._pop_closed_at = time.monotonic()

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

    def _show_toast(self, pct, title, subtitle, color):
        if self._hidden:
            return
        try:
            if self._toast is not None:
                self._toast.close()
            self._toast = Toast(self.root, self._theme, pct, title, subtitle, color,
                                on_close=self._clear_toast)
        except Exception:
            _log_exc()

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

        def status(session, color):  # a non-usage state (offline / rate-limited)
            return {"_demo": True, "plan": None, "session": session,
                    "session_pct": None, "weekly_pct": None, "face_color": color,
                    "session_color": color, "weekly_color": color, "model_rows": []}

        return [
            (step(20, "green", 8, "green", 180), 4.0),    # 1  comfortable — green
            (step(62, "amber", 15, "green", 120), 4.0),   # 2  getting close — amber
            (step(84, "red", 22, "green", 40), 4.5),      # 3  session crosses 80% → alert
            (step(93, "red", 24, "green", 16), 4.5),      # 4  session crosses 90% → alert
            (step(48, "amber", 88, "red", 90), 4.5),      # 5  WEEKLY crosses 80% → alert
            (step(100, "red", 30, "amber", 6), 4.5),      # 6  100% → "limit reached"
            (step(85, "red", 30, "amber", 300), 6.0),     # 7  drop ≤90 → resume (Tier 1 notify)
            (step(40, "green", 30, "amber", 240,
                  cost_tokens=2_450_000, cost_usd=8.74), 5.0),   # 8  cost line (click to see)
            (status("usage limit reached", "red"), 4.5),  # 9  rate-limited (429) state
            (step(100, "red", 30, "amber", 5), 4.5),      # 10 limit reached again (capped)
            (step(88, "red", 30, "amber", 260), 6.0),     # 11 drop ≤90 → resume (Tier 2 auto)
            (status("offline (demo)", "grey"), 4.0),      # 12 graceful offline state
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
        self._sig = None    # force a redraw once real data arrives
        self._wake.set()    # wake the poll thread to fetch + publish now

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

    def _demo_tick(self):
        if not self._demo:
            return  # exited — stop the loop
        disp, hold = self._demo_seq[self._demo_i % len(self._demo_seq)]
        with self._lock:
            self._disp = dict(disp)
            self._poll_seq += 1
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
    def _strip_sig(self, disp):
        return (
            self._bg_hex, self._theme,
            disp.get("session_pct"), disp.get("session_color"),
            render._fmt_left(disp.get("session_resets_at")),
            disp.get("weekly_pct"), disp.get("weekly_color"),
            disp.get("face_pct"),
        )

    def _draw(self, disp):
        if self._card:  # off-Windows: a rounded floating pill (its own bg + alpha corners)
            T = render.THEMES.get(self._theme, render.THEMES["light"])
            strip = render.render_strip(disp, T["panel_bot"], self._theme, scale=3, metrics=self._metrics)
            img = _round_alpha(strip, min(strip.size[1] // 2, 15))
        else:  # Windows: opaque strip painted in the sampled taskbar color (blends in)
            img = render.render_strip(disp, self._bg_hex, self._theme, scale=3, metrics=self._metrics)
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
            sig = self._strip_sig(disp)
            if sig != self._sig:
                self._draw(disp)
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
