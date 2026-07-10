"""macOS menu-bar adapter (rumps).

The menu-bar title shows a colored dot + the "most critical" percent as native
text; the dropdown holds the full breakdown. Polling runs synchronously on the
main run loop so all UI mutations stay main-thread-safe (a rare slow request
briefly delays a tick, which is acceptable for a menu-bar app).
"""

import rumps

import usage_core as core

DOT = {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚪"}


class MenuApp(rumps.App):
    def __init__(self):
        super().__init__("…", quit_button=None)
        self._state = core.PollState()
        self.menu = ["Loading…"]
        self._tick(None)  # immediate first render

    @rumps.timer(90)
    def _tick(self, _):
        try:
            result = core.poll_once(self._state)
            if isinstance(result, core.Usage):
                disp = core.format_breakdown(result)
            else:
                disp = core.status_display(result)
        except core.CredentialsMissing:
            disp = core.status_display(core.Status.NO_CREDS)
        except Exception:
            disp = core.status_display(core.Status.ERROR)
        self.title = f"{DOT.get(disp['face_color'], '⚪')} {disp['face_pct']}"
        self._rebuild(disp)

    def _rebuild(self, disp) -> None:
        rows = []
        if disp.get("plan"):
            rows.append(disp["plan"])
            rows.append(None)  # separator
        if disp.get("session"):
            rows.append(disp["session"])
        if disp.get("weekly"):
            rows.append(disp["weekly"])
        for model in disp.get("models", []):
            rows.append("    " + model)
        rows.append(None)
        rows.append(rumps.MenuItem("Refresh now", callback=self._refresh))
        rows.append(rumps.MenuItem("Quit", callback=rumps.quit_application))
        self.menu.clear()
        self.menu = rows

    def _refresh(self, _):
        self._tick(None)

    def run(self) -> None:
        super().run()
