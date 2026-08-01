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


@pytest.mark.parametrize("pid", [2 ** 32, 2 ** 48, 99999999999999999999])
def test_pid_alive_false_for_out_of_range_pids(pid):
    # A stray timestamp-named file (20240101120000.json) yields a pid far past
    # 32 bits; handing that to ctypes raises ArgumentError and would kill the
    # whole poll loop.
    assert sc.pid_alive(pid) is False


@pytest.mark.parametrize("pid", [2 ** 32, 99999999999999999999])
def test_proc_start_ms_none_for_out_of_range_pids(pid):
    assert sc.proc_start_ms(pid) is None


def test_scan_survives_a_timestamp_named_file(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "20240101120000.json").write_text(
        json.dumps({"pid": 20240101120000, "sessionId": "junk"}),
        encoding="utf-8")
    _write_session(tmp_path, 4242, sessionId="real", status="busy")
    sessions, health = sc.scan(tmp_path, alive=lambda pid: pid == 4242)
    assert [s.session_id for s in sessions] == ["real"]
    assert health == "ok"


def test_scan_survives_a_file_that_explodes_the_liveness_probe(tmp_path):
    # Structural guarantee: one bad file can never take down the whole scan.
    _write_session(tmp_path, 1, sessionId="boom")
    _write_session(tmp_path, 2, sessionId="fine")

    def explode(pid):
        if pid == 1:
            raise RuntimeError("kernel32 went sideways")
        return True

    sessions, health = sc.scan(tmp_path, alive=explode)
    assert [s.session_id for s in sessions] == ["fine"]
    assert health == "ok"


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX branch")
def test_proc_start_ms_reads_this_process_on_posix():
    started = sc.proc_start_ms(os.getpid())
    # Linux/macOS both support it now; if a platform can't, None is the
    # documented fail-soft answer and the session is simply kept.
    if started is not None:
        assert 946_684_800_000 < started <= sc.now_ms() + 5000


# --- /proc and ps parsing: pure, so testable on any platform -------------- #
def test_parse_proc_stat_start_basic():
    # Fields: pid (comm) state ppid ... starttime is the 22nd overall.
    fields = ["4242", "(bash)", "S"] + [str(i) for i in range(4, 22)] + ["500"]
    text = " ".join(fields)
    # btime 1000, 100Hz -> 1000 + 500/100 = 1005s
    assert sc.parse_proc_stat_start(text, btime=1000, hz=100) == 1_005_000


def test_parse_proc_stat_start_comm_with_spaces_and_parens():
    # A process may be named "(my weird) proc)" — splitting on whitespace or
    # the FIRST ')' would take the wrong field.
    fields = ["4242", "((my weird) proc))", "S"] + \
             [str(i) for i in range(4, 22)] + ["500"]
    assert sc.parse_proc_stat_start(" ".join(fields), btime=0, hz=100) == 5_000


def test_parse_proc_stat_start_rejects_garbage():
    assert sc.parse_proc_stat_start("nonsense", btime=0) is None
    assert sc.parse_proc_stat_start("(x) too short", btime=0) is None
    assert sc.parse_proc_stat_start("", btime=0) is None


def test_parse_proc_stat_start_rejects_bad_hz():
    fields = ["1", "(a)", "S"] + [str(i) for i in range(4, 22)] + ["500"]
    assert sc.parse_proc_stat_start(" ".join(fields), btime=0, hz=0) is None


def test_the_cli_fallback_is_rate_limited_even_when_it_finds_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "sessions_from_cli",
                        lambda **k: calls.append(1) or [])
    sc.reset_cli_cache()
    for tick in range(10):            # ten polls inside one interval
        sc._cli_fallback(timeout=5, at=100.0 + tick)
    assert len(calls) == 1, (
        "'no sessions' is a real answer — rate-limiting only non-empty ones "
        "spawns a subprocess on every tick for the quiet case, which is most "
        "of the time")


def test_the_cli_fallback_asks_again_once_the_interval_passes(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "sessions_from_cli",
                        lambda **k: calls.append(1) or [])
    sc.reset_cli_cache()
    sc._cli_fallback(timeout=5, at=100.0)
    sc._cli_fallback(timeout=5, at=100.0 + sc.CLI_MIN_INTERVAL_S + 1)
    assert len(calls) == 2


def test_a_stale_start_time_cache_does_not_hide_a_session_forever(monkeypatch):
    # The pid's previous holder started long ago and was cached. The pid is
    # now a NEW session; without invalidation the mismatch would be permanent.
    sc.reset_proc_start_cache()
    sc._proc_start_cache[4242] = 1_000_000
    monkeypatch.setattr(sc, "_proc_start_posix", lambda pid: 9_000_000)
    monkeypatch.setattr(sc, "proc_start_ms",
                        lambda pid: sc._proc_start_cache.get(
                            pid, sc._proc_start_posix(pid)))
    assert sc.pid_matches_session(4242, 9_000_000) is True
    assert 4242 not in sc._proc_start_cache or \
        sc._proc_start_cache[4242] != 1_000_000


def test_a_genuinely_recycled_pid_is_still_rejected(monkeypatch):
    sc.reset_proc_start_cache()
    monkeypatch.setattr(sc, "proc_start_ms", lambda pid: 9_000_000)
    assert sc.pid_matches_session(4242, 1_000_000) is False, (
        "self-correcting on a mismatch must not turn into never rejecting")


