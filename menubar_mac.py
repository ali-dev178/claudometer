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
import sessions_core
import settings
import config
import updates
import autostart

DOT = {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚪"}

#: Live sessions tick far faster than the usage poll. A FULL menu rebuild leaks
#: rumps callback registrations, so this fast timer only rewrites the titles of
#: existing items in place — the same trick the freshness line already uses.
#: A rebuild happens only when the SET of sessions changes.
SESS_TICK = 2.0


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
        self._sessions_on = self._cfg["sessions"]
        self._sessions_max_rows = self._cfg["sessions_max_rows"]
        self._sess_tracker = sessions_core.SessionTracker()
        self._sess_disp = {}
        self._sess_items = []      # MenuItems whose titles are rewritten in place
        self._sess_shape = None    # ids + statuses; a change here forces a rebuild
        self._last_disp = None
        self.menu = ["Loading…"]
        self._timer = rumps.Timer(self._tick, self._cfg["poll"])
        self._timer.start()
        self._tick(None)  # immediate first render
        if self._sessions_on:
            self._sess_timer = rumps.Timer(self._sess_tick, SESS_TICK)
            self._sess_timer.start()

    def _title(self, disp):
        """Menu-bar title: dot (most-critical severity) + the enabled meters,
        labelled — e.g. '🟡 S 61% · W 18%'. Falls back to the compact face
        percent for status states (offline / no-data) that have no numbers."""
        dot = DOT.get(disp.get("face_color"), "⚪")
        parts = []
        if "session" in self._metrics and disp.get("session_pct") is not None:
            parts.append("S %d%%" % disp["session_pct"])
        if "weekly" in self._metrics and disp.get("weekly_pct") is not None:
            parts.append("W %d%%" % disp["weekly_pct"])
        if not parts:
            return "%s %s" % (dot, disp.get("face_pct", "—"))
        return "%s %s" % (dot, " · ".join(parts))

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
        self.title = self._title(disp)
        self._last_disp = disp   # so _sess_tick can rebuild without re-polling
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

    # -- live sessions ---------------------------------------------------- #
    @staticmethod
    def _row_title(row) -> str:
        return f"    {row['emoji']} {row['label']}  ·  {row['detail']}"

    def _sess_tick(self, _=None) -> None:
        """Refresh live sessions without rebuilding the menu when we can."""
        try:
            self._sess_tracker.update(sessions_core.snapshot())
            live = sessions_core.enrich_all(self._sess_tracker.sessions)
            self._sess_disp = sessions_core.format_sessions(
                live, max_rows=self._sessions_max_rows)
        except Exception:
            return

        rows = self._sess_disp.get("sessions_rows") or []
        shape = tuple((r["session_id"], r["status"]) for r in rows)
        if shape != self._sess_shape:
            # The set of sessions changed, so the row COUNT changed — only a
            # rebuild can add or remove items.
            self._sess_shape = shape
            if self._last_disp is not None:
                self._rebuild(self._last_disp)
            return
        # Same rows, newer dwell text: rewrite titles in place (no rebuild, so
        # no leaked callbacks).
        for item, row in zip(self._sess_items, rows):
            try:
                item.title = self._row_title(row)
            except Exception:
                pass

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
        # Live Claude Code sessions. The items are kept so _sess_tick can
        # rewrite their titles without a (leaky) full rebuild.
        self._sess_items = []
        sess_rows = self._sess_disp.get("sessions_rows")
        if self._sessions_on and sess_rows is not None:
            rows.append(None)
            summary = self._sess_disp.get("sessions_summary")
            rows.append(rumps.MenuItem(
                f"Live sessions — {summary}" if summary else "Live sessions"))
            if not sess_rows:
                rows.append(rumps.MenuItem(
                    "    " + (self._sess_disp.get("sessions_empty")
                              or "No sessions running")))
            sess_seen = {}
            for row in sess_rows:
                title = self._row_title(row)
                if title in sess_seen:   # rumps dedupes by title — pad so two
                    sess_seen[title] += 1   # identically-named sessions both show
                    title += " " * sess_seen[title]
                else:
                    sess_seen[title] = 0
                item = rumps.MenuItem(title)
                self._sess_items.append(item)
                rows.append(item)
            if self._sess_disp.get("sessions_overflow"):
                rows.append(rumps.MenuItem(
                    f"    +{self._sess_disp['sessions_overflow']} more"))

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
