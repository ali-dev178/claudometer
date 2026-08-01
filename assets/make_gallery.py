"""Generate the README's scenario galleries — every state, including the small ones.

make_assets.py draws the marketing shots: the hero, one strip, one popover. This
draws the reference: a labelled grid per surface, so every state the widget can
be in has a picture next to it. Run:  py assets/make_gallery.py

Everything here goes through the REAL render functions and the REAL formatters,
so a gallery can't drift from the app — if a state stops rendering, this stops
generating. The Tk surfaces (the answer window, Settings) need a display and
live in capture_answer.py / capture_settings.py.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render                                   # noqa: E402
import sessions_core as sc                      # noqa: E402
from make_assets import drop_shadow             # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
METRICS = ("session", "weekly", "sessions")
PAGE_TOP, PAGE_BOT = "#eef1f6", "#e2e7ef"
INK, SUBINK = "#3d4653", "#79828f"


def now():
    return datetime.now(timezone.utc)


def sessions(*spec):
    """Build real Sessions from (title, project, status, age_s, reason)."""
    stamp = sc.now_ms()
    return [
        sc.Session(session_id=f"s{i}", pid=4000 + i, cwd=f"/work/{project}",
                   name=f"{project}-{i}", title=title, status=status,
                   waiting_for=reason, status_updated_at=stamp - age * 1000)
        for i, (title, project, status, age, reason) in enumerate(spec)
    ]


W, B, S, I = sc.WAITING, sc.BUSY, sc.SHELL, sc.IDLE


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def _wrap(d, text, font, limit):
    """Greedy wrap, so a long note can't run off the edge of the sheet."""
    out, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if line and d.textlength(trial, font=font) > limit:
            out.append(line)
            line = word
        else:
            line = trial
    if line:
        out.append(line)
    return out


def rows_sheet(items, out, title, note="", label_w=190, gap=16, pad=30,
               bg=None, shadow=False):
    """A vertical list of (label, image) with the label to the left."""
    f_lbl = render._font("sb", 13)
    f_note = render._font("reg", 12)
    f_title = render._font("sb", 17)
    imgs = [im for _l, im in items]
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    width = pad * 2 + label_w + max(im.width for im in imgs)
    lines = _wrap(probe, note, f_note, width - pad * 2) if note else []
    head = 34 + 17 * len(lines)
    height = pad * 2 + head + sum(im.height for im in imgs) + gap * (len(imgs) - 1)
    if bg:
        sheet = Image.new("RGBA", (width, height), bg)
    else:
        sheet = render._vgrad(width, height, PAGE_TOP, PAGE_BOT).convert("RGBA")
    d = ImageDraw.Draw(sheet)
    d.text((pad, pad), title, font=f_title, fill=INK)
    for n, line in enumerate(lines):
        d.text((pad, pad + 24 + n * 17), line, font=f_note, fill=SUBINK)
    y = pad + head
    for label, im in items:
        d.text((pad, y + im.height / 2), label, font=f_lbl, fill=INK, anchor="lm")
        if shadow:
            sh, sp = drop_shadow(im.size, 8, 14, 46)
            sheet.alpha_composite(sh, (pad + label_w - sp, y - sp + 6))
        sheet.alpha_composite(im.convert("RGBA"), (pad + label_w, y))
        y += im.height + gap
    sheet.convert("RGB").save(os.path.join(OUT, out))
    print("wrote", out, sheet.size)