def test_parse_ps_lstart_round_trip():
    # Read as UTC: the caller runs ps with TZ=UTC precisely so this stamp is
    # unambiguous. Local time repeats an hour every autumn, and resolving it
    # the wrong way is indistinguishable from a recycled pid.
    from datetime import datetime, timezone

    stamp = datetime(2026, 8, 1, 10, 23, 45, tzinfo=timezone.utc)
    text = stamp.strftime("%a %b %d %H:%M:%S %Y")
    assert sc.parse_ps_lstart(text) == int(stamp.timestamp() * 1000)


def test_parse_ps_lstart_is_not_affected_by_the_local_timezone(monkeypatch):
    a = sc.parse_ps_lstart("Sun Oct 25 02:30:00 2026")   # inside the DST fold
    b = sc.parse_ps_lstart("Sun Oct 25 02:30:00 2026")
    assert a == b and a is not None
    # Same wall clock, one hour apart in the stamp, must be one hour apart.
    later = sc.parse_ps_lstart("Sun Oct 25 03:30:00 2026")
    assert later - a == 3_600_000


def test_parse_ps_lstart_handles_padded_single_digit_day():
    # ps pads the day, giving a double space: "Fri Aug  1 ..."
    assert sc.parse_ps_lstart("Fri Aug  1 10:23:45 2026") is not None


def test_parse_ps_lstart_rejects_garbage():
    for bad in ("", None, "not a date", "Fri Aug 1 2026"):
        assert sc.parse_ps_lstart(bad) is None


def test_proc_start_cache_avoids_repeat_lookups(monkeypatch):
    # On macOS this is a subprocess; it must not run once per poll.
    sc.reset_proc_start_cache()
    calls = []
    monkeypatch.setattr(sc.sys, "platform", "darwin")

    def fake_run(*a, **k):
        calls.append(a)
        return type("R", (), {"returncode": 0,
                              "stdout": "Fri Aug  1 10:23:45 2026"})()

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    first = sc._proc_start_posix(4242)
    second = sc._proc_start_posix(4242)
    assert first == second is not None
    assert len(calls) == 1
    sc.reset_proc_start_cache()


def test_proc_start_posix_failure_is_not_cached(monkeypatch):
    sc.reset_proc_start_cache()
    monkeypatch.setattr(sc.sys, "platform", "darwin")
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1,
                                                       "stdout": ""})())
    assert sc._proc_start_posix(4242) is None
    assert 4242 not in sc._proc_start_cache


def test_proc_start_posix_never_raises(monkeypatch):
    sc.reset_proc_start_cache()
    monkeypatch.setattr(sc.sys, "platform", "darwin")

    def boom(*a, **k):
        raise OSError("no ps here")

    monkeypatch.setattr(sc.subprocess, "run", boom)
    assert sc._proc_start_posix(4242) is None


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


# None, not [] — "I couldn't ask" has to be distinguishable from "it said
# none", or a CLI that fails looks exactly like a machine with no sessions.
def test_sessions_from_cli_unknown_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout=CLI_JSON, returncode=1))
    assert sc.sessions_from_cli() is None


def test_sessions_from_cli_unknown_on_bad_json(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout="not json"))
    assert sc.sessions_from_cli() is None


def test_sessions_from_cli_unknown_on_non_list_json(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout='{"a": 1}'))
    assert sc.sessions_from_cli() is None


def test_sessions_from_cli_unknown_on_timeout(monkeypatch):
    monkeypatch.setattr(
        sc.subprocess, "run",
        _FakeRun(raises=sc.subprocess.TimeoutExpired(cmd="claude", timeout=1)))
    assert sc.sessions_from_cli() is None


def test_sessions_from_cli_reports_a_genuine_empty_list(monkeypatch):
    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout="[]"))
    assert sc.sessions_from_cli() == [], (
        "the CLI ran and said none — that is an answer, not a failure")


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


def test_a_reported_empty_list_is_cached_like_any_other_answer(monkeypatch):
    # "There are no sessions" is an ANSWER. Refusing to cache it meant the
    # quiet case — most of the time — spawned `claude agents --json` on every
    # tick of a one-second loop.
    fake = _FakeRun(stdout="[]")
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc.reset_cli_cache()
    for tick in range(6):
        sc._cli_fallback(timeout=1, at=100.0 + tick * 0.2)
    assert len(fake.calls) == 1


def test_a_failed_call_does_not_empty_the_list(monkeypatch):
    # The original worry, kept: a transient failure must not pin the UI to
    # "no sessions". It is now handled by distinguishing "the CLI said none"
    # from "the CLI didn't answer", rather than by never caching.
    good = _FakeRun(stdout=CLI_JSON)
    monkeypatch.setattr(sc.subprocess, "run", good)
    sc.reset_cli_cache()
    first = sc._cli_fallback(timeout=1, at=100.0)
    assert first, "sanity: the good call returned sessions"

    monkeypatch.setattr(sc.subprocess, "run", _FakeRun(stdout="not json"))
    later = sc._cli_fallback(timeout=1, at=100.0 + sc.CLI_MIN_INTERVAL_S + 1)
    assert [s.session_id for s in later] == [s.session_id for s in first], (
        "a failed call means 'I don't know', not 'nothing is running'")


