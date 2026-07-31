"""Tests for widget_bar's session logic, without building a Tk window.

These methods coordinate three modules (sessions_core, hooks, focus) and the
bugs they hide are cross-cutting: hooks left registered with nothing draining
them, a burst of alerts destroying each other, session data leaking into the
demo. The methods are bound onto a stub `self` so they can be driven directly.
"""

import json
import os

import pytest

import hooks
import sessions_core as sc
import widget_bar


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CLAUDOMETER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDOMETER_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setattr(hooks.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(sc.Path, "home", staticmethod(lambda: tmp_path))
    return cfg


class Widget:
    """Enough of BarWidget for the session methods to run."""

    _demo = False
    _hidden = False
    _sessions_max_rows = 6
    _sess_alerts_on = True
    _sess_alert_on = ("waiting", "idle", "stuck")
    _sess_quiet_fg = False
    SESS_ENRICH_EVERY = widget_bar.BarWidget.SESS_ENRICH_EVERY
    HEARTBEAT_EVERY = widget_bar.BarWidget.HEARTBEAT_EVERY

    def __init__(self, sessions_on=True, hooks_on=False, stuck=0):
        self._sessions_on = sessions_on
        self._sess_hooks_on = hooks_on
        self._sess_tracker = sc.SessionTracker(confirm_ticks=1)
        self._sess_stuck = sc.StuckWatcher(stuck)
        self._sess_seeded = True
        self._sess_disp = {}
        self._sess_extra = {}
        self._sess_hook_notes = {}
        self._sess_enriched_at = 0.0
        self._sess_heartbeat_at = 0.0
        self._sess_known_ids = frozenset()
        self.toasts = []

    def _show_toast(self, pct, title, subtitle, color):
        self.toasts.append((title, subtitle, color))

    _sessions_tick = widget_bar.BarWidget._sessions_tick
    _drain_hook_events = widget_bar.BarWidget._drain_hook_events
    _session_alerts = widget_bar.BarWidget._session_alerts
    _with_sessions = widget_bar.BarWidget._with_sessions
    _apply_hooks = widget_bar.BarWidget._apply_hooks


def _session(**kw):
    base = dict(session_id="a", pid=1, cwd="/p/x", name="n",
                status=sc.BUSY, status_updated_at=sc.now_ms())
    base.update(kw)
    return sc.Session(**base)


def _queue(n=3):
    hooks.spool_dir().mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (hooks.spool_dir() / f"{i}.json").write_text(
            json.dumps({"hook_event_name": "Stop", "session_id": "a"}),
            encoding="utf-8")


# --------------------------------------------------------------------------- #
# Hooks only make sense while the session monitor is on
# --------------------------------------------------------------------------- #
def test_hooks_are_removed_when_sessions_are_turned_off():
    w = Widget(sessions_on=True)
    w._apply_hooks(True)
    assert hooks.status() == hooks.INSTALLED

    w._sessions_on = False
    w._apply_hooks(True)          # the setting still says "on"
    assert hooks.status() == hooks.ABSENT, (
        "hooks left registered with the monitor off would fire in every Claude "
        "session for a reader that never drains them")
    assert w._sess_hooks_on is False


def test_turning_sessions_off_clears_the_queue():
    w = Widget(sessions_on=True)
    w._apply_hooks(True)
    _queue(4)
    w._sessions_on = False
    w._apply_hooks(True)
    assert hooks.read_events() == []


def test_hooks_install_only_with_sessions_on():
    w = Widget(sessions_on=False)
    w._apply_hooks(True)
    assert hooks.status() == hooks.ABSENT
    assert w._sess_hooks_on is False


def test_apply_hooks_removes_a_stale_registration():
    Widget(sessions_on=True)._apply_hooks(True)
    assert hooks.status() == hooks.INSTALLED
    # A later run with the setting off must take it back out.
    Widget(sessions_on=True)._apply_hooks(False)
    assert hooks.status() == hooks.ABSENT


def test_apply_hooks_survives_an_unparsable_settings_file(_isolate):
    (_isolate / "settings.json").write_text("{ broken", encoding="utf-8")
    w = Widget(sessions_on=True)
    w._apply_hooks(True)                     # must not raise
    assert w._sess_hooks_on is False         # and must not claim success


# --------------------------------------------------------------------------- #
# Draining and the heartbeat
# --------------------------------------------------------------------------- #
def test_drain_is_a_noop_when_hooks_are_off():
    _queue(2)
    Widget(hooks_on=False)._drain_hook_events()
    assert len(hooks.read_events()) == 2     # untouched, not consumed


def test_drain_consumes_and_writes_a_heartbeat():
    _queue(2)
    w = Widget(hooks_on=True)
    w._drain_hook_events()
    assert hooks.read_events() == []
    assert hooks.heartbeat_path().is_file()


def test_heartbeat_is_throttled(monkeypatch):
    w = Widget(hooks_on=True)
    calls = []
    monkeypatch.setattr(widget_bar.claude_hooks, "touch_heartbeat",
                        lambda: calls.append(1))
    w._drain_hook_events()
    w._drain_hook_events()
    w._drain_hook_events()
    assert len(calls) == 1, "a write per tick is pure churn"


def test_notification_message_becomes_the_waiting_reason():
    hooks.spool_dir().mkdir(parents=True, exist_ok=True)
    (hooks.spool_dir() / "n.json").write_text(json.dumps({
        "hook_event_name": "Notification", "session_id": "a",
        "message": "Claude needs your permission to run Bash"}), encoding="utf-8")
    w = Widget(hooks_on=True)
    w._drain_hook_events()
    assert "permission" in w._sess_hook_notes["a"]


def test_stop_clears_the_note():
    hooks.spool_dir().mkdir(parents=True, exist_ok=True)
    (hooks.spool_dir() / "n.json").write_text(json.dumps({
        "hook_event_name": "Notification", "session_id": "a",
        "message": "waiting"}), encoding="utf-8")
    w = Widget(hooks_on=True)
    w._drain_hook_events()
    (hooks.spool_dir() / "s.json").write_text(json.dumps({
        "hook_event_name": "Stop", "session_id": "a"}), encoding="utf-8")
    w._drain_hook_events()
    assert "a" not in w._sess_hook_notes


def test_hook_note_without_a_session_id_is_ignored():
    hooks.spool_dir().mkdir(parents=True, exist_ok=True)
    (hooks.spool_dir() / "n.json").write_text(json.dumps({
        "hook_event_name": "Notification", "message": "hi"}), encoding="utf-8")
    w = Widget(hooks_on=True)
    w._drain_hook_events()
    assert w._sess_hook_notes == {}


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #
def test_a_burst_of_alerts_becomes_one_toast():
    w = Widget()
    busy = [_session(session_id=str(i), pid=i, status=sc.BUSY) for i in range(3)]
    idle = [_session(session_id=str(i), pid=i, status=sc.IDLE) for i in range(3)]
    w._sess_tracker.update(busy)
    events = w._sess_tracker.update(idle)
    w._session_alerts(events, idle)
    assert len(w.toasts) == 1
    assert w.toasts[0][0] == "3 sessions finished"


def test_startup_is_silent():
    w = Widget()
    w._sess_seeded = False
    live = [_session(status=sc.WAITING)]
    events = w._sess_tracker.update(live)
    w._session_alerts(events, live)
    assert w.toasts == []


def test_nothing_alerts_while_hidden():
    w = Widget()
    w._hidden = True
    busy = [_session(status=sc.BUSY)]
    w._sess_tracker.update(busy)
    events = w._sess_tracker.update([_session(status=sc.IDLE)])
    w._session_alerts(events, [_session(status=sc.IDLE)])
    assert w.toasts == []


def test_a_block_during_a_fullscreen_app_is_reported_afterwards():
    base = sc.now_ms() - 40 * 60_000
    blocked = [_session(status=sc.WAITING, status_updated_at=base)]
    w = Widget(stuck=10)
    w._hidden = True
    w._session_alerts([], blocked)
    assert w.toasts == []
    w._hidden = False
    w._session_alerts([], blocked)
    assert len(w.toasts) == 1 and "waiting" in w.toasts[0][0].lower()


def test_alerts_can_be_switched_off():
    w = Widget()
    w._sess_alerts_on = False
    busy = [_session(status=sc.BUSY)]
    w._sess_tracker.update(busy)
    events = w._sess_tracker.update([_session(status=sc.IDLE)])
    w._session_alerts(events, [_session(status=sc.IDLE)])
    assert w.toasts == []


# --------------------------------------------------------------------------- #
# The tick itself
# --------------------------------------------------------------------------- #
def test_tick_is_inert_with_sessions_off():
    w = Widget(sessions_on=False)
    w._sessions_tick()
    assert w._sess_disp == {}


def test_tick_is_inert_during_the_demo():
    w = Widget()
    w._demo = True
    w._sessions_tick()
    assert w._sess_disp == {}
    assert w._with_sessions({"a": 1}) == {"a": 1}


def test_with_sessions_leaves_the_usage_meter_alone():
    w = Widget()
    w._sess_disp = sc.format_sessions([_session(status=sc.WAITING)])
    merged = w._with_sessions({"session_pct": 61, "session_color": "amber"})
    assert merged["session_pct"] == 61
    assert merged["session_color"] == "amber"      # not the sessions colour
    assert merged["sessions_color"] == "red"


def test_tick_never_raises_when_the_registry_is_unreadable(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(widget_bar.sessions_core, "snapshot", boom)
    monkeypatch.setattr(widget_bar, "_log_exc", lambda *a: None)
    w = Widget()
    w._sessions_tick()                 # must not raise
    assert w._sess_disp == {}
