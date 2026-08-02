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
import sessions_core as sc   # noqa: E402
import make_assets     # noqa: E402  (reuse popover_rgba / place_card / radial / drop_shadow)
# The real placement rule, rather than constants that drift from it: the
# popover aligns with the strip's LEFT edge and sits just above it, and hand
# tuning had it floating 30px high and 48px to the right of where the app
# actually puts it. _popover_xy is pure arithmetic.
from widget_bar import _popover_xy    # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
THEME = "light"
#: What the strip actually ships with. render_strip defaults to the two usage
#: meters, so leaving this off silently drops the live-session dots — the
#: popover keys off the session data directly and shows them either way, which
#: is exactly how the strip came to be missing them while the tour looked fine.
METRICS = ("session", "weekly", "sessions")
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


#: The live sessions the tour runs with. Built through the real Session and
#: format_sessions, so the dots, their order and the row text are the app's.
W_, B_, S_, I_ = sc.WAITING, sc.BUSY, sc.SHELL, sc.IDLE
_CAST = [("Ship the release pipeline", "claude-widget"),
         ("Refactor the payment retries", "checkout-api"),
         ("Draft the migration plan", "docs-site"),
         ("Explore the caching idea", "proxy-passer")]


def sessions(*spec):
    """format_sessions for (index, status, age_s, reason) tuples."""
    stamp = sc.now_ms()
    live = []
    for index, status, age, reason in spec:
        title, project = _CAST[index % len(_CAST)]
        live.append(sc.Session(
            session_id=f"hero-{index}", pid=4000 + index, cwd=f"/work/{project}",
            name=f"{project}-{index}", title=title, status=status,
            waiting_for=reason, status_updated_at=stamp - age * 1000))
    return sc.format_sessions(live)


#: What the strip carries for most of the tour: three sessions, nothing wrong.
CALM = sessions((1, B_, 720, ""), (2, S_, 45, ""), (3, I_, 1560, ""))
#: One of them blocks, and sorts to the front.
BLOCKED = sessions((0, W_, 240, "input needed"), (1, B_, 720, ""),
                   (2, S_, 45, ""), (3, I_, 1560, ""))
#: Answered — it goes back to work.
ANSWERED = sessions((0, B_, 2, ""), (1, B_, 725, ""), (2, S_, 50, ""),
                    (3, I_, 1565, ""))


def disp(session, weekly, fable=None, foot="Updated just now · auto", cost=None,
         live=CALM, pulse=False):
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
    if live is not None:
        d.update(live)
    if pulse:
        d["_pulse"] = True
    if cost:
        d["cost_tokens"], d["cost_usd"] = cost
    return d