def grid_sheet(items, out, title, cols=3, note="", gap=26, pad=30, cap=22):
    """A grid of (caption, image) cards, captions underneath."""
    f_cap = render._font("sb", 12)
    f_note = render._font("reg", 12)
    f_title = render._font("sb", 17)
    probe0 = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    # Columns are as wide as the WIDER of the card and its caption: a 64px
    # tray disc under a four-word label would otherwise have its caption
    # running into its neighbour's.
    cw = max(max(im.width for _c, im in items),
             max(probe0.textlength(c, font=f_cap) for c, _im in items) + 10)
    cw = int(cw)
    rows = [items[i:i + cols] for i in range(0, len(items), cols)]
    row_h = [max(im.height for _c, im in r) + cap for r in rows]
    width = pad * 2 + cw * cols + gap * (cols - 1)
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    lines = _wrap(probe, note, f_note, width - pad * 2) if note else []
    head = 34 + 17 * len(lines)
    height = pad * 2 + head + sum(row_h) + gap * (len(rows) - 1)
    sheet = render._vgrad(width, height, PAGE_TOP, PAGE_BOT).convert("RGBA")
    d = ImageDraw.Draw(sheet)
    d.text((pad, pad), title, font=f_title, fill=INK)
    for n, line in enumerate(lines):
        d.text((pad, pad + 24 + n * 17), line, font=f_note, fill=SUBINK)
    y = pad + head
    for row, rh in zip(rows, row_h):
        x = pad
        # Captions share a baseline across the row: cards differ in height by
        # a lot here, and captions floating at each card's own bottom edge
        # reads as scattered rather than as a sheet.
        base = y + rh - cap + 13
        for caption, im in row:
            # Centred in the column, not against the card, so a narrow card
            # under a wide caption still reads as one unit.
            left = x + (cw - im.width) // 2
            sh, sp = drop_shadow(im.size, 10, 16, 50)
            sheet.alpha_composite(sh, (left - sp, y - sp + 8))
            sheet.alpha_composite(im.convert("RGBA"), (left, y))
            d.text((x + cw / 2, base), caption,
                   font=f_cap, fill=SUBINK, anchor="mm")
            x += cw + gap
        y += rh + gap
    sheet.convert("RGB").save(os.path.join(OUT, out))
    print("wrote", out, sheet.size)


def strip(disp, bg="#20242c", theme="dark", metrics=METRICS):
    return render.render_strip(disp, bg, theme, scale=3, metrics=metrics)


# --------------------------------------------------------------------------- #
# The strip, in every state it has
# --------------------------------------------------------------------------- #
def strip_gallery():
    t = now()
    base = {"session_resets_at": t + timedelta(hours=1, minutes=21)}

    def usage(pct, color, weekly=18, wcolor="green", **extra):
        d = dict(base, session_pct=pct, session_color=color,
                 weekly_pct=weekly, weekly_color=wcolor)
        d.update(extra)
        return d

    live = sc.format_sessions(sessions(
        ("Ship the release pipeline", "claude-widget", W, 240, "input needed"),
        ("Refactor the payment retries", "checkout-api", B, 720, ""),
        ("Draft the migration plan", "docs-site", S, 45, ""),
        ("Explore the caching idea", "proxy-passer", I, 1560, "")))
    calm = sc.format_sessions(sessions(
        ("Explore the caching idea", "proxy-passer", I, 1560, "")))
    crowd = sc.format_sessions(
        sessions(*[(f"Session {n}", "repo", B, 60 * n, "") for n in range(11)]),
        max_rows=6)

    items = [
        ("Comfortable", strip(usage(22, "green", 6))),
        ("Getting close", strip(usage(61, "amber"))),
        ("Near the limit", strip(usage(94, "red", 42, "amber"))),
        ("Limit reached", strip(usage(100, "red", 42, "amber"))),
        ("Weekly is the tight one", strip(usage(31, "green", 88, "red"))),
        ("Session meter only", strip(usage(61, "amber"), metrics=("session",))),
        ("Weekly meter only", strip(usage(61, "amber"), metrics=("weekly",))),
        ("With live sessions", strip(dict(usage(61, "amber"), **live))),
        ("One session, nothing wrong", strip(dict(usage(22, "green"), **calm))),
        ("More than fit — +N", strip(dict(usage(61, "amber"), **crowd))),
        ("No sessions running",
         strip(dict(usage(61, "amber"), **sc.format_sessions([])))),
        ("A session needs you (pulse)",
         strip(dict(usage(61, "amber"), _pulse=True, **live))),
        ("Offline / no data", strip({"session": "offline — last known"})),
        ("Rate limited", strip({"session": "usage limit reached",
                                "face_color": "red"})),
        ("The tour, unmistakably",
         strip(dict(usage(61, "amber"), _demo=True, **live))),
    ]
    rows_sheet(items, "gallery-strip.png", "Every state the strip can be in",
               "Real renders, dark palette on a dark taskbar. "
               "Blocked sessions sort first, so a red dot always leads.",
               label_w=230)


