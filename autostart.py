"""Launch-at-login for Claudometer (no UI deps, best-effort, per-user).

Each OS has a native per-user autostart mechanism — no admin rights needed:
  * Windows: a value under HKCU\\...\\CurrentVersion\\Run.
  * macOS:   a LaunchAgent plist (~/Library/LaunchAgents) with RunAtLoad.
  * Linux:   a .desktop file in ~/.config/autostart.

``is_enabled()`` reports the current state; ``set_enabled(bool)`` creates or
removes the entry and returns the resulting state (False if it couldn't apply).
The command written reproduces however the widget is running now — the frozen
.exe/.app if packaged, else ``pythonw app.py <mode>`` from this checkout.
"""

import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

APP_NAME = "Claudometer"
_LABEL = "com.claudometer.app"
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _mode_args():
    """Preserve the launch mode (bar/tray/both) at login; never persist 'demo'."""
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    return [mode] if mode in ("bar", "tray", "both") else []


def launch_argv():
    """The argv list that re-launches this same widget at login."""
    if getattr(sys, "frozen", False):  # packaged .exe / .app re-reads argv
        return [sys.executable] + _mode_args()
    exe = sys.executable
    if sys.platform == "win32":
        # Prefer pythonw.exe so login doesn't flash a console window.
        cand = Path(exe).with_name("pythonw.exe")
        if cand.exists():
            exe = str(cand)
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    return [exe, app] + _mode_args()


# --------------------------------------------------------------------------- #
# Windows — HKCU Run key
# --------------------------------------------------------------------------- #
def _win_command():
    import subprocess
    return subprocess.list2cmdline(launch_argv())


def _win_is_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def _win_set(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _win_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
# macOS — LaunchAgent plist
# --------------------------------------------------------------------------- #
def _mac_plist_path():
    return Path.home() / "Library" / "LaunchAgents" / (_LABEL + ".plist")


def _mac_plist_text():
    args = "\n".join("    <string>%s</string>" % _xml_escape(a) for a in launch_argv())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        '  <key>Label</key>\n  <string>%s</string>\n'
        '  <key>ProgramArguments</key>\n  <array>\n%s\n  </array>\n'
        '  <key>RunAtLoad</key>\n  <true/>\n'
        '</dict>\n</plist>\n' % (_LABEL, args)
    )


def _mac_is_enabled():
    return _mac_plist_path().exists()


def _mac_set(enabled):
    p = _mac_plist_path()
    if enabled:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_mac_plist_text(), encoding="utf-8")
    elif p.exists():
        p.unlink()


# --------------------------------------------------------------------------- #
# Linux — XDG autostart .desktop
# --------------------------------------------------------------------------- #
def _linux_desktop_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "claudometer.desktop"


def _linux_desktop_text():
    import subprocess
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=%s\n"
        "Exec=%s\n"
        "X-GNOME-Autostart-enabled=true\n" % (APP_NAME, subprocess.list2cmdline(launch_argv()))
    )


def _linux_is_enabled():
    return _linux_desktop_path().exists()


def _linux_set(enabled):
    p = _linux_desktop_path()
    if enabled:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_linux_desktop_text(), encoding="utf-8")
    elif p.exists():
        p.unlink()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def is_enabled() -> bool:
    try:
        if sys.platform == "win32":
            return _win_is_enabled()
        if sys.platform == "darwin":
            return _mac_is_enabled()
        return _linux_is_enabled()
    except Exception:
        return False


def set_enabled(enabled: bool) -> bool:
    """Apply the desired state; return the actual state afterwards."""
    try:
        if sys.platform == "win32":
            _win_set(enabled)
        elif sys.platform == "darwin":
            _mac_set(enabled)
        else:
            _linux_set(enabled)
    except Exception:
        pass
    return is_enabled()
