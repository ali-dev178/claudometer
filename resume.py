"""Find and resume the most-recent Claude Code session when the limit resets.

Tier 1 (open_terminal): opens a VISIBLE terminal in the session's directory
running ``claude --resume <id>`` so the user continues the work, supervised.

Tier 2 (run_auto): headless, UNATTENDED resume — opt-in and risky; it can make
changes with no human watching, so it's gated behind config toggles.

Session identity comes from ``~/.claude/sessions/*.json`` (each has sessionId +
cwd; most-recently-updated wins), with ``~/.claude/history.jsonl`` as a fallback.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CLAUDE = "claude"  # resolved by the shell (npm global: claude.ps1 / claude.cmd)


def _config_dir(config_dir=None):
    return Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def last_session(config_dir=None):
    """Return {"session_id", "cwd"} for the most recently active Claude Code
    session, or None if none can be found."""
    base = _config_dir(config_dir)
    best = None

    sdir = base / "sessions"
    if sdir.exists():
        for f in sdir.glob("*.json"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if best and mt <= best[0]:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid, cwd = data.get("sessionId"), data.get("cwd")
            if sid and cwd and Path(cwd).exists():
                best = (mt, sid, cwd)
    if best:
        return {"session_id": best[1], "cwd": best[2]}

    hist = base / "history.jsonl"
    if hist.exists():
        try:
            last = None
            with hist.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last = line
            if last:
                o = json.loads(last)
                if o.get("sessionId") and o.get("project"):
                    return {"session_id": o["sessionId"], "cwd": o["project"]}
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------- #
def _sh(s):  # POSIX single-quote
    return "'" + str(s).replace("'", "'\\''") + "'"


def _ps(s):  # PowerShell single-quote
    return "'" + str(s).replace("'", "''") + "'"


def open_terminal(cwd, session_id):
    """Tier 1: open a visible terminal in *cwd* running `claude --resume <id>`."""
    inner = f"{CLAUDE} --resume {session_id}"
    try:
        if sys.platform == "win32":
            if shutil.which("wt"):
                subprocess.Popen(["wt", "-d", cwd, "powershell", "-NoExit", "-Command", inner])
            else:
                subprocess.Popen(
                    ["powershell", "-NoExit", "-Command",
                     f"Set-Location -LiteralPath {_ps(cwd)}; {inner}"],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
        elif sys.platform == "darwin":
            script = (f'tell application "Terminal" to do script "cd {_sh(cwd)} && {inner}"\n'
                      'tell application "Terminal" to activate')
            subprocess.Popen(["osascript", "-e", script])
        else:
            for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"],
                         ["konsole", "-e"], ["xterm", "-e"]):
                if shutil.which(term[0]):
                    subprocess.Popen(term + ["bash", "-lc", f"cd {_sh(cwd)}; {inner}; exec bash"])
                    break
        return True
    except Exception:
        return False


def run_auto(cwd, session_id, prompt, skip_permissions=False, max_turns=30, config_dir=None):
    """Tier 2: headless, unattended resume. Output is logged. Returns the log
    path on success, else None.

    Safety rails: caps agentic turns with --max-turns, and defaults to
    --permission-mode acceptEdits (edits allowed, other commands still gated)
    unless the user explicitly opts into --dangerously-skip-permissions.
    """
    log = _config_dir(config_dir) / f"claudometer-resume-{session_id[:8]}.log"
    args = [CLAUDE, "--resume", session_id, "-p", prompt]
    if max_turns:
        args += ["--max-turns", str(int(max_turns))]
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    else:
        args += ["--permission-mode", "acceptEdits"]
    try:
        if sys.platform == "win32":
            joined = " ".join(_ps(a) if (" " in a or a.startswith("-") is False) else a for a in args)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 f"Set-Location -LiteralPath {_ps(cwd)}; {joined} *> {_ps(str(log))}"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            with log.open("w", encoding="utf-8") as fh:
                subprocess.Popen(args, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT)
        return str(log)
    except Exception:
        return None
