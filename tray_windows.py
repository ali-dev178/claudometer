"""Windows / Linux system-tray adapter (pystray + Pillow).

Draws the "most critical" usage percent onto a colored disc icon and rebuilds
the right-click menu with the full breakdown on every poll.
"""

import threading
import webbrowser

import pystray
from pystray import Menu, MenuItem
from PIL import Image, ImageDraw, ImageFont

import usage_core as core
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


class TrayApp:
    def __init__(self):
        self.icon = pystray.Icon("claude_usage")
        self.icon.icon = render_icon("...", "grey")
        self.icon.title = "Claude usage: loading..."
        self.icon.menu = self._build_menu(None)
        self._state = core.PollState()
        self._stop = threading.Event()
        self._wake = threading.Event()

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

    def _apply(self, disp) -> None:
        self.icon.icon = render_icon(disp["face_pct"], disp["face_color"])
        self.icon.title = disp["tooltip"]
        self.icon.menu = self._build_menu(disp)
        self.icon.update_menu()

    # -- poll loop (daemon thread) --------------------------------------- #
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = core.poll_once(self._state)
                if isinstance(result, core.Usage):
                    disp = core.format_breakdown(result)
                else:
                    disp = core.status_display(result)
            except core.CredentialsMissing:
                disp = core.status_display(core.Status.NO_CREDS)
            except Exception as exc:  # never let the loop die
                disp = core.status_display(core.Status.ERROR)
                disp["tooltip"] = f"Claude usage: error ({exc})"
            try:
                self._apply(disp)
            except Exception:
                pass
            wait = self._state.backoff or core.poll_seconds()
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._loop, daemon=True).start()

        # icon.run() must be on the main thread.
        self.icon.run(setup=setup)
