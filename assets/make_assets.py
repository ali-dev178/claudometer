"""Generate the README marketing images.

Composites the REAL rendered widget (from render.py) onto mocked Windows and
macOS desktops with soft drop shadows. Run:  py assets/make_assets.py
"""

import os
import sys
from datetime import datetime, timezone, timedelta

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render  # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))


def sample_disp():
    now = datetime.now(timezone.utc)
    return {
        "plan": "Plan: Max (5x)",
        "session_pct": 61, "session_color": "amber",
        "session_resets_at": now + timedelta(minutes=82),
        "weekly_pct": 18, "weekly_color": "green",
        "weekly_resets_at": now + timedelta(days=3, hours=2),
        "model_rows": [{"label": "Fable", "pct": 4, "color": "green"}],
        "session": None, "weekly": None, "face_pct": "61%",
    }


# --------------------------------------------------------------------------- #
def radial(w, h, cx, cy, radius, color, max_alpha):
    g = Image.new("L", (w, h), 0)
    ImageDraw.Draw(g).ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                              fill=max_alpha)
    g = g.filter(ImageFilter.GaussianBlur(radius * 0.5))
    col = Image.new("RGBA", (w, h), color + (0,))
    col.putalpha(g)
    return col


def drop_shadow(size, radius, blur, alpha):
    w, h = size
    pad = blur * 3
    sh = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([pad, pad, pad + w, pad + h],
                                         radius=radius, fill=(0, 0, 0, alpha))
    return sh.filter(ImageFilter.GaussianBlur(blur)), pad


def popover_rgba(disp, theme):
    rgb, _ = render.render_popover(disp, theme)
    w, h = rgb.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=16, fill=255)
    im = rgb.convert("RGBA")
    im.putalpha(mask)
    return im


def place_card(bg, card, x, y, blur=26, alpha=75, dy=18):
    sh, pad = drop_shadow(card.size, 16, blur, alpha)
    bg.alpha_composite(sh, (x - pad, y - pad + dy))
    bg.alpha_composite(card, (x, y))


