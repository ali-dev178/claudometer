"""Capture the answer window into assets/answer.png.

Two shots side by side: the question a blocked session is asking, and the
conversation that follows once it has been answered — which is the part that
is easy to assume doesn't exist.

Like capture_settings.py this is a real Tk window, so it must run on Windows
with a display:

    py assets/capture_answer.py
"""
import ctypes
import os
import sys
import tempfile
from ctypes import wintypes

os.environ.setdefault("CLAUDOMETER_CONFIG",
                      os.path.join(tempfile.gettempdir(), "cw_capture.toml"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk                       # noqa: E402
from PIL import Image, ImageDraw           # noqa: E402
import render                              # noqa: E402
import sessions_core as sc                 # noqa: E402
import widget_bar                          # noqa: E402
from capture_settings import BMIH           # noqa: E402
from make_assets import drop_shadow         # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32


def _capture(hwnd):
    """Copy the window's own device context.

    PrintWindow — which capture_settings.py uses — leaves black bands across
    this one: several widgets never answer the redraw it forces. Reading what
    is genuinely on screen avoids the question. The window is topmost, so
    nothing is in front of it to be copied by mistake.
    """
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    hdc = user32.GetDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)   # SRCCOPY
    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth, bmi.biHeight = w, -h
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)
    return img

#: Verbatim from a real waiting session — including the descriptions Claude
#: Code prints under each choice, which the parser has to step over.
ASKING = [
    "✻ Crunched for 5m 15s",
    "> what else is overrated?",
    "─────────────────────────────────────────────────",
    " [ ] Overrated",
    "What's the most overrated thing everyone seems to agree is great?",
    "> 1. Networking events",
    "     Rooms full of people exchanging cards and never speaking again.",
    "  2. Waking up at 5am",
    "     The productivity cult's favourite badge of honour.",
    "  3. Open-plan offices",
    "  4. Most new AI tools",
    "  5. Type something.",
    "  6. Chat about this",
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
]

TALKING = [
    "> 6",
    "⏺ Fair — \"overrated\" is doing a lot of work in that question.",
    "  Networking events aren't useless, they're just badly matched to what",
    "  most people go in wanting. The ones who get value out of them have",
    "  usually decided beforehand who they want to talk to.",
    "",
    "  Which of these did you actually want to argue about?",
    "> ",
]


def _grab(theme, lines, status, dwell_ms):
    row = sc.format_sessions([sc.Session(
        session_id="s", pid=4242, cwd="/work/mian-electric",
        name="mian-electric-b8", title="Answer random questions",
        status=status, waiting_for="input needed",
        status_updated_at=sc.now_ms() - dwell_ms)])["sessions_rows"][0]
    state = {"alive": True, "readable": True, "row": row, "lines": lines,
             "prompt": sc.parse_console_prompt(lines)}
    root = tk.Tk()
    root.geometry("100x24+0+0")   # a tiny visible master keeps the child mapped
    win = widget_bar.AnswerWindow(root, theme, row,
                                  on_send=lambda r, t, s=True: (True, None),
                                  on_open_terminal=lambda r: None,
                                  poll=lambda r: state)
    win.top.deiconify()
    win.top.geometry("+80+80")    # on screen and clear of the taskbar
    win.top.lift()
    for _ in range(60):
        win.top.update_idletasks()
        win.top.update()
    img = _capture(win.top.winfo_id())
    root.destroy()
    return img


#: The rest of what a session can put in front of you. Each is what the
#: session's screen actually looks like in that state; the window parses it
#: exactly as it would live.
PERMISSION = [
    "● I need to run the test suite before changing anything else.",
    "Claude needs your permission to run: pytest -q",
    "> ",
]
WORKING = [
    "> put the breaking changes at the top",
    "● Reworking the summary now.",
    "✻ Wrangling (12s · ↑ 1.4k tokens · esc to interrupt)",
]
SHELL_RUNNING = [
    "> 1",
    "● Running the suite to check nothing broke.",
    "  ⎿  pytest -q",
    "✻ Running (8s · esc to interrupt)",
]
UNREADABLE = None


TEN = ["● Which heading should these go under?"] + \
      [f"{'>' if n == 1 else ' '} {n}. Option number {n}" for n in range(1, 11)] + \
      ["Enter to select · ↑/↓ to navigate · Esc to cancel"]

SECOND = [
    "● How should I write up the breaking changes?",
    "  1. Call them out at the top",
    "> 2. Keep them in with the rest",
    "  3. Leave them out for now",
    "Enter to select · ↑/↓ to navigate · Esc to cancel",
]

LONG_REPLY = [
    "> why not both?",
    "● Because the two readings pull in opposite directions. Calling them out",
    "  at the top serves the person upgrading in a hurry, who wants to know",
    "  what will break before they read anything else. Keeping them inline",
    "  serves the person reading the whole thing, for whom a separate section",
    "  means reading the same change twice. Doing both means the notes",
    "  contradict themselves about which list is authoritative.",
    "> ",
]