# --------------------------------------------------------------------------- #
# …and on whatever it is sitting on
# --------------------------------------------------------------------------- #
def contrast_gallery():
    t = now()
    disp = {"session_pct": 92, "session_color": "amber",
            "session_resets_at": t + timedelta(minutes=59),
            "weekly_pct": 39, "weekly_color": "green"}
    disp.update(sc.format_sessions(sessions(
        ("Ship it", "widget", W, 240, "input needed"),
        ("Refactor", "api", B, 720, ""),
        ("Draft", "docs", I, 45, ""))))
    backgrounds = [
        ("Windows dark taskbar", "#202020"), ("Windows light taskbar", "#f3f3f3"),
        ("Mid grey", "#808080"), ("Teal wallpaper", "#008080"),
        ("Olive", "#808000"), ("Hot pink", "#e0409a"),
        ("Deep blue", "#0050c0"), ("Claude orange", "#d97757"),
    ]
    items = []
    for label, bg in backgrounds:
        theme = "dark" if render.contrast_ratio(
            render._rgb(bg), render._rgb(render.THEMES["dark"]["dim"])) >= \
            render.contrast_ratio(
                render._rgb(bg), render._rgb(render.THEMES["light"]["dim"])) \
            else "light"
        items.append((label, strip(disp, bg, theme)))
    rows_sheet(items, "gallery-contrast.png",
               "Legible on whatever it lands on",
               "The palette is chosen by contrast, then each label is lifted to "
               "the WCAG AA ratio. Severity colours never change hue — red, "
               "amber and green are the message: the numbers only go lighter or "
               "darker, and the dots, too small to darken without becoming three "
               "identical blobs, keep their colour and take a ring instead.",
               label_w=230)


# --------------------------------------------------------------------------- #
# Toasts
# --------------------------------------------------------------------------- #
def toast_gallery():
    def toast(pct, title, sub, color, theme="light"):
        return render.render_toast(pct, title, sub, color, theme)

    items = [
        ("A session needs you — waits until dealt with",
         toast(None, "Needs you", "Ship the release pipeline · input needed", "red")),
        ("Permission prompt, from the hook",
         toast(None, "Needs you",
               "checkout-api · permission to run Bash", "red")),
        ("One finished",
         toast(None, "Finished", "Explore the caching idea · proxy-passer", "green")),
        ("Several at once, summarised",
         toast(None, "3 session updates", "2 finished · 1 needs you", "amber")),
        ("Blocked too long",
         toast(None, "Still waiting", "Ship the release pipeline · 12m", "red")),
        ("Crossing a usage threshold",
         toast(80, "Session usage at 80%", "1h 8m left", "amber")),
        ("The weekly window, dark theme",
         toast(92, "Weekly usage at 92%", "resets Thu 10:59 AM", "red", "dark")),
        ("Out of session for now",
         toast(100, "Session limit reached", "resets in 6m", "red", "dark")),
    ]
    grid_sheet(items, "gallery-toasts.png", "Every alert, and when you get it",
               cols=2,
               note="A “needs you” toast has no timeout: it waits until "
                    "you deal with it or the session unblocks. The rest fade.")


