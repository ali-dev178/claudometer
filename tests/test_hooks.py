"""Hermetic pytest suite for hooks.py and hook_relay.py.

This module edits a file that belongs to Claude Code, so the tests lean hard on
the safety properties: unrelated settings survive, a file we can't parse is
never overwritten, removal restores what was there, and the relay can't disturb
the session that spawned it.

Every test points CLAUDE_CONFIG_DIR and CLAUDOMETER_EVENTS_DIR inside tmp_path,
so the real ~/.claude is never read or written.
"""

import json
import os
import subprocess
import sys
import time

import pytest

import hooks


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CLAUDOMETER_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("CLAUDOMETER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hooks.Path, "home", staticmethod(lambda: tmp_path))
    return cfg


def _write(cfg, data):
    (cfg / "settings.json").write_text(json.dumps(data, indent=2),
                                       encoding="utf-8")


def _read(cfg):
    return json.loads((cfg / "settings.json").read_text(encoding="utf-8"))


OTHER_HOOK = {"hooks": [{"type": "command", "command": "echo not-ours"}]}


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def test_only_four_events_are_registered():
    # Pre/PostToolUse fire constantly and each costs a process spawn on the
    # critical path of the user's session.
    assert hooks.EVENTS == ("Notification", "Stop", "SessionStart", "SessionEnd")
    assert "PreToolUse" not in hooks.EVENTS
    assert "PostToolUse" not in hooks.EVENTS


def test_build_entries_covers_every_event():
    entries = hooks.build_entries("CMD")
    assert set(entries) == set(hooks.EVENTS)
    for event in hooks.EVENTS:
        hook = entries[event][0]["hooks"][0]
        assert hook["type"] == "command" and hook["command"] == "CMD"
        assert hook["timeout"] == hooks.HOOK_TIMEOUT_S


def test_build_entries_empty_without_an_interpreter(monkeypatch):
    monkeypatch.setattr(hooks, "interpreter", lambda: None)
    assert hooks.build_entries() == {}


def test_hook_command_quotes_paths_with_spaces(monkeypatch):
    monkeypatch.setattr(hooks, "interpreter", lambda: r"C:\Program Files\py.exe")
    cmd = hooks.hook_command()
    assert cmd.startswith('"C:\\Program Files\\py.exe"')
    assert hooks.RELAY_NAME in cmd


def test_relay_file_exists():
    assert hooks.relay_path().is_file()


# --------------------------------------------------------------------------- #
# install / status
# --------------------------------------------------------------------------- #
def test_install_creates_settings_when_absent(_isolate):
    assert hooks.install() is True
    data = _read(_isolate)
    assert set(data["hooks"]) == set(hooks.EVENTS)
    assert hooks.status() == hooks.INSTALLED


def test_install_preserves_unrelated_settings(_isolate):
    _write(_isolate, {"statusLine": {"type": "command", "command": "node x.js"},
                      "effortLevel": "low"})
    assert hooks.install() is True
    data = _read(_isolate)
    assert data["statusLine"] == {"type": "command", "command": "node x.js"}
    assert data["effortLevel"] == "low"


def test_install_preserves_someone_elses_hooks(_isolate):
    _write(_isolate, {"hooks": {"Stop": [OTHER_HOOK],
                                "PreToolUse": [OTHER_HOOK]}})
    hooks.install()
    data = _read(_isolate)
    assert OTHER_HOOK in data["hooks"]["Stop"]      # theirs kept
    assert len(data["hooks"]["Stop"]) == 2          # plus ours
    assert data["hooks"]["PreToolUse"] == [OTHER_HOOK]   # untouched entirely


def test_install_is_idempotent(_isolate):
    hooks.install()
    hooks.install()
    hooks.install()
    for event in hooks.EVENTS:
        assert len(_read(_isolate)["hooks"][event]) == 1


def test_install_replaces_a_stale_entry_of_ours(_isolate, monkeypatch):
    monkeypatch.setattr(hooks, "interpreter", lambda: "/old/python")
    hooks.install()
    assert hooks.status() == hooks.INSTALLED
    monkeypatch.setattr(hooks, "interpreter", lambda: "/new/python")
    assert hooks.status() == hooks.STALE
    hooks.install()
    assert hooks.status() == hooks.INSTALLED
    for event in hooks.EVENTS:
        entries = _read(_isolate)["hooks"][event]
        assert len(entries) == 1
        assert "/new/python" in entries[0]["hooks"][0]["command"]


def test_install_backs_up_the_original(_isolate):
    _write(_isolate, {"effortLevel": "low"})
    hooks.install()
    backup = _isolate / "settings.json.claudometer-bak"
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == {"effortLevel": "low"}


def test_backup_keeps_the_pristine_original(_isolate):
    _write(_isolate, {"effortLevel": "low"})
    hooks.install()
    hooks.remove()
    hooks.install()
    backup = _isolate / "settings.json.claudometer-bak"
    # Still the file as it was BEFORE we ever touched it.
    assert json.loads(backup.read_text(encoding="utf-8")) == {"effortLevel": "low"}


def test_install_refuses_when_settings_is_unparsable(_isolate):
    (_isolate / "settings.json").write_text("{ not json", encoding="utf-8")
    assert hooks.install() is False
    # The user's file is left exactly as found rather than overwritten.
    assert (_isolate / "settings.json").read_text(encoding="utf-8") == "{ not json"
    assert hooks.status() == hooks.ABSENT


def test_install_refuses_without_an_interpreter(_isolate, monkeypatch):
    monkeypatch.setattr(hooks, "interpreter", lambda: None)
    assert hooks.install() is False
    assert not (_isolate / "settings.json").exists()


def test_status_unavailable_without_an_interpreter(monkeypatch):
    monkeypatch.setattr(hooks, "interpreter", lambda: None)
    assert hooks.status() == hooks.UNAVAILABLE


def test_status_absent_on_a_fresh_config():
    assert hooks.status() == hooks.ABSENT


def test_status_absent_when_only_other_hooks_exist(_isolate):
    _write(_isolate, {"hooks": {"Stop": [OTHER_HOOK]}})
    assert hooks.status() == hooks.ABSENT


def test_settings_readable_contract(_isolate):
    assert hooks.settings_readable() is True          # absent is fine
    _write(_isolate, {"a": 1})
    assert hooks.settings_readable() is True
    (_isolate / "settings.json").write_text("{ broken", encoding="utf-8")
    assert hooks.settings_readable() is False


def test_read_settings_tolerates_a_bom(_isolate):
    (_isolate / "settings.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"effortLevel": "low"}).encode())
    assert hooks.read_settings() == {"effortLevel": "low"}


def test_read_settings_ignores_a_non_object(_isolate):
    (_isolate / "settings.json").write_text("[1,2,3]", encoding="utf-8")
    assert hooks.read_settings() == {}


# --------------------------------------------------------------------------- #
# remove
# --------------------------------------------------------------------------- #
def test_remove_takes_only_our_entries(_isolate):
    _write(_isolate, {"hooks": {"Stop": [OTHER_HOOK]}, "effortLevel": "low"})
    hooks.install()
    assert hooks.remove() is True
    data = _read(_isolate)
    assert data["hooks"] == {"Stop": [OTHER_HOOK]}
    assert data["effortLevel"] == "low"
    assert hooks.status() == hooks.ABSENT


def test_remove_drops_empty_lists_and_the_hooks_object(_isolate):
    _write(_isolate, {"effortLevel": "low"})
    hooks.install()
    hooks.remove()
    data = _read(_isolate)
    assert "hooks" not in data          # no empty {} left behind
    assert data == {"effortLevel": "low"}


def test_remove_is_a_noop_when_nothing_is_installed(_isolate):
    _write(_isolate, {"effortLevel": "low"})
    assert hooks.remove() is True
    assert _read(_isolate) == {"effortLevel": "low"}


def test_remove_refuses_on_an_unparsable_file(_isolate):
    (_isolate / "settings.json").write_text("{ broken", encoding="utf-8")
    assert hooks.remove() is False
    assert (_isolate / "settings.json").read_text(encoding="utf-8") == "{ broken"


def test_install_remove_round_trip_is_lossless(_isolate):
    original = {"statusLine": {"type": "command", "command": "node x.js"},
                "enabledPlugins": {"a@b": True},
                "hooks": {"PreToolUse": [OTHER_HOOK]},
                "effortLevel": "low"}
    _write(_isolate, original)
    hooks.install()
    hooks.remove()
    assert _read(_isolate) == original


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #
def test_preview_is_the_json_that_will_be_written():
    text = hooks.preview()
    parsed = json.loads(text)
    assert set(parsed["hooks"]) == set(hooks.EVENTS)


def test_preview_explains_itself_without_an_interpreter(monkeypatch):
    monkeypatch.setattr(hooks, "interpreter", lambda: None)
    text = hooks.preview()
    assert "aren't available" in text
    with pytest.raises(ValueError):
        json.loads(text)            # prose, not JSON


# --------------------------------------------------------------------------- #
# The spool
# --------------------------------------------------------------------------- #
def _drop(payload, name="1-1.json"):
    d = hooks.spool_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def test_read_events_returns_and_drains():
    _drop({"hook_event_name": "Stop", "session_id": "a"})
    assert len(hooks.read_events()) == 1
    assert hooks.read_events() == []        # consumed


def test_read_events_empty_without_a_spool():
    assert hooks.read_events() == []


def test_read_events_is_oldest_first():
    for i in (3, 1, 2):
        _drop({"hook_event_name": "Stop", "n": i}, name=f"{i}.json")
    assert [e["n"] for e in hooks.read_events()] == [1, 2, 3]


def test_read_events_deletes_unparsable_files():
    d = hooks.spool_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.json").write_text("{ broken", encoding="utf-8")
    assert hooks.read_events() == []
    # Deleted, or we would re-read it forever.
    assert not (d / "bad.json").exists()


def test_read_events_honours_the_cap():
    for i in range(20):
        _drop({"n": i}, name=f"{i:03d}.json")
    assert len(hooks.read_events(max_files=5)) == 5


def test_read_events_ignores_non_object_payloads():
    _drop([1, 2, 3])
    assert hooks.read_events() == []


def test_prune_removes_only_old_files():
    _drop({"a": 1}, name="old.json")
    _drop({"a": 2}, name="new.json")
    d = hooks.spool_dir()
    os.utime(d / "old.json", (1000, 1000))
    assert hooks.prune(max_age_s=60, now=10_000) == 1
    assert not (d / "old.json").exists()
    assert (d / "new.json").exists()


def test_prune_without_a_spool_is_zero():
    assert hooks.prune() == 0


def test_clear_spool_empties_it():
    _drop({"a": 1}, name="a.json")
    _drop({"a": 2}, name="b.json")
    hooks.clear_spool()
    assert hooks.read_events() == []


# --------------------------------------------------------------------------- #
# summarize
# --------------------------------------------------------------------------- #
def test_summarize_extracts_the_useful_fields():
    out = hooks.summarize({
        "hook_event_name": "Notification", "session_id": "abc",
        "cwd": "/p/x", "message": "Claude needs your permission to run Bash",
        "title": "Claude Code"})
    assert out["event"] == "Notification" and out["session_id"] == "abc"
    assert "permission" in out["message"]


def test_summarize_handles_a_stop_payload():
    out = hooks.summarize({"hook_event_name": "Stop", "session_id": "a",
                           "last_assistant_message": "all done"})
    assert out["last_message"] == "all done"


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_summarize_rejects_non_dicts(bad):
    assert hooks.summarize(bad) == {}


def test_summarize_fills_missing_fields_with_empty_strings():
    out = hooks.summarize({"hook_event_name": "Stop"})
    assert out["message"] == "" and out["session_id"] == ""


# --------------------------------------------------------------------------- #
# hook_relay — runs as a real subprocess, the way Claude Code invokes it
# --------------------------------------------------------------------------- #
def _relay_env(extra=None):
    env = dict(os.environ)
    env["CLAUDOMETER_EVENTS_DIR"] = str(hooks.spool_dir())
    env["CLAUDOMETER_HOME"] = str(hooks.home_dir())
    env["CLAUDE_CONFIG_DIR"] = str(hooks.settings_path().parent)
    env.update(extra or {})
    return env


def _run_relay(payload, env_extra=None, script=None):
    hooks.touch_heartbeat()          # healthy by default; staleness is opt-in
    return subprocess.run([sys.executable, str(script or hooks.relay_path())],
                          input=payload, capture_output=True, timeout=30,
                          env=_relay_env(env_extra))


def test_relay_writes_the_payload_to_the_spool():
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "z"}).encode()
    out = _run_relay(payload)
    assert out.returncode == 0
    events = hooks.read_events()
    assert len(events) == 1 and events[0]["session_id"] == "z"


def test_relay_is_silent():
    # Anything on stdout can be interpreted by the session that spawned it.
    out = _run_relay(json.dumps({"hook_event_name": "Stop"}).encode())
    assert out.stdout == b"" and out.stderr == b""


def test_relay_survives_empty_stdin():
    out = _run_relay(b"")
    assert out.returncode == 0
    assert hooks.read_events() == []


def test_relay_survives_garbage_stdin():
    # It doesn't parse, so garbage is written through and rejected on read.
    out = _run_relay(b"not json at all")
    assert out.returncode == 0
    assert hooks.read_events() == []


def test_relay_never_leaves_a_partial_file():
    _run_relay(json.dumps({"hook_event_name": "Stop"}).encode())
    leftovers = list(hooks.spool_dir().glob("*.tmp"))
    assert not leftovers, "relay must rename into place, never leave .tmp"


def test_relay_concurrent_writes_do_not_collide():
    hooks.touch_heartbeat()
    payloads = [json.dumps({"hook_event_name": "Stop", "n": i}).encode()
                for i in range(6)]
    procs = []
    env = _relay_env()
    for p in payloads:
        procs.append(subprocess.Popen(
            [sys.executable, str(hooks.relay_path())], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))
    for proc, p in zip(procs, payloads):
        proc.communicate(input=p, timeout=30)
    assert len(hooks.read_events()) == 6


# --------------------------------------------------------------------------- #
# Surviving — and cleaning up after — an uninstall
# --------------------------------------------------------------------------- #
def test_hook_points_at_the_copy_not_the_app_directory():
    # Pointing at the app means the hook dies on every update and permanently
    # on uninstall, leaving Claude Code spawning a failing command forever.
    command = hooks.hook_command()
    assert str(hooks.installed_relay_path()) in command
    assert str(hooks.relay_path().parent) not in command


def test_install_copies_the_relay_and_writes_a_heartbeat():
    hooks.install()
    assert hooks.installed_relay_path().is_file()
    assert hooks.heartbeat_path().is_file()


def test_installed_relay_is_a_faithful_copy():
    hooks.install()
    assert (hooks.installed_relay_path().read_bytes()
            == hooks.relay_path().read_bytes())


def test_remove_deletes_the_copy_and_heartbeat():
    hooks.install()
    hooks.remove()
    assert not hooks.installed_relay_path().exists()
    assert not hooks.heartbeat_path().exists()


def test_updating_the_app_does_not_strand_the_hook(_isolate, monkeypatch):
    """The command must not change when the application directory moves."""
    hooks.install()
    before = _read(_isolate)["hooks"]["Stop"][0]["hooks"][0]["command"]
    moved = _isolate.parent / "new-app-location"
    moved.mkdir()
    monkeypatch.setattr(hooks, "relay_path", lambda: moved / hooks.RELAY_NAME)
    (moved / hooks.RELAY_NAME).write_text("# moved", encoding="utf-8")
    hooks.install()
    after = _read(_isolate)["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert before == after
    assert hooks.status() == hooks.INSTALLED


def test_relay_still_runs_after_the_app_is_deleted(_isolate, tmp_path):
    """An uninstall must not make Claude Code spawn a missing file."""
    hooks.install()
    hooks.touch_heartbeat()
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "a"}).encode()
    # Run the INSTALLED copy — the app directory is irrelevant to it.
    out = subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                         input=payload, capture_output=True, timeout=30,
                         env=_relay_env())
    assert out.returncode == 0 and out.stderr == b""
    assert len(hooks.read_events()) == 1