OFFLINE = dict({
    "plan": "Plan: Max (5x)",
    "session_pct": 61, "session_color": "grey",
    "session_resets_at": NOW + timedelta(hours=2, minutes=55),
    "weekly_pct": 18, "weekly_color": "grey",
    "weekly_resets_at": NOW + timedelta(days=3, hours=2),
    "model_rows": [{"label": "Fable", "pct": 18, "color": "grey"}],
    "session": "offline — last known", "face_pct": "61%", "face_color": "grey",
    "foot": {"text": "offline · showing last known", "dot": "amber"},
}, **CALM)   # sessions are read locally, so they survive losing the network


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
    # Centred, but never closer than a comfortable gap to the widget. The
    # strip grew when the live-session dots went in and the cluster, which
    # knew nothing about that, ended up touching the last dot.
    widest = max(render.render_strip(d, TB_HEX, THEME, scale=3,
                                     metrics=METRICS).width
                 for d in (disp(41, 21, live=BLOCKED), disp(100, 54)))
    x0 = max(W // 2 - (len(icons) * 44) // 2, 26 + widest + 46)
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


def _strip_home(strip_h):
    return 26, H - TB_H + (TB_H - strip_h) // 2


def compose(d_disp, *, pop_alpha=255, pop_dy=0, toast=None, toast_alpha=255,
            toast_dy=0, caption=None, strip_xy=None, ghost=False, pop_at=None):
    im = BG.copy()
    if strip_xy is None:
        # normal: docked on the taskbar
        strip = render.render_strip(d_disp, TB_HEX, THEME, scale=3,
                                    metrics=METRICS).convert("RGBA")
        im.alpha_composite(strip, _strip_home(strip.height))
    else:
        # dragged onto the desktop: the real widget samples the pixels under it
        # so it reads as floating text — mimic that by sampling the backdrop, and
        # leave a faint "home" ghost on the taskbar.
        if ghost:
            g = render.render_strip(d_disp, TB_HEX, THEME, scale=3,
                                    metrics=METRICS).convert("RGBA")
            g.putalpha(g.split()[3].point(lambda p: int(p * 0.30)))
            im.alpha_composite(g, _strip_home(g.height))
        x, y = int(strip_xy[0]), int(strip_xy[1])
        sx = max(0, min(W - 1, x + 42))
        sy = max(0, min(H - 1, y + 14))
        bg_hex = "#%02x%02x%02x" % BG.getpixel((sx, sy))[:3]
        strip = render.render_strip(d_disp, bg_hex, THEME, scale=3,
                                    metrics=METRICS).convert("RGBA")
        sh, pad = make_assets.drop_shadow(strip.size, 8, 15, 55)
        im.alpha_composite(sh, (x - pad, y - pad + 6))
        im.alpha_composite(strip, (x, y))
    if pop_alpha > 0:
        pop = make_assets.popover_rgba(d_disp, THEME)
        if pop_at is not None:                 # anchored to a dragged strip
            px, py = int(pop_at[0]), int(pop_at[1]) + pop_dy
        else:
            # Docked: exactly where the app would put it — left edge aligned
            # with the strip, bottom tucked just above it.
            sh = render.render_strip(d_disp, TB_HEX, THEME, scale=3,
                                     metrics=METRICS).height
            ax, at = _strip_home(sh)
            px, py = _popover_xy(ax, at, at + sh, pop.width, pop.height,
                                 (0, 0, W, H - TB_H))
            py += pop_dy
        make_assets.place_card(im, _fade(pop, pop_alpha) if pop_alpha < 255 else pop,
                               px, py)
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

# 2b ── live sessions, and answering the blocked one -------------------------
# Built from the app's own alert payload so the card says what the app says.
NEEDS = sc.alert_for(sc.Transition(
    "status", sc.Session(session_id="hero-0", pid=4000, cwd="/work/claude-widget",
                         name="claude-widget-0", title="Ship the release pipeline",
                         status=W_, waiting_for="run the test suite?",
                         status_updated_at=sc.now_ms() - 20_000),
    B_, W_))
NEEDS_TOAST = render.render_toast(None, NEEDS["title"], NEEDS["subtitle"],
                                  NEEDS["color"], THEME,
                                  choices=("Yes", "No")).convert("RGBA")

caps = "Every Claude Code session, right on the strip"
busy = disp(41, 21)
xfade(compose(disp(92, 54), caption="Live session & weekly usage, color-coded"),
      compose(busy, caption=caps), 4, 55)
hold(compose(busy, caption=caps), 1500)

# one of them blocks: it sorts to the front, the leading dot goes red, and the
# strip pulses once — the same beat the real widget plays.
capb = "One needs you — the dot turns red and the strip pulses"
blocked_now = disp(41, 21, live=BLOCKED)
for i in range(4):
    add(compose(disp(41, 21, live=BLOCKED, pulse=i % 2 == 0), caption=capb), 110)
hold(compose(blocked_now, caption=capb), 900)

capa = "Answer it from the toast — without leaving what you're doing"
for i in range(1, 6):
    t = ease(i / 5)
    add(compose(blocked_now, toast=NEEDS_TOAST, toast_alpha=int(255 * t),
                toast_dy=int((1 - t) * 40), caption=capa), 55)
hold(compose(blocked_now, toast=NEEDS_TOAST, caption=capa), 2100)

# answered — the toast goes and the session is back at work.
answered_now = disp(41, 21, live=ANSWERED)
for i in range(1, 5):
    add(compose(blocked_now, toast=NEEDS_TOAST,
                toast_alpha=int(255 * (1 - i / 4)), caption=capa), 55)
hold(compose(answered_now, caption="…and it's back to work"), 1500)

# 3 ── threshold alert -------------------------------------------------------
peak = disp(92, 54)
xfade(compose(answered_now, caption="…and it's back to work"),
      compose(peak, caption="Desktop alert before you hit a limit"), 4, 55)
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
for i in range(1, 11):                       # the 5-hour window resets to 0%
    t = ease(i / 10)                         # (weekly is independent — it stays)
    s = int(round(100 - t * 100))            # 100 -> 0
    add(compose(disp(s, 54), caption=capr), 55)
reset_state = disp(0, 54)
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

# 8 ── drag it anywhere ------------------------------------------------------
capd = "Drag it anywhere — it remembers the spot"
dd = disp(41, 21)
_dragged = render.render_strip(dd, TB_HEX, THEME, scale=3, metrics=METRICS)
_HOME = _strip_home(_dragged.height)
# Kept on screen by the strip's real width rather than a number that was right
# before the live-session dots made it wider — at the old target its last dot
# hung off the right edge of the frame.
_B = (536, 250)
_C = (min(770, W - _dragged.width - 26), 150)


def _drag(xy):
    return compose(dd, pop_alpha=0, strip_xy=xy, ghost=True, caption=capd)


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


xfade(compose(OFFLINE, caption="Graceful when you're offline"), _drag(_HOME), 4, 55)
for i in range(1, 12):                        # lift off the taskbar, glide up
    add(_drag(_lerp(_HOME, _B, ease(i / 11))), 55)
hold(_drag(_B), 1100)
for i in range(1, 9):                          # nudge to a second spot
    add(_drag(_lerp(_B, _C, ease(i / 8))), 55)
hold(_drag(_C), 1100)

# 8b ── open the details from the new spot (popover follows the widget) -------
capo = "Open the details right where it sits"
_striph = render.render_strip(dd, TB_HEX, THEME, scale=3, metrics=METRICS).height
_pop = make_assets.popover_rgba(dd, THEME)
# Same rule as the docked case, anchored to where the widget was dragged to.
# Up here there is no room above, so it drops below — which is the app's own
# behaviour, not a special case for the tour.
_anchor = _popover_xy(int(_C[0]), int(_C[1]), int(_C[1]) + _striph,
                      _pop.width, _pop.height, (0, 0, W, H - TB_H))


def _drag_open(alpha, dy=0):
    return compose(dd, strip_xy=_C, ghost=True, pop_alpha=alpha, pop_at=_anchor,
                   pop_dy=dy, caption=capo)


for i in range(1, 8):                          # popover reveals below the strip
    t = ease(i / 7)
    add(_drag_open(int(255 * t), dy=int((1 - t) * -14)), 55)
hold(_drag_open(255), 1900)


# 9 ── end card --------------------------------------------------------------
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
xfade(_drag_open(255), ec, 5, 55)
hold(ec, 2400)
xfade(ec, compose(start, pop_alpha=0), 5, 55)   # gentle loop back


# --------------------------------------------------------------------------- #
def _shared_palette(colors=256):
    """One colour table for the whole GIF.

    Left to itself PIL picks a palette per frame, so the encoder writes a
    local colour table for each and the same flat backdrop quantises slightly
    differently frame to frame — which is both larger and visibly noisier.

    Built from CROPS of the interface rather than whole frames. Most of a frame
    is pale backdrop, and a palette weighted by pixel count spends itself on
    two hundred shades of grey-blue while the amber, red and green that carry
    the meaning share what is left. The first attempt at this did exactly
    that: severity colours washed out and the accent turned muddy pink.
    """
    picks = frames[::max(1, len(frames) // 20)][:20]
    # The interface lives in these bands; the last keeps enough backdrop to
    # quantise the gradient smoothly.
    bands = [(60, 140, 560, 580), (0, H - TB_H, W, H),
             (620, 380, W, H - TB_H), (0, 0, W, 120)]
    tiles = [frame.crop(box) for frame in picks for box in bands]
    # …and the palette that MEANS something gets a block of its own. Sampling
    # alone is a popularity contest: the "needs you" toast is on screen for a
    # dozen frames out of a hundred and thirty, so its red lost every slot and
    # came out magenta. These are the colours the app chose on purpose.
    T = render.THEMES[THEME]
    keys = ("green", "amber", "red", "accent", "accent_soft", "neutral", "dim",
            "faint", "grey", "track", "panel_top", "panel_bot", "border")
    swatch = Image.new("RGB", (240, 60 * len(keys)))
    paint = ImageDraw.Draw(swatch)
    for index, key in enumerate(keys):
        paint.rectangle([0, index * 60, 240, index * 60 + 60], fill=T[key])
    tiles.append(swatch)
    width = max(t.width for t in tiles)
    montage = Image.new("RGB", (width, sum(t.height for t in tiles)))
    y = 0
    for tile in tiles:
        montage.paste(tile, (0, y))
        y += tile.height
    return montage.quantize(colors=colors, method=Image.MEDIANCUT)


def _save():
    palette = _shared_palette()
    # dither=NONE: the backdrop is a smooth gradient and dithering it turns a
    # flat area into noise that no two frames share, which is exactly what a
    # GIF cannot compress.
    flat = [f.quantize(palette=palette, dither=Image.Dither.NONE)
            for f in frames]
    flat[0].save(
        os.path.join(OUT, "hero.gif"), save_all=True, append_images=flat[1:],
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
