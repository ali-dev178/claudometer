"""Tests for console_send.

The Win32 delivery itself needs a real console and is verified by hand against
a live Claude session; what's tested here is everything guarding it — because
this types into a running AI session, and the failure mode of a mistake is
text arriving somewhere it shouldn't.
"""

import sys

import pytest

import console_send


# --------------------------------------------------------------------------- #
# clean — what actually gets typed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("yes", "yes"),
    ("  yes  ", "yes"),
    ("yes\n", "yes"),
    ("two\nlines", "two lines"),
    ("tab\there", "tab here"),
    ("carriage\rreturn", "carriage return"),
    ("bell\x07here", "bellhere"),
    ("nul\x00here", "nulhere"),
    ("", ""),
    (None, ""),
    ("   ", ""),
])
def test_clean(raw, expected):
    assert console_send.clean(raw) == expected


def test_clean_strips_newlines_rather_than_sending_them():
    # An embedded newline would submit the answer half-typed; the caller adds
    # exactly one Enter at the end.
    assert "\n" not in console_send.clean("first\nsecond")
    assert "\r" not in console_send.clean("first\rsecond")


def test_clean_caps_length():
    assert len(console_send.clean("z" * 10_000)) == console_send.MAX_LEN


def test_clean_keeps_unicode():
    assert console_send.clean("ja — visst 🎉") == "ja — visst 🎉"


# --------------------------------------------------------------------------- #
# send_text guards — nothing reaches a console without passing these
# --------------------------------------------------------------------------- #
#: These assert the ARGUMENT guards, which only run once send_text has got
#: past "is this even Windows". Off Windows it returns before reaching them,
#: which is correct behaviour and a different assertion.
windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="the guards being tested are Windows-only")


@windows_only
@pytest.mark.parametrize("text", ["", "   ", None, "\n\n"])
def test_send_refuses_empty_text_when_not_submitting(text):
    ok, err = console_send.send_text(1234, text, submit=False)
    assert ok is False and err == "nothing to send"


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_text_with_submit_is_a_bare_enter(text):
    # That's how you accept the highlighted option in a numbered menu, so it
    # must get past the empty check and fail later (on the console), not here.
    ok, err = console_send.send_text(1234, text, submit=True)
    assert ok is False
    assert err != "nothing to send", err


@windows_only
@pytest.mark.parametrize("pid", [0, -1, None, "abc", ""])
def test_send_refuses_a_bad_pid(pid):
    ok, err = console_send.send_text(pid, "yes")
    assert ok is False and err == "no session"


def test_send_reports_failure_rather_than_raising(monkeypatch):
    if not console_send.can_send():
        pytest.skip("Windows-only path")

    class Boom:
        def __getattr__(self, name):
            raise OSError("no kernel32")

    monkeypatch.setattr(console_send.ctypes, "windll", Boom(), raising=False)
    ok, err = console_send.send_text(4242, "yes")
    assert ok is False and err


def test_send_to_a_pid_with_no_console_fails_cleanly():
    if not console_send.can_send():
        pytest.skip("Windows-only path")
    # A pid that exists but owns no console (this test process under pytest may
    # or may not); either way it must return, not raise.
    ok, err = console_send.send_text(999_999, "yes")
    assert ok is False and isinstance(err, str)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows fallback")
def test_not_supported_off_windows():
    assert console_send.can_send() is False
    ok, err = console_send.send_text(1234, "yes")
    assert ok is False and "Windows" in err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_can_send_on_windows():
    assert console_send.can_send() is True


def test_sends_are_serialised():
    # Only one console can be attached at a time, so the lock has to exist.
    assert console_send._LOCK is not None


# --------------------------------------------------------------------------- #
# Which rows get read when the window is taller than the cap
# --------------------------------------------------------------------------- #
def test_a_tall_window_is_trimmed_from_the_top():
    top, bottom = console_send.visible_rows(0, 300, 300, max_rows=120)
    assert (top, bottom) == (180, 300), (
        "the question, its menu and the newest reply are all at the BOTTOM — "
        "capping from the top reads 120 rows of scrollback and misses them")


def test_a_short_window_is_left_alone():
    assert console_send.visible_rows(0, 59, 59, max_rows=120) == (0, 59)


def test_the_row_range_never_leaves_the_buffer():
    assert console_send.visible_rows(-5, 999, 40, max_rows=120) == (0, 40)
    top, bottom = console_send.visible_rows(30, 10, 40, max_rows=120)
    assert top <= bottom, "an inverted range would read the whole buffer"


# --------------------------------------------------------------------------- #
# Characters a console record cannot hold
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="ctypes wchar is Windows")
def test_an_emoji_does_not_abort_the_send():
    # A console record holds ONE wchar_t. Handing it a non-BMP character
    # raises, after the widget has already attached to the session's console.
    console_send._records(console_send.clean("nice 🎉"))


def test_a_non_bmp_character_becomes_a_surrogate_pair():
    assert list(console_send._units("a🎉")) == ["a", "\ud83c", "\udf89"]


def test_ordinary_text_is_one_unit_per_character():
    assert list(console_send._units("café")) == ["c", "a", "f", "é"]


# --------------------------------------------------------------------------- #
# Borrowing a session's console must not cost us our own
# --------------------------------------------------------------------------- #
class _Kernel:
    """Records what a detach did, so the logic can be checked anywhere."""

    def __init__(self):
        self.calls = []

    def FreeConsole(self):
        self.calls.append("free")
        return 1

    def AttachConsole(self, pid):
        self.calls.append(("attach", pid))
        return 1


def test_a_borrowed_console_is_given_back_when_we_had_one():
    k = _Kernel()
    console_send._detach(k, had_console=True)
    assert k.calls == ["free", ("attach", console_send._ATTACH_PARENT)], (
        "run from a terminal, a bare FreeConsole hands that terminal back and "
        "everything printed afterwards goes nowhere")


def test_nothing_is_reattached_when_we_never_had_a_console():
    k = _Kernel()
    console_send._detach(k, had_console=False)
    assert k.calls == ["free"], (
        "the packaged widget owns no console — attaching to its parent would "
        "be claiming one it was never given")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console")
def test_the_widgets_own_console_survives_a_send():
    import ctypes
    kernel32 = ctypes.windll.kernel32
    before = bool(kernel32.GetConsoleWindow())
    if not before:
        pytest.skip("this run has no console to lose")
    console_send.send_text(999_999, "x")       # a pid with no console
    assert bool(kernel32.GetConsoleWindow()) is True, (
        "run from a terminal — which is what the README tells contributors to "
        "do — a bare FreeConsole hands that terminal back and everything "
        "printed afterwards goes nowhere")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console")
def test_the_widgets_own_console_survives_a_read():
    import ctypes
    kernel32 = ctypes.windll.kernel32
    if not kernel32.GetConsoleWindow():
        pytest.skip("this run has no console to lose")
    console_send.read_screen(999_999)
    assert bool(kernel32.GetConsoleWindow()) is True
