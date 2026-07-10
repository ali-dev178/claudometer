"""Entry point.

Usage (no arg default: macOS = menu bar, Windows = taskbar strip, Linux = tray):
    py app.py            # default per platform (above)
    py app.py bar        # floating readable numbers (Windows taskbar / macOS + Linux card)
    py app.py tray       # Windows/Linux: tray icon only
    py app.py both       # Windows: tray icon + floating taskbar readout
    py app.py demo       # scripted, offline tour through every feature

The macOS default is the native menu bar; `bar`/`demo` open the floating widget.
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
        # Default is the native menu bar (tested); `bar`/`demo` opt into the
        # experimental cross-platform floating widget.
        if mode in ("bar", "both", "demo"):
            from widget_bar import BarWidget
            BarWidget(demo=(mode == "demo")).run()
        else:
            from menubar_mac import MenuApp
            MenuApp().run()
        return

    if not mode:  # packaged/no-arg default: the flagship on Windows, tray on Linux
        mode = "bar" if sys.platform == "win32" else "tray"
    if mode == "both" and sys.platform != "win32":
        mode = "bar"   # no tray-companion off-Windows; the floating widget covers it

    if mode == "demo":  # scripted, offline "try every feature" tour
        from widget_bar import BarWidget
        BarWidget(demo=True).run()
    elif mode == "bar":
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
