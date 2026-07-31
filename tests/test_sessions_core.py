"""Hermetic pytest suite for sessions_core.py.

Covers the PURE logic without ever spawning a process or reading the user's
real ~/.claude:

  * status/label/color vocabulary, dwell math and formatting, display ordering
  * process liveness — including the guarantee that ``os.kill`` is never used
    on Windows (where CPython maps it onto TerminateProcess)
  * registry parsing, health reporting, dead-PID and recycled-PID rejection
  * the ``claude agents --json`` fallback and its rate limit, with
    ``subprocess.run`` monkeypatched so NO real CLI is ever invoked
  * snapshot's decision about when the fallback is allowed to run
  * transition diffing, transcript location, tail reading and enrichment

Every test writes only under ``tmp_path`` and never touches the network.
"""

import json
import os
import sys

import pytest

import sessions_core as sc


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """No ambient CLAUDE_CONFIG_DIR, and Path.home() points inside tmp_path so
    the ~/.claude fallback can never reach the real user's data."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(sc.Path, "home", staticmethod(lambda: fake_home))
    sc.reset_cli_cache()
    sc.reset_transcript_cache()
    yield fake_home
    sc.reset_cli_cache()
    sc.reset_transcript_cache()


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Hard stop: nothing in this suite may spawn a real process."""
    def boom(*a, **k):
        raise AssertionError("test tried to spawn a real subprocess: %r" % (a,))

    monkeypatch.setattr(sc.subprocess, "run", boom)


def _alive_all(_pid):
    return True


def _alive_none(_pid):
    return False


