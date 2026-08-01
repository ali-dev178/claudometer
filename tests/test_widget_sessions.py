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
        self._sess_sticky_pid = 0
        self._sess_disp = {}
        self.toasts = []
        self.pulses = 0

    def _show_toast(self, pct, title, subtitle, color, duration=6500,
                    on_click=None):
        self.toasts.append((title, subtitle, color, duration, on_click))

    def _pulse_strip(self, step=0):
        self.pulses = getattr(self, "pulses", 0) + 1

    _retire_sticky_toast = widget_bar.BarWidget._retire_sticky_toast
    _toast = None

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


# --------------------------------------------------------------------------- #
# Attention: sticky toasts, the jump, and the pulse
# --------------------------------------------------------------------------- #
def _block(w, pid=7):
    live = [_session(pid=pid, status=sc.WAITING, waiting_for="input needed")]
    w._sess_tracker.update([_session(pid=pid, status=sc.BUSY)])
    events = w._sess_tracker.update(live)
    w._sess_disp = sc.format_sessions(live)
    w._session_alerts(events, live)
    return live


def test_a_needs_you_toast_is_sticky_and_clickable():
    w = Widget()
    _block(w)
    title, _sub, _color, duration, on_click = w.toasts[-1]
    assert title == "Needs you"
    assert duration is None, "a request must wait for you, not time out"
    assert callable(on_click), "clicking it should take you to that terminal"


def test_a_finished_toast_still_times_out():
    w = Widget()
    busy = [_session(status=sc.BUSY)]
    w._sess_tracker.update(busy)
    events = w._sess_tracker.update([_session(status=sc.IDLE)])
    w._session_alerts(events, [_session(status=sc.IDLE)])
    assert w.toasts[-1][3] == 6500
    assert w.toasts[-1][4] is None


def test_becoming_blocked_pulses_the_strip():
    w = Widget()
    _block(w)
    assert w.pulses == 1


def test_finishing_does_not_pulse():
    w = Widget()
    busy = [_session(status=sc.BUSY)]
    w._sess_tracker.update(busy)
    events = w._sess_tracker.update([_session(status=sc.IDLE)])
    w._session_alerts(events, [_session(status=sc.IDLE)])
    assert w.pulses == 0


def test_the_sticky_toast_retires_when_the_session_unblocks():
    closed = []

    class T:
        def close(self):
            closed.append(1)

    w = Widget()
    _block(w, pid=7)
    assert w._sess_sticky_pid == 7
    w._toast = T()
    w._retire_sticky_toast([_session(pid=7, status=sc.BUSY)])   # answered
    assert w._sess_sticky_pid == 0 and closed == [1]


def test_the_sticky_toast_is_dismissed_when_sessions_are_turned_off():
    closed = []

    class T:
        def close(self):
            closed.append(1)

    w = Widget()
    _block(w, pid=7)
    w._toast = T()
    w._sessions_on = False
    w._sessions_tick()
    assert w._sess_sticky_pid == 0 and closed == [1], (
        "a sticky toast has no timeout — switching the monitor off must take "
        "it down, or it stays on screen forever")


# --------------------------------------------------------------------------- #
# Answering a blocked session — this types into a live AI session
# --------------------------------------------------------------------------- #
class Answering(Widget):
    _sess_answer_on = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.opened = []
        self.focused = []
        self.notes = []

    _row_for_pid = widget_bar.BarWidget._row_for_pid
    _send_answer = widget_bar.BarWidget._send_answer
    _act_on_session = widget_bar.BarWidget._act_on_session

    def _open_answer(self, row):
        self.opened.append(row)

    def _focus_session(self, row):
        self.focused.append(row)

    def _notify_session(self, msg):
        self.notes.append(msg)


def _blocked_disp(pid=7):
    return sc.format_sessions([_session(pid=pid, status=sc.WAITING,
                                        waiting_for="input needed")])


def test_a_blocked_row_opens_the_answer_window(monkeypatch):
    monkeypatch.setattr(widget_bar.console_send, "can_send", lambda: True)
    w = Answering()
    w._sess_disp = _blocked_disp()
    w._act_on_session(w._sess_disp["sessions_rows"][0])
    assert len(w.opened) == 1 and not w.focused


def test_a_working_row_goes_to_the_terminal(monkeypatch):
    monkeypatch.setattr(widget_bar.console_send, "can_send", lambda: True)
    w = Answering()
    w._sess_disp = sc.format_sessions([_session(pid=7, status=sc.BUSY)])
    w._act_on_session(w._sess_disp["sessions_rows"][0])
    assert len(w.focused) == 1 and not w.opened


def test_falls_back_to_the_terminal_where_sending_is_impossible(monkeypatch):
    monkeypatch.setattr(widget_bar.console_send, "can_send", lambda: False)
    w = Answering()
    w._sess_disp = _blocked_disp()
    w._act_on_session(w._sess_disp["sessions_rows"][0])
    assert len(w.focused) == 1 and not w.opened


