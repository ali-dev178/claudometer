"""Claudometer hook relay — spawned by Claude Code, exits immediately.

Claude Code runs this once per hook event with the event JSON on stdin. It is
on the critical path of the user's session, so it does the least possible work:
read stdin, drop the bytes in the spool directory, exit. Parsing happens later,
in Claudometer, where being slow costs nobody anything.

It must also never fail loudly. A hook that errors or writes to stdout can
disturb the session it is reporting on, so every path here swallows its
exception and returns 0.
"""

import os
import sys
import time


def spool_dir():
    return (os.environ.get("CLAUDOMETER_EVENTS_DIR")
            or os.path.join(os.path.expanduser("~"), ".claudometer-events"))


def main():
    try:
        data = sys.stdin.buffer.read()
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