def _write_session(cfg, pid, **fields):
    """Write a <pid>.json registry file under *cfg*/sessions."""
    sdir = cfg / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": f"sid-{pid}", "cwd": str(cfg / "proj"),
            "kind": "interactive", "status": "idle"}
    data.update(fields)
    (sdir / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _session(**fields):
    base = {"pid": 1, "session_id": "s1", "cwd": "/tmp/proj", "status": sc.IDLE}
    base.update(fields)
    return sc.Session(**base)


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
def test_known_statuses_match_the_cli_enum():
    assert sc.KNOWN_STATUSES == ("busy", "shell", "idle", "waiting")


@pytest.mark.parametrize("status,color", [
    (sc.WAITING, "red"), (sc.BUSY, "amber"), (sc.SHELL, "accent"),
    (sc.IDLE, "green"), (sc.UNKNOWN, "grey"),
])
def test_status_color(status, color):
    assert sc.status_color(status) == color


def test_status_color_unrecognized_is_grey():
    assert sc.status_color("banana") == "grey"


def test_status_label_unrecognized_is_unknown():
    assert sc.status_label("banana") == "unknown"


def test_colors_use_only_real_theme_keys():
    # Guards against inventing a color the renderer has no entry for.
    from render import THEMES
    for theme in THEMES.values():
        for status in list(sc.KNOWN_STATUSES) + [sc.UNKNOWN]:
            assert sc.status_color(status) in theme


def test_status_text_plain():
    assert _session(status=sc.IDLE).status_text == "done"


def test_status_text_includes_waiting_reason():
    s = _session(status=sc.WAITING, waiting_for="input needed")
    assert s.status_text == "needs you: input needed"


def test_status_text_waiting_without_reason():
    assert _session(status=sc.WAITING).status_text == "needs you"


def test_waiting_reason_ignored_when_not_waiting():
    s = _session(status=sc.BUSY, waiting_for="stale reason")
    assert s.status_text == "working"


# --------------------------------------------------------------------------- #
# Session derived properties
# --------------------------------------------------------------------------- #
def test_project_is_basename():
    assert _session(cwd="/a/b/claude-widget").project == "claude-widget"


def test_project_handles_windows_separators_and_trailing_slash():
    assert _session(cwd="C:\\Personal Space\\widget\\").project == "widget"


def test_project_falls_back_to_cwd_when_no_basename():
    assert _session(cwd="/").project == "/"


def test_label_prefers_title_then_name_then_project():
    assert _session(title="T", name="N", cwd="/a/P").label == "T"
    assert _session(name="N", cwd="/a/P").label == "N"
    assert _session(cwd="/a/P").label == "P"


def test_label_final_fallback():
    assert _session(cwd="").label == "session"


def test_is_background_for_both_spellings():
    assert _session(kind="bg").is_background
    assert _session(kind="background").is_background
    assert not _session(kind="interactive").is_background


def test_color_property_matches_status():
    assert _session(status=sc.WAITING).color == "red"


# --------------------------------------------------------------------------- #
# Dwell
# --------------------------------------------------------------------------- #
def test_dwell_none_without_timestamps():
    assert sc.dwell_seconds(_session()) is None


def test_dwell_uses_status_updated_at():
    s = _session(status_updated_at=1_000_000, updated_at=1)
    assert sc.dwell_seconds(s, at_ms=1_060_000) == 60


def test_dwell_falls_back_to_updated_at():
    s = _session(updated_at=1_000_000)
    assert sc.dwell_seconds(s, at_ms=1_030_000) == 30


def test_dwell_never_negative_on_clock_skew():
    s = _session(status_updated_at=2_000_000)
    assert sc.dwell_seconds(s, at_ms=1_000_000) == 0


def test_dwell_uses_wall_clock_when_no_at_ms(monkeypatch):
    monkeypatch.setattr(sc, "now_ms", lambda: 5_000_000)
    assert sc.dwell_seconds(_session(status_updated_at=4_000_000)) == 1000


@pytest.mark.parametrize("secs,text", [
    (None, ""), (0, "just now"), (9, "just now"), (10, "10s"), (59, "59s"),
    (60, "1m"), (119, "1m"), (3599, "59m"), (3600, "1h 00m"),
    (7500, "2h 05m"), (86399, "23h 59m"), (86400, "1d 0h"), (273600, "3d 4h"),
])
def test_fmt_dwell(secs, text):
    assert sc.fmt_dwell(secs) == text


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #
def test_sort_puts_waiting_first_then_running_then_idle():
    idle = _session(session_id="i", status=sc.IDLE, status_updated_at=900)
    busy = _session(session_id="b", status=sc.BUSY, status_updated_at=900)
    wait = _session(session_id="w", status=sc.WAITING, status_updated_at=900)
    order = [s.session_id for s in sc.sort_sessions([idle, busy, wait], at_ms=1000)]
    assert order == ["w", "b", "i"]


def test_shell_ranks_with_busy_not_after_idle():
    idle = _session(session_id="i", status=sc.IDLE, status_updated_at=900)
    shell = _session(session_id="s", status=sc.SHELL, status_updated_at=900)
    order = [s.session_id for s in sc.sort_sessions([idle, shell], at_ms=1000)]
    assert order == ["s", "i"]


def test_unknown_sorts_last():
    unknown = _session(session_id="u", status=sc.UNKNOWN, status_updated_at=900)
    idle = _session(session_id="i", status=sc.IDLE, status_updated_at=900)
    order = [s.session_id for s in sc.sort_sessions([unknown, idle], at_ms=1000)]
    assert order == ["i", "u"]


def test_longest_blocked_waiting_session_leads():
    recent = _session(session_id="recent", status=sc.WAITING,
                      status_updated_at=9_000)
    stuck = _session(session_id="stuck", status=sc.WAITING,
                     status_updated_at=1_000)
    order = [s.session_id
             for s in sc.sort_sessions([recent, stuck], at_ms=10_000_000)]
    assert order == ["stuck", "recent"]


def test_most_recently_active_leads_among_idle():
    old = _session(session_id="old", status=sc.IDLE, status_updated_at=1_000)
    new = _session(session_id="new", status=sc.IDLE, status_updated_at=9_000)
    order = [s.session_id for s in sc.sort_sessions([old, new], at_ms=10_000)]
    assert order == ["new", "old"]


def test_sort_is_stable_and_total_for_identical_sessions():
    a = _session(session_id="a", pid=2, name="same", status=sc.IDLE)
    b = _session(session_id="b", pid=1, name="same", status=sc.IDLE)
    order = [s.pid for s in sc.sort_sessions([a, b], at_ms=1000)]
    assert order == [1, 2]          # tie broken by pid, never raises


def test_sort_does_not_mutate_input():
    items = [_session(session_id="i", status=sc.IDLE),
             _session(session_id="w", status=sc.WAITING)]
    sc.sort_sessions(items, at_ms=1000)
    assert [s.session_id for s in items] == ["i", "w"]


# --------------------------------------------------------------------------- #
# stuck_sessions
# --------------------------------------------------------------------------- #
def test_stuck_sessions_only_reports_waiting():
    busy = _session(session_id="b", status=sc.BUSY, status_updated_at=0)
    assert sc.stuck_sessions([busy], minutes=1, at_ms=10_000_000) == []


def test_stuck_sessions_respects_threshold():
    base = 1_000_000
    s = _session(status=sc.WAITING, status_updated_at=base)
    assert sc.stuck_sessions([s], minutes=10, at_ms=base + 9 * 60 * 1000) == []
    assert sc.stuck_sessions([s], minutes=10, at_ms=base + 10 * 60 * 1000) == [s]


def test_stuck_sessions_skips_unknown_dwell():
    s = _session(status=sc.WAITING)   # no timestamps at all
    assert sc.stuck_sessions([s], minutes=0, at_ms=10_000) == []


# --------------------------------------------------------------------------- #
# Liveness
# --------------------------------------------------------------------------- #
def test_pid_alive_true_for_this_process():
    assert sc.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_impossible_pids():
    assert sc.pid_alive(0) is False
    assert sc.pid_alive(-5) is False


@pytest.mark.parametrize("value", [None, "", "abc", 3.5j, [], {}])
def test_pid_alive_false_for_garbage(value):
    assert sc.pid_alive(value) is False


def test_pid_alive_accepts_numeric_string():
    assert sc.pid_alive(str(os.getpid())) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific danger")
def test_pid_alive_never_calls_os_kill_on_windows(monkeypatch):
    # os.kill on Windows maps onto TerminateProcess for signal 0 — using it as
    # a liveness probe would kill the user's Claude session.
    def forbidden(*a, **k):
        raise AssertionError("os.kill must never be used on Windows")

    monkeypatch.setattr(os, "kill", forbidden)
    assert sc.pid_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX branch")
def test_pid_alive_posix_permission_error_means_alive(monkeypatch):
    def denied(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert sc.pid_alive(12345) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX branch")
def test_pid_alive_posix_lookup_error_means_dead(monkeypatch):
    def missing(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", missing)
    assert sc.pid_alive(12345) is False


@pytest.mark.skipif(sys.platform != "win32", reason="GetProcessTimes is Windows-only")
def test_proc_start_ms_is_sane_for_this_process():
    started = sc.proc_start_ms(os.getpid())
    assert started is not None
    # This test process began in the past, and well after the year 2000.
    assert 946_684_800_000 < started <= sc.now_ms() + 1000


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows returns None")
def test_proc_start_ms_none_off_windows():
    assert sc.proc_start_ms(os.getpid()) is None


def test_proc_start_ms_none_for_garbage():
    assert sc.proc_start_ms("nope") is None
    assert sc.proc_start_ms(0) is None


# --------------------------------------------------------------------------- #
# pid_matches_session — deliberately conservative
# --------------------------------------------------------------------------- #
def test_pid_match_true_when_started_at_unknown():
    assert sc.pid_matches_session(1234, 0) is True


def test_pid_match_true_when_proc_start_unreadable(monkeypatch):
    monkeypatch.setattr(sc, "proc_start_ms", lambda _pid: None)
    assert sc.pid_matches_session(1234, 1_000_000) is True


def test_pid_match_true_within_tolerance(monkeypatch):
    monkeypatch.setattr(sc, "proc_start_ms", lambda _pid: 1_000_000)
    assert sc.pid_matches_session(1234, 1_030_000) is True


def test_pid_match_false_when_recycled(monkeypatch):
    monkeypatch.setattr(sc, "proc_start_ms", lambda _pid: 1_000_000)
    # Two hours apart: this PID belongs to some other process now.
    assert sc.pid_matches_session(1234, 1_000_000 + 7_200_000) is False


def test_pid_match_tolerance_is_configurable(monkeypatch):
    monkeypatch.setattr(sc, "proc_start_ms", lambda _pid: 1_000_000)
    assert sc.pid_matches_session(1234, 1_005_000, tolerance_s=1) is False
    assert sc.pid_matches_session(1234, 1_005_000, tolerance_s=10) is True


# --------------------------------------------------------------------------- #
# parse_session
# --------------------------------------------------------------------------- #
def test_parse_session_full_record():
    data = {
        "pid": 28692, "sessionId": "da55", "cwd": "C:\\Personal Space\\w",
        "startedAt": 1785508278847, "version": "2.1.220", "kind": "interactive",
        "entrypoint": "cli", "name": "claude-widget-b0", "status": "busy",
        "updatedAt": 1785508479465, "statusUpdatedAt": 1785508479465,
        "waitingFor": "", "agent": "a", "state": "success", "detail": "d",
        "tempo": "active", "needs": "n", "jobId": "j", "logPath": "l",
        "tmux": "t",
    }
    s = sc.parse_session(data)
    assert s.pid == 28692 and s.session_id == "da55"
    assert s.name == "claude-widget-b0" and s.status == "busy"
    assert s.status_updated_at == 1785508479465
    assert s.kind == "interactive" and s.entrypoint == "cli"
    assert s.agent == "a" and s.state == "success" and s.tempo == "active"
    assert s.job_id == "j" and s.log_path == "l" and s.tmux == "t"
    assert s.source == "file"


def test_parse_session_requires_session_id():
    assert sc.parse_session({"pid": 1, "status": "idle"}) is None


def test_parse_session_rejects_non_dict():
    for value in (None, [], "x", 3):
        assert sc.parse_session(value) is None


def test_parse_session_unknown_status_becomes_unknown():
    assert sc.parse_session({"sessionId": "s", "status": "wat"}).status == sc.UNKNOWN


def test_parse_session_missing_status_becomes_unknown():
    assert sc.parse_session({"sessionId": "s"}).status == sc.UNKNOWN


def test_parse_session_normalizes_background_kind():
    assert sc.parse_session({"sessionId": "s", "kind": "background"}).kind == "bg"


def test_parse_session_falls_back_to_filename_pid():
    assert sc.parse_session({"sessionId": "s"}, pid=77).pid == 77


def test_parse_session_prefers_pid_in_payload():
    assert sc.parse_session({"sessionId": "s", "pid": 5}, pid=77).pid == 5


def test_parse_session_coerces_wrong_types():
    s = sc.parse_session({"sessionId": "s", "pid": "12", "name": 5,
                          "startedAt": "99", "updatedAt": None})
    assert s.pid == 12 and s.name == "" and s.started_at == 99
    assert s.updated_at == 0


def test_parse_session_booleans_are_not_ints():
    s = sc.parse_session({"sessionId": "s", "startedAt": True})
    assert s.started_at == 0


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def test_scan_no_dir(tmp_path):
    assert sc.scan(tmp_path / "nothing") == ([], "no-dir")


def test_scan_empty_dir_is_healthy(tmp_path):
    (tmp_path / "sessions").mkdir()
    assert sc.scan(tmp_path) == ([], "ok")


def test_scan_reads_a_live_session(tmp_path):
    _write_session(tmp_path, 4242, status="waiting", waitingFor="input needed")
    sessions, health = sc.scan(tmp_path, alive=_alive_all)
    assert health == "ok" and len(sessions) == 1
    assert sessions[0].status == "waiting"
    assert sessions[0].waiting_for == "input needed"


def test_scan_drops_dead_pids(tmp_path):
    _write_session(tmp_path, 4242)
    sessions, health = sc.scan(tmp_path, alive=_alive_none)
    assert sessions == [] and health == "ok"     # healthy: parsed, just not live


def test_scan_drops_recycled_pids(tmp_path, monkeypatch):
    _write_session(tmp_path, 4242, startedAt=1_000_000)
    monkeypatch.setattr(sc, "proc_start_ms", lambda _pid: 9_000_000)
    sessions, _ = sc.scan(tmp_path, alive=_alive_all)
    assert sessions == []


def test_scan_keeps_pid_when_creation_time_unreadable(tmp_path, monkeypatch):
    _write_session(tmp_path, 4242, startedAt=1_000_000)
    monkeypatch.setattr(sc, "proc_start_ms", lambda _pid: None)
    sessions, _ = sc.scan(tmp_path, alive=_alive_all)
    assert len(sessions) == 1


def test_scan_ignores_non_pid_filenames(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "notes.json").write_text(json.dumps({"sessionId": "x"}),
                                     encoding="utf-8")
    assert sc.scan(tmp_path, alive=_alive_all) == ([], "ok")


def test_scan_ignores_non_json_files(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "1234.lock").write_text("", encoding="utf-8")
    assert sc.scan(tmp_path, alive=_alive_all) == ([], "ok")


def test_scan_survives_a_corrupt_file(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "1.json").write_text("{ broken", encoding="utf-8")
    _write_session(tmp_path, 2)
    sessions, health = sc.scan(tmp_path, alive=_alive_all)
    assert [s.pid for s in sessions] == [2]
    assert health == "ok"


def test_scan_reports_unparsable_when_schema_changed(tmp_path):
    # Files are present and valid JSON, but carry no session id any more.
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "1.json").write_text(json.dumps({"process": 1}), encoding="utf-8")
    (sdir / "2.json").write_text(json.dumps({"process": 2}), encoding="utf-8")
    assert sc.scan(tmp_path, alive=_alive_all) == ([], "unparsable")


def test_scan_corrupt_only_is_unparsable(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "1.json").write_text("{ broken", encoding="utf-8")
    assert sc.scan(tmp_path, alive=_alive_all) == ([], "unparsable")


def test_scan_uses_env_config_dir(monkeypatch, tmp_path):
    _write_session(tmp_path, 7)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    sessions, _ = sc.scan(alive=_alive_all)
    assert [s.pid for s in sessions] == [7]


def test_scan_reads_multiple_sessions(tmp_path):
    _write_session(tmp_path, 1, status="busy")
    _write_session(tmp_path, 2, status="idle")
    sessions, _ = sc.scan(tmp_path, alive=_alive_all)
    assert sorted(s.pid for s in sessions) == [1, 2]


# --------------------------------------------------------------------------- #
# CLI fallback
# --------------------------------------------------------------------------- #
class _FakeRun:
    """Stand-in for subprocess.run that records calls and replays a result."""

    def __init__(self, stdout="", returncode=0, raises=None):
        self.stdout, self.returncode, self.raises = stdout, returncode, raises
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.raises:
            raise self.raises
        return type("R", (), {"stdout": self.stdout, "returncode": self.returncode,
                              "stderr": ""})()


CLI_JSON = json.dumps([
    {"pid": 1, "cwd": "/a", "kind": "interactive", "startedAt": 10,
     "sessionId": "s1", "name": "one", "status": "busy"},
    {"pid": 2, "cwd": "/b", "kind": "background", "startedAt": 20,
     "sessionId": "s2", "name": "two", "status": "waiting",
     "waitingFor": "input needed"},
])


def test_sessions_from_cli_parses_output(monkeypatch):
    fake = _FakeRun(stdout=CLI_JSON)
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sessions = sc.sessions_from_cli()
    assert [s.session_id for s in sessions] == ["s1", "s2"]
    assert all(s.source == "cli" for s in sessions)
    assert sessions[1].kind == "bg"          # "background" normalized back
    assert sessions[1].waiting_for == "input needed"


def test_sessions_from_cli_invokes_agents_json(monkeypatch):
    fake = _FakeRun(stdout="[]")
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc.sessions_from_cli()
    assert fake.calls[0]["args"] == ["claude", "agents", "--json"]


def test_sessions_from_cli_all_flag(monkeypatch):
    fake = _FakeRun(stdout="[]")
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc.sessions_from_cli(include_done=True)
    assert fake.calls[0]["args"] == ["claude", "agents", "--json", "--all"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console flash")
def test_sessions_from_cli_suppresses_console_window(monkeypatch):
    fake = _FakeRun(stdout="[]")
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc.sessions_from_cli()
    assert fake.calls[0]["kwargs"].get("creationflags")


def test_sessions_from_cli_falls_back_to_claude_cmd(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "claude":
            raise OSError("not found")
        return type("R", (), {"stdout": CLI_JSON, "returncode": 0, "stderr": ""})()

    monkeypatch.setattr(sc.subprocess, "run", run)
    assert len(sc.sessions_from_cli()) == 2
    assert [c[0] for c in calls] == ["claude", "claude.cmd"]


def test_sessions_from_cli_empty_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout=CLI_JSON, returncode=1))
    assert sc.sessions_from_cli() == []


def test_sessions_from_cli_empty_on_bad_json(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout="not json"))
    assert sc.sessions_from_cli() == []


def test_sessions_from_cli_empty_on_non_list_json(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout='{"a": 1}'))
    assert sc.sessions_from_cli() == []