def test_switching_the_feature_off_goes_to_the_terminal(monkeypatch):
    monkeypatch.setattr(widget_bar.console_send, "can_send", lambda: True)
    w = Answering()
    w._sess_answer_on = False
    w._sess_disp = _blocked_disp()
    w._act_on_session(w._sess_disp["sessions_rows"][0])
    assert len(w.focused) == 1 and not w.opened


def test_send_refuses_empty_text_when_not_submitting():
    w = Answering()
    w._sess_disp = _blocked_disp()
    ok, err = w._send_answer(w._sess_disp["sessions_rows"][0], "   ",
                             submit=False)
    assert ok is False and "something" in err.lower()


def test_a_bare_enter_is_allowed_through(monkeypatch):
    # Accepting the highlighted option in a numbered menu sends only Enter.
    sent = []
    monkeypatch.setattr(widget_bar.console_send, "send_text",
                        lambda pid, text, **k: sent.append((pid, text, k)) or (True, None))
    w = Answering()
    w._sess_disp = _blocked_disp(pid=4242)
    ok, _ = w._send_answer(w._sess_disp["sessions_rows"][0], "", submit=True)
    assert ok is True
    assert sent == [(4242, "", {"submit": True})], sent


def test_send_refuses_a_session_that_has_gone(monkeypatch):
    sent = []
    monkeypatch.setattr(widget_bar.console_send, "send_text",
                        lambda *a, **k: sent.append(a) or (True, None))
    w = Answering()
    row = _blocked_disp()["sessions_rows"][0]
    w._sess_disp = sc.format_sessions([])          # it ended meanwhile
    ok, err = w._send_answer(row, "yes")
    assert ok is False and "gone" in err.lower()
    assert not sent, "nothing may be typed into a session that no longer exists"


def test_send_refuses_a_session_that_stopped_waiting(monkeypatch):
    sent = []
    monkeypatch.setattr(widget_bar.console_send, "send_text",
                        lambda *a, **k: sent.append(a) or (True, None))
    w = Answering()
    row = _blocked_disp()["sessions_rows"][0]
    # It was answered in the terminal while the window was open.
    w._sess_disp = sc.format_sessions([_session(pid=7, status=sc.BUSY)])
    ok, err = w._send_answer(row, "yes")
    assert ok is False and "waiting" in err.lower()
    assert not sent, "an answer to a question already dealt with must not land"


def test_a_good_send_reaches_the_right_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(widget_bar.console_send, "send_text",
                        lambda pid, text, **k: sent.append((pid, text)) or (True, None))
    w = Answering()
    w._sess_disp = _blocked_disp(pid=4242)
    ok, err = w._send_answer(w._sess_disp["sessions_rows"][0], " yes ")
    assert ok is True and err is None
    assert sent == [(4242, "yes")], sent


def test_a_successful_send_retires_the_sticky_toast(monkeypatch):
    closed = []

    class T:
        def close(self):
            closed.append(1)

    monkeypatch.setattr(widget_bar.console_send, "send_text",
                        lambda *a, **k: (True, None))
    w = Answering()
    w._sess_disp = _blocked_disp()
    w._sess_sticky_pid = 7
    w._toast = T()
    w._send_answer(w._sess_disp["sessions_rows"][0], "yes")
    assert w._sess_sticky_pid == 0 and closed == [1]


def test_a_failed_send_reports_the_reason(monkeypatch):
    monkeypatch.setattr(widget_bar.console_send, "send_text",
                        lambda *a, **k: (False, "that session's console isn't reachable"))
    w = Answering()
    w._sess_disp = _blocked_disp()
    ok, err = w._send_answer(w._sess_disp["sessions_rows"][0], "yes")
    assert ok is False and "console" in err


def test_the_row_carries_the_question():
    rows = _blocked_disp()["sessions_rows"]
    assert rows[0]["question"] == "input needed"


def test_a_working_row_has_no_question():
    rows = sc.format_sessions([_session(status=sc.BUSY)])["sessions_rows"]
    assert rows[0]["question"] == ""


# --------------------------------------------------------------------------- #
# Readable text on whatever the strip is sitting on
# --------------------------------------------------------------------------- #
def test_contrast_ratio_extremes():
    assert round(widget_bar._contrast((0, 0, 0), (255, 255, 255)), 1) == 21.0
    assert round(widget_bar._contrast((80, 80, 80), (80, 80, 80)), 1) == 1.0


@pytest.mark.parametrize("rgb,expected", [
    ((233, 238, 243), "light"),      # a light taskbar
    ((32, 32, 32), "dark"),          # a dark taskbar
    ((255, 255, 255), "light"),
    ((0, 0, 0), "dark"),
])
def test_theme_matches_the_obvious_cases(rgb, expected):
    assert widget_bar._theme_for_bg(rgb) == expected


@pytest.mark.parametrize("rgb", [(58, 110, 220), (128, 128, 128), (0, 150, 160),
                                 (222, 205, 180), (120, 90, 30)])
