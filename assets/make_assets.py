"""Generate the README marketing images.

Composites the REAL rendered widget (from render.py) onto mocked Windows and
macOS desktops with soft drop shadows. Run:  py assets/make_assets.py

This covers every rendered surface: the strip (+ severity/offline states), the
popover (light/dark, per-model, cost), threshold alert toasts, resume toasts,
the fullscreen overlay, the mac menu bar and the app icon. The one surface it
can't draw is the native Settings window (real Tk widgets) — regenerate that
with  py assets/capture_settings.py  (Windows, needs a display).
Keep these in sync: whenever the UI changes, re-run both scripts.
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
            ("row", f"Session   {disp['session_pct']}%   ·   resets in 1h 22m"),
            ("row", f"Weekly (all)   {disp['weekly_pct']}%   ·   resets Thu Jul 16, 11:00 AM"),
            ("row", f"     Fable   {disp['model_rows'][0]['pct']}%"),
            ("sep", ""), ("act", "Settings"), ("act", "Refresh now"), ("act", "Quit")]
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


def _action_toast_rgba(title, subtitle, action, theme):
    img = render.render_action_toast(title, subtitle, action, theme)
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=14, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def resume_showcase():
    a = _action_toast_rgba("Session limit reset", "Click to resume where you left off",
                           "Resume", "light")
    b = _action_toast_rgba("Session reset — auto-resuming", "resuming in 18s  ·  click to cancel",
                           "Cancel", "dark")
    pad, gap = 40, 34
    W = pad * 2 + a.width + gap + b.width
    H = pad * 2 + max(a.height, b.height) + 26
    bg = render._vgrad(W, H, "#eef1f6", "#dbe1ea").convert("RGBA")
    place_card(bg, a, pad, pad, blur=20, alpha=55)
    place_card(bg, b, pad + a.width + gap, pad, blur=20, alpha=55)
    d = ImageDraw.Draw(bg)
    f = render._font("sb", 12)
    d.text((pad + a.width / 2, pad + a.height + 13), "Tier 1 — notify + one click",
           font=f, fill="#6b7480", anchor="mm")
    d.text((pad + a.width + gap + b.width / 2, pad + b.height + 13), "Tier 2 — auto (opt-in)",
           font=f, fill="#6b7480", anchor="mm")
    bg.convert("RGB").save(os.path.join(OUT, "resume.png"))


def _toast_rgba(pct, title, subtitle, color, theme):
    img = render.render_toast(pct, title, subtitle, color, theme)
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=14, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def alerts_showcase():
    """Desktop threshold alerts (the toast you get when you cross 80% / 90%)."""
    a = _toast_rgba(80, "Session usage at 80%", "1h 8m left", "amber", "light")
    b = _toast_rgba(92, "Weekly usage at 92%", "resets Thu 10:59 AM", "red", "dark")
    pad, gap = 40, 34
    W = pad * 2 + a.width + gap + b.width
    H = pad * 2 + max(a.height, b.height) + 26
    bg = render._vgrad(W, H, "#eef1f6", "#dbe1ea").convert("RGBA")
    place_card(bg, a, pad, pad, blur=20, alpha=55)
    place_card(bg, b, pad + a.width + gap, pad, blur=20, alpha=55)
    d = ImageDraw.Draw(bg)
    f = render._font("sb", 12)
    d.text((pad + a.width / 2, pad + a.height + 13), "Approaching (80%)",
           font=f, fill="#6b7480", anchor="mm")
    d.text((pad + a.width + gap + b.width / 2, pad + b.height + 13), "Critical (90%+)",
           font=f, fill="#6b7480", anchor="mm")
    bg.convert("RGB").save(os.path.join(OUT, "alerts.png"))


def strip_states():
    """The strip's color-coded severity plus the graceful offline state."""
    now = datetime.now(timezone.utc)
    states = [
        ({"session_pct": 22, "session_color": "green", "weekly_pct": 6, "weekly_color": "green",
          "session_resets_at": now + timedelta(hours=3, minutes=40)}, "Comfortable"),
        ({"session_pct": 61, "session_color": "amber", "weekly_pct": 18, "weekly_color": "green",
          "session_resets_at": now + timedelta(hours=1, minutes=22)}, "Getting close"),
        ({"session_pct": 94, "session_color": "red", "weekly_pct": 42, "weekly_color": "amber",
          "session_resets_at": now + timedelta(minutes=48)}, "Near the limit"),
        ({"session_pct": 100, "session_color": "red", "weekly_pct": 42, "weekly_color": "amber",
          "session_resets_at": now + timedelta(minutes=6)}, "Limit reached"),
        ({"session": "offline — last known"}, "Offline / no data"),
    ]
    bgc = "#e8edf3"
    strips = [(render.render_strip(disp, bgc, "light", scale=3), lbl) for disp, lbl in states]
    pad, gap, labelw = 26, 22, 150
    W = pad * 2 + labelw + max(s.width for s, _ in strips)
    H = pad * 2 + sum(s.height for s, _ in strips) + gap * (len(strips) - 1)
    bg = Image.new("RGB", (W, H), bgc)
    d = ImageDraw.Draw(bg)
    f = render._font("reg", 13)
    y = pad
    for s, lbl in strips:
        bg.paste(s, (pad + labelw, y))
        d.text((pad, y + s.height // 2), lbl, font=f, fill="#6b7480", anchor="lm")
        y += s.height + gap
    bg.save(os.path.join(OUT, "strip-states.png"))


def cost_showcase():
    """The popover with the opt-in estimated-cost line."""
    now = datetime.now(timezone.utc)
    disp = {
        "plan": "Plan: Max (5x)",
        "session_pct": 61, "session_color": "amber",
        "session_resets_at": now + timedelta(minutes=82),
        "weekly_pct": 18, "weekly_color": "green",
        "weekly_resets_at": now + timedelta(days=3, hours=2),
        "model_rows": [{"label": "Fable", "pct": 4, "color": "green"}],
        "cost_tokens": 2_450_000, "cost_usd": 8.74,
    }
    pop = popover_rgba(disp, "light")
    pad = 44
    W, H = pop.width + pad * 2, pop.height + pad * 2 + 26
    bg = render._vgrad(W, H, "#eef1f6", "#dbe1ea").convert("RGBA")
    place_card(bg, pop, pad, pad, blur=22, alpha=55)
    d = ImageDraw.Draw(bg)
    f = render._font("sb", 12)
    d.text((pad + pop.width / 2, pad + pop.height + 13), "Estimated cost today (opt-in)",
           font=f, fill="#6b7480", anchor="mm")
    bg.convert("RGB").save(os.path.join(OUT, "popover-cost.png"))


def fullscreen_showcase():
    """The strip staying visible over a fullscreen movie (hide_on_fullscreen = false)."""
    W, H = 1200, 675
    bg = render._vgrad(W, H, "#0c1220", "#05070c").convert("RGBA")
    bg.alpha_composite(radial(W, H, int(W * 0.60), int(H * 0.40), 460, (232, 138, 66), 150))
    bg.alpha_composite(radial(W, H, int(W * 0.18), int(H * 0.72), 400, (46, 74, 132), 90))
    # vignette (darken the edges like a cinematic frame)
    vg = Image.new("L", (W, H), 255)
    ImageDraw.Draw(vg).ellipse([int(-W * 0.15), int(-H * 0.15), int(W * 1.15), int(H * 1.15)], fill=0)
    vg = vg.filter(ImageFilter.GaussianBlur(150)).point(lambda p: int(p * 0.72))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dark.putalpha(vg)
    bg.alpha_composite(dark)
    d = ImageDraw.Draw(bg)

    bar = 46  # letterbox bars
    d.rectangle([0, 0, W, bar], fill=(0, 0, 0))
    d.rectangle([0, H - bar, W, H], fill=(0, 0, 0))
    fsub = render._font("reg", 22)
    sub = "— We still have thirty-eight minutes."
    d.text(((W - d.textlength(sub, font=fsub)) / 2, H - bar - 44), sub, font=fsub, fill=(236, 236, 236))

    disp = {"session_pct": 42, "session_color": "green",
            "session_resets_at": datetime.now(timezone.utc) + timedelta(minutes=38, seconds=50),
            "weekly_pct": 8, "weekly_color": "green",
            "weekly_resets_at": datetime.now(timezone.utc) + timedelta(days=2)}
    strip = render.render_strip(disp, "#12151b", "dark", scale=3).convert("RGBA")
    bg.alpha_composite(strip, (26, H - bar - 26 - strip.height))

    ftag = render._font("sb", 12)
    tag = "hide_on_fullscreen = false"
    tw = d.textlength(tag, font=ftag)
    d.rounded_rectangle([26, bar + 22, 26 + tw + 22, bar + 22 + 26], radius=13, fill=(0, 0, 0, 130))
    d.text((37, bar + 22 + 13), tag, font=ftag, fill=(238, 238, 238), anchor="lm")
    bg.convert("RGB").save(os.path.join(OUT, "fullscreen.png"))


def app_icon():
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, S - 8, S - 8], radius=56, fill="#d97757")
    render._spark(d, S / 2, S / 2 - 2, S * 0.30, "#ffffff")
    img.save(os.path.join(OUT, "icon.png"))
    img.save(os.path.join(OUT, "icon.ico"),
             sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    disp = sample_disp()
    hero_windows(disp)
    hero_mac(disp)
    themes_side_by_side(disp)
    strip_closeup(disp)
    strip_states()
    alerts_showcase()
    cost_showcase()
    resume_showcase()
    fullscreen_showcase()
    app_icon()
    print("wrote:", ", ".join(sorted(f for f in os.listdir(OUT)
                                     if f.endswith((".png", ".ico")))))
