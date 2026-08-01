"""The Windows tray adapter.

It had no tests at all, which is how a tooltip reading "1 session(s) waiting
on you" survived: nothing renders it in the test suite and nobody re-reads a
string that has been there a while.

The icon and the tooltip are the whole surface — the tray shows one disc and
one line of hover text — so that is what these cover.
"""

import sys

import pytest
from PIL import Image

import sessions_core as sc
import usage_core as core

tray = pytest.importorskip("tray_windows",
                           reason="needs pystray, which is Windows/Linux only")


# --------------------------------------------------------------------------- #
# The disc
# --------------------------------------------------------------------------- #
def test_every_face_renders_a_disc():
    for text, color in (("...", "grey"), ("0%", "green"), ("61%", "amber"),
                        ("94%", "red"), ("100%", "red"), ("-", "grey"),
                        ("!", "red"), ("12", "red")):
        icon = tray.render_icon(text, color, size=64)
        assert isinstance(icon, Image.Image) and icon.size == (64, 64)


def test_the_longest_face_still_fits_inside_the_disc():
    # "100%" is four glyphs where the others are two or three; the font is
    # shrunk to fit rather than overflowing the circle.
    wide = tray.render_icon("100%", "red", size=64)
    narrow = tray.render_icon("9%", "green", size=64)
    assert wide.size == narrow.size


def test_an_unknown_colour_falls_back_rather_than_raising():
    assert tray.render_icon("61%", "chartreuse", size=32).size == (32, 32)


def test_every_status_has_a_face_the_icon_can_draw():
    for status in core.Status:
        disp = core.status_display(status)
        icon = tray.render_icon(disp["face_pct"], disp["face_color"], size=48)
        assert isinstance(icon, Image.Image)


# --------------------------------------------------------------------------- #
# The tooltip and which disc wins
# --------------------------------------------------------------------------- #
class _Icon:
    def __init__(self):
        self.icon = self.title = self.menu = None
        self.updates = 0

    def update_menu(self):
        self.updates += 1


class App:
    """Enough of TrayApp for _apply to run."""

    _apply = tray.TrayApp._apply
    _sig = staticmethod(tray.TrayApp._sig)    # it takes only the dict

    def __init__(self, sess_disp=None):
        self.icon = _Icon()
        self._last_sig = None
        self._sess_disp = sess_disp or {}

    def _build_menu(self, merged):
        return ()


def _usage(pct=61, color="amber"):
    return {"face_pct": f"{pct}%", "face_color": color,
            "tooltip": f"Claude · session {pct}%"}


def _blocked(n):
    return sc.format_sessions([
        sc.Session(session_id=f"s{i}", pid=100 + i, cwd="/w/p", name=f"p-{i}",
                   status=sc.WAITING, waiting_for="input needed",
                   status_updated_at=sc.now_ms())
        for i in range(n)])


def test_one_blocked_session_is_singular():
    app = App(_blocked(1))
    app._apply(_usage())
    assert app.icon.title == "Claude · 1 session waiting on you", (
        "this line is read far more often at one than at many, and "
        "\"1 session(s)\" is the lazy plural")


def test_several_blocked_sessions_are_plural():
    app = App(_blocked(3))
    app._apply(_usage())
    assert app.icon.title == "Claude · 3 sessions waiting on you"


def test_a_blocked_session_outranks_the_usage_number():
    app = App(_blocked(2))
    app._apply(_usage(94, "red"))
    assert "waiting on you" in app.icon.title, (
        "usage is something to pace against; a blocked session is something "
        "to go and do")


def test_with_nothing_blocked_the_usage_tooltip_is_used():
    app = App(sc.format_sessions([]))
    app._apply(_usage())
    assert app.icon.title == "Claude · session 61%"


def test_an_unchanged_signature_does_not_repaint():
    app = App(_blocked(1))
    app._apply(_usage())
    first = app.icon.updates
    app._apply(_usage())
    assert app.icon.updates == first, (
        "rebuilding the menu every poll churns the tray for no reason")


def test_a_changed_block_count_does_repaint():
    app = App(_blocked(1))
    app._apply(_usage())
    app._sess_disp = _blocked(2)
    app._apply(_usage())
    assert app.icon.title == "Claude · 2 sessions waiting on you"
