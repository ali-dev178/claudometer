"""macOS menu-bar adapter (rumps).

The menu-bar title shows a colored dot + the "most critical" percent as native
text; the dropdown holds the full breakdown. Polling runs synchronously on the
main run loop so all UI mutations stay main-thread-safe (a rare slow request
briefly delays a tick, which is acceptable for a menu-bar app).

Settings: the menu-bar app is a lighter adapter than the Windows widget — it
does not (yet) implement alerts, resume, cost, fullscreen-hide, theme or accent,
so its Settings submenu only exposes what it actually honors (which meters to
show, the poll interval) plus an "Open config file…" shortcut for everything
else. All of it reads/writes the same ~/.claudometer.toml via settings.load/save.
"""

import os
import subprocess
from datetime import datetime

import rumps

import usage_core as core
import settings
import config
import updates
import autostart

DOT = {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚪"}


class MenuApp(rumps.App):
    def __init__(self):
        super().__init__(name="Claudometer", title="…", quit_button=None)
        self._cfg = settings.load()
        self._metrics = list(self._cfg["metrics"])
        self._state = core.PollState()
        self._last_sig = None
        # Freshness footer: the last poll's local time + whether it came from a
        # manual "Refresh now" click or an automatic timer tick (mirrors the
        # Windows/floating-widget popover footer).
        self._pending_source = "auto"
        self._updated_item = None
        self.menu = ["Loading…"]
        self._timer = rumps.Timer(self._tick, self._cfg["poll"])
        self._timer.start()
        self._tick(None)  # immediate first render

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
        # Rebuild only when the menu content changes — rebuilding every tick
        # leaks rumps callback registrations (they're never pruned).
        sig = (disp.get("plan"), disp.get("session"), disp.get("weekly"),
               tuple(disp.get("models", [])), tuple(self._metrics))
        if sig != self._last_sig:
            self._last_sig = sig
            self._rebuild(disp)
        # Always refresh the freshness line (kept out of `sig` so it doesn't
        # force a full menu rebuild every tick — that leaks rumps callbacks).
        source, self._pending_source = self._pending_source, "auto"
        if self._updated_item is not None:
            self._updated_item.title = self._updated_label(source)

    def _updated_label(self, source: str) -> str:
        return f"Updated {datetime.now().strftime('%-I:%M %p')} · {source}"

    def _rebuild(self, disp) -> None:
        rows = []
        if disp.get("plan"):
            rows.append(disp["plan"])
            rows.append(None)  # separator
            if "session" in self._metrics and disp.get("session"):
                rows.append(disp["session"])
            if "weekly" in self._metrics and disp.get("weekly"):
                rows.append(disp["weekly"])
        elif disp.get("session"):
            rows.append(disp["session"])  # status/error note — always shown
        seen = {}
        for model in disp.get("models", []):
            row = "    " + model
            if row in seen:  # rumps dedupes by title; pad so no meter is dropped
                seen[row] += 1
                row += " " * seen[row]
            else:
                seen[row] = 0
            rows.append(row)
        rows.append(None)
        # A disabled (callback-less) info line showing data freshness + source.
        self._updated_item = rumps.MenuItem(self._updated_label(self._pending_source))
        rows.append(self._updated_item)
        rows.append(self._settings_menu())
        rows.append(rumps.MenuItem("Refresh now", callback=self._refresh))
        rows.append(rumps.MenuItem("Check for Updates…", callback=self._check_updates))
        rows.append(rumps.MenuItem("View on GitHub", callback=self._github))
        rows.append(rumps.MenuItem("Quit", callback=rumps.quit_application))
        self.menu.clear()
        self.menu = rows

    # -- settings --------------------------------------------------------- #
    def _settings_menu(self):
        m = rumps.MenuItem("Settings")
        sess = rumps.MenuItem("Show Session meter", callback=self._toggle_session)
        sess.state = "session" in self._metrics
        week = rumps.MenuItem("Show Weekly meter", callback=self._toggle_weekly)
        week.state = "weekly" in self._metrics
        login = rumps.MenuItem("Start at login", callback=self._toggle_login)
        login.state = autostart.is_enabled()
        m.update([sess, week, login, None,
                  rumps.MenuItem("Poll interval…", callback=self._set_poll), None,
                  rumps.MenuItem("Open config file…", callback=self._open_config)])
        return m

    def _toggle_login(self, sender):
        sender.state = autostart.set_enabled(not sender.state)

    def _toggle_session(self, sender):
        self._toggle_metric("session", sender)

    def _toggle_weekly(self, sender):
        self._toggle_metric("weekly", sender)

    def _toggle_metric(self, name, sender):
        chosen = set(self._metrics)
        if name in chosen and len(chosen) > 1:  # keep at least one meter
            chosen.discard(name)
        else:
            chosen.add(name)
        self._metrics = [x for x in ("session", "weekly") if x in chosen]
        sender.state = name in self._metrics
        self._cfg["metrics"] = list(self._metrics)
        self._save()
        self._tick(None)

    def _set_poll(self, _):
        resp = rumps.Window("Seconds between usage updates (60–300):", "Poll interval",
                            default_text=str(self._cfg["poll"]), ok="Save",
                            cancel="Cancel", dimensions=(120, 22)).run()
        if not resp.clicked:
            return
        try:
            v = max(60, min(300, int(resp.text.strip())))
        except ValueError:
            return
        self._cfg["poll"] = v
        self._save()
        self._timer.stop()
        self._timer = rumps.Timer(self._tick, v)
        self._timer.start()

    def _open_config(self, _):
        path = str(settings.config_path())
        if not os.path.exists(path):
            self._save()  # materialize the file so there's something to edit
        subprocess.Popen(["open", path])

    def _save(self):
        try:
            settings.save(self._cfg)
        except Exception:
            pass

    def _github(self, _):
        import webbrowser
        webbrowser.open(config.REPO_URL)

    def _check_updates(self, _):
        import webbrowser
        res = updates.check()  # brief network call on the runloop; acceptable
        if res["status"] == "update":
            if rumps.alert(
                    "Claudometer",
                    f"A new version is available: {res['latest']}\n"
                    f"You have {res['current']}.\n\nOpen the download page?",
                    ok="Open", cancel="Later"):
                webbrowser.open(res["url"])
        elif res["status"] == "current":
            rumps.alert("Claudometer",
                        f"You're on the latest version ({res['current']}).")
        else:
            rumps.alert("Claudometer",
                        "Couldn't check for updates right now. Please try again later.")

    def _refresh(self, _):
        self._pending_source = "manual"  # this tick's data came from a click
        self._tick(None)

    def run(self) -> None:
        super().run()
