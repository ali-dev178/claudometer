"""Windows / Linux system-tray adapter (pystray + Pillow).

Draws the "most critical" usage percent onto a colored disc icon and rebuilds
the right-click menu with the full breakdown on every poll.
"""

import threading
import time
import webbrowser

import pystray
from pystray import Menu, MenuItem
from PIL import Image, ImageDraw, ImageFont

import usage_core as core
import sessions_core
import settings
import cost
import updates

COLORS = {
    "green": (46, 160, 67),
    "amber": (219, 154, 4),
    "red": (218, 54, 51),
    "grey": (110, 118, 129),
}

_FONT_PATHS = [
    "C:/Windows/Fonts/segoeuib.ttf",   # Segoe UI Bold (Windows)
    "C:/Windows/Fonts/arialbd.ttf",    # Arial Bold (Windows)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _measure(text: str, font) -> tuple:
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top, left, top


def _fit_font(text: str, size: int):
    """Largest font whose text fits ~86% of the disc."""
    font_size = int(size * 0.62)
    while font_size > 8:
        font = _load_font(font_size)
        width, height, _, _ = _measure(text, font)
        if width <= size * 0.86 and height <= size * 0.86:
            return font
        font_size -= 2
    return _load_font(10)


def render_icon(text: str, color_name: str, size: int = 64) -> Image.Image:
    color = COLORS.get(color_name, COLORS["grey"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([1, 1, size - 2, size - 2], fill=color + (255,))
    font = _fit_font(text, size)
    width, height, off_x, off_y = _measure(text, font)
    x = (size - width) / 2 - off_x
    y = (size - height) / 2 - off_y
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return img


def _fmt_tokens(n) -> str:
    """1234 -> '1.2k', 7051178 -> '7.1M'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(int(n))


class TrayApp:
    def __init__(self):
        self.icon = pystray.Icon("claude_usage")
        self.icon.icon = render_icon("...", "grey")
        self.icon.title = "Claude usage: loading..."
        self.icon.menu = self._build_menu(None)
        self._state = core.PollState()
        self._stop = threading.Event()
        self._wake = threading.Event()
        cfg = settings.load()
        self._sessions_on = cfg["sessions"]
        self._sessions_max_rows = cfg["sessions_max_rows"]
        self._sess_tracker = sessions_core.SessionTracker()
        self._sess_disp = {}
        self._usage_disp = None
        self._last_sig = None

    # -- menu ------------------------------------------------------------- #
    def _build_menu(self, disp) -> Menu:
        if not disp:
            return Menu(
                MenuItem("Loading…", None, enabled=False),
                Menu.SEPARATOR,
                MenuItem("Refresh now", self._refresh_now),
                MenuItem("Quit", self._quit),
            )
        items = []
        if disp.get("plan"):
            items.append(MenuItem(disp["plan"], None, enabled=False))
        items.append(Menu.SEPARATOR)
        if disp.get("session"):
            items.append(MenuItem(disp["session"], None, enabled=False))
        if disp.get("weekly"):
            items.append(MenuItem(disp["weekly"], None, enabled=False))
        for model in disp.get("models", []):
            items.append(MenuItem("    " + model, None, enabled=False))
        rows = disp.get("sessions_rows")
        if rows is not None:
            items.append(Menu.SEPARATOR)
            summary = disp.get("sessions_summary")
            items.append(MenuItem(
                f"Live sessions — {summary}" if summary else "Live sessions",
                None, enabled=False))
            if not rows:
                items.append(MenuItem("    " + (disp.get("sessions_empty")
                                                or "No sessions running"),
                                      None, enabled=False))
            for row in rows:
                items.append(MenuItem(
                    f"    {row['emoji']} {row['label']}  ·  {row['detail']}",
                    None, enabled=False))
            if disp.get("sessions_overflow"):
                items.append(MenuItem(f"    +{disp['sessions_overflow']} more",
                                      None, enabled=False))
            for row in disp.get("sessions_recent") or []:
                items.append(MenuItem(f"    ⚪ {row['label']}  ·  {row['detail']}",
                                      None, enabled=False))
        # Which project is burning today's usage. Menus have no height limit,
        # unlike the popover, so the breakdown lives here.
        projects = disp.get("cost_projects")
        if projects:
            items.append(Menu.SEPARATOR)
            items.append(MenuItem("Today by project", None, enabled=False))
            for row in projects:
                items.append(MenuItem(
                    f"    {row['project']}  ·  {_fmt_tokens(row['tokens'])} tokens"
                    f"  ·  ~${row['cost']:.2f}", None, enabled=False))
        items.append(Menu.SEPARATOR)
        items.append(MenuItem("Refresh now", self._refresh_now))
        items.append(MenuItem("Check for Updates…", self._check_updates))
        items.append(MenuItem("Quit", self._quit))
        return Menu(*items)

    # -- actions ---------------------------------------------------------- #
    def _refresh_now(self, icon=None, item=None):
        self._wake.set()

    def _check_updates(self, icon=None, item=None):
        # Runs on pystray's menu-callback thread; a short network call is fine.
        def worker():
            res = updates.check()
            if res["status"] == "update":
                self._notify(f"Version {res['latest']} is available — opening "
                             f"the download page.")
                webbrowser.open(res["url"])
            elif res["status"] == "current":
                self._notify(f"You're on the latest version ({res['current']}).")
            else:
                self._notify("Couldn't check for updates. Please try again later.")
        threading.Thread(target=worker, daemon=True).start()

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "Claudometer")
        except Exception:
            pass  # some backends lack balloon support

    def _quit(self, icon=None, item=None):
        self._stop.set()
        self._wake.set()
        self.icon.stop()

    @staticmethod
    def _sig(disp) -> tuple:
        """What the tray actually displays. The loop now ticks every couple of
        seconds for sessions, so without this the icon bitmap would be
        re-rendered and the menu rebuilt constantly — which also fights a menu
        the user currently has open."""
        return (
            disp.get("face_pct"), disp.get("face_color"), disp.get("tooltip"),
            disp.get("plan"), disp.get("session"), disp.get("weekly"),
            tuple(disp.get("models") or ()),
            disp.get("sessions_summary"), disp.get("sessions_overflow"),
            tuple((r["label"], r["detail"], r["emoji"])
                  for r in disp.get("sessions_rows") or ()),
            disp.get("sessions_rows") is not None,
            disp.get("sessions_blocked"),
            tuple((r["label"], r["detail"])
                  for r in disp.get("sessions_recent") or ()),
            tuple((r["project"], r["tokens"])
                  for r in disp.get("cost_projects") or ()),
        )

    def _apply(self, disp) -> None:
        merged = dict(disp)
        merged.update(self._sess_disp)
        sig = self._sig(merged)
        if sig == self._last_sig:
            return
        self._last_sig = sig
        # A blocked session outranks the usage number: usage is something to
        # pace yourself against, a blocked session is something to go and do.
        blocked = merged.get("sessions_blocked") or 0
        if blocked:
            self.icon.icon = render_icon(str(blocked), "red")
            self.icon.title = f"Claude · {blocked} session(s) waiting on you"
        else:
            self.icon.icon = render_icon(disp["face_pct"], disp["face_color"])
            self.icon.title = merged["tooltip"]
        self.icon.menu = self._build_menu(merged)
        self.icon.update_menu()

    # -- live sessions ---------------------------------------------------- #
    #: Sessions change far faster than the usage numbers, so the loop ticks at
    #: this interval and only re-polls usage on its own (much slower) schedule.
    SESS_TICK = 2.0

    def _refresh_sessions(self) -> None:
        if not self._sessions_on:
            self._sess_disp = {}
            return
        try:
            self._sess_tracker.update(sessions_core.snapshot())
            live = sessions_core.enrich_all(self._sess_tracker.sessions)
            self._sess_disp = sessions_core.format_sessions(
                live, max_rows=self._sessions_max_rows)
            self._sess_disp["sessions_recent"] = sessions_core.format_recent(
                self._sess_tracker.recent)
        except Exception:
            self._sess_disp = {}

    # -- poll loop (daemon thread) --------------------------------------- #
    def _poll_usage(self):
        try:
            result = core.poll_once(self._state)
            if isinstance(result, core.Usage):
                disp = core.format_breakdown(result)
                try:
                    disp["cost_projects"] = cost.compute_today_by_project(limit=5) or []
                except Exception:
                    pass
                return disp
            return core.status_display(result)
        except core.CredentialsMissing:
            return core.status_display(core.Status.NO_CREDS)
        except Exception as exc:  # never let the loop die
            disp = core.status_display(core.Status.ERROR)
            disp["tooltip"] = f"Claude usage: error ({exc})"
            return disp

    def _loop(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_poll or self._wake.is_set():
                self._usage_disp = self._poll_usage()
                next_poll = time.monotonic() + (self._state.backoff
                                                or core.poll_seconds())
            self._refresh_sessions()
            if self._usage_disp is not None:
                try:
                    self._apply(self._usage_disp)
                except Exception:
                    pass
            self._wake.wait(timeout=self.SESS_TICK)
            self._wake.clear()

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._loop, daemon=True).start()

        # icon.run() must be on the main thread.
        self.icon.run(setup=setup)
