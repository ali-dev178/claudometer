"""Hermetic pytest suite for focus.py.

The ancestor walk is pure given a {pid: parent} map, so it is tested directly
against hand-built maps — including the cyclic and self-referential shapes a
real process snapshot can contain. The Windows API calls are exercised only
for their fail-soft contract; nothing here reads or focuses a real window.
"""

import sys

import pytest

import focus


# --------------------------------------------------------------------------- #
# ancestors — pure given a parent map
# --------------------------------------------------------------------------- #
# claude.exe -> powershell.exe -> WindowsTerminal.exe -> explorer.exe
CHAIN = {100: 200, 200: 300, 300: 400, 400: 0}


def test_ancestors_walks_up_to_the_terminal():
    assert focus.ancestors(100, CHAIN) == [100, 200, 300, 400]


def test_ancestors_stops_at_an_unknown_parent():
    assert focus.ancestors(100, {100: 200}) == [100, 200]


def test_ancestors_stops_at_pid_zero():
    assert focus.ancestors(400, CHAIN) == [400]


def test_ancestors_respects_the_hop_limit():
    assert focus.ancestors(100, CHAIN, limit=2) == [100, 200]


def test_ancestors_limit_floor_is_one():
    assert focus.ancestors(100, CHAIN, limit=0) == [100]


def test_ancestors_survives_a_cycle():
    # A snapshot can contain a self-referential or looping parent chain.
    assert focus.ancestors(1, {1: 2, 2: 3, 3: 1}) == [1, 2, 3]


def test_ancestors_survives_self_parent():
    assert focus.ancestors(5, {5: 5}) == [5]


@pytest.mark.parametrize("pid", [0, -1, None, "x", "", 3.5j])
def test_ancestors_rejects_garbage_pids(pid):
    assert focus.ancestors(pid, CHAIN) == []


def test_ancestors_accepts_a_numeric_string():
    assert focus.ancestors("100", CHAIN) == [100, 200, 300, 400]


def test_ancestors_empty_map_returns_just_the_pid():
    assert focus.ancestors(100, {}) == [100]


# --------------------------------------------------------------------------- #
# is_session_foreground
# --------------------------------------------------------------------------- #
def test_foreground_true_for_the_owning_terminal(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 300)
    assert focus.is_session_foreground(100, CHAIN) is True


def test_foreground_true_for_the_session_process_itself(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 100)
    assert focus.is_session_foreground(100, CHAIN) is True


def test_foreground_false_for_an_unrelated_window(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 999)
    assert focus.is_session_foreground(100, CHAIN) is False


def test_foreground_false_when_it_cannot_be_determined(monkeypatch):
    # Fail soft: a missed suppression is a stray toast, but a wrong
    # suppression silently swallows the notification you needed.
    monkeypatch.setattr(focus, "foreground_pid", lambda: None)
    assert focus.is_session_foreground(100, CHAIN) is False


def test_foreground_false_for_a_garbage_pid(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 300)
    assert focus.is_session_foreground(0, CHAIN) is False


def test_foreground_beyond_the_hop_limit_is_not_matched(monkeypatch):
    deep = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9, 9: 10, 10: 0}
    monkeypatch.setattr(focus, "foreground_pid", lambda: 10)
    assert focus.is_session_foreground(1, deep) is False


# --------------------------------------------------------------------------- #
# exclusive_foreground_pid — the rule alert suppression actually uses
# --------------------------------------------------------------------------- #
# Two sessions sharing one terminal window (the common real shape: tabs).
#   claude 100 -> shell 200 -+
#                            +-> terminal 300 -> explorer 400
#   claude 101 -> shell 201 -+
TABS = {100: 200, 200: 300, 101: 201, 201: 300, 300: 400, 400: 0}


def test_exclusive_returns_the_only_session_under_the_window(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 300)
    assert focus.exclusive_foreground_pid([100], TABS) == 100


def test_exclusive_returns_none_when_tabs_share_a_window(monkeypatch):
    # Measured on a real machine: three claude sessions, one Warp window. If
    # this suppressed, focusing the terminal would silence every alert.
    monkeypatch.setattr(focus, "foreground_pid", lambda: 300)
    assert focus.exclusive_foreground_pid([100, 101], TABS) is None


def test_exclusive_ignores_sessions_in_other_windows(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 300)
    other = {**TABS, 500: 501, 501: 600, 600: 0}
    assert focus.exclusive_foreground_pid([100, 500], other) == 100


def test_exclusive_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 999)
    assert focus.exclusive_foreground_pid([100, 101], TABS) is None


def test_exclusive_none_when_foreground_unknown(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: None)
    assert focus.exclusive_foreground_pid([100], TABS) is None


def test_exclusive_none_for_an_empty_session_list(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 300)
    assert focus.exclusive_foreground_pid([], TABS) is None


def test_exclusive_matches_the_session_process_itself(monkeypatch):
    monkeypatch.setattr(focus, "foreground_pid", lambda: 100)
    assert focus.exclusive_foreground_pid([100, 101], TABS) == 100


# --------------------------------------------------------------------------- #
# Platform surface — fail-soft contracts
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows fallback")
def test_parent_map_empty_off_windows():
    assert focus.parent_map() == {}


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows fallback")
def test_foreground_pid_none_off_windows():
    assert focus.foreground_pid() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_parent_map_includes_this_process():
    parents = focus.parent_map()
    import os
    assert parents, "snapshot returned nothing"
    assert os.getpid() in parents


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_ancestors_of_this_process_starts_with_it():
    import os
    chain = focus.ancestors(os.getpid())
    assert chain and chain[0] == os.getpid()


def test_parent_map_never_raises(monkeypatch):
    monkeypatch.setattr(focus, "_IS_WIN", True)

    class Boom:
        def __getattr__(self, name):
            raise OSError("no kernel32 here")

    monkeypatch.setattr(focus.ctypes, "windll", Boom(), raising=False)
    assert focus.parent_map() == {}


def test_foreground_pid_never_raises(monkeypatch):
    monkeypatch.setattr(focus, "_IS_WIN", True)

    class Boom:
        def __getattr__(self, name):
            raise OSError("no user32 here")

    monkeypatch.setattr(focus.ctypes, "windll", Boom(), raising=False)
    assert focus.foreground_pid() is None
