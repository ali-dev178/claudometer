"""Entry point.

Usage (no arg default: macOS = menu bar, Windows = taskbar strip, Linux = tray):
    py app.py            # default per platform (above)
    py app.py bar        # Windows: floating readable numbers over the taskbar
    py app.py tray       # Windows/Linux: tray icon only
    py app.py both       # Windows: tray icon + floating taskbar readout
"""

import os
import subprocess
import sys


def _run_tray():
    from tray_windows import TrayApp
    TrayApp().run()


def _run_bar():
    from widget_bar import BarWidget
    BarWidget().run()


def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()

    if sys.platform == "darwin":
        from menubar_mac import MenuApp
        MenuApp().run()
        return

    if not mode:  # packaged/no-arg default: the flagship on Windows, tray on Linux
        mode = "bar" if sys.platform == "win32" else "tray"
    if mode in ("bar", "both") and sys.platform != "win32":
        mode = "tray"  # bar/both are Windows-only (widget_bar needs ctypes.windll)

    if mode == "bar":
        _run_bar()
    elif mode == "both":
        # Run the tray in a separate process (its own message loop) and the
        # floating bar here on the main thread.
        if getattr(sys, "frozen", False):  # packaged .exe re-reads argv
            subprocess.Popen([sys.executable, "tray"])
        else:
            here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
            subprocess.Popen([sys.executable, here, "tray"])
        _run_bar()
    else:  # "", "tray", win32 / linux default
        _run_tray()


if __name__ == "__main__":
    main()