def test_a_permanently_broken_cli_is_still_rate_limited(monkeypatch):
    fake = _FakeRun(stdout="not json")
    monkeypatch.setattr(sc.subprocess, "run", fake)
    sc.reset_cli_cache()
    for tick in range(6):
        sc._cli_fallback(timeout=1, at=100.0 + tick * 0.2)
    assert len(fake.calls) <= 2, (
        "a CLI that never works must not cost a process on every tick")


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
# parse_console_prompt — the live question, read off the session's screen
# --------------------------------------------------------------------------- #
# Taken verbatim from a real waiting session; the transcript had nothing,
# because a tool call only lands there once it has been answered.
REAL_SCREEN = [
    "✻ Crunched for 5m 15s",
    "> again",
    "─────────────────────────────────────────────────",
    " [ ] Overrated",
    "What's the most overrated thing everyone seems to agree is great?",
    "> 1. Networking events",
    "     Rooms full of people exchanging cards and never speaking again.",
    "  2. Waking up at 5am",
    "     The productivity cult's favorite badge of honor.",
    "  3. Open-plan offices",
    "     Collaboration, allegedly. Noise, actually.",
    "  4. Most new AI tools",
    "  5. Type something.",
    "─────────────────────────────────────────────────",
    "  6. Chat about this",
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
]


def test_parse_console_prompt_reads_the_real_screen():
    out = sc.parse_console_prompt(REAL_SCREEN)
    assert out["question"] == \
        "What's the most overrated thing everyone seems to agree is great?"
    assert out["options"] == (
        "Networking events", "Waking up at 5am", "Open-plan offices",
        "Most new AI tools", "Type something.", "Chat about this")


def test_parse_console_prompt_reports_where_the_prompt_begins():
    out = sc.parse_console_prompt(REAL_SCREEN)
    assert REAL_SCREEN[out["start"]] == out["question"], (
        "'start' is what lets a caller show the screen ABOVE the prompt "
        "without repeating the choices it is already drawing as buttons")


def test_parse_console_prompt_strips_the_speech_bullet_from_the_question():
    out = sc.parse_console_prompt([
        "● Which heading should these go under?",
        "> 1. Breaking changes", "  2. Upgrade notes",
        "Enter to select · ↑/↓ to navigate · Esc to cancel"])
    assert out["question"] == "Which heading should these go under?", (
        "the bullet means 'Claude is talking'; in front of a question we are "
        "already presenting as the question, it is litter")


def test_parse_console_prompt_finds_the_highlighted_option():
    assert sc.parse_console_prompt(REAL_SCREEN)["selected"] == 1