def _make_stale():
    hooks.touch_heartbeat()
    old = time.time() - (hooks.STALE_DAYS + 1) * 86400
    os.utime(hooks.heartbeat_path(), (old, old))


def test_relay_uninstalls_itself_once_abandoned(_isolate):
    _write(_isolate, {"effortLevel": "low"})
    hooks.install()
    _make_stale()
    out = subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                         input=json.dumps({"hook_event_name": "Stop"}).encode(),
                         capture_output=True, timeout=30, env=_relay_env())
    assert out.returncode == 0
    data = _read(_isolate)
    assert "hooks" not in data                    # entries gone
    assert data == {"effortLevel": "low"}         # everything else intact
    assert not hooks.installed_relay_path().exists()   # deleted itself
    assert not hooks.heartbeat_path().exists()


def test_abandoned_relay_keeps_other_peoples_hooks(_isolate):
    _write(_isolate, {"hooks": {"Stop": [OTHER_HOOK], "PreToolUse": [OTHER_HOOK]}})
    hooks.install()
    _make_stale()
    subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                   input=b"{}", capture_output=True, timeout=30,
                   env=_relay_env())
    data = _read(_isolate)
    assert data["hooks"]["Stop"] == [OTHER_HOOK]
    assert data["hooks"]["PreToolUse"] == [OTHER_HOOK]


