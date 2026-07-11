"""Generate the animated hero (assets/hero.gif) for the README.

Renders the REAL widget surfaces (render.py) onto a mocked Windows desktop and
animates a short, looping tour — usage rising with the color shifting
green -> amber -> red, the details popover opening, the live "Updated ... ago"
footer + refresh, and a threshold alert toast — first in the light theme, then
the dark theme, so it doubles as a theme showcase.

Deterministic and code-only (no screen capture), so it regenerates in the same
pipeline as the stills:  py assets/make_hero_gif.py

A contact sheet of sampled frames is written next to it (assets/_hero_sheet.png)
for eyeballing the motion; that file is not committed.
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render          # noqa: E402
import make_assets     # noqa: E402  (reuse popover_rgba / place_card / radial)

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1200, 720
TB_H = 52
FINAL_W = 900          # downscale for a lighter GIF
NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
def color_for(p):
    return "green" if p < 50 else ("amber" if p <= 80 else "red")


def disp(session, weekly, fable=4, foot="Updated just now · auto", cost=None):
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


def _taskbar(bg, tb_hex, dark):
    d = ImageDraw.Draw(bg)
    d.rectangle([0, H - TB_H, W, H], fill=tb_hex)
    icons = ["#3b6fd6", "#5c636e", "#e5a100", "#2ea043", "#d97757", "#8250df", "#0aa2c0"]
    x0 = W // 2 - (len(icons) * 44) // 2
    iy = H - TB_H + (TB_H - 26) // 2
    for i, c in enumerate(icons):
        d.rounded_rectangle([x0 + i * 44, iy, x0 + i * 44 + 26, iy + 26], radius=6, fill=c)
    fc, fd = render._font("sb", 13), render._font("reg", 11)
    clock_fg = "#eef0f3" if dark else "#1f242b"
    date_fg = "#aab1bd" if dark else "#6b7480"
    d.text((W - 20, H - TB_H + 17), "10:24 AM", font=fc, fill=clock_fg, anchor="rm")
    d.text((W - 20, H - TB_H + 35), "Mon, Jul 13", font=fd, fill=date_fg, anchor="rm")


def light_bg():
    bg = render._vgrad(W, H, "#22324f", "#0d1420").convert("RGBA")
    bg.alpha_composite(make_assets.radial(W, H, W // 2, 130, 560, (86, 120, 180), 95))
    _taskbar(bg, "#f2f4f7", dark=False)
    return bg


def dark_bg():
    bg = render._vgrad(W, H, "#1b1230", "#0a0712").convert("RGBA")
    bg.alpha_composite(make_assets.radial(W, H, W - 280, 150, 520, (214, 130, 104), 80))
    bg.alpha_composite(make_assets.radial(W, H, 240, 600, 460, (90, 80, 150), 60))
    _taskbar(bg, "#1b1d22", dark=True)
    return bg


BG = {"light": light_bg(), "dark": dark_bg()}
TB_HEX = {"light": "#f2f4f7", "dark": "#1b1d22"}


def _fade_alpha(img, a):
    if a >= 255:
        return img
    chan = img.split()[3].point(lambda p: p * a // 255)
    out = img.copy()
    out.putalpha(chan)
    return out


def frame(d, theme, pop_alpha=255, pop_dy=0, toast=None, toast_dy=0):
    im = BG[theme].copy()
    strip = render.render_strip(d, TB_HEX[theme], theme, scale=3).convert("RGBA")
    im.alpha_composite(strip, (24, H - TB_H + (TB_H - strip.height) // 2))
    if pop_alpha > 0:
        pop = make_assets.popover_rgba(d, theme)
        x, y = 60, H - TB_H - pop.height - 26 + pop_dy
        if pop_alpha < 255:
            make_assets.place_card(im, _fade_alpha(pop, pop_alpha), x, y)
        else:
            make_assets.place_card(im, pop, x, y)
    if toast is not None:
        tw, th = toast.size
        im.alpha_composite(toast, (W - tw - 26, H - TB_H - th - 22 + toast_dy))
    return im.convert("RGB")


# --------------------------------------------------------------------------- #
# Storyboard
# --------------------------------------------------------------------------- #
frames, durs = [], []


def add(img, ms):
    frames.append(img)
    durs.append(ms)


def hold(img, ms, n):
    for _ in range(n):
        add(img, ms)


# --- LIGHT: popover opens, usage climbs, refresh, alert ---------------------
low = disp(12, 4)
for a in (0, 70, 130, 190, 255):                 # popover fades up
    add(frame(low, "light", pop_alpha=a, pop_dy=(255 - a) // 12), 60)
hold(frame(low, "light"), 90, 3)

for s, w, f in [(12, 4, 4), (31, 9, 6), (52, 18, 11),
                (74, 33, 19), (90, 52, 28)]:      # usage rising + colors
    hold(frame(disp(s, w, f), "light"), 120, 3)

peak = disp(90, 52, 28)
hold(frame(disp(90, 52, 28, foot="Refreshing…"), "light"), 110, 3)
hold(frame(peak, "light"), 110, 4)               # -> "Updated just now"

toast = render.render_toast(90, "Session · 5-hour limit",
                            "90% used — heads up", "red", "light").convert("RGBA")
for dy in (70, 40, 16, 0):                        # toast slides in
    add(frame(peak, "light", toast=toast, toast_dy=dy), 60)
hold(frame(peak, "light", toast=toast), 110, 8)
hold(frame(peak, "light"), 90, 2)

# --- crossfade to DARK ------------------------------------------------------
lightEnd = frame(peak, "light")
darkStart = frame(disp(61, 18, cost=(1360000, 4.20)), "dark")
for t in (0.2, 0.4, 0.6, 0.8):
    add(Image.blend(lightEnd, darkStart, t), 55)

# --- DARK: cost view, per-model, limit reached ------------------------------
hold(frame(disp(61, 18, cost=(1360000, 4.20)), "dark"), 120, 5)
hold(frame(disp(78, 33, cost=(2010000, 6.05)), "dark"), 120, 4)
hold(frame(disp(96, 66), "dark"), 130, 4)
hold(frame(disp(100, 66), "dark"), 140, 5)        # "limit reached"
hold(frame(disp(100, 66), "dark"), 120, 2)

# --- crossfade back to LIGHT (smooth loop) ----------------------------------
darkEnd = frame(disp(100, 66), "dark")
lightStart = frame(low, "light")
for t in (0.25, 0.5, 0.75):
    add(Image.blend(darkEnd, lightStart, t), 55)


# --------------------------------------------------------------------------- #
def _save():
    scale = FINAL_W / W
    small = [f.resize((FINAL_W, int(H * scale)), Image.LANCZOS) for f in frames]
    # per-frame adaptive palette keeps gradients clean; optimize shrinks it
    small[0].save(
        os.path.join(OUT, "hero.gif"), save_all=True, append_images=small[1:],
        duration=durs, loop=0, optimize=True, disposal=2,
    )
    kb = os.path.getsize(os.path.join(OUT, "hero.gif")) // 1024
    print("wrote assets/hero.gif  frames=%d  %dx%d  %d KB"
          % (len(small), FINAL_W, int(H * scale), kb))

    # contact sheet for review (sampled frames)
    idxs = list(range(0, len(frames), max(1, len(frames) // 12)))[:12]
    cols, tw = 3, 360
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