def test_sessions_from_cli_empty_on_timeout(monkeypatch):
    monkeypatch.setattr(
        sc.subprocess, "run",
        _FakeRun(raises=sc.subprocess.TimeoutExpired(cmd="claude", timeout=1)))
    assert sc.sessions_from_cli() == []


def test_sessions_from_cli_skips_unusable_entries(monkeypatch):
    payload = json.dumps([{"pid": 1}, {"sessionId": "ok", "pid": 2}])
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout=payload))
    assert [s.session_id for s in sc.sessions_from_cli()] == ["ok"]


def test_cli_fallback_is_rate_limited(monkeypatch):
    fake = _FakeRun(stdout=CLI_JSON)
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc._cli_fallback(timeout=1, at=100.0)
    sc._cli_fallback(timeout=1, at=100.0 + sc.CLI_MIN_INTERVAL_S - 0.1)
    assert len(fake.calls) == 1                      # second call served from cache


def test_cli_fallback_refreshes_after_the_interval(monkeypatch):
    fake = _FakeRun(stdout=CLI_JSON)
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc._cli_fallback(timeout=1, at=100.0)
    sc._cli_fallback(timeout=1, at=100.0 + sc.CLI_MIN_INTERVAL_S + 0.1)
    assert len(fake.calls) == 2


