"""Generate the animated hero (assets/hero.gif) for the README.

Renders the REAL widget surfaces (render.py) onto an elegant, mocked light
Windows desktop and animates a calm, professional product tour:

    open -> live usage rising (green->amber->red) -> threshold alert ->
    limit reached -> reset & auto-resume -> estimated cost / per-model ->
    graceful offline -> end card

Design notes for a crisp, non-blurry result:
  * Everything is composited at NATIVE resolution (no post-downscale), so text
    stays sharp.
  * A flat, limited-palette backdrop keeps the GIF from dithering.
  * Still "holds" are a single long-duration frame (slow pacing, small file);
    only real motion (count-ups, slides, fades) spends extra frames.

Deterministic and code-only, regenerates in the same pipeline as the stills:
    py assets/make_hero_gif.py

Also writes assets/_hero_sheet.png (a review contact sheet, git-ignored).
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render          # noqa: E402
import make_assets     # noqa: E402  (reuse popover_rgba / place_card / radial / drop_shadow)

OUT = os.path.dirname(os.path.abspath(__file__))
THEME = "light"
W, H = 1040, 640
TB_H = 50
TB_HEX = "#f4f6fa"
POP_X = 74
POP_BOTTOM = H - TB_H - 30      # anchor popover by its bottom so it never jumps
NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def color_for(p):
    return "green" if p < 50 else ("amber" if p <= 80 else "red")


def disp(session, weekly, fable=None, foot="Updated just now · auto", cost=None):
    fable = int(round(session * 0.3)) if fable is None else fable
    face = max(session, weekly)
    d = {
        "plan": "Plan: Max (5x)",
        "session_pct": session, "session_color": color_for(session),
        "session_resets_at": NOW + timedelta(hours=2, minutes=55),
        "weekly_pct": weekly, "weekly_color": color_for(weekly),
        "weekly_resets_at": NOW + timedelta(days=3, hours=2),
        "model_rows": [{"label": "Fable", "pct": fable, "color": color_for(fable)}],
        "face_pct": "%d%%" % face, "face_color": color_for(face),
        "foot": {"text": foot, "dot": "amber" if "Refresh" in foot else "green"},
    }
    if cost:
        d["cost_tokens"], d["cost_usd"] = cost
    return d


OFFLINE = {
    "plan": "Plan: Max (5x)",
    "session_pct": 61, "session_color": "grey",
    "session_resets_at": NOW + timedelta(hours=2, minutes=55),
    "weekly_pct": 18, "weekly_color": "grey",
    "weekly_resets_at": NOW + timedelta(days=3, hours=2),
    "model_rows": [{"label": "Fable", "pct": 18, "color": "grey"}],
    "session": "offline — last known", "face_pct": "61%", "face_color": "grey",
    "foot": {"text": "offline · showing last known", "dot": "amber"},
}


def ease(t):                       # smoothstep 0..1
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


# --------------------------------------------------------------------------- #
# Backdrop (built once)
# --------------------------------------------------------------------------- #
def _backdrop():
    bg = render._vgrad(W, H, "#eef2f8", "#d7e0ee").convert("RGBA")
    bg.alpha_composite(make_assets.radial(W, H, W // 2, 90, 560, (150, 180, 225), 60))
    d = ImageDraw.Draw(bg)
    # taskbar
    d.rectangle([0, H - TB_H, W, H], fill=TB_HEX)
    d.line([0, H - TB_H, W, H - TB_H], fill=(0, 0, 0, 18), width=1)
    icons = ["#3b6fd6", "#5c636e", "#e5a100", "#2ea043", "#d97757", "#8250df", "#0aa2c0"]
    x0 = W // 2 - (len(icons) * 44) // 2
    iy = H - TB_H + (TB_H - 25) // 2
    for i, c in enumerate(icons):
        d.rounded_rectangle([x0 + i * 44, iy, x0 + i * 44 + 25, iy + 25], radius=6, fill=c)
    fc, fd = render._font("sb", 13), render._font("reg", 11)
    d.text((W - 20, H - TB_H + 16), "10:24 AM", font=fc, fill="#1f242b", anchor="rm")
    d.text((W - 20, H - TB_H + 34), "Mon, Jul 13", font=fd, fill="#6b7480", anchor="rm")
    return bg


BG = _backdrop()


# --------------------------------------------------------------------------- #
# Frame composition
# --------------------------------------------------------------------------- #
def _fade(img, a):
    if a >= 255:
        return img
    chan = img.split()[3].point(lambda p: p * a // 255)
    out = img.copy()
    out.putalpha(chan)
    return out


def _caption(im, text):
    if not text:
        return
    d = ImageDraw.Draw(im)
    f = render._font("sb", 15)
    tw = d.textlength(text, font=f)
    pad_x, pad_y = 16, 8
    cx, cy = W // 2, 46
    box = [cx - tw / 2 - pad_x, cy - pad_y - 8, cx + tw / 2 + pad_x, cy + pad_y + 8]
    pill = Image.new("RGBA", (int(box[2] - box[0]), int(box[3] - box[1])), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle(
        [0, 0, pill.width - 1, pill.height - 1], radius=pill.height // 2,
        fill=(23, 28, 38, 220))
    im.alpha_composite(pill, (int(box[0]), int(box[1])))
    d.text((cx, cy), text, font=f, fill="#f4f6fa", anchor="mm")


def compose(d_disp, *, pop_alpha=255, pop_dy=0, toast=None, toast_alpha=255,
            toast_dy=0, caption=None):
    im = BG.copy()
    strip = render.render_strip(d_disp, TB_HEX, THEME, scale=3).convert("RGBA")
    im.alpha_composite(strip, (26, H - TB_H + (TB_H - strip.height) // 2))
    if pop_alpha > 0:
        pop = make_assets.popover_rgba(d_disp, THEME)
        y = POP_BOTTOM - pop.height + pop_dy
        make_assets.place_card(im, _fade(pop, pop_alpha) if pop_alpha < 255 else pop,
                               POP_X, y)
    if toast is not None and toast_alpha > 0:
        t = _fade(toast, toast_alpha) if toast_alpha < 255 else toast
        tw, th = t.size
        # shadowed card, bottom-right above the taskbar
        make_assets.place_card(im, t, W - tw - 30, H - TB_H - th - 24 + toast_dy,
                               blur=22, alpha=70)
    _caption(im, caption)
    return im.convert("RGB")


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #
frames, durs = [], []


def add(img, ms):
    frames.append(img)
    durs.append(ms)


def hold(img, ms):
    add(img, ms)


def xfade(a, b, n=4, ms=45):
    for i in range(1, n):
        add(Image.blend(a, b, i / n), ms)


ALERT = render.render_toast(90, "Session · 5-hour limit",
                            "90% used — heads up", "red", THEME).convert("RGBA")
RESUME = render.render_action_toast(
    "Session reset — pick up where you left off",
    "resuming in 6s · click to cancel", "Resume now", THEME).convert("RGBA")


# 1 ── open ------------------------------------------------------------------
start = disp(8, 3)
add(compose(start, pop_alpha=0), 40)
for i in range(1, 8):                       # eased slide-up + fade-in
    t = ease(i / 7)
    add(compose(start, pop_alpha=int(255 * t), pop_dy=int((1 - t) * 26)), 45)
hold(compose(start, caption="Your Claude limits — always on your taskbar"), 1700)

# 2 ── usage rising (smooth count-up) + colors -------------------------------
cap = "Live session & weekly usage, color-coded"
for i in range(0, 22):
    t = ease(i / 21)
    s = int(8 + t * 84)                     # 8 -> 92
    w = int(3 + t * 51)                     # 3 -> 54
    add(compose(disp(s, w), caption=cap), 55)
hold(compose(disp(92, 54), caption=cap), 1500)

# 3 ── threshold alert -------------------------------------------------------
peak = disp(92, 54)
for i in range(1, 6):
    t = ease(i / 5)
    add(compose(peak, toast=ALERT, toast_alpha=int(255 * t),
                toast_dy=int((1 - t) * 40), caption="Desktop alert before you hit a limit"), 55)
hold(compose(peak, toast=ALERT, caption="Desktop alert before you hit a limit"), 1700)

# 4 ── limit reached ---------------------------------------------------------
for i in range(1, 6):
    s = 92 + int(ease(i / 5) * 8)           # 92 -> 100
    add(compose(disp(s, 54), toast=ALERT, toast_alpha=int(255 * (1 - i / 5))), 55)
hold(compose(disp(100, 54), caption="Know the moment you're capped"), 1800)

# 5 ── reset & auto-resume ---------------------------------------------------
capr = "Auto-resume the moment your limit resets"
for i in range(1, 9):                        # usage drops back as the window resets
    t = ease(i / 8)
    s = int(100 - t * 92)                    # 100 -> 8
    add(compose(disp(s, 54 - int(t * 8)), caption=capr), 55)
reset_state = disp(6, 46)
for i in range(1, 6):                        # resume toast slides in
    t = ease(i / 5)
    add(compose(reset_state, toast=RESUME, toast_alpha=int(255 * t),
                toast_dy=int((1 - t) * 40), caption=capr), 55)
hold(compose(reset_state, toast=RESUME, caption=capr), 1900)

# 6 ── estimated cost / per-model --------------------------------------------
capc = "Estimated cost & per-model usage"
cost_a = disp(41, 21, cost=(820000, 2.55))
cost_b = disp(41, 21, cost=(1360000, 4.20))
xfade(compose(reset_state, toast=RESUME, caption=capr), compose(cost_a, caption=capc), 4, 55)
for i in range(1, 7):                         # tokens/cost tick up
    t = ease(i / 6)
    tok = int(820000 + t * 540000)
    usd = 2.55 + t * 1.65
    add(compose(disp(41, 21, cost=(tok, usd)), caption=capc), 55)
hold(compose(cost_b, caption=capc), 1700)

# 7 ── graceful offline ------------------------------------------------------
xfade(compose(cost_b, caption=capc), compose(OFFLINE, caption="Graceful when you're offline"), 4, 55)
hold(compose(OFFLINE, caption="Graceful when you're offline"), 1600)


# 8 ── end card --------------------------------------------------------------
def end_card():
    im = BG.copy()
    d = ImageDraw.Draw(im)
    cx, cy = W // 2, H // 2 - 40
    render._spark(d, cx, cy - 66, 34, "#d97757")
    d.text((cx, cy + 6), "Claudometer", font=render._font("sb", 40),
           fill="#1a2230", anchor="mm")
    d.text((cx, cy + 52), "Your Claude usage limits, always visible.",
           font=render._font("reg", 17), fill="#55617a", anchor="mm")
    cmd = "pipx install claudometer"
    f = render._font("sb", 16)
    tw = d.textlength(cmd, font=f)
    bw, bh = tw + 40, 40
    pill = Image.new("RGBA", (int(bw), bh), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=bh // 2,
                                           fill=(23, 28, 38, 235))
    im.alpha_composite(pill, (int(cx - bw / 2), cy + 82))
    d.text((cx, cy + 82 + bh // 2), cmd, font=f, fill="#f4f6fa", anchor="mm")
    return im.convert("RGB")


ec = end_card()
xfade(compose(OFFLINE, caption="Graceful when you're offline"), ec, 5, 55)
hold(ec, 2400)
xfade(ec, compose(start, pop_alpha=0), 5, 55)   # gentle loop back


# --------------------------------------------------------------------------- #
def _save():
    frames[0].save(
        os.path.join(OUT, "hero.gif"), save_all=True, append_images=frames[1:],
        duration=durs, loop=0, optimize=True, disposal=2,
    )
    kb = os.path.getsize(os.path.join(OUT, "hero.gif")) // 1024
    total = sum(durs) / 1000.0
    print("wrote assets/hero.gif  frames=%d  %dx%d  %.1fs  %d KB"
          % (len(frames), W, H, total, kb))

    idxs = list(range(0, len(frames), max(1, len(frames) // 12)))[:12]
    cols = 3
    tw = 360
    th = int(tw * H / W)
    rows = (len(idxs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), "#0d1420")
    for k, i in enumerate(idxs):
        sheet.paste(frames[i].resize((tw, th), Image.LANCZOS),
                    ((k % cols) * tw, (k // cols) * th))
    sheet.save(os.path.join(OUT, "_hero_sheet.png"))
    print("wrote assets/_hero_sheet.png (review only)")


if __name__ == "__main__":
    _save()
