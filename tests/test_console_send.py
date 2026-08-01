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