def test_cli_fallback_retries_while_empty(monkeypatch):
    # An empty result must not be cached, or a transient failure would pin the
    # UI to "no sessions" for the whole interval.
    fake = _FakeRun(stdout="[]")
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc._cli_fallback(timeout=1, at=100.0)
    sc._cli_fallback(timeout=1, at=100.5)
    assert len(fake.calls) == 2


def test_reset_cli_cache_forces_a_fresh_call(monkeypatch):
    fake = _FakeRun(stdout=CLI_JSON)
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc._cli_fallback(timeout=1, at=100.0)
    sc.reset_cli_cache()
    sc._cli_fallback(timeout=1, at=100.1)
    assert len(fake.calls) == 2


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_prefers_the_registry(monkeypatch, tmp_path):
    _write_session(tmp_path, 5, status="busy")
    monkeypatch.setattr(sc, "sessions_from_cli",
                        lambda **k: pytest.fail("CLI must not run"))
    sessions = sc.snapshot(tmp_path, alive=_alive_all)
    assert [s.pid for s in sessions] == [5]


def test_snapshot_empty_registry_does_not_shell_out(monkeypatch, tmp_path):
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(sc, "sessions_from_cli",
                        lambda **k: pytest.fail("CLI must not run"))
    assert sc.snapshot(tmp_path, alive=_alive_all) == []


