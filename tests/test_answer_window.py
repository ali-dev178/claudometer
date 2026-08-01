"""The answer window, through every state a session can put it in.

These build a real Toplevel, because the thing being checked is what the
window actually shows and whether its controls work — a stub would only prove
the stub agrees with itself.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sessions_core as sc          # noqa: E402
import widget_bar                   # noqa: E402

MENU = [
    "  ⏺ Reading the release notes.",
    "  ⎿  Read 4 files",
    "",
    "  How should I write up the breaking changes?",
    "  > 1. Call them out at the top",
    "    2. Keep them in with the rest",
    "    3. Leave them out for now",
    "  Enter to select",
]

CHAT = [
    "  ⏺ Reading the release notes.",
    "  ⏺ Right — I'll put them at the top under their own heading.",
    "",
    "  > ",
]


@pytest.fixture(scope="module")
def root():
    """One root for the module.

    Tearing a root down and building another repeatedly leaves Tcl unable to
    find its own library on this platform, which showed up as tests skipping
    at random. Toplevels are per-test; the root is not.
    """
    try:
        import tkinter as tk
        top = tk.Tk()
    except Exception as exc:                       # headless CI has no display
        pytest.skip(f"no Tk display: {exc}")
    top.withdraw()
    yield top
    try:
        top.destroy()
    except Exception:
        pass


def _row(pid=4242, status=sc.WAITING, **kw):
    row = sc.format_sessions([sc.Session(
        session_id="s", pid=pid, cwd="/work/notes", name="notes-1",
        title="Release notes", status=status, waiting_for="input needed",
        status_updated_at=sc.now_ms() - 60_000)])["sessions_rows"][0]
    row.update(kw)
    return row


def _state(lines=MENU, alive=True, readable=True, row=None):
    return {"alive": alive, "readable": readable, "row": row or _row(),
            "lines": lines,
            "prompt": sc.parse_console_prompt(lines) if lines else None}


class Harness:
    """A window plus the scripted session behind it."""

    def __init__(self, root, states, send=(True, None)):
        self.root = root
        self.states = list(states)
        self.polls = 0
        self.sent = []
        self.send_result = send
        self.closed = False
        self.opened_terminal = []
        self.win = widget_bar.AnswerWindow(
            self.root, "light", _row(), self._send, self.opened_terminal.append,
            poll=self._poll, on_close=self._on_close)
        self.win.top.withdraw()
        self.pump()

    def _poll(self, row):
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return state

    def _send(self, row, text, submit=True):
        self.sent.append((row.get("pid"), text, submit))
        return self.send_result

    def _on_close(self):
        self.closed = True

    def pump(self):
        for _ in range(4):
            self.root.update_idletasks()
            self.root.update()

    def buttons(self):
        return [b.cget("text") for b in self.win._opts.winfo_children()]

    def quick(self):
        return [b.cget("text") for b in self.win._quick.winfo_children()]

    def reply(self):
        return self.win._reply.cget("text")

    def note(self):
        return self.win._note.cget("text")

    def status(self):
        return self.win.status.cget("text")

    def destroy(self):
        try:
            self.win.close()
        except Exception:
            pass


@pytest.fixture
def harness(root):
    made = []

    def build(states, **kw):
        h = Harness(root, states, **kw)
        made.append(h)
        return h

    yield build
    for h in made:
        h.destroy()


# --------------------------------------------------------------------------- #
# A question with a menu
# --------------------------------------------------------------------------- #
def test_the_menu_is_offered_as_buttons(harness):
    h = harness([_state()])
    assert h.buttons() == ["1.  Call them out at the top",
                           "2.  Keep them in with the rest",
                           "3.  Leave them out for now"]


def test_the_highlighted_option_is_marked(harness):
    h = harness([_state()])
    first = h.win._opts.winfo_children()[0]
    rest = h.win._opts.winfo_children()[1]
    assert first.cget("bg") != rest.cget("bg"), (
        "the option the session has highlighted is the one Enter would take — "
        "it has to be visibly different")


def test_a_menu_leaves_out_yes_no(harness):
    h = harness([_state()])
    assert h.quick() == [], (
        "Yes and No mean nothing against a numbered menu")


def test_a_question_crowds_out_the_transcript(harness):
    h = harness([_state()])
    assert h.reply() == "", (
        "the question and its choices say it better right below — showing "
        "the terminal's version of the same thing above is noise")


def test_picking_an_option_sends_its_number(harness):
    h = harness([_state()])
    h.win._opts.winfo_children()[1].invoke()
    assert h.sent == [(4242, "2", True)]


# --------------------------------------------------------------------------- #
# The point of the whole thing: answering starts a conversation
# --------------------------------------------------------------------------- #
def test_the_window_stays_open_after_a_successful_send(harness):
    h = harness([_state()])
    h.win._opts.winfo_children()[2].invoke()
    h.pump()
    assert not h.closed, (
        "'chat about this' continues here — closing would send the user to "
        "the terminal for exactly what this window is for")
    assert h.win.top.winfo_exists()


def test_the_box_is_cleared_and_kept_ready_after_sending(harness):
    h = harness([_state()])
    h.win.var.set("say more")
    h.win._send("say more")
    assert h.win.var.get() == ""
    assert h.status() == "sent"


def test_the_menu_gives_way_to_the_conversation(harness):
    h = harness([_state(MENU), _state(CHAT)])
    h.win._tick()
    h.pump()
    assert h.buttons() == [], "the question has been answered"
    assert "put them at the top" in h.reply(), (
        "what it said back is the whole reason the window stayed")
    assert h.win._entry.cget("state") == "normal"


def test_only_what_it_said_since_you_last_typed(harness):
    h = harness([_state(["⏺ An older answer nobody needs again.",
                         "> put them at the top",
                         "⏺ Done — they're in their own section now.",
                         "> "])])
    assert h.reply() == "Done — they're in their own section now."


def test_the_transcript_is_prose_not_a_terminal(harness):
    h = harness([_state(CHAT)])
    assert "\n" not in h.reply() and "⏺" not in h.reply(), (
        "terminal lines are wrapped to the terminal's width and marked with "
        "glyphs a UI font lacks — the sentence is what matters")


def test_a_failed_send_says_why_and_keeps_the_text(harness):
    h = harness([_state()], send=(False, "the session refused the input"))
    h.win.var.set("hello")
    h.win._send("hello")
    assert "refused" in h.status()
    assert h.win.var.get() == "hello", "retyping it would be the user's cost"


# --------------------------------------------------------------------------- #
# States that are not a question
# --------------------------------------------------------------------------- #
def test_a_free_text_prompt_offers_yes_and_no(harness):
    h = harness([_state(["  Do you want to proceed?", "  > "])])
    assert h.buttons() == []
    assert h.quick() == ["Yes", "No"]


def test_a_working_session_says_input_will_be_queued(harness):
    h = harness([_state(CHAT, row=_row(status=sc.BUSY))])
    assert "queued" in h.note().lower()
    assert h.win._entry.cget("state") == "normal", (
        "Claude Code takes typing while it works — so may this")
    assert h.quick() == [], "there is no question to say yes to"


def test_an_unreadable_screen_is_admitted(harness):
    h = harness([_state(None, readable=False)])
    assert "can't read" in h.note().lower()
    assert h.win._entry.cget("state") == "normal", (
        "reading and writing are separate — an answer may still land")


def test_a_session_that_ends_disables_the_controls(harness):
    h = harness([_state(MENU), _state(None, alive=False)])
    h.win._tick()
    h.pump()
    assert "ended" in h.note().lower()
    assert h.win._entry.cget("state") == "disabled"
    assert h.win._send_btn.cget("state") == "disabled"
    assert h.win._enter_btn.cget("state") == "disabled"
    assert all(b.cget("state") == "disabled"
               for b in h.win._opts.winfo_children())
    assert h.win.top.winfo_exists(), (
        "the last thing it said is still worth reading")


def test_an_ended_session_is_not_polled_again(harness):
    h = harness([_state(MENU), _state(None, alive=False)])
    h.win._tick()
    before = h.polls
    h.win._tick()
    assert h.polls == before, "there is nothing left to ask about"


# --------------------------------------------------------------------------- #
# The menu moving under the pointer
# --------------------------------------------------------------------------- #
MOVED = [
    "  Which heading should they go under?",
    "  > 1. Breaking changes",
    "    2. Upgrade notes",
    "  Enter to select",
]


def test_a_choice_that_moved_is_refused(harness):
    h = harness([_state(MENU), _state(MOVED)])
    h.win._opts.winfo_children()[2].invoke()      # "3" — gone by now
    assert h.sent == [], (
        "3 meant 'leave them out' when the button was drawn and means nothing "
        "now — sending it would answer a question the user never read")
    assert "moved" in h.status().lower()
    assert h.buttons() == ["1.  Breaking changes", "2.  Upgrade notes"]


def test_a_choice_that_is_still_there_goes_through(harness):
    h = harness([_state(MENU)])
    h.win._opts.winfo_children()[0].invoke()
    assert h.sent == [(4242, "1", True)]


def test_the_buttons_survive_a_tick_that_changes_nothing(harness):
    h = harness([_state(MENU)])
    ids = [str(b) for b in h.win._opts.winfo_children()]
    h.win._tick()
    h.pump()
    assert [str(b) for b in h.win._opts.winfo_children()] == ids, (
        "rebuilding the buttons under the pointer every second would make "
        "them unclickable")


# --------------------------------------------------------------------------- #
# Size
# --------------------------------------------------------------------------- #
def test_the_window_fits_itself_to_its_contents(harness):
    short = harness([_state(["  Do you want to proceed?", "  > "])])
    tall = harness([_state()])
    assert tall.win.top.winfo_reqheight() > short.win.top.winfo_reqheight(), (
        "six choices need more room than none — a fixed height either wastes "
        "it or hides them")


class _Resize:
    """A <Configure> for a window the test harness keeps withdrawn."""

    def __init__(self, widget, width, height):
        self.widget, self.width, self.height = widget, width, height


def test_a_hand_resize_is_left_alone(harness):
    h = harness([_state(MENU), _state(CHAT)])
    asked = h.win._set_geom
    h.win._sized_by_user(_Resize(h.win.top, *asked))          # the WM obeys
    h.win._sized_by_user(_Resize(h.win.top, asked[0] + 230, asked[1] + 400))
    assert h.win._user_sized is True
    h.win._tick()                        # the reply arrives
    h.pump()
    assert h.win._set_geom == asked, (
        "re-fitting the window every second would be it arguing with the "
        "person who just dragged it bigger")


def test_the_size_we_asked_for_is_not_a_resize(harness):
    h = harness([_state(MENU)])
    h.win._sized_by_user(_Resize(h.win.top, *h.win._set_geom))
    assert h.win._user_sized is False, (
        "every geometry call we make raises Configure — treating those as "
        "the user resizing would switch auto-fit off immediately")


def test_the_window_settling_into_place_is_not_a_resize(harness):
    h = harness([_state(MENU)])
    asked = h.win._set_geom
    h.win._geom_settled = False        # as it is the moment geometry is set
    # Configure fires while a window is being mapped, long before it is the
    # size that was asked for. Counting those would kill auto-fit at birth.
    for size in ((1, 1), (asked[0], 120), asked):
        h.win._sized_by_user(_Resize(h.win.top, *size))
    assert h.win._user_sized is False


def test_nothing_said_yet_shows_no_transcript(harness):
    h = harness([_state(["> just do it", "✻ Cogitated for 14s"])])
    assert h.reply() == "", (
        "an empty box where a sentence goes reads as something failing")


# --------------------------------------------------------------------------- #
# Retiring itself once there is nothing left to answer
# --------------------------------------------------------------------------- #
def _stale(win, seconds=None):
    """Wind the clock back to just past the closing threshold."""
    win._touched = time.monotonic() - (
        seconds if seconds is not None else win.CLOSE_AFTER + 1)


def test_a_finished_session_closes_itself(harness):
    h = harness([_state(CHAT, row=_row(status=sc.IDLE))])
    _stale(h.win)
    h.win._tick()
    assert h.closed is True, (
        "it opened because something needed answering — once nothing does, "
        "leaving it up means closing it by hand every single time")


def test_it_warns_before_it_goes(harness):
    h = harness([_state(CHAT, row=_row(status=sc.IDLE))])
    _stale(h.win, h.win.CLOSE_AFTER - 4)
    h.win._tick()
    assert h.closed is False
    assert "closing in" in h.status(), (
        "vanishing mid-sentence while being read would be worse than "
        "overstaying")


def test_a_waiting_session_never_closes_itself(harness):
    h = harness([_state(MENU)])
    _stale(h.win)
    h.win._tick()
    assert h.closed is False, "it is asking you something right now"


def test_a_working_session_never_closes_itself(harness):
    h = harness([_state(CHAT, row=_row(status=sc.BUSY))])
    _stale(h.win)
    h.win._tick()
    assert h.closed is False, "it is about to say something"


def test_a_half_typed_message_keeps_it_open(harness):
    h = harness([_state(CHAT, row=_row(status=sc.IDLE))])
    h.win.var.set("what about the second one")
    _stale(h.win)
    h.win._tick()
    assert h.closed is False, "closing would throw away what they wrote"


def test_using_it_resets_the_clock(harness):
    h = harness([_state(CHAT, row=_row(status=sc.IDLE))])
    _stale(h.win, h.win.CLOSE_AFTER - 2)
    h.win._touch()
    h.win._tick()
    assert h.closed is False


def test_a_session_that_ended_also_retires(harness):
    h = harness([_state(MENU), _state(None, alive=False)])
    h.win._tick()
    _stale(h.win)
    h.win._tick()
    assert h.closed is True, "nothing can be sent to it and it says nothing more"


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
def test_closing_reports_once_and_stops_polling(harness):
    h = harness([_state()])
    h.win.close()
    before = h.polls
    h.win._tick()
    h.win.close()
    assert h.polls == before and h.closed is True


def test_opening_the_terminal_closes_the_window(harness):
    h = harness([_state()])
    h.win._open_terminal()
    assert h.closed is True
    assert h.opened_terminal and h.opened_terminal[0]["pid"] == 4242