# --------------------------------------------------------------------------- #
# The popover
# --------------------------------------------------------------------------- #
def popover_gallery():
    t = now()

    def pop(disp, theme="light"):
        img, _hit = render.render_popover(disp, theme)
        return img

    usage = {"plan": "Plan: Max (5x)",
             "session_pct": 61, "session_color": "amber",
             "session_resets_at": t + timedelta(minutes=82),
             "weekly_pct": 18, "weekly_color": "green",
             "weekly_resets_at": t + timedelta(days=3, hours=2),
             "model_rows": [{"label": "Fable", "pct": 4, "color": "green"}],
             "foot": {"text": "Updated just now · auto", "dot": "green"}}

    full = sc.format_sessions(sessions(
        ("Ship the release pipeline", "claude-widget", W, 240, "input needed"),
        ("Refactor the payment retries", "checkout-api", B, 720, ""),
        ("Draft the migration plan", "docs-site", S, 45, ""),
        ("Explore the caching idea", "proxy-passer", I, 1560, "")))
    crowd = sc.format_sessions(
        sessions(*[(f"Session {n}", "repo", B, 60 * n, "") for n in range(11)]),
        max_rows=6)
    one = sc.format_sessions(sessions(
        ("Ship the release pipeline", "claude-widget", W, 240, "input needed")))

    items = [
        ("Usage only", pop(usage)),
        ("With live sessions", pop(dict(usage, **full))),
        ("More than fit", pop(dict(usage, **crowd))),
        ("One session, blocked", pop(dict(usage, **one))),
        ("Estimated cost (opt-in)",
         pop(dict(usage, cost_tokens=2_450_000, cost_usd=8.74))),
        ("Nothing running", pop(dict(usage, **sc.format_sessions([])))),
        ("Dark", pop(dict(usage, **full), "dark")),
        ("Offline", pop({"plan": None, "session": "offline — last known",
                         "session_pct": None, "weekly_pct": None,
                         "model_rows": []})),
        ("Rate limited", pop({"plan": None, "session": "usage limit reached",
                              "session_pct": None, "weekly_pct": None,
                              "face_color": "red", "model_rows": []}, "dark")),
    ]
    grid_sheet(items, "gallery-popover.png", "The details popover, end to end",
               cols=3,
               note="Click the strip to open it. Blocked sessions sort to the "
                    "top; each row shows how long it has held that state.")


# --------------------------------------------------------------------------- #
# The Windows tray icon — a surface the README never showed
# --------------------------------------------------------------------------- #
def tray_gallery():
    try:
        import tray_windows as tray
    except Exception as exc:                    # pystray missing on this host
        print("skipped gallery-tray.png:", exc)
        return
    import usage_core as core

    def disc(text, color):
        return tray.render_icon(text, color, size=64)

    items = [("Starting up", disc("...", "grey")),
             ("Comfortable", disc("22%", "green")),
             ("Getting close", disc("61%", "amber")),
             ("Near the limit", disc("94%", "red")),
             ("Limit reached", disc("100%", "red"))]
    # The non-numeric faces, one disc per distinct look rather than per status.
    seen = {}
    for status in core.Status:
        d = core.status_display(status)
        seen.setdefault((d["face_pct"], d["face_color"]), []).append(status)
    words = {"-": "Offline / no data", "!": "Not logged in"}
    for (text, color), _statuses in seen.items():
        label = "Rate limited" if color == "red" else words.get(text, text)
        items.append((label, disc(text, color)))
    items.append(("1 waiting", disc("1", "red")))
    items.append(("12 waiting", disc("12", "red")))
    grid_sheet(items, "gallery-tray.png",
               "The Windows tray icon", cols=5,
               note="The number itself is the icon. A blocked session "
                    "outranks the usage figure — usage is something to pace "
                    "against, a blocked session is something to go and do — "
                    "so the disc turns red and counts them instead.")


if __name__ == "__main__":
    strip_gallery()
    contrast_gallery()
    toast_gallery()
    popover_gallery()
    tray_gallery()