def test_snapshot_dead_sessions_do_not_shell_out(monkeypatch, tmp_path):
    _write_session(tmp_path, 5)
    monkeypatch.setattr(sc, "sessions_from_cli",
                        lambda **k: pytest.fail("CLI must not run"))
    assert sc.snapshot(tmp_path, alive=_alive_none) == []


def test_snapshot_falls_back_when_registry_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout=CLI_JSON))
    sessions = sc.snapshot(tmp_path / "gone", alive=_alive_all)
    assert [s.session_id for s in sessions] == ["s2", "s1"]   # waiting first


def test_snapshot_falls_back_when_schema_changed(monkeypatch, tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "1.json").write_text(json.dumps({"process": 1}), encoding="utf-8")
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout=CLI_JSON))
    assert len(sc.snapshot(tmp_path, alive=_alive_all)) == 2


def test_snapshot_fallback_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "sessions_from_cli",
                        lambda **k: pytest.fail("CLI must not run"))
    assert sc.snapshot(tmp_path / "gone", cli_fallback=False) == []


def test_snapshot_returns_sorted(tmp_path):
    _write_session(tmp_path, 1, status="idle", statusUpdatedAt=100)
    _write_session(tmp_path, 2, status="waiting", statusUpdatedAt=100)
    sessions = sc.snapshot(tmp_path, alive=_alive_all, at_ms=1000)
    assert [s.status for s in sessions] == ["waiting", "idle"]


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def test_diff_detects_appearance():
    new = _session(session_id="a")
    events = sc.diff([], [new])
    assert [(e.kind, e.session.session_id) for e in events] == [("appeared", "a")]


def test_diff_detects_disappearance():
    old = _session(session_id="a")
    events = sc.diff([old], [])
    assert [(e.kind, e.session.session_id) for e in events] == [("gone", "a")]


def test_diff_detects_status_change():
    before = _session(session_id="a", status=sc.BUSY)
    after = _session(session_id="a", status=sc.IDLE)
    events = sc.diff([before], [after])
    assert len(events) == 1
    assert events[0].kind == "status"
    assert events[0].before == sc.BUSY and events[0].after == sc.IDLE
    assert events[0].session is after


def test_diff_silent_when_nothing_changed():
    s = _session(session_id="a", status=sc.BUSY)
    assert sc.diff([s], [_session(session_id="a", status=sc.BUSY)]) == []


def test_diff_ignores_transitions_involving_unknown():
    # An unreadable write must not fire a "done!" toast.
    assert sc.diff([_session(session_id="a", status=sc.UNKNOWN)],
                   [_session(session_id="a", status=sc.IDLE)]) == []
    assert sc.diff([_session(session_id="a", status=sc.BUSY)],
                   [_session(session_id="a", status=sc.UNKNOWN)]) == []


def test_diff_keys_on_session_id_not_pid():
    before = _session(session_id="a", pid=1, status=sc.BUSY)
    after = _session(session_id="a", pid=2, status=sc.BUSY)
    assert sc.diff([before], [after]) == []


def test_diff_falls_back_to_pid_without_session_id():
    before = _session(session_id="", pid=9, status=sc.BUSY)
    after = _session(session_id="", pid=9, status=sc.IDLE)
    events = sc.diff([before], [after])
    assert len(events) == 1 and events[0].kind == "status"


def test_diff_reports_several_changes_at_once():
    before = [_session(session_id="a", status=sc.BUSY),
              _session(session_id="b", status=sc.IDLE)]
    after = [_session(session_id="a", status=sc.WAITING),
             _session(session_id="c", status=sc.BUSY)]
    kinds = sorted((e.kind, e.session.session_id) for e in sc.diff(before, after))
    assert kinds == [("appeared", "c"), ("gone", "b"), ("status", "a")]


# --------------------------------------------------------------------------- #
# Torn reads — the CLI rewrites <pid>.json with a non-atomic truncate+write
# --------------------------------------------------------------------------- #
def test_read_json_reads_a_good_file(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert sc._read_json(f) == {"a": 1}


def test_read_json_retries_past_a_truncated_read(tmp_path, monkeypatch):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"a": 1}), encoding="utf-8")
    reads = {"n": 0}
    real = type(f).read_text

    def flaky(self, *a, **k):
        reads["n"] += 1
        if reads["n"] == 1:
            return ""              # caught mid truncate-and-write
        return real(self, *a, **k)

    monkeypatch.setattr(type(f), "read_text", flaky)
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    assert sc._read_json(f) == {"a": 1}
    assert reads["n"] == 2


def test_read_json_retries_past_a_half_written_read(tmp_path, monkeypatch):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"a": 1}), encoding="utf-8")
    reads = {"n": 0}
    real = type(f).read_text

    def flaky(self, *a, **k):
        reads["n"] += 1
        if reads["n"] == 1:
            return '{"a":'         # partial write
        return real(self, *a, **k)

    monkeypatch.setattr(type(f), "read_text", flaky)
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    assert sc._read_json(f) == {"a": 1}


def test_read_json_gives_up_on_real_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    f = tmp_path / "a.json"
    f.write_text("{ broken", encoding="utf-8")
    assert sc._read_json(f) is None


def test_read_json_does_not_retry_a_missing_file(tmp_path):
    # An OSError is not a torn read — retrying it would just burn time.
    assert sc._read_json(tmp_path / "nope.json") is None


def test_read_json_single_read_when_healthy(tmp_path, monkeypatch):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"a": 1}), encoding="utf-8")
    monkeypatch.setattr(sc.time, "sleep",
                        lambda _s: pytest.fail("healthy read must not sleep"))
    assert sc._read_json(f) == {"a": 1}


def test_scan_recovers_a_session_from_a_torn_read(tmp_path, monkeypatch):
    _write_session(tmp_path, 4242, status="busy")
    reads = {"n": 0}
    real = sc.Path.read_text

    def flaky(self, *a, **k):
        reads["n"] += 1
        if reads["n"] == 1:
            return ""
        return real(self, *a, **k)

    monkeypatch.setattr(sc.Path, "read_text", flaky)
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    sessions, health = sc.scan(tmp_path, alive=_alive_all)
    assert [s.pid for s in sessions] == [4242]     # not dropped
    assert health == "ok"


