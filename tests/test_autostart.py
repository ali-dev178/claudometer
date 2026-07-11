"""Launch-at-login logic. The pure/portable parts run everywhere; the disk
round-trip runs on the current platform without touching real user state
(Linux uses a redirected XDG dir; the plist/desktop text is validated as data).
"""

import sys
from xml.dom.minidom import parseString

import pytest

import autostart


def test_launch_argv_is_runnable():
    argv = autostart.launch_argv()
    assert isinstance(argv, list) and argv
    # From source (tests aren't frozen) it points python at app.py.
    assert not getattr(sys, "frozen", False)
    assert argv[0]  # an interpreter/executable path
    assert any(a.endswith("app.py") for a in argv)


def test_mode_args_preserves_known_modes(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.py", "tray"])
    assert autostart._mode_args() == ["tray"]
    monkeypatch.setattr(sys, "argv", ["app.py", "bar"])
    assert autostart._mode_args() == ["bar"]


def test_mode_args_drops_demo_and_unknown(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app.py", "demo"])
    assert autostart._mode_args() == []
    monkeypatch.setattr(sys, "argv", ["app.py", "wat"])
    assert autostart._mode_args() == []
    monkeypatch.setattr(sys, "argv", ["app.py"])
    assert autostart._mode_args() == []


def test_mac_plist_text_is_well_formed_xml():
    doc = parseString(autostart._mac_plist_text())  # raises on malformed XML
    text = doc.toxml()
    assert "com.claudometer.app" in text
    assert "RunAtLoad" in text


def test_is_enabled_returns_bool():
    assert isinstance(autostart.is_enabled(), bool)


@pytest.mark.skipif(sys.platform in ("win32", "darwin"),
                    reason="Linux XDG autostart round-trip")
def test_linux_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart.is_enabled() is False
    assert autostart.set_enabled(True) is True
    desktop = tmp_path / "autostart" / "claudometer.desktop"
    assert desktop.exists()
    assert "X-GNOME-Autostart-enabled=true" in desktop.read_text()
    assert autostart.set_enabled(False) is False
    assert not desktop.exists()