# --------------------------------------------------------------------------- #
def hero_windows(disp):
    W, H = 1280, 800
    bg = render._vgrad(W, H, "#22324f", "#0d1420").convert("RGBA")
    bg.alpha_composite(radial(W, H, W // 2, 150, 560, (86, 120, 180), 95))
    d = ImageDraw.Draw(bg)

    tb_h = 56
    d.rectangle([0, H - tb_h, W, H], fill="#f2f4f7")
    icons = ["#3b6fd6", "#5c636e", "#e5a100", "#2ea043", "#d97757", "#8250df", "#0aa2c0"]
    x0 = W // 2 - (len(icons) * 46) // 2
    iy = H - tb_h + (tb_h - 28) // 2
    for i, c in enumerate(icons):
        d.rounded_rectangle([x0 + i * 46, iy, x0 + i * 46 + 28, iy + 28], radius=7, fill=c)
    fc, fd = render._font("sb", 13), render._font("reg", 11)
    d.text((W - 20, H - tb_h + 19), "10:24 AM", font=fc, fill="#1f242b", anchor="rm")
    d.text((W - 20, H - tb_h + 37), "Mon, Jul 13", font=fd, fill="#6b7480", anchor="rm")

    strip = render.render_strip(disp, "#f2f4f7", "light", scale=3).convert("RGBA")
    bg.alpha_composite(strip, (18, H - tb_h + (tb_h - strip.height) // 2))

    pop = popover_rgba(disp, "light")
    place_card(bg, pop, 56, H - tb_h - pop.height - 30)
    bg.convert("RGB").save(os.path.join(OUT, "hero-windows.png"))


def hero_mac(disp):
    W, H = 1280, 800
    bg = render._vgrad(W, H, "#402c56", "#241a33").convert("RGBA")
    bg.alpha_composite(radial(W, H, W - 300, 160, 520, (214, 130, 104), 95))
    bg.alpha_composite(radial(W, H, 220, 640, 460, (90, 80, 150), 70))
    d = ImageDraw.Draw(bg)

    mb_h = 30
    d.rectangle([0, 0, W, mb_h], fill=(18, 18, 24, 235))
    fb, fr = render._font("sb", 12), render._font("reg", 12)
    d.ellipse([16, 9, 28, 21], fill="#e6e6e6")  # apple-logo stand-in
    x = 42
    d.text((x, 15), "Claudometer", font=fb, fill="#ffffff", anchor="lm")
    x += 96
    for m in ("File", "Edit", "View", "Window", "Help"):
        d.text((x, 15), m, font=fr, fill="#d6d9de", anchor="lm")
        x += d.textlength(m, font=fr) + 22

    # right-side status: our item (dot + %) then clock
    clock = "Mon 10:24 AM"
    cw = d.textlength(clock, font=fr)
    d.text((W - 18, 15), clock, font=fr, fill="#eef0f3", anchor="rm")
    item_x2 = W - 18 - cw - 22
    pct = f"{disp['session_pct']}%"
    pw = d.textlength(pct, font=fb)
    d.text((item_x2, 15), pct, font=fb, fill="#ffffff", anchor="rm")
    dot_x = item_x2 - pw - 12
    d.ellipse([dot_x - 5, 10, dot_x + 5, 20], fill=render.THEMES["dark"]["amber"])
    item_left = dot_x - 8

    # native-style dropdown under the item
    T = render.THEMES["dark"]
    rows = [("head", "Plan: Max (5x)"), ("sep", ""),
            ("row", f"Session   {disp['session_pct']}%   ·   1h 22m left"),
            ("row", f"Weekly   {disp['weekly_pct']}%   ·   resets Thu 10:59 AM"),
            ("row", f"     Fable   {disp['model_rows'][0]['pct']}%"),
            ("sep", ""), ("act", "Refresh now"), ("act", "Quit")]
    mw = 288
    heights = {"head": 30, "sep": 11, "row": 28, "act": 28}
    mh = sum(heights[k] for k, _ in rows) + 12
    mx, my = min(item_left, W - mw - 14), mb_h + 6
    menu = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
    md = ImageDraw.Draw(menu)
    md.rounded_rectangle([0, 0, mw - 1, mh - 1], radius=12, fill=(38, 40, 48, 250),
                         outline=(255, 255, 255, 22))
    fr2, fh = render._font("reg", 12), render._font("sb", 11)
    yy = 6
    for kind, text in rows:
        hgt = heights[kind]
        if kind == "sep":
            md.line([12, yy + hgt // 2, mw - 12, yy + hgt // 2], fill=(255, 255, 255, 26))
        elif kind == "head":
            md.text((16, yy + hgt / 2), text, font=fh, fill=T["faint"], anchor="lm")
        else:
            col = T["neutral"] if kind == "row" else T["accent"]
            md.text((16, yy + hgt / 2), text, font=fr2, fill=col, anchor="lm")
        yy += hgt
    sh, pad = drop_shadow((mw, mh), 12, 22, 90)
    bg.alpha_composite(sh, (mx - pad, my - pad + 8))
    bg.alpha_composite(menu, (mx, my))

    # a little dock for macOS flavor
    dock_icons = ["#2b7cff", "#34c759", "#ff9500", "#ff375f", "#d97757", "#8e8e93"]
    dw = len(dock_icons) * 58 + 20
    dx, dy = W // 2 - dw // 2, H - 78
    dock = Image.new("RGBA", (dw, 62), (0, 0, 0, 0))
    ImageDraw.Draw(dock).rounded_rectangle([0, 0, dw - 1, 61], radius=18,
                                           fill=(255, 255, 255, 40), outline=(255, 255, 255, 40))
    bg.alpha_composite(dock, (dx, dy))
    for i, c in enumerate(dock_icons):
        ix = dx + 12 + i * 58
        d.rounded_rectangle([ix, dy + 8, ix + 46, dy + 54], radius=12, fill=c)

    bg.convert("RGB").save(os.path.join(OUT, "macos-menubar.png"))


def themes_side_by_side(disp):
    lp, dp = popover_rgba(disp, "light"), popover_rgba(disp, "dark")
    pad, gap = 46, 40
    W = pad * 2 + lp.width + gap + dp.width
    H = pad * 2 + max(lp.height, dp.height) + 26
    bg = render._vgrad(W, H, "#eef1f6", "#dbe1ea").convert("RGBA")
    place_card(bg, lp, pad, pad, blur=22, alpha=55)
    place_card(bg, dp, pad + lp.width + gap, pad, blur=22, alpha=55)
    d = ImageDraw.Draw(bg)
    f = render._font("sb", 12)
    d.text((pad + lp.width / 2, pad + lp.height + 13), "Light", font=f, fill="#6b7480", anchor="mm")
    d.text((pad + lp.width + gap + dp.width / 2, pad + dp.height + 13), "Dark",
           font=f, fill="#6b7480", anchor="mm")
    bg.convert("RGB").save(os.path.join(OUT, "popover-themes.png"))


def strip_closeup(disp):
    strip = render.render_strip(disp, "#e8edf3", "light", scale=3)
    pad = 26
    W, H = strip.width + pad * 2, strip.height + pad * 2
    bg = Image.new("RGB", (W, H), "#e8edf3")
    bg.paste(strip, (pad, pad))
    bg.save(os.path.join(OUT, "strip.png"))


if __name__ == "__main__":
    disp = sample_disp()
    hero_windows(disp)
    hero_mac(disp)
    themes_side_by_side(disp)
    strip_closeup(disp)
    print("wrote:", ", ".join(sorted(f for f in os.listdir(OUT) if f.endswith(".png"))))
