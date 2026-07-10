"""Hermetic pytest suite for resume.py.

Covers the PURE logic and command-building WITHOUT ever launching a process:
  * ``last_session`` parsing / validation (cwd must exist, most-recent wins,
    history.jsonl fallback), driven off a monkeypatched sessions source rooted
    in ``tmp_path``.
  * quoting helpers ``_sh`` / ``_ps``.
  * ``open_terminal`` / ``run_auto`` command construction — ``subprocess.Popen``,
    ``shutil.which`` and (defensively) ``os.startfile`` are monkeypatched so NO
    real process is ever spawned; we only inspect the args they were handed and
    assert the session id / cwd are safely quoted.

Every test is self-contained: it writes only under ``tmp_path`` and never
touches the network, real credentials, or the user's real ~/.claude.
"""

import json
import os
import sys

import pytest

import resume


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Make sure no ambient CLAUDE_CONFIG_DIR leaks in, and Path.home() points
    somewhere harmless inside tmp_path (so the ~/.claude fallback can never read
    the real user's data)."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(resume.Path, "home", staticmethod(lambda: fake_home))
    return fake_home


class _Recorder:
    """Stand-in for subprocess.Popen that records the call instead of spawning."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, *popen_args, **kwargs):
        self.calls.append({"args": args, "popen_args": popen_args, "kwargs": kwargs})
        return object()  # a harmless fake "process handle"

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def popen_rec(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(resume.subprocess, "Popen", rec)
    return rec


@pytest.fixture
def no_startfile(monkeypatch):
    """Defensively ensure os.startfile (Windows only) is never really invoked."""
    started = []
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda *a, **k: started.append((a, k)))
    return started


def _write_session(sdir, name, data, mtime=None):
    sdir.mkdir(parents=True, exist_ok=True)
    f = sdir / name
    f.write_text(json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


# --------------------------------------------------------------------------- #
# _config_dir
# --------------------------------------------------------------------------- #
def test_config_dir_explicit_argument_wins(tmp_path):
    explicit = tmp_path / "explicit"
    assert resume._config_dir(explicit) == resume.Path(explicit)


def test_config_dir_uses_env_when_no_arg(monkeypatch, tmp_path):
    envdir = tmp_path / "from_env"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(envdir))
    assert resume._config_dir() == resume.Path(str(envdir))


def test_config_dir_falls_back_to_home_claude(_isolate_env):
    # _isolate_env pointed Path.home() at a fake home dir.
    assert resume._config_dir() == _isolate_env / ".claude"


def test_config_dir_arg_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "env"))
    arg = tmp_path / "arg"
    assert resume._config_dir(arg) == resume.Path(arg)


# --------------------------------------------------------------------------- #
# Quoting helpers: _sh (POSIX) and _ps (PowerShell)
# --------------------------------------------------------------------------- #
def test_sh_wraps_in_single_quotes():
    assert resume._sh("abc") == "'abc'"


def test_sh_escapes_embedded_single_quote():
    # POSIX close-quote, escaped literal quote, reopen-quote.
    assert resume._sh("a'b") == "'a'\\''b'"


def test_sh_neutralizes_shell_metacharacters():
    dangerous = "x; rm -rf /"
    out = resume._sh(dangerous)
    assert out == "'x; rm -rf /'"
    # The whole payload lives inside one quoted token.
    assert out.startswith("'") and out.endswith("'")


def test_sh_coerces_non_string():
    assert resume._sh(123) == "'123'"


def test_ps_wraps_in_single_quotes():
    assert resume._ps("abc") == "'abc'"


def test_ps_doubles_embedded_single_quote():
    # PowerShell escapes a literal single quote by doubling it.
    assert resume._ps("a'b") == "'a''b'"


def test_ps_neutralizes_powershell_metacharacters():
    out = resume._ps("$(Get-Item C:\\); echo pwn")
    # Nothing is expanded because it's a literal single-quoted string.
    assert out == "'$(Get-Item C:\\); echo pwn'"


def test_ps_coerces_non_string():
    assert resume._ps(42) == "'42'"


# --------------------------------------------------------------------------- #
# last_session: sessions/*.json path
# --------------------------------------------------------------------------- #
def test_last_session_none_when_config_dir_absent(tmp_path):
    assert resume.last_session(config_dir=tmp_path / "does-not-exist") is None


def test_last_session_none_when_no_sessions_or_history(tmp_path):
    (tmp_path / "sessions").mkdir()
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_reads_single_valid_session(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_session(tmp_path / "sessions", "s.json",
                   {"sessionId": "abc123", "cwd": str(cwd)})
    result = resume.last_session(config_dir=tmp_path)
    assert result == {"session_id": "abc123", "cwd": str(cwd)}


def test_last_session_skips_session_with_missing_cwd_on_disk(tmp_path):
    # cwd points at a path that does not exist -> session is rejected.
    _write_session(tmp_path / "sessions", "s.json",
                   {"sessionId": "abc123", "cwd": str(tmp_path / "gone")})
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_skips_session_missing_session_id(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_session(tmp_path / "sessions", "s.json", {"cwd": str(cwd)})
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_skips_session_missing_cwd_field(tmp_path):
    _write_session(tmp_path / "sessions", "s.json", {"sessionId": "abc123"})
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_ignores_invalid_json_file(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    (sdir / "bad.json").write_text("{not valid json", encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_picks_most_recently_modified(tmp_path):
    cwd_old = tmp_path / "old"
    cwd_old.mkdir()
    cwd_new = tmp_path / "new"
    cwd_new.mkdir()
    sdir = tmp_path / "sessions"
    _write_session(sdir, "old.json",
                   {"sessionId": "OLD", "cwd": str(cwd_old)}, mtime=1000)
    _write_session(sdir, "new.json",
                   {"sessionId": "NEW", "cwd": str(cwd_new)}, mtime=5000)
    result = resume.last_session(config_dir=tmp_path)
    assert result == {"session_id": "NEW", "cwd": str(cwd_new)}


def test_last_session_valid_wins_even_if_newer_is_invalid(tmp_path):
    # Newest file is invalid (cwd gone); an older valid one should still win.
    cwd_ok = tmp_path / "ok"
    cwd_ok.mkdir()
    sdir = tmp_path / "sessions"
    _write_session(sdir, "valid.json",
                   {"sessionId": "GOOD", "cwd": str(cwd_ok)}, mtime=1000)
    _write_session(sdir, "invalid.json",
                   {"sessionId": "BAD", "cwd": str(tmp_path / "gone")}, mtime=9000)
    result = resume.last_session(config_dir=tmp_path)
    assert result == {"session_id": "GOOD", "cwd": str(cwd_ok)}


def test_last_session_uses_env_config_dir(monkeypatch, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_session(tmp_path / "sessions", "s.json",
                   {"sessionId": "ENVID", "cwd": str(cwd)})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert resume.last_session() == {"session_id": "ENVID", "cwd": str(cwd)}


# --------------------------------------------------------------------------- #
# last_session: history.jsonl fallback
# --------------------------------------------------------------------------- #
def test_last_session_history_fallback_when_no_sessions(tmp_path):
    cwd = tmp_path / "hproj"
    cwd.mkdir()
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"sessionId": "HIST", "project": str(cwd)}) + "\n",
        encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path) == {
        "session_id": "HIST", "cwd": str(cwd)}


def test_last_session_history_uses_last_nonblank_line(tmp_path):
    cwd1 = tmp_path / "p1"
    cwd1.mkdir()
    cwd2 = tmp_path / "p2"
    cwd2.mkdir()
    lines = [
        json.dumps({"sessionId": "FIRST", "project": str(cwd1)}),
        json.dumps({"sessionId": "LAST", "project": str(cwd2)}),
        "   ",  # trailing blank line must be ignored
    ]
    (tmp_path / "history.jsonl").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path) == {
        "session_id": "LAST", "cwd": str(cwd2)}


def test_last_session_history_rejects_missing_project_on_disk(tmp_path):
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"sessionId": "HIST", "project": str(tmp_path / "gone")}) + "\n",
        encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_history_rejects_missing_fields(tmp_path):
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"sessionId": "HIST"}) + "\n", encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_history_bad_json_returns_none(tmp_path):
    (tmp_path / "history.jsonl").write_text("{ broken\n", encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path) is None


def test_last_session_sessions_take_priority_over_history(tmp_path):
    cwd_s = tmp_path / "sproj"
    cwd_s.mkdir()
    cwd_h = tmp_path / "hproj"
    cwd_h.mkdir()
    _write_session(tmp_path / "sessions", "s.json",
                   {"sessionId": "FROM_SESSIONS", "cwd": str(cwd_s)})
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"sessionId": "FROM_HISTORY", "project": str(cwd_h)}) + "\n",
        encoding="utf-8")
    assert resume.last_session(config_dir=tmp_path)["session_id"] == "FROM_SESSIONS"


# --------------------------------------------------------------------------- #
# open_terminal: early-return validation (no spawn)
# --------------------------------------------------------------------------- #
def test_open_terminal_false_without_session_id(popen_rec, no_startfile, tmp_path):
    assert resume.open_terminal(str(tmp_path), "") is False
    assert popen_rec.calls == []


def test_open_terminal_false_without_cwd(popen_rec, no_startfile):
    assert resume.open_terminal("", "sid") is False
    assert popen_rec.calls == []


def test_open_terminal_false_when_cwd_absent(popen_rec, no_startfile, tmp_path):
    assert resume.open_terminal(str(tmp_path / "missing"), "sid") is False
    assert popen_rec.calls == []


# --------------------------------------------------------------------------- #
# open_terminal: Windows command construction
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_open_terminal_win_uses_wt_when_available(monkeypatch, popen_rec,
                                                  no_startfile, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.shutil, "which",
                        lambda name: "C:\\wt.exe" if name == "wt" else None)
    assert resume.open_terminal(str(cwd), "sid42") is True
    args = popen_rec.last["args"]
    assert args[0] == "C:\\wt.exe"
    assert "-d" in args and str(cwd) in args
    # The claude command is passed to powershell -Command as the final token.
    inner = args[-1]
    assert inner == f"{resume.CLAUDE} --resume 'sid42'"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_open_terminal_win_falls_back_to_powershell(monkeypatch, popen_rec,
                                                     no_startfile, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.shutil, "which", lambda name: None)  # no wt
    assert resume.open_terminal(str(cwd), "sid42") is True
    args = popen_rec.last["args"]
    assert args[0] == "powershell"
    command = args[-1]
    assert resume._ps(str(cwd)) in command
    assert "--resume 'sid42'" in command
    assert "Set-Location -LiteralPath" in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_open_terminal_win_quotes_dangerous_session_id(monkeypatch, popen_rec,
                                                       no_startfile, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.shutil, "which", lambda name: None)
    evil = "a'; Remove-Item C:\\ -Recurse; '"
    assert resume.open_terminal(str(cwd), evil) is True
    command = popen_rec.last["args"][-1]
    # The single quote is doubled (PowerShell escaping) so it can't break out.
    assert resume._ps(evil) in command
    assert "a''; Remove-Item" in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_open_terminal_win_returns_false_on_popen_error(monkeypatch, no_startfile,
                                                        tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.shutil, "which", lambda name: None)

    def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(resume.subprocess, "Popen", boom)
    assert resume.open_terminal(str(cwd), "sid") is False


# --------------------------------------------------------------------------- #
# open_terminal: POSIX command construction (forced via monkeypatched platform)
# --------------------------------------------------------------------------- #
def test_open_terminal_darwin_builds_osascript(monkeypatch, popen_rec,
                                               no_startfile, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.sys, "platform", "darwin")
    assert resume.open_terminal(str(cwd), "sid42") is True
    args = popen_rec.last["args"]
    assert args[0] == "osascript"
    script = args[-1]
    assert resume._sh(str(cwd)) in script
    assert resume._sh("sid42") in script
    assert 'tell application "Terminal"' in script


def test_open_terminal_linux_uses_first_available_terminal(monkeypatch, popen_rec,
                                                           no_startfile, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.sys, "platform", "linux")
    # Only gnome-terminal is "installed".
    monkeypatch.setattr(resume.shutil, "which",
                        lambda name: "/usr/bin/gnome-terminal"
                        if name == "gnome-terminal" else None)
    assert resume.open_terminal(str(cwd), "sid42") is True
    args = popen_rec.last["args"]
    assert args[0] == "gnome-terminal"
    joined = " ".join(args)
    assert resume._sh(str(cwd)) in joined
    assert resume._sh("sid42") in joined
    assert "exec bash" in joined


def test_open_terminal_linux_quotes_dangerous_session_id(monkeypatch, popen_rec,
                                                        no_startfile, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.sys, "platform", "linux")
    monkeypatch.setattr(resume.shutil, "which",
                        lambda name: "/usr/bin/xterm" if name == "xterm" else None)
    evil = "x'; rm -rf ~; '"
    assert resume.open_terminal(str(cwd), evil) is True
    joined = " ".join(popen_rec.last["args"])
    assert resume._sh(evil) in joined


def test_open_terminal_linux_no_terminal_still_returns_true(monkeypatch, popen_rec,
                                                           no_startfile, tmp_path):
    # No terminal emulator is found; loop finds nothing, but function still
    # reaches its success return without spawning anything.
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.sys, "platform", "linux")
    monkeypatch.setattr(resume.shutil, "which", lambda name: None)
    assert resume.open_terminal(str(cwd), "sid") is True
    assert popen_rec.calls == []


# --------------------------------------------------------------------------- #
# run_auto: early-return validation (no spawn)
# --------------------------------------------------------------------------- #
def test_run_auto_none_without_session_id(popen_rec, tmp_path):
    assert resume.run_auto(str(tmp_path), "", "prompt") is None
    assert popen_rec.calls == []


def test_run_auto_none_without_cwd(popen_rec):
    assert resume.run_auto("", "sid", "prompt") is None
    assert popen_rec.calls == []


def test_run_auto_none_when_cwd_absent(popen_rec, tmp_path):
    assert resume.run_auto(str(tmp_path / "missing"), "sid", "prompt") is None
    assert popen_rec.calls == []


# --------------------------------------------------------------------------- #
# run_auto: Windows command construction
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_run_auto_win_builds_quoted_command(popen_rec, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    log = resume.run_auto(str(cwd), "sid42", "do the thing", config_dir=tmp_path)
    assert log is not None
    assert log == str(tmp_path / "claudometer-resume-sid42.log")
    args = popen_rec.last["args"]
    assert args[0] == "powershell"
    command = args[-1]
    # cwd and every flag are single-quoted (PowerShell style).
    assert resume._ps(str(cwd)) in command
    assert "--resume" in command and resume._ps("sid42") in command
    assert resume._ps("do the thing") in command
    # log path is redirected via *>
    assert "*>" in command
    assert resume._ps(str(log)) in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_run_auto_win_includes_max_turns_and_acceptedits_by_default(popen_rec,
                                                                   tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    resume.run_auto(str(cwd), "sid", "p", max_turns=7, config_dir=tmp_path)
    command = popen_rec.last["args"][-1]
    assert resume._ps("--max-turns") in command
    assert resume._ps("7") in command
    assert resume._ps("--permission-mode") in command
    assert resume._ps("acceptEdits") in command
    assert "--dangerously-skip-permissions" not in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_run_auto_win_skip_permissions_flag(popen_rec, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    resume.run_auto(str(cwd), "sid", "p", skip_permissions=True, config_dir=tmp_path)
    command = popen_rec.last["args"][-1]
    assert resume._ps("--dangerously-skip-permissions") in command
    assert resume._ps("acceptEdits") not in command


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific branch")
def test_run_auto_win_quotes_dangerous_prompt(popen_rec, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    evil = "hi'; Remove-Item C:\\ -Recurse -Force; '"
    resume.run_auto(str(cwd), "sid", evil, config_dir=tmp_path)
    command = popen_rec.last["args"][-1]
    # The prompt is single-quoted with the embedded quote doubled -> no breakout.
    assert resume._ps(evil) in command
    assert "hi''; Remove-Item" in command


# --------------------------------------------------------------------------- #
# run_auto: log-file naming / sanitization (cross-platform via forced posix)
# --------------------------------------------------------------------------- #
def _force_posix(monkeypatch, popen_rec):
    """Force run_auto down the non-win32 branch so the log file is actually
    created (via log.open) but Popen is a no-op recorder."""
    monkeypatch.setattr(resume.sys, "platform", "linux")


def test_run_auto_posix_creates_log_and_records_args(monkeypatch, popen_rec,
                                                     tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _force_posix(monkeypatch, popen_rec)
    log = resume.run_auto(str(cwd), "sid42", "prompt text", config_dir=tmp_path)
    assert log == str(tmp_path / "claudometer-resume-sid42.log")
    assert os.path.exists(log)  # log file opened for writing
    # On posix the flags are passed directly as a list to Popen.
    args = popen_rec.last["args"]
    assert args[0] == resume.CLAUDE
    assert args[1:] == ["--resume", "sid42", "-p", "prompt text",
                        "--max-turns", "30", "--permission-mode", "acceptEdits"]
    # cwd is passed to Popen as a kwarg, not concatenated into a shell string.
    assert popen_rec.last["kwargs"].get("cwd") == str(cwd)


def test_run_auto_posix_skip_permissions(monkeypatch, popen_rec, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _force_posix(monkeypatch, popen_rec)
    resume.run_auto(str(cwd), "sid", "p", skip_permissions=True, config_dir=tmp_path)
    args = popen_rec.last["args"]
    assert "--dangerously-skip-permissions" in args
    assert "acceptEdits" not in args


def test_run_auto_posix_max_turns_zero_omits_flag(monkeypatch, popen_rec, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _force_posix(monkeypatch, popen_rec)
    resume.run_auto(str(cwd), "sid", "p", max_turns=0, config_dir=tmp_path)
    args = popen_rec.last["args"]
    assert "--max-turns" not in args


def test_run_auto_log_name_sanitizes_unsafe_session_id(monkeypatch, popen_rec,
                                                      tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _force_posix(monkeypatch, popen_rec)
    # Slashes / spaces / punctuation are stripped; only [alnum-_] survive.
    log = resume.run_auto(str(cwd), "a/b c:d*e-1_2", "p", config_dir=tmp_path)
    assert log == str(tmp_path / "claudometer-resume-abcde-1_2.log")


def test_run_auto_log_name_truncates_to_40_chars(monkeypatch, popen_rec, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _force_posix(monkeypatch, popen_rec)
    sid = "z" * 100
    log = resume.run_auto(str(cwd), sid, "p", config_dir=tmp_path)
    assert log == str(tmp_path / ("claudometer-resume-" + "z" * 40 + ".log"))


def test_run_auto_log_name_defaults_when_all_stripped(monkeypatch, popen_rec,
                                                     tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _force_posix(monkeypatch, popen_rec)
    # All characters are unsafe -> falls back to the literal "session".
    log = resume.run_auto(str(cwd), "!!!@@@###", "p", config_dir=tmp_path)
    assert log == str(tmp_path / "claudometer-resume-session.log")


def test_run_auto_posix_returns_none_on_popen_error(monkeypatch, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setattr(resume.sys, "platform", "linux")

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(resume.subprocess, "Popen", boom)
    assert resume.run_auto(str(cwd), "sid", "p", config_dir=tmp_path) is None
