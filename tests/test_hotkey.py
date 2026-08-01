"""Tests for hotkey.py.

``parse`` is pure and runs anywhere; registration is Windows-only and is
skipped elsewhere. Nothing here leaves a hotkey registered — a stuck global
shortcut would affect the whole machine, not just the test run.
"""

import sys

import pytest

import hotkey


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
def test_parse_ctrl_alt_letter():
    mods, key = hotkey.parse("ctrl+alt+j")
    assert mods == hotkey.MOD_CONTROL | hotkey.MOD_ALT
    assert key == ord("J")


def test_parse_is_case_insensitive():
    assert hotkey.parse("CTRL+Alt+J") == hotkey.parse("ctrl+alt+j")


def test_parse_accepts_dashes():
    assert hotkey.parse("ctrl-alt-j") == hotkey.parse("ctrl+alt+j")


def test_parse_tolerates_whitespace():
    assert hotkey.parse("  ctrl + alt + j ") == hotkey.parse("ctrl+alt+j")


@pytest.mark.parametrize("alias,expected", [
    ("ctrl", hotkey.MOD_CONTROL), ("control", hotkey.MOD_CONTROL),
    ("alt", hotkey.MOD_ALT), ("shift", hotkey.MOD_SHIFT),
    ("win", hotkey.MOD_WIN), ("cmd", hotkey.MOD_WIN), ("super", hotkey.MOD_WIN),
])
def test_parse_modifier_aliases(alias, expected):
    assert hotkey.parse(f"{alias}+j")[0] == expected


def test_parse_function_keys():
    assert hotkey.parse("ctrl+shift+f5")[1] == 0x74


def test_parse_digits():
    assert hotkey.parse("alt+1")[1] == ord("1")


@pytest.mark.parametrize("named,code", [
    ("space", 0x20), ("tab", 0x09), ("enter", 0x0D), ("esc", 0x1B),
])
def test_parse_named_keys(named, code):
    assert hotkey.parse(f"ctrl+{named}")[1] == code


def test_parse_rejects_a_bare_key():
    # A shortcut with no modifier would swallow that key from every app.
    assert hotkey.parse("j") is None
    assert hotkey.parse("f5") is None


def test_parse_rejects_modifiers_only():
    assert hotkey.parse("ctrl+alt") is None


@pytest.mark.parametrize("bad", ["", None, "   ", "ctrl+banana", "ctrl++",
                                 123, [], "ctrl+multi"])
def test_parse_rejects_garbage(bad):
    assert hotkey.parse(bad) is None


# --------------------------------------------------------------------------- #
# Hotkey lifecycle
# --------------------------------------------------------------------------- #
def test_unparsable_spec_reports_and_does_not_register():
    hk = hotkey.Hotkey("ctrl+banana", lambda: None)
    assert hk.registered is False
    assert hk.error and "unrecognised" in hk.error


def test_poll_is_zero_when_not_registered():
    hk = hotkey.Hotkey("ctrl+banana", lambda: None)
    assert hk.poll() == 0


def test_unregister_is_safe_when_never_registered():
    hotkey.Hotkey("ctrl+banana", lambda: None).unregister()   # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows fallback")
def test_not_supported_off_windows():
    hk = hotkey.Hotkey("ctrl+alt+j", lambda: None)
    assert hk.registered is False
    assert "Windows" in (hk.error or "")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_register_and_unregister_round_trip():
    # F24 + all modifiers: as close to certainly-free as a global combo gets.
    hk = hotkey.Hotkey("ctrl+alt+shift+f12", lambda: None, hotkey_id=0xC1AE)
    try:
        if not hk.registered:
            pytest.skip(f"combination unavailable here: {hk.error}")
        assert hk.poll() == 0            # nothing pressed yet
        again = hotkey.Hotkey("ctrl+alt+shift+f12", lambda: None,
                              hotkey_id=0xC1AE)
        assert again.registered is False, "the same id twice must not succeed"
        assert "taken" in (again.error or "")
    finally:
        hk.unregister()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_reregistering_after_unregister_succeeds():
    hk = hotkey.Hotkey("ctrl+alt+shift+f11", lambda: None, hotkey_id=0xC1AF)
    if not hk.registered:
        pytest.skip(f"combination unavailable here: {hk.error}")
    hk.unregister()
    again = hotkey.Hotkey("ctrl+alt+shift+f11", lambda: None, hotkey_id=0xC1AF)
    try:
        assert again.registered is True
    finally:
        again.unregister()


def test_a_failing_callback_cannot_escape(monkeypatch):
    monkeypatch.setattr(hotkey, "_IS_WIN", False)
    hk = hotkey.Hotkey("ctrl+alt+j", lambda: 1 / 0)
    hk.registered = True                      # pretend, then poll safely
    monkeypatch.setattr(hotkey.ctypes, "windll", None, raising=False)
    assert hk.poll() == 0                     # swallowed, not raised