# --------------------------------------------------------------------------- #
# Duplicate session ids
# --------------------------------------------------------------------------- #
def test_scan_dedupes_same_session_id_keeping_freshest(tmp_path):
    _write_session(tmp_path, 100, sessionId="same", updatedAt=10, status="idle")
    _write_session(tmp_path, 200, sessionId="same", updatedAt=99, status="busy")
    sessions, _ = sc.scan(tmp_path, alive=_alive_all)
    assert len(sessions) == 1
    assert sessions[0].pid == 200 and sessions[0].status == "busy"


def test_scan_keeps_distinct_session_ids(tmp_path):
    _write_session(tmp_path, 100, sessionId="a")
    _write_session(tmp_path, 200, sessionId="b")
    sessions, _ = sc.scan(tmp_path, alive=_alive_all)
    assert sorted(s.session_id for s in sessions) == ["a", "b"]


def test_dedupe_falls_back_to_pid_without_session_id():
    a = _session(session_id="", pid=1)
    b = _session(session_id="", pid=2)
    assert len(sc._dedupe([a, b])) == 2


# --------------------------------------------------------------------------- #
# SessionTracker — debounced transitions
# --------------------------------------------------------------------------- #
def test_tracker_reports_first_snapshot_as_appeared():
    t = sc.SessionTracker()
    events = t.update([_session(session_id="a")])
    assert [(e.kind, e.session.session_id) for e in events] == [("appeared", "a")]


def test_tracker_reports_status_change():
    t = sc.SessionTracker()
    t.update([_session(session_id="a", status=sc.BUSY)])
    events = t.update([_session(session_id="a", status=sc.IDLE)])
    assert len(events) == 1 and events[0].kind == "status"
    assert events[0].before == sc.BUSY and events[0].after == sc.IDLE


def test_tracker_silent_when_nothing_changes():
    t = sc.SessionTracker()
    t.update([_session(session_id="a", status=sc.BUSY)])
    assert t.update([_session(session_id="a", status=sc.BUSY)]) == []


def test_tracker_ignores_a_single_dropped_poll():
    # THE point of the debounce: one torn read must not fire "session ended".
    t = sc.SessionTracker(confirm_ticks=2)
    t.update([_session(session_id="a", status=sc.BUSY)])
    assert t.update([]) == []                       # vanished for one poll
    assert t.update([_session(session_id="a", status=sc.BUSY)]) == []


def test_tracker_keeps_a_briefly_missing_session_visible():
    t = sc.SessionTracker(confirm_ticks=2)
    t.update([_session(session_id="a", status=sc.BUSY)])
    t.update([])
    assert [s.session_id for s in t.sessions] == ["a"]   # row must not blink


def test_tracker_confirms_a_sustained_disappearance():
    t = sc.SessionTracker(confirm_ticks=2)
    t.update([_session(session_id="a")])
    assert t.update([]) == []
    events = t.update([])
    assert [(e.kind, e.session.session_id) for e in events] == [("gone", "a")]
    assert t.sessions == []


def test_tracker_confirm_ticks_one_reports_immediately():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a")])
    assert [e.kind for e in t.update([])] == ["gone"]


def test_tracker_confirm_ticks_floor_is_one():
    assert sc.SessionTracker(confirm_ticks=0).confirm_ticks == 1


def test_tracker_gone_session_reappearing_is_a_new_appearance():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a")])
    t.update([])                                     # confirmed gone
    assert [e.kind for e in t.update([_session(session_id="a")])] == ["appeared"]


def test_tracker_status_change_while_briefly_missing_is_reported():
    t = sc.SessionTracker(confirm_ticks=3)
    t.update([_session(session_id="a", status=sc.BUSY)])
    t.update([])
    events = t.update([_session(session_id="a", status=sc.WAITING)])
    assert len(events) == 1 and events[0].kind == "status"
    assert events[0].after == sc.WAITING


def test_tracker_suppresses_unknown_status_transitions():
    t = sc.SessionTracker()
    t.update([_session(session_id="a", status=sc.BUSY)])
    assert t.update([_session(session_id="a", status=sc.UNKNOWN)]) == []


def test_tracker_sessions_are_sorted_blocked_first():
    t = sc.SessionTracker()
    t.update([_session(session_id="i", status=sc.IDLE, status_updated_at=900),
              _session(session_id="w", status=sc.WAITING, status_updated_at=900)],
             at_ms=1000)
    assert [s.session_id for s in t.sessions] == ["w", "i"]


def test_tracker_handles_several_sessions_independently():
    t = sc.SessionTracker(confirm_ticks=2)
    t.update([_session(session_id="a", status=sc.BUSY),
              _session(session_id="b", status=sc.BUSY)])
    events = t.update([_session(session_id="a", status=sc.IDLE)])
    assert [e.kind for e in events] == ["status"]      # b only missing once
    events = t.update([_session(session_id="a", status=sc.IDLE)])
    assert [(e.kind, e.session.session_id) for e in events] == [("gone", "b")]


def test_tracker_reset_forgets_everything():
    t = sc.SessionTracker()
    t.update([_session(session_id="a")])
    t.reset()
    assert t.sessions == []
    # After a reset the session is announced fresh rather than silently kept.
    assert [e.kind for e in t.update([_session(session_id="a")])] == ["appeared"]


def test_tracker_sessions_list_is_a_copy():
    t = sc.SessionTracker()
    t.update([_session(session_id="a")])
    t.sessions.clear()
    assert len(t.sessions) == 1


# --------------------------------------------------------------------------- #
# project_slug / transcript_path
# --------------------------------------------------------------------------- #
def test_project_slug_windows_path():
    assert sc.project_slug("C:\\Personal Space\\claude-widget") == \
        "C--Personal-Space-claude-widget"


def test_project_slug_underscores_become_dashes():
    assert sc.project_slug("C:\\Personal Space\\personal_projects\\proxy-passer-lua") \
        == "C--Personal-Space-personal-projects-proxy-passer-lua"