def _grab_state(theme, lines, status, dwell_ms, alive=True, readable=True,
                after=None):
    """One window in one state, captured. *after* nudges it before the shot."""
    row = sc.format_sessions([sc.Session(
        session_id="s", pid=4242, cwd="/work/claude-widget",
        name="claude-widget-b8", title="Ship the release pipeline",
        status=status, waiting_for="input needed",
        status_updated_at=sc.now_ms() - dwell_ms)])["sessions_rows"][0]
    state = {"alive": alive, "readable": readable, "row": row, "lines": lines,
             "prompt": sc.parse_console_prompt(lines) if lines else None}
    root = tk.Tk()
    root.geometry("100x24+0+0")
    win = widget_bar.AnswerWindow(root, theme, row,
                                  on_send=lambda r, t, s=True: (True, None),
                                  on_open_terminal=lambda r: None,
                                  poll=lambda r: state)
    win.top.deiconify()
    win.top.geometry("+80+80")
    win.top.lift()
    for _ in range(60):
        win.top.update_idletasks()
        win.top.update()
    if after:
        after(win)
        for _ in range(30):
            win.top.update_idletasks()
            win.top.update()
    img = _capture(win.top.winfo_id())
    root.destroy()
    return img


def gallery():
    """Every state the answer window can be in, one sheet."""
    from make_gallery import grid_sheet          # noqa: E402

    items = [
        ("A question, with its choices",
         _grab_state("light", ASKING, sc.WAITING, 4 * 60_000)),
        ("A permission prompt — no menu",
         _grab_state("light", PERMISSION, sc.WAITING, 20_000)),
        ("The conversation it started",
         _grab_state("light", TALKING, sc.IDLE, 30_000)),
        ("Working — what you send is queued",
         _grab_state("light", WORKING, sc.BUSY, 12_000)),
        ("Running the command you approved",
         _grab_state("light", SHELL_RUNNING, sc.SHELL, 8_000)),
        ("Its screen can't be read",
         _grab_state("light", UNREADABLE, sc.WAITING, 60_000, readable=False)),
        ("The session ended",
         _grab_state("light", None, sc.IDLE, 60_000, alive=False)),
        ("The highlight on option 2",
         _grab_state("light", SECOND, sc.WAITING, 90_000)),
        ("Ten options — the tall extreme",
         _grab_state("light", TEN, sc.WAITING, 30_000)),
        ("A long reply, rejoined as prose",
         _grab_state("light", LONG_REPLY, sc.IDLE, 15_000)),
        ("Sent — and still here",
         _grab_state("light", ASKING, sc.WAITING, 4 * 60_000,
                     after=lambda w: w._say("sent"))),
        ("A send that failed",
         _grab_state("light", ASKING, sc.WAITING, 4 * 60_000,
                     after=lambda w: w._say("the session refused the input"))),
        ("Nothing left to answer — closing",
         _grab_state("light", LONG_REPLY, sc.IDLE, 30_000,
                     after=lambda w: w._say(
                         "nothing left to answer — closing in 7s"))),
        ("Dark",
         _grab_state("dark", ASKING, sc.WAITING, 4 * 60_000)),
    ]
    grid_sheet(items, "gallery-answer.png",
               "Answering a session, in every state it can be in", cols=4,
               note="Windows only. The question is read off the session's own "
                    "screen, because while it is waiting that is the only "
                    "place the question exists — a tool call reaches the "
                    "transcript once it has been ANSWERED.")


def main():
    asking = _grab("light", ASKING, sc.WAITING, 4 * 60_000)
    talking = _grab("dark", TALKING, sc.IDLE, 30_000)
    m, gap = 44, 46
    W = m + asking.width + gap + talking.width + m
    H = m + max(asking.height, talking.height) + 34 + m
    bg = render._vgrad(W, H, "#eef1f6", "#e2e7ef").convert("RGBA")
    d = ImageDraw.Draw(bg)
    f = render._font("sb", 13)

    def place(im, x, label):
        sh, pad = drop_shadow(im.size, 10, 20, 70)
        bg.alpha_composite(sh, (x - pad, m - pad + 7))
        bg.paste(im, (x, m))
        d.text((x + im.width / 2, m + max(asking.height, talking.height) + 16),
               label, font=f, fill="#5b6675", anchor="mm")

    place(asking, m, "It asks — you answer, without leaving what you're doing")
    place(talking, m + asking.width + gap,
          "…and it stays, because that was the start of a conversation")
    bg.convert("RGB").save(os.path.join(OUT, "answer.png"))
    print("wrote assets/answer.png", (W, H))


if __name__ == "__main__":
    main()
    gallery()