def test_abandoned_relay_writes_nothing_to_the_spool(_isolate):
    hooks.install()
    _make_stale()
    subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                   input=json.dumps({"hook_event_name": "Stop"}).encode(),
                   capture_output=True, timeout=30, env=_relay_env())
    assert hooks.read_events() == []


def test_abandoned_relay_leaves_an_unparsable_settings_alone(_isolate):
    hooks.install()
    (_isolate / "settings.json").write_text("{ broken", encoding="utf-8")
    _make_stale()
    subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                   input=b"{}", capture_output=True, timeout=30,
                   env=_relay_env())
    # Better a stale hook than a destroyed configuration.
    assert (_isolate / "settings.json").read_text(encoding="utf-8") == "{ broken"


def test_a_missing_heartbeat_counts_as_abandoned(_isolate):
    hooks.install()
    hooks.heartbeat_path().unlink()
    subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                   input=b"{}", capture_output=True, timeout=30,
                   env=_relay_env())
    assert "hooks" not in _read(_isolate)


def test_a_fresh_heartbeat_keeps_the_relay_working(_isolate):
    hooks.install()
    hooks.touch_heartbeat()
    subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                   input=json.dumps({"hook_event_name": "Stop"}).encode(),
                   capture_output=True, timeout=30, env=_relay_env())
    assert hooks.status() == hooks.INSTALLED
    assert len(hooks.read_events()) == 1


def test_reinstall_after_self_uninstall(_isolate):
    """Coming back must just work, silently."""
    hooks.install()
    _make_stale()
    subprocess.run([sys.executable, str(hooks.installed_relay_path())],
                   input=b"{}", capture_output=True, timeout=30,
                   env=_relay_env())
    assert hooks.status() == hooks.ABSENT
    assert hooks.install() is True
    assert hooks.status() == hooks.INSTALLED