def test_project_slug_preserves_existing_dashes():
    assert sc.project_slug("W:\\videoslots-mts") == "W--videoslots-mts"


def test_project_slug_posix_path():
    assert sc.project_slug("/home/me/my proj") == "-home-me-my-proj"


def test_transcript_path_via_slug(tmp_path):
    cwd = "C:\\Personal Space\\w"
    d = tmp_path / "projects" / sc.project_slug(cwd)
    d.mkdir(parents=True)
    f = d / "sid.jsonl"
    f.write_text("", encoding="utf-8")
    assert sc.transcript_path("sid", cwd, tmp_path) == f


def test_transcript_path_scan_fallback_when_slug_wrong(tmp_path):
    d = tmp_path / "projects" / "some-other-name"
    d.mkdir(parents=True)
    f = d / "sid.jsonl"
    f.write_text("", encoding="utf-8")
    # cwd would produce a different slug, so only the scan can find this.
    assert sc.transcript_path("sid", "/unrelated/path", tmp_path) == f


def test_transcript_path_none_when_absent(tmp_path):
    (tmp_path / "projects").mkdir()
    assert sc.transcript_path("sid", "/x", tmp_path) is None


def test_transcript_path_none_without_projects_dir(tmp_path):
    assert sc.transcript_path("sid", "/x", tmp_path) is None


def test_transcript_path_none_without_session_id(tmp_path):
    assert sc.transcript_path("", "/x", tmp_path) is None


def test_transcript_path_is_cached(tmp_path):
    d = tmp_path / "projects" / "n"
    d.mkdir(parents=True)
    f = d / "sid.jsonl"
    f.write_text("", encoding="utf-8")
    assert sc.transcript_path("sid", None, tmp_path) == f
    # A second lookup is served from cache even if the tree is unreadable now.
    assert sc.transcript_path("sid", None, tmp_path / "gone") == f


def test_transcript_cache_ignores_deleted_files(tmp_path):
    d = tmp_path / "projects" / "n"
    d.mkdir(parents=True)
    f = d / "sid.jsonl"
    f.write_text("", encoding="utf-8")
    sc.transcript_path("sid", None, tmp_path)
    f.unlink()
    assert sc.transcript_path("sid", None, tmp_path) is None


# --------------------------------------------------------------------------- #
# _tail_text
# --------------------------------------------------------------------------- #
def test_tail_text_returns_whole_small_file(tmp_path):
    # write_bytes, not write_text: on Windows text mode would rewrite \n as
    # \r\n and this asserts on exact bytes.
    f = tmp_path / "a.jsonl"
    f.write_bytes(b"one\ntwo\n")
    assert sc._tail_text(f, 1000) == "one\ntwo\n"


def test_tail_text_starts_at_a_line_boundary(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(b"aaaa\nbbbb\ncccc\n")
    # Landing mid-"bbbb" must drop that partial line entirely.
    assert sc._tail_text(f, 8) == "cccc\n"


def test_tail_text_preserves_crlf_line_endings(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(b"aaaa\r\nbbbb\r\ncccc\r\n")
    assert sc._tail_text(f, 9) == "cccc\r\n"


def test_tail_text_empty_when_no_newline_in_window(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(b"x" * 100)
    assert sc._tail_text(f, 10) == ""


def test_tail_text_missing_file(tmp_path):
    assert sc._tail_text(tmp_path / "nope", 100) == ""


def test_tail_text_survives_broken_utf8(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_bytes(b"ok\n\xff\xfe\n")
    assert "ok" in sc._tail_text(f, 1000)


# --------------------------------------------------------------------------- #
# read_transcript_tail
# --------------------------------------------------------------------------- #
def _transcript(tmp_path, entries, name="t.jsonl"):
    f = tmp_path / name
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return f


def test_read_transcript_tail_extracts_everything(tmp_path):
    f = _transcript(tmp_path, [
        {"type": "ai-title", "aiTitle": "Ship the session monitor"},
        {"type": "assistant", "gitBranch": "main", "message": {
            "model": "claude-opus-5", "usage": {"input_tokens": 7,
                                                "output_tokens": 42},
            "content": [{"type": "tool_use", "name": "PowerShell"}]}},
    ])
    info = sc.read_transcript_tail(f)
    assert info["title"] == "Ship the session monitor"
    assert info["model"] == "claude-opus-5"
    assert info["tool"] == "PowerShell"
    assert info["git_branch"] == "main"
    assert info["input_tokens"] == 7 and info["output_tokens"] == 42


def test_read_transcript_tail_takes_the_most_recent_tool(tmp_path):
    f = _transcript(tmp_path, [
        {"type": "assistant", "message": {
            "content": [{"type": "tool_use", "name": "Old"}]}},
        {"type": "assistant", "message": {
            "content": [{"type": "tool_use", "name": "New"}]}},
    ])
    assert sc.read_transcript_tail(f)["tool"] == "New"


def test_read_transcript_tail_skips_text_only_replies(tmp_path):
    f = _transcript(tmp_path, [
        {"type": "assistant", "message": {
            "content": [{"type": "tool_use", "name": "Grep"}]}},
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "done"}]}},
    ])
    assert sc.read_transcript_tail(f)["tool"] == "Grep"


def test_read_transcript_tail_counts_distinct_subagents(tmp_path):
    f = _transcript(tmp_path, [
        {"type": "user", "isSidechain": True, "agentId": "a1"},
        {"type": "assistant", "isSidechain": True, "agentId": "a1"},
        {"type": "user", "isSidechain": True, "agentId": "a2"},
    ])
    assert sc.read_transcript_tail(f)["subagents"] == 2


def test_read_transcript_tail_counts_roots_without_agent_id(tmp_path):
    f = _transcript(tmp_path, [
        {"type": "user", "isSidechain": True, "parentUuid": None},
        {"type": "assistant", "isSidechain": True, "parentUuid": "u1"},
        {"type": "user", "isSidechain": True, "parentUuid": None},
    ])
    assert sc.read_transcript_tail(f)["subagents"] == 2