def test_theme_always_picks_the_more_readable_of_the_two(rgb):
    import render

    chosen = widget_bar._theme_for_bg(rgb)
    ratios = {}
    for name, theme in render.THEMES.items():
        text = theme["neutral"].lstrip("#")
        ratios[name] = widget_bar._contrast(
            rgb, tuple(int(text[i:i + 2], 16) for i in (0, 2, 4)))
    assert ratios[chosen] == max(ratios.values()), (
        f"{rgb} picked {chosen} at {ratios[chosen]:.1f}:1 over "
        f"{max(ratios, key=ratios.get)} at {max(ratios.values()):.1f}:1")


def test_the_sticky_toast_survives_while_still_blocked():
    closed = []

    class T:
        def close(self):
            closed.append(1)

    w = Widget()
    _block(w, pid=7)
    w._toast = T()
    w._retire_sticky_toast([_session(pid=7, status=sc.WAITING)])
    assert w._sess_sticky_pid == 7 and closed == []


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


def test_tick_leaves_the_demo_alone():
    # The tour drives the list itself; the live tick must not clobber it.
    w = Widget()
    w._demo = True
    w._sess_disp = {"sessions_count": 3}
    w._sessions_tick()
    assert w._sess_disp == {"sessions_count": 3}


# --------------------------------------------------------------------------- #
# The demo tour
# --------------------------------------------------------------------------- #
class DemoWidget(Widget):
    _demo = True
    _sess_alerts_on = False          # the tour must alert anyway
    _sess_alert_on = ()              # ...and show every kind

    _demo_session_set = widget_bar.BarWidget._demo_session_set
    _demo_sessions_tick = widget_bar.BarWidget._demo_sessions_tick
    _demo_timeline = widget_bar.BarWidget._demo_timeline
    _reset_demo_sessions = widget_bar.BarWidget._reset_demo_sessions
    _DEMO_SESSIONS = widget_bar.BarWidget._DEMO_SESSIONS


def test_demo_timeline_scenes_carry_sessions():
    w = DemoWidget()
    scenes = w._demo_timeline()
    with_sessions = [s for s, _hold in scenes if "_sessions" in s]
    assert len(with_sessions) >= 8, "the dot row should be live for most of the tour"


def test_demo_session_set_builds_real_sessions():
    w = DemoWidget()
    live = w._demo_session_set([(0, sc.WAITING, "input needed"),
                                (1, sc.BUSY, "")])
    assert all(isinstance(s, sc.Session) for s in live)
    assert live[0].status == sc.WAITING and live[0].waiting_for == "input needed"
    assert live[0].title and live[0].project


def test_demo_alerts_even_when_the_user_turned_them_off():
    w = DemoWidget()
    w._sess_seeded = True
    busy = w._demo_session_set([(0, sc.BUSY, "")])
    w._sess_tracker.update(busy)
    blocked = w._demo_session_set([(0, sc.WAITING, "input needed")])
    events = w._sess_tracker.update(blocked)
    w._sess_disp = sc.format_sessions(blocked)
    w._session_alerts(events, blocked)
    assert w.toasts and w.toasts[-1][0] == "Needs you"


def test_demo_shows_every_alert_kind():
    # The user's sessions_alert_on is empty above; the tour overrides it.
    w = DemoWidget()
    w._sess_seeded = True
    live = w._demo_session_set([(0, sc.BUSY, "")])
    w._sess_tracker.update(live)
    events = w._sess_tracker.update([])          # it ended
    w._sess_disp = {}
    w._session_alerts(events, [])
    assert w.toasts and "ended" in w.toasts[-1][0].lower()


def test_demo_first_scene_is_silent():
    w = DemoWidget()
    w._reset_demo_sessions()
    scenes = w._demo_timeline()
    first = next(s for s, _h in scenes if "_sessions" in s)
    w._demo_sessions_tick(first["_sessions"])
    assert w.toasts == [], "the opening scene is a baseline, not news"


def test_demo_run_produces_the_headline_moments():
    """Walk the whole tour and check it actually demonstrates the feature."""
    w = DemoWidget()
    w._reset_demo_sessions()
    for scene, _hold in w._demo_timeline():
        live = scene.get("_sessions")
        if live is None:
            continue
        w._demo_sessions_tick(live)
    titles = [t[0] for t in w.toasts]
    assert "Needs you" in titles, "a session blocking is the whole point"
    assert any("finished" in t.lower() for t in titles)
    assert w.pulses >= 1, "the strip should pulse when something blocks"
    # A blocked scene must produce a sticky, clickable toast.
    sticky = [t for t in w.toasts if t[0] == "Needs you"]
    assert sticky and sticky[0][3] is None and callable(sticky[0][4])


def test_demo_reset_clears_everything():
    w = DemoWidget()
    w._sess_disp = {"sessions_count": 3}
    w._sess_sticky_pid = 42
    w._flash = True
    w._reset_demo_sessions()
    assert w._sess_disp == {} and w._sess_sticky_pid == 0
    assert w._flash is False and w._sess_seeded is False
    assert w._sess_tracker.sessions == [] and w._sess_tracker.recent == []


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
