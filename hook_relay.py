"""Claudometer hook relay — spawned by Claude Code, exits immediately.

Claude Code runs this once per hook event with the event JSON on stdin. It is
on the critical path of the user's session, so it does the least possible work:
read stdin, drop the bytes in the spool directory, exit. Parsing happens later,
in Claudometer, where being slow costs nobody anything.

Two things shape the rest of this file:

* It is installed as a COPY under ~/.claudometer, not run from the application
  directory, so uninstalling or updating Claudometer cannot leave Claude Code
  invoking a path that no longer exists.
* Because it outlives the app, it has to notice when the app is gone. A
  heartbeat file is refreshed while Claudometer runs; once it goes stale this
  script removes the hook entries from Claude Code's settings, deletes the
  spool and deletes itself. If Claudometer comes back, it simply reinstalls.

It therefore deliberately imports nothing from Claudometer — by the time the
cleanup path matters, none of it may exist. The small amount of duplicated
knowledge (where settings.json lives, how our entries are recognised) is the
price of being able to clean up after an uninstall at all.

It must also never fail loudly: a hook that errors or writes to stdout can
disturb the session it is reporting on, so every path swallows and returns 0.
"""

import json
import os
import sys
import time

RELAY_NAME = "hook_relay.py"
HEARTBEAT_NAME = "heartbeat"
STALE_SECONDS = 7 * 24 * 60 * 60


def home_dir():
    return (os.environ.get("CLAUDOMETER_HOME")
            or os.path.join(os.path.expanduser("~"), ".claudometer"))


def spool_dir():
    return (os.environ.get("CLAUDOMETER_EVENTS_DIR")
            or os.path.join(os.path.expanduser("~"), ".claudometer-events"))


def settings_path():
    base = (os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude"))
    return os.path.join(base, "settings.json")


def is_abandoned():
    """True when nothing has refreshed the heartbeat for a long time.

    A missing heartbeat counts as abandoned: install() always writes one, so
    its absence means Claudometer is gone rather than merely idle.
    """
    try:
        path = os.path.join(home_dir(), HEARTBEAT_NAME)
        return time.time() - os.stat(path).st_mtime > STALE_SECONDS
    except OSError:
        return True


def _rmtree(path):
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def uninstall():
    """Remove our hook entries, the spool, and this script.

    Only entries that run this relay are touched; every other key and anyone
    else's hooks are written back unchanged. A settings.json that fails to
    parse is left completely alone — better a stale hook than a destroyed
    configuration.
    """
    path = settings_path()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None

    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        hooks = data["hooks"]
        changed = False
        for event in list(hooks):
            entries = hooks.get(event)
            if not isinstance(entries, list):
                continue
            kept = []
            for entry in entries:
                commands = (entry or {}).get("hooks") or [] \
                    if isinstance(entry, dict) else []
                mine = any(isinstance(h, dict) and RELAY_NAME in str(h.get("command", ""))
                           for h in commands)
                if mine:
                    changed = True
                else:
                    kept.append(entry)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            data.pop("hooks", None)
        if changed:
            try:
                tmp = path + ".claudometer-tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                    fh.write("\n")
                os.replace(tmp, path)
            except OSError:
                pass

    _rmtree(spool_dir())
    try:
        os.remove(os.path.join(home_dir(), HEARTBEAT_NAME))
    except OSError:
        pass
    try:
        os.remove(os.path.join(home_dir(), RELAY_NAME))   # delete ourselves last
    except OSError:
        pass


def main():
    try:
        data = sys.stdin.buffer.read()
        if is_abandoned():
            # Nobody is draining the spool. Clean up instead of writing into it
            # forever. One stat call, so the healthy path pays almost nothing.
            uninstall()
            return 0
        if not data:
            return 0
        target = spool_dir()
        os.makedirs(target, exist_ok=True)
        # time_ns + pid is unique enough; several sessions can fire at once.
        name = "%d-%d.json" % (time.time_ns(), os.getpid())
        tmp = os.path.join(target, name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        # Rename into place so the reader can never see a half-written file.
        # Claude Code's own registry writer skips this, and the torn reads it
        # causes are exactly what this avoids.
        os.replace(tmp, os.path.join(target, name))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