def test_read_transcript_tail_no_subagents_key_when_none(tmp_path):
    f = _transcript(tmp_path, [{"type": "assistant", "message": {"model": "m"}}])
    assert "subagents" not in sc.read_transcript_tail(f)


def test_read_transcript_tail_ignores_corrupt_lines(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text('{ broken\n' + json.dumps(
        {"type": "assistant", "message": {"model": "m"}}) + "\n", encoding="utf-8")
    assert sc.read_transcript_tail(f)["model"] == "m"


def test_read_transcript_tail_handles_crlf_line_endings(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(("\r\n".join([
        json.dumps({"type": "ai-title", "aiTitle": "CRLF"}),
        json.dumps({"type": "assistant", "message": {"model": "m"}}),
    ]) + "\r\n").encode("utf-8"))
    info = sc.read_transcript_tail(f)
    assert info["title"] == "CRLF" and info["model"] == "m"


def test_read_transcript_tail_ignores_non_object_lines(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text('[1,2]\n"str"\n', encoding="utf-8")
    assert sc.read_transcript_tail(f) == {}


def test_read_transcript_tail_empty_for_missing_file(tmp_path):
    assert sc.read_transcript_tail(tmp_path / "nope") == {}


def test_read_transcript_tail_handles_malformed_message(tmp_path):
    f = _transcript(tmp_path, [
        {"type": "assistant", "message": "not a dict"},
        {"type": "assistant", "message": {"content": "not a list"}},
    ])
    assert sc.read_transcript_tail(f) == {}


def test_read_transcript_tail_respects_the_byte_window(tmp_path):
    entries = [{"type": "assistant", "message": {"model": "old"}}]
    entries += [{"type": "user", "pad": "x" * 200} for _ in range(50)]
    f = _transcript(tmp_path, entries)
    # The old model line is far outside a small tail window.
    assert "model" not in sc.read_transcript_tail(f, max_bytes=512)


# --------------------------------------------------------------------------- #
# last_prompts
# --------------------------------------------------------------------------- #
def test_last_prompts_maps_session_to_latest_text(tmp_path):
    lines = [
        {"display": "first", "sessionId": "a"},
        {"display": "second", "sessionId": "a"},
        {"display": "other", "sessionId": "b"},
    ]
    (tmp_path / "history.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert sc.last_prompts(tmp_path) == {"a": "second", "b": "other"}


def test_last_prompts_empty_without_file(tmp_path):
    assert sc.last_prompts(tmp_path) == {}


def test_last_prompts_ignores_bad_lines(tmp_path):
    (tmp_path / "history.jsonl").write_text(
        '{ broken\n' + json.dumps({"display": "ok", "sessionId": "a"}) + "\n",
        encoding="utf-8")
    assert sc.last_prompts(tmp_path) == {"a": "ok"}


def test_last_prompts_skips_entries_missing_fields(tmp_path):
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"display": "no session"}) + "\n"
        + json.dumps({"sessionId": "no display"}) + "\n", encoding="utf-8")
    assert sc.last_prompts(tmp_path) == {}


# --------------------------------------------------------------------------- #
# enrich
# --------------------------------------------------------------------------- #
def test_enrich_fills_transcript_and_prompt(tmp_path):
    d = tmp_path / "projects" / "slug"
    d.mkdir(parents=True)
    _transcript(d, [
        {"type": "ai-title", "aiTitle": "Nice title"},
        {"type": "assistant", "message": {
            "model": "claude-opus-5",
            "content": [{"type": "tool_use", "name": "Read"}]}},
    ], name="sid.jsonl")
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"display": "do the thing", "sessionId": "sid"}) + "\n",
        encoding="utf-8")

    out = sc.enrich(_session(session_id="sid", cwd="/x"), tmp_path)
    assert out.title == "Nice title"
    assert out.tool == "Read"
    assert out.model == "claude-opus-5"
    assert out.last_prompt == "do the thing"


def test_enrich_returns_input_unchanged_when_nothing_found(tmp_path):
    s = _session(session_id="sid")
    assert sc.enrich(s, tmp_path) is s


def test_enrich_label_uses_the_ai_title(tmp_path):
    d = tmp_path / "projects" / "slug"
    d.mkdir(parents=True)
    _transcript(d, [{"type": "ai-title", "aiTitle": "Ship it"}], name="sid.jsonl")
    out = sc.enrich(_session(session_id="sid", name="widget-b0"), tmp_path)
    assert out.label == "Ship it"


def test_enrich_never_raises_on_a_broken_tree(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(sc, "transcript_path", boom)
    monkeypatch.setattr(sc, "last_prompts", boom)
    s = _session(session_id="sid")
    assert sc.enrich(s, tmp_path) is s


def test_enrich_accepts_a_shared_prompt_map(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "last_prompts",
                        lambda *a, **k: pytest.fail("must reuse the map"))
    out = sc.enrich(_session(session_id="sid"), tmp_path, prompts={"sid": "hi"})
    assert out.last_prompt == "hi"


def test_enrich_all_reads_history_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "last_prompts",
                        lambda *a, **k: calls.append(1) or {"a": "p", "b": "q"})
    out = sc.enrich_all([_session(session_id="a"), _session(session_id="b")],
                        tmp_path)
    assert len(calls) == 1
    assert [s.last_prompt for s in out] == ["p", "q"]


def test_enrich_all_survives_history_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(sc, "last_prompts", boom)
    out = sc.enrich_all([_session(session_id="a")], tmp_path)
    assert len(out) == 1 and out[0].last_prompt == ""


def test_enrich_does_not_mutate_the_original(tmp_path):
    d = tmp_path / "projects" / "slug"
    d.mkdir(parents=True)
    _transcript(d, [{"type": "ai-title", "aiTitle": "T"}], name="sid.jsonl")
    original = _session(session_id="sid")
    out = sc.enrich(original, tmp_path)
    assert original.title == "" and out.title == "T"