def test_parse_console_prompt_marker_elsewhere():
    screen = ["Pick one", "  1. first", "> 2. second", "  3. third",
              "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    out = sc.parse_console_prompt(screen)
    assert out["selected"] == 2 and len(out["options"]) == 3


# --------------------------------------------------------------------------- #
# What a session has said since you last typed
# --------------------------------------------------------------------------- #
TALKING = [
    "● An older answer you have already read.",
    "> put the breaking changes at the top",
    "● Right — they get their own section, above everything else.",
    "  Two of the four are only breaking if you were relying on the old",
    "  default, so I'll say which.",
    "",
    "  Do you want the old default documented as well?",
    "> ",
]


def test_latest_reply_starts_after_your_last_input():
    out = sc.latest_reply(TALKING)
    assert out.startswith("Right — they get their own section")
    assert "already read" not in out, (
        "you answered that one — what makes this worth showing is the part "
        "you haven't")


def test_latest_reply_rejoins_the_terminals_wrapping():
    out = sc.latest_reply(TALKING)
    assert "the old default, so I'll say which." in out, (
        "a line break at the terminal's width is not a sentence ending")
    assert "\n" not in out


def test_latest_reply_drops_the_bookkeeping():
    out = sc.latest_reply(["✻ Cogitated for 14s", "⎿  Read 4 files (312 lines)",
                           " [ ] Bad advice", "● Here's what I found.", "> "])
    assert out == "Here's what I found.", (
        "how long it thought and what a tool returned is not it talking")


def test_latest_reply_keeps_a_quoted_prompt_in_the_reply():
    # A long reply that quotes a shell prompt, still being written, so the
    # input box is nowhere near the bottom of the screen.
    screen = ["● Run this:", "  > npm run build",
              "  Then check the output.", "  It writes to dist/.",
              "  Nothing else changes.", "  Let me know how it goes.",
              "  I'll wait.", "  Ready when you are.", "  Anything else?",
              "  That's everything."]
    out = sc.latest_reply(screen)
    assert out.startswith("Run this:"), (
        "a '>' quoted inside a reply is not the input box — only the bottom "
        "of the screen is searched for that, and here there is no box on "
        "screen at all")
    assert "That's everything." in out


def test_latest_reply_is_empty_when_it_has_not_spoken():
    assert sc.latest_reply(["> just do it", "✻ Cogitated for 14s"]) == ""
    assert sc.latest_reply(None) == "" and sc.latest_reply([]) == ""


def test_latest_reply_does_not_mistake_a_menu_choice_for_your_input():
    out = sc.latest_reply(["● Which way round?", "> 1. First", "  2. Second"])
    assert out.startswith("Which way round?"), (
        "'> 1.' is the menu's highlight marker, not something you typed")


def test_latest_reply_stops_at_the_prompt():
    start = sc.parse_console_prompt(REAL_SCREEN)["start"]
    assert "Networking events" not in sc.latest_reply(REAL_SCREEN, upto=start)


#: Verbatim from a session that had just answered — including the input box
#: and the status bar UNDER it, which is what the window wrongly showed.
REAL_TALKING = [
    "● That came back as a clarify request again — what would you like to clarify?",
    "✻ Worked for 5s",
    "✻ recap: You're testing the multiple-choice question picker with random"
    " casual questions, not project work. (disable recaps in /config)",
    "  again please",
    "● User answered Claude's questions:",
    "  ⎿ · You get to permanently delete one everyday annoyance. Which goes?"
    " → Traffic",
    "● Traffic — the honest answer. It's the only one on that list you can't"
    " opt out of by changing a habit or a setting.",
    "",
    "  Ready for another whenever you are.",
    "✻ Sautéed for 8s",
    "",
    "> again please",
    "  mian-electric  main  Opus 5 (1M context)  ctx 5%",
    "  ▣manual mode on · ← for agents",
]


def test_latest_reply_ignores_the_input_box_and_status_bar():
    out = sc.latest_reply(REAL_TALKING)
    assert out.startswith("Traffic — the honest answer")
    assert out.endswith("Ready for another whenever you are.")
    assert "ctx 5%" not in out and "manual mode" not in out, (
        "the input box is at the BOTTOM of the TUI, so anything keyed off "
        "'after the last thing typed' lands in the status bar under it")
    assert "clarify request" not in out, "that is an older turn"


def test_latest_reply_knows_the_bullet_a_real_session_prints():
    # U+25CF, read off a live session — NOT U+23FA, which is what these
    # fixtures used to say. Every test passed and the reply pane was empty for
    # every real session, because the fixtures agreed with the guess instead
    # of with Claude Code.
    assert sc.latest_reply(["● Here's what I found.", "> "]) == \
        "Here's what I found."
    assert sc._SAID_RE.match("● x"), "U+25CF is THE bullet"
    for lookalike in ("⏺", "•", "⧉"):
        assert sc._SAID_RE.match(lookalike + " x"), (
            f"{lookalike!r} still has to parse — it turns up in transcripts "
            f"and docs, and a near-miss shows a blank pane with no clue why")


def test_latest_reply_is_capped():
    out = sc.latest_reply(["● " + "word " * 400], max_chars=120)
    assert len(out) <= 120


def test_prose_that_mentions_cancelling_is_not_a_menu():
    # Claude writing a numbered list and then a sentence containing one hint
    # phrase used to become clickable buttons — and clicking one typed a bare
    # digit into the session as the next instruction.
    screen = [
        "● I found three problems:",
        "  1. the retry budget is unbounded",
        "  2. the timeout is per-attempt",
        "  3. errors are swallowed",
        "● Tell me which to fix, or press Esc to cancel.",
        "> ",
    ]
    assert sc.parse_console_prompt(screen) is None, (
        "inventing a menu is not a display glitch: the options become buttons "
        "and clicking one sends a wrong instruction to a live agent")


def test_the_real_hint_bar_is_still_a_menu():
    screen = ["Which way round?", "> 1. First", "  2. Second",
              "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    out = sc.parse_console_prompt(screen)
    assert out is not None and out["options"] == ("First", "Second")


def test_a_two_phrase_hint_bar_is_enough():
    screen = ["Which way round?", "> 1. First", "  2. Second",
              "Enter to select · Esc to cancel"]
    assert sc.parse_console_prompt(screen) is not None


def test_parse_console_prompt_needs_the_menu_hint():
    # Numbered prose is not a menu.
    screen = ["Here are three things:", "1. one", "2. two", "3. three"]
    assert sc.parse_console_prompt(screen) is None


def test_parse_console_prompt_rejects_a_broken_run():
    # 1,2,4 isn't a menu we can address by number.
    screen = ["Pick", "  1. a", "  2. b", "  4. d", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    assert sc.parse_console_prompt(screen) is None


def test_parse_console_prompt_skips_separators_for_the_question():
    screen = ["The actual question?", "────────────────────", "  1. a",
              "  2. b", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    assert sc.parse_console_prompt(screen)["question"] == "The actual question?"


@pytest.mark.parametrize("screen", [None, [], ["nothing here"], [""] * 5])
def test_parse_console_prompt_handles_nothing(screen):
    assert sc.parse_console_prompt(screen) is None


def test_parse_console_prompt_without_options_is_none():
    assert sc.parse_console_prompt(["Enter to select · ↑/↓ to navigate · Esc to cancel"]) is None


def test_parse_console_prompt_scrubs_control_characters():
    screen = ["Question\there?", "  1. opt\tone", "  2. two", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    out = sc.parse_console_prompt(screen)
    assert "\t" not in out["question"]
    assert all("\t" not in o for o in out["options"])


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def _t(kind, session, before="", after=""):
    return sc.Transition(kind, session, before=before, after=after)


def test_alert_on_becoming_waiting():
    s = _session(name="widget-b0", cwd="/x/widget",
                 status=sc.WAITING, waiting_for="input needed")
    a = sc.alert_for(_t("status", s, sc.BUSY, sc.WAITING))
    assert a["kind"] == "waiting" and a["color"] == "red"
    assert a["title"] == "Needs you"
    assert "widget-b0" in a["subtitle"] and "input needed" in a["subtitle"]


def test_alert_waiting_without_reason_falls_back_to_project():
    s = _session(name="n", cwd="/x/widget", status=sc.WAITING)
    a = sc.alert_for(_t("status", s, sc.BUSY, sc.WAITING))
    assert "widget" in a["subtitle"]


def test_alert_on_finishing():
    s = _session(name="n", cwd="/x/widget", status=sc.IDLE)
    a = sc.alert_for(_t("status", s, sc.BUSY, sc.IDLE))
    assert a["kind"] == "idle" and a["color"] == "green"
    assert a["title"] == "Finished"


def test_alert_on_session_ending():
    a = sc.alert_for(_t("gone", _session(name="n", cwd="/x/widget")))
    assert a["kind"] == "gone" and a["title"] == "Session ended"


@pytest.mark.parametrize("after", [sc.BUSY, sc.SHELL])
def test_no_alert_for_routine_work(after):
    # ->busy / ->shell happen constantly; alerting on them trains you to ignore
    # the toasts entirely.
    assert sc.alert_for(_t("status", _session(status=after), sc.IDLE, after)) is None


def test_no_alert_for_appearance():
    assert sc.alert_for(_t("appeared", _session())) is None


def test_alert_carries_pid_for_the_foreground_check():
    a = sc.alert_for(_t("gone", _session(pid=4242)))
    assert a["pid"] == 4242


def test_alert_scrubs_newlines():
    s = _session(title="two\nlines", cwd="/x/y", status=sc.WAITING,
                 waiting_for="a\nb")
    a = sc.alert_for(_t("status", s, sc.BUSY, sc.WAITING))
    assert "\n" not in a["subtitle"] and "\n" not in a["title"]


def test_alerts_for_filters_by_kind():
    events = [
        _t("status", _session(session_id="a", status=sc.WAITING), sc.BUSY, sc.WAITING),
        _t("status", _session(session_id="b", status=sc.IDLE), sc.BUSY, sc.IDLE),
        _t("gone", _session(session_id="c")),
    ]
    assert [a["kind"] for a in sc.alerts_for(events, ("waiting",))] == ["waiting"]
    assert sorted(a["kind"] for a in sc.alerts_for(events, ("idle", "gone"))) \
        == ["gone", "idle"]
    assert sc.alerts_for(events, ()) == []


def test_default_alert_kinds_exclude_gone():
    # Ending a session is usually something you just did yourself.
    assert "gone" not in sc.DEFAULT_ALERT_KINDS
    assert set(sc.DEFAULT_ALERT_KINDS) <= set(sc.ALERT_KINDS)


def test_alert_for_stuck_reports_the_wait():
    s = _session(name="n", cwd="/x/widget", status=sc.WAITING,
                 waiting_for="input needed", status_updated_at=1_000_000)
    a = sc.alert_for_stuck(s, at_ms=1_000_000 + 12 * 60_000)
    assert a["kind"] == "stuck" and a["color"] == "red"
    assert "12m" in a["title"]
    assert "input needed" in a["subtitle"]


# --------------------------------------------------------------------------- #
# coalesce_alerts — only one toast exists at a time
# --------------------------------------------------------------------------- #
def _mk_alerts(*kinds):
    out = []
    for i, kind in enumerate(kinds):
        s = _session(session_id=str(i), pid=i, cwd=f"/p/repo{i}",
                     name=f"sess-{i}", status=sc.WAITING,
                     waiting_for="input needed")
        if kind == "stuck":
            out.append(sc.alert_for_stuck(s))
        elif kind == sc.WAITING:
            out.append(sc.alert_for(_t("status", s, sc.BUSY, sc.WAITING)))
        elif kind == sc.IDLE:
            s = _session(session_id=str(i), pid=i, cwd=f"/p/repo{i}",
                         name=f"sess-{i}", status=sc.IDLE)
            out.append(sc.alert_for(_t("status", s, sc.BUSY, sc.IDLE)))
        else:
            s = _session(session_id=str(i), pid=i, cwd=f"/p/repo{i}",
                         name=f"sess-{i}")
            out.append(sc.alert_for(_t("gone", s)))
    return out


def test_coalesce_none_for_empty():
    assert sc.coalesce_alerts([]) is None


def test_coalesce_passes_a_single_alert_through_untouched():
    one = _mk_alerts(sc.IDLE)
    assert sc.coalesce_alerts(one) is one[0]


def test_coalesce_same_kind_counts_and_lists():
    merged = sc.coalesce_alerts(_mk_alerts(sc.IDLE, sc.IDLE, sc.IDLE))
    assert merged["title"] == "3 sessions finished"
    assert merged["color"] == "green"
    for name in ("sess-0", "sess-1", "sess-2"):
        assert name in merged["subtitle"]


def test_coalesce_mixed_kinds_summarises_counts():
    merged = sc.coalesce_alerts(_mk_alerts(sc.IDLE, sc.WAITING, sc.WAITING))
    assert merged["title"] == "3 session updates"
    assert "2 need you" in merged["subtitle"]
    assert "1 finished" in merged["subtitle"]


def test_coalesce_takes_the_colour_of_the_most_urgent():
    # Two finishes and one block: the block is what matters.
    merged = sc.coalesce_alerts(_mk_alerts(sc.IDLE, sc.IDLE, sc.WAITING))
    assert merged["color"] == "red" and merged["kind"] == sc.WAITING


def test_coalesce_urgency_order_is_not_declaration_order():
    merged = sc.coalesce_alerts(_mk_alerts("gone", "stuck"))
    assert merged["kind"] == "stuck"        # stuck outranks gone


def test_coalesce_rollup_belongs_to_no_session():
    # pid 0 so the foreground filter can never match and drop the rollup.
    merged = sc.coalesce_alerts(_mk_alerts(sc.IDLE, sc.IDLE))
    assert merged["pid"] == 0 and merged["session_id"] == ""


def test_coalesce_subtitle_is_single_line():
    merged = sc.coalesce_alerts(_mk_alerts(sc.IDLE, sc.IDLE, sc.IDLE))
    assert "\n" not in merged["subtitle"]


# --------------------------------------------------------------------------- #
# StuckWatcher
# --------------------------------------------------------------------------- #
def test_stuck_watcher_fires_once_per_block():
    base = 1_000_000
    s = _session(session_id="a", status=sc.WAITING, status_updated_at=base)
    w = sc.StuckWatcher(minutes=10)
    assert w.check([s], at_ms=base + 9 * 60_000) == []
    assert [x.session_id for x in w.check([s], at_ms=base + 10 * 60_000)] == ["a"]
    # Still blocked on later polls — must not nag again.
    assert w.check([s], at_ms=base + 11 * 60_000) == []
    assert w.check([s], at_ms=base + 60 * 60_000) == []


def test_stuck_watcher_rearms_after_the_session_moves_on():
    base = 1_000_000
    blocked = _session(session_id="a", status=sc.WAITING, status_updated_at=base)
    w = sc.StuckWatcher(minutes=10)
    w.check([blocked], at_ms=base + 10 * 60_000)
    w.check([_session(session_id="a", status=sc.BUSY)], at_ms=base + 11 * 60_000)
    again = _session(session_id="a", status=sc.WAITING,
                     status_updated_at=base + 20 * 60_000)
    assert [x.session_id
            for x in w.check([again], at_ms=base + 31 * 60_000)] == ["a"]


def test_stuck_watcher_ignores_non_waiting_sessions():
    s = _session(status=sc.BUSY, status_updated_at=0)
    assert sc.StuckWatcher(minutes=1).check([s], at_ms=10_000_000) == []


@pytest.mark.parametrize("minutes", [0, None, -1])
def test_stuck_watcher_disabled(minutes):
    s = _session(status=sc.WAITING, status_updated_at=0)
    assert sc.StuckWatcher(minutes=minutes).check([s], at_ms=10_000_000) == []


def test_stuck_watcher_forgets_departed_sessions():
    base = 1_000_000
    s = _session(session_id="a", status=sc.WAITING, status_updated_at=base)
    w = sc.StuckWatcher(minutes=10)
    w.check([s], at_ms=base + 10 * 60_000)
    w.check([], at_ms=base + 11 * 60_000)          # session gone entirely
    assert w._fired == set()


def test_stuck_watcher_reset():
    base = 1_000_000
    s = _session(session_id="a", status=sc.WAITING, status_updated_at=base)
    w = sc.StuckWatcher(minutes=10)
    w.check([s], at_ms=base + 10 * 60_000)
    w.reset()
    assert [x.session_id
            for x in w.check([s], at_ms=base + 11 * 60_000)] == ["a"]


def test_stuck_watcher_skips_unknown_dwell():
    assert sc.StuckWatcher(minutes=1).check([_session(status=sc.WAITING)],
                                            at_ms=10_000_000) == []


# --------------------------------------------------------------------------- #
# format_sessions — the flat dict every UI adapter renders verbatim
# --------------------------------------------------------------------------- #
def test_format_keys_never_collide_with_usage_core():
    # usage_core owns session_pct / session_color / session_resets_at for the
    # 5-hour meter, and both dicts get merged before rendering. A singular key
    # here would silently repaint that meter.
    import usage_core

    usage_keys = set(usage_core.status_display(usage_core.Status.OFFLINE))
    out = sc.format_sessions([_session()])
    assert not (set(out) & usage_keys), set(out) & usage_keys
    assert all(k.startswith("sessions_") for k in out)


@pytest.mark.parametrize("raw,expected", [
    ("line one\nline two", "line one line two"),
    ("a\tb", "a b"),
    ("a\r\nb", "a b"),
    ("  padded  ", "padded"),
    ("bell\x07here", "bell here"),
    ("nul\x00here", "nul here"),
    ("multi   spaces", "multi spaces"),
    ("", ""),
    (None, ""),
])
def test_oneline_scrubs_control_characters(raw, expected):
    assert sc.oneline(raw) == expected


def test_oneline_caps_length():
    assert len(sc.oneline("z" * 5000)) == 200


def test_format_scrubs_newlines_out_of_every_rendered_field():
    # A newline reaching Pillow raises "can't measure length of multiline
    # text", which escapes render_popover and takes down the whole widget.
    # Titles are model-generated and a POSIX dir name may contain a newline.
    s = _session(title="two\nlines", cwd="/p/we\nird", status=sc.WAITING,
                 waiting_for="input\nneeded", tool="a\tb", model="m\nz",
                 git_branch="br\nanch", last_prompt="p\n\nq")
    row = sc.format_sessions([s])["sessions_rows"][0]
    for key in ("label", "project", "status_text", "detail", "tool", "model",
                "branch", "last_prompt"):
        assert "\n" not in row[key] and "\t" not in row[key], key


def test_format_renders_after_scrubbing():
    # The end-to-end guarantee: hostile content must not crash the renderer.
    import render

    s = _session(title="two\nlines", cwd="/p/x", status=sc.BUSY)
    disp = dict(sc.format_sessions([s]))
    render.render_popover(disp, "light")      # must not raise


def test_format_empty_list():
    out = sc.format_sessions([])
    assert out["sessions_rows"] == []
    assert out["sessions_count"] == 0
    assert out["sessions_blocked"] == 0
    assert out["sessions_overflow"] == 0
    assert out["sessions_color"] == "grey"
    assert out["sessions_summary"] == ""
    assert "no sessions" in out["sessions_tooltip"].lower()


def test_format_row_fields():
    s = _session(session_id="a", pid=7, cwd="/x/widget", name="widget-b0",
                 status=sc.WAITING, waiting_for="input needed",
                 status_updated_at=1_000_000)
    row = sc.format_sessions([s], at_ms=1_240_000)["sessions_rows"][0]
    assert row["session_id"] == "a" and row["pid"] == 7
    assert row["label"] == "widget-b0" and row["project"] == "widget"
    assert row["status"] == sc.WAITING
    assert row["color"] == "red" and row["emoji"] == "🔴"
    assert row["dwell"] == "4m"
    assert row["detail"] == "needs you: input needed · 4m"


def test_format_detail_omits_dwell_when_unknown():
    row = sc.format_sessions([_session(status=sc.IDLE)])["sessions_rows"][0]
    assert row["detail"] == "done"


def test_format_orders_blocked_first():
    out = sc.format_sessions([
        _session(session_id="i", status=sc.IDLE, status_updated_at=900),
        _session(session_id="w", status=sc.WAITING, status_updated_at=900),
    ], at_ms=1000)
    assert [r["session_id"] for r in out["sessions_rows"]] == ["w", "i"]


def test_format_caps_rows_and_reports_overflow():
    many = [_session(session_id=str(i), status=sc.IDLE) for i in range(10)]
    out = sc.format_sessions(many, max_rows=4)
    assert len(out["sessions_rows"]) == 4
    assert out["sessions_count"] == 10
    assert out["sessions_overflow"] == 6


def test_format_no_overflow_when_it_fits():
    out = sc.format_sessions([_session()], max_rows=6)
    assert out["sessions_overflow"] == 0


def test_format_max_rows_floor_is_one():
    many = [_session(session_id=str(i)) for i in range(3)]
    assert len(sc.format_sessions(many, max_rows=0)["sessions_rows"]) == 3


def test_format_counts_blocked():
    out = sc.format_sessions([
        _session(session_id="a", status=sc.WAITING),
        _session(session_id="b", status=sc.WAITING),
        _session(session_id="c", status=sc.BUSY),
    ])
    assert out["sessions_blocked"] == 2


def test_format_carries_enrichment_through():
    s = _session(title="Ship it", tool="Bash", model="claude-opus-5",
                 git_branch="main", last_prompt="go")
    row = sc.format_sessions([s])["sessions_rows"][0]
    assert row["label"] == "Ship it" and row["tool"] == "Bash"
    assert row["model"] == "claude-opus-5" and row["branch"] == "main"
    assert row["last_prompt"] == "go"


@pytest.mark.parametrize("statuses,expected", [
    ([sc.WAITING, sc.BUSY, sc.IDLE], "red"),
    ([sc.BUSY, sc.IDLE], "amber"),
    ([sc.SHELL, sc.IDLE], "amber"),
    ([sc.IDLE], "green"),
    ([sc.UNKNOWN], "grey"),
    ([], "grey"),
])
def test_overall_color(statuses, expected):
    sessions = [_session(session_id=str(i), status=s)
                for i, s in enumerate(statuses)]
    assert sc.overall_color(sessions) == expected


def test_summarize_orders_by_urgency():
    out = sc.summarize([
        _session(session_id="a", status=sc.IDLE),
        _session(session_id="b", status=sc.BUSY),
        _session(session_id="c", status=sc.WAITING),
    ])
    assert out == "1 needs you   ·   1 working   ·   1 done"


def test_summarize_empty():
    assert sc.summarize([]) == ""


def test_summarize_counts_unknown_last():
    out = sc.summarize([_session(session_id="a", status=sc.UNKNOWN),
                        _session(session_id="b", status=sc.BUSY)])
    assert out == "1 working   ·   1 unknown"


@pytest.mark.parametrize("status", list(sc.KNOWN_STATUSES) + [sc.UNKNOWN])
def test_status_emoji_defined_for_every_status(status):
    assert sc.status_emoji(status)


def test_status_emoji_unknown_fallback():
    assert sc.status_emoji("banana") == sc.status_emoji(sc.UNKNOWN)


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


def test_read_json_retries_past_a_split_multibyte_character(tmp_path, monkeypatch):
    # A cwd with non-ASCII in it makes this reachable: a truncated write can
    # end mid UTF-8 sequence, and read_text then raises UnicodeDecodeError
    # (a ValueError, NOT an OSError).
    # ensure_ascii=False, or the payload is pure ASCII and there is nothing to
    # split \u2014 the whole point is a real multi-byte sequence on disk.
    payload = json.dumps({"a": "\u00d6stberg"}, ensure_ascii=False).encode("utf-8")
    torn = payload[:payload.rindex(b"\xc3") + 1]
    f = tmp_path / "a.json"
    reads = {"n": 0}

    def flaky(self, *a, **k):
        reads["n"] += 1
        if reads["n"] == 1:
            return torn.decode("utf-8")      # raises UnicodeDecodeError
        return payload.decode("utf-8")

    monkeypatch.setattr(type(f), "read_text", flaky)
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    assert sc._read_json(f) == {"a": "\u00d6stberg"}
    assert reads["n"] == 2


def test_read_json_survives_a_permanently_split_multibyte_file(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    payload = json.dumps({"a": "\u00d6stberg"}, ensure_ascii=False).encode("utf-8")
    f = tmp_path / "a.json"
    f.write_bytes(payload[:payload.rindex(b"\xc3") + 1])
    assert sc._read_json(f) is None          # must not raise


def test_scan_recovers_a_session_split_mid_utf8(tmp_path, monkeypatch):
    _write_session(tmp_path, 4242, status="busy", cwd="C:/\u00d6stberg")
    path = tmp_path / "sessions" / "4242.json"
    # Rewrite without \u escapes so the bytes on disk really are multi-byte.
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")),
                               ensure_ascii=False), encoding="utf-8")
    payload = path.read_bytes()
    reads = {"n": 0}

    def flaky(self, *a, **k):
        reads["n"] += 1
        if reads["n"] == 1:
            return payload[:payload.rindex(b"\xc3") + 1].decode("utf-8")
        return payload.decode("utf-8")

    monkeypatch.setattr(sc.Path, "read_text", flaky)
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    sessions, health = sc.scan(tmp_path, alive=_alive_all)
    assert [s.pid for s in sessions] == [4242]      # recovered, not dropped
    assert health == "ok"


def test_read_json_gives_up_on_real_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)
    f = tmp_path / "a.json"
    f.write_text("{ broken", encoding="utf-8")
    assert sc._read_json(f) is None


def test_read_json_tolerates_a_utf8_bom(tmp_path):
    # PowerShell's Set-Content defaults to a BOM on Windows; a BOM reaching
    # json.loads fails the parse and silently drops a live session.
    f = tmp_path / "a.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode("utf-8"))
    assert sc._read_json(f) == {"a": 1}


def test_scan_reads_a_session_file_with_a_bom(tmp_path):
    _write_session(tmp_path, 4242, status="busy")
    path = tmp_path / "sessions" / "4242.json"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    sessions, health = sc.scan(tmp_path, alive=_alive_all)
    assert [s.pid for s in sessions] == [4242] and health == "ok"


def test_transcript_tail_tolerates_a_bom(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_bytes(b"\xef\xbb\xbf"
                  + json.dumps({"type": "ai-title", "aiTitle": "B"}).encode())
    assert sc.read_transcript_tail(f)["title"] == "B"


def test_last_prompts_tolerates_a_bom(tmp_path):
    (tmp_path / "history.jsonl").write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps({"display": "hi", "sessionId": "a"}).encode() + b"\n")
    assert sc.last_prompts(tmp_path) == {"a": "hi"}


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


def test_tracker_records_ended_sessions():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a", name="one")], at_ms=1000)
    t.update([], at_ms=5000)
    assert [(when, s.name) for when, s in t.recent] == [(5000, "one")]


def test_tracker_recent_is_newest_first():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a", name="one"),
              _session(session_id="b", name="two")], at_ms=1000)
    t.update([_session(session_id="b", name="two")], at_ms=2000)   # a ended
    t.update([], at_ms=3000)                                       # b ended
    assert [s.name for _w, s in t.recent] == ["two", "one"]


def test_tracker_recent_is_capped():
    t = sc.SessionTracker(confirm_ticks=1)
    for i in range(sc.SessionTracker.RECENT_LIMIT + 5):
        t.update([_session(session_id=str(i))], at_ms=1000 + i)
        t.update([], at_ms=2000 + i)
    assert len(t.recent) == sc.SessionTracker.RECENT_LIMIT


def test_tracker_recent_ignores_a_brief_dropout():
    t = sc.SessionTracker(confirm_ticks=2)
    t.update([_session(session_id="a")], at_ms=1000)
    t.update([], at_ms=2000)                        # one missed poll only
    t.update([_session(session_id="a")], at_ms=3000)
    assert t.recent == []


def test_tracker_recent_is_a_copy():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a")], at_ms=1000)
    t.update([], at_ms=2000)
    t.recent.clear()
    assert len(t.recent) == 1


def test_tracker_reset_clears_recent():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a")], at_ms=1000)
    t.update([], at_ms=2000)
    t.reset()
    assert t.recent == []


# --------------------------------------------------------------------------- #
# format_recent
# --------------------------------------------------------------------------- #
def test_format_recent_rows():
    t = sc.SessionTracker(confirm_ticks=1)
    t.update([_session(session_id="a", name="widget-b0", cwd="/x/widget")],
             at_ms=1_000_000)
    t.update([], at_ms=1_000_000)
    rows = sc.format_recent(t.recent, at_ms=1_000_000 + 5 * 60_000)
    assert rows[0]["label"] == "widget-b0"
    assert rows[0]["project"] == "widget"
    assert rows[0]["ago"] == "5m"
    assert rows[0]["detail"] == "ended 5m"


def test_format_recent_empty():
    assert sc.format_recent([]) == []


def test_format_recent_respects_the_limit():
    t = sc.SessionTracker(confirm_ticks=1)
    for i in range(6):
        t.update([_session(session_id=str(i))], at_ms=1000)
        t.update([], at_ms=2000)
    assert len(sc.format_recent(t.recent, at_ms=3000, limit=3)) == 3


def test_format_recent_scrubs_control_characters():
    rows = sc.format_recent([(1000, _session(title="a\nb", cwd="/x/y"))],
                            at_ms=2000)
    assert "\n" not in rows[0]["label"]


def test_format_recent_handles_a_future_timestamp():
    rows = sc.format_recent([(9_000, _session(name="n"))], at_ms=1_000)
    assert rows[0]["ago"] == "just now"      # never a negative age


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
