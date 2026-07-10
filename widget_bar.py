"""Always-on-top taskbar readout (Windows) with a premium details popover.

The strip and the popover are both rendered as high-quality Pillow images
(see render.py) and shown via ImageTk. The strip's background is sampled from
the taskbar so it looks like floating text yet stays fully clickable; clicking
it opens a polished popover with circular usage gauges.

Interactions: left-click = open/close popover · left-drag = move (remembered)
· right-click = Details / Refresh / Quit.
"""

import ctypes
from ctypes import wintypes
import json
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

from PIL import ImageTk

import usage_core as core
import render
import settings
import cost
import resume

POS_FILE = Path.home() / ".claude_widget_bar.json"
TASKBAR_H = 48
DRAG_THRESHOLD = 4


def _set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _screen_w():
    return ctypes.windll.user32.GetSystemMetrics(0)


def _screen_size():
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_gdi32.GetPixel.restype = ctypes.c_uint


def _get_pixel(x, y):
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


# --------------------------------------------------------------------------- #
# Details popover (image-based)
# --------------------------------------------------------------------------- #
class Popover:
    def __init__(self, root, theme, get_disp, anchor_x, anchor_top,
                 on_refresh, on_quit, on_close):
        self.theme = theme
        self.get_disp = get_disp
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.on_close = on_close
        self._closed = False
        self._after = None
        self._sig = None
        self._hits = {}
        self.anchor_x = anchor_x
        self.anchor_top = anchor_top

        key = render.THEMES[theme]["key"]
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=key)
        self.top.attributes("-transparentcolor", key)
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

    def _render(self, force=False):
        disp = self.get_disp() or {}
        sig = self._sig_of(disp)
        if not force and sig == self._sig:
            return
        self._sig = sig
        img, hits = render.render_popover(disp, self.theme)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self._hits = hits
        sw = _screen_w()
        px = min(max(8, self.anchor_x), sw - w - 8)
        py = self.anchor_top - h - 8
        self.canvas.configure(width=w, height=h)
        self.top.geometry(f"{w}x{h}+{int(px)}+{int(py)}")
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _click(self, e):
        for name, (x1, y1, x2, y2) in self._hits.items():
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                if name == "refresh":
                    self.on_refresh()
                elif name == "quit":
                    self.on_quit()
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
        key = render.THEMES[theme]["key"]
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=key)
        self.top.attributes("-transparentcolor", key)
        img = render.render_toast(pct, title, subtitle, color_name, theme)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        c = tk.Canvas(self.top, width=w, height=h, bg=key, highlightthickness=0, bd=0)
        c.pack()
        c.create_image(0, 0, anchor="nw", image=self._photo)
        c.bind("<Button-1>", lambda e: self.close())
        sw, sh = _screen_size()
        self.top.geometry(f"{w}x{h}+{sw - w - 20}+{sh - TASKBAR_H - h - 16}")
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

        key = render.THEMES[theme]["key"]
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=key)
        self.top.attributes("-transparentcolor", key)
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
        img = render.render_action_toast(self._title, sub, self._action, self._theme)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self.canvas.configure(width=w, height=h)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        sw, sh = _screen_size()
        self.top.geometry(f"{w}x{h}+{sw - w - 20}+{sh - TASKBAR_H - h - 16}")

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
# Taskbar strip (image-based)
# --------------------------------------------------------------------------- #
class BarWidget:
    def __init__(self):
        _set_dpi_aware()
        self.root = tk.Tk()
        self.root.title("Claudometer")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack()

        cfg = settings.load()
        self._poll = cfg["poll"]
        self._metrics = tuple(cfg["metrics"])
        self._forced_theme = cfg["theme"] if cfg["theme"] in ("light", "dark") else None
        self._alerts_on = cfg["alerts"]
        self._thresholds = cfg["alert_thresholds"]
        self._show_cost = cfg["show_cost"]
        if cfg["accent"]:
            for _t in render.THEMES.values():
                _t["accent"] = cfg["accent"]
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
        self._poll_seq = 0        # bumped by the poll thread on each new result
        self._processed_seq = 0   # last poll processed for alerts/resume (main thread)

        self._theme = self._forced_theme or "light"
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

    # -- background matching --------------------------------------------- #
    def _apply_bg(self, rgb):
        hexc = "#%02x%02x%02x" % tuple(rgb[:3])
        if hexc == self._bg_hex:
            return
        self._bg_hex = hexc
        self._theme = self._forced_theme or ("light" if _lum(rgb) > 140 else "dark")
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
        self._popover = Popover(
            self.root, self._theme, self._get_disp,
            self.root.winfo_x(), self.root.winfo_y(),
            self._refresh_now, self._quit, self._on_popover_closed,
        )

    def _on_popover_closed(self):
        self._popover = None
        self._pop_closed_at = time.monotonic()

    # -- threshold alerts ------------------------------------------------- #
    def _maybe_alert(self, disp):
        """Fire a toast when session/weekly crosses a configured threshold upward.
        Runs on the main thread. Skipped while hidden (fullscreen) so a crossing
        isn't marked-alerted-but-suppressed — it's re-detected on unhide."""
        if not self._alerts_on or self._hidden:
            return
        for which in ("session", "weekly"):
            pct = disp.get(f"{which}_pct")
            if pct is None:
                continue
            reached = {t for t in self._thresholds if pct >= t}
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
        if not (self._resume_notify or self._resume_auto):
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
        elif self._resume_state == "resumed" and sp >= 100:
            self._resume_state = "capped"  # genuinely re-capped -> re-arm

    def _clear_resume_toast(self):
        self._resume_toast = None

    def _fire_resume(self):
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
            # don't auto-resume again until a genuine re-cap (avoids a second
            # unattended run if the just-launched job pushes usage back to 100%)
            self._resume_state = "resumed"
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

    def _popup_menu(self, e):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Details…", command=self._toggle_popover)
        menu.add_command(label="Refresh now", command=self._refresh_now)
        menu.add_separator()
        menu.add_command(label="Quit", command=self._quit)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _refresh_now(self):
        self._wake.set()

    def _quit(self):
        self._save_pos()
        self.root.destroy()

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
        img = render.render_strip(disp, self._bg_hex, self._theme, scale=3, metrics=self._metrics)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self.canvas.configure(width=w, height=h)
        if self._need_autoplace:
            sw, sh = _screen_size()
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
                self._disp = disp
                self._poll_seq += 1
            wait = self._state.backoff or self._poll
            self._wake.wait(timeout=wait)

    def _refresh_ui(self):
        # Hide over fullscreen apps (movies, games, presentations) like the
        # taskbar does; show again when they exit.
        fs = _fullscreen_active()
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
