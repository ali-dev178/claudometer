"""High-quality Pillow rendering for the taskbar readout and details popover.

Everything is drawn supersampled (scale x3) and downscaled with LANCZOS for
crisp anti-aliasing — rounded gradient card, sleek horizontal usage meters,
badges, and refined typography. tkinter's canvas can't do this natively, so the
UI is composed as images and shown through ImageTk.
"""

import os
from datetime import datetime, timezone

from PIL import Image, ImageColor, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Palettes
# --------------------------------------------------------------------------- #
THEMES = {
    "light": {
        "key": "#f3f5f8",
        "panel_top": "#ffffff", "panel_bot": "#f5f8fc",
        "border": "#e5e9ef", "hair": "#eef1f5",
        "neutral": "#161b22", "dim": "#6b7480", "faint": "#98a1ad",
        "track": "#e9edf2",
        "accent": "#d97757", "accent_soft": "#f8ece5",
        "green": "#12a150", "amber": "#d99400", "red": "#e5484d",
    },
    "dark": {
        "key": "#0b0e14",
        "panel_top": "#191f2b", "panel_bot": "#12161f",
        "border": "#2a3342", "hair": "#222a37",
        "neutral": "#e9edf4", "dim": "#9aa4b3", "faint": "#69727e",
        "track": "#262f3d",
        "accent": "#e58e6f", "accent_soft": "#2d241f",
        "green": "#2ec26a", "amber": "#e2a53c", "red": "#ff6b6e",
    },
}


def sev_color(T, name):
    return T.get(name, T["dim"])


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
_FCACHE = {}
_FONT_FILES = {
    "reg": "segoeui.ttf", "sb": "segoeuisb.ttf",
    "bold": "segoeuib.ttf", "light": "segoeuil.ttf",
}


def _font(kind, size):
    size = int(size)
    k = (kind, size)
    if k in _FCACHE:
        return _FCACHE[k]
    for name in (_FONT_FILES.get(kind, "segoeui.ttf"), "segoeui.ttf", "arial.ttf"):
        try:
            f = ImageFont.truetype("C:/Windows/Fonts/" + name, size)
            break
        except OSError:
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
    else:
        f = ImageFont.load_default()
    _FCACHE[k] = f
    return f


# --------------------------------------------------------------------------- #
# Time formatting
# --------------------------------------------------------------------------- #
def _fmt_left(dt):
    if not dt:
        return ""
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "resetting"
    h, m = secs // 3600, (secs % 3600) // 60
    if h >= 24:
        d, h = h // 24, h % 24
        return f"{d}d {h}h left"
    if h:
        return f"{h}h {m}m left"
    return f"{m}m left"


def _fmt_at(dt):
    if not dt:
        return ""
    loc = dt.astimezone()
    hf = "%#I" if os.name == "nt" else "%-I"
    return loc.strftime(f"resets %a {hf}:%M %p")


def _fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(int(n))


def _fmt_usd(x):
    return f"${x:,.2f}"


# --------------------------------------------------------------------------- #
# Drawing primitives
# --------------------------------------------------------------------------- #
def _vgrad(w, h, c1, c2):
    a, b = ImageColor.getrgb(c1), ImageColor.getrgb(c2)
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), (int(a[0] + (b[0] - a[0]) * t),
                              int(a[1] + (b[1] - a[1]) * t),
                              int(a[2] + (b[2] - a[2]) * t)))
    return col.resize((w, h))


def _spark(d, cx, cy, s, color):
    """A four-pointed sparkle used as the brand mark."""
    k = 0.26 * s
    d.polygon([(cx, cy - s), (cx + k, cy - k), (cx + s, cy), (cx + k, cy + k),
               (cx, cy + s), (cx - k, cy + k), (cx - s, cy), (cx - k, cy - k)],
              fill=color)


def _rrbar(d, x1, y1, x2, y2, pct, color, track):
    r = (y2 - y1) / 2
    d.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=track)
    fw = (x2 - x1) * min(max(pct or 0, 0), 100) / 100.0
    if fw > 0:
        d.rounded_rectangle([x1, y1, x1 + max(fw, 2 * r), y2], radius=r, fill=color)


def _big_pct(d, x, ymid, pct, fbig, fpct, color, dim):
    """Large percentage value with a smaller raised percent sign."""
    if pct is None:
        d.text((x, ymid), "—", font=fbig, fill=dim, anchor="lm")
        return
    num = str(pct)
    nw = d.textlength(num, font=fbig)
    d.text((x, ymid), num, font=fbig, fill=color, anchor="lm")
    d.text((x + nw + 2, ymid - fbig.size * 0.17), "%", font=fpct, fill=color, anchor="lm")


def _badge_right(d, x_right, cy, text, font, T, S):
    tw = d.textlength(text, font=font)
    padx, h = 9 * S, 18 * S
    x2, x1 = x_right, x_right - (tw + padx * 2)
    d.rounded_rectangle([x1, cy - h / 2, x2, cy + h / 2], radius=h / 2, fill=T["accent_soft"])
    d.text(((x1 + x2) / 2, cy), text, font=font, fill=T["accent"], anchor="mm")


# --------------------------------------------------------------------------- #
# Taskbar strip
# --------------------------------------------------------------------------- #
def render_strip(disp, bg_hex, theme, scale=3, metrics=("session", "weekly")):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    PAD, DOT_R, DOTGAP, GGAP, H = 9 * S, 4 * S, 9 * S, 22 * S, 30 * S

    f_lbl = _font("reg", 12 * S)
    f_num = _font("sb", 13 * S)
    f_dim = _font("reg", 11 * S)

    groups = []
    if "session" in metrics and disp.get("session_pct") is not None:
        g = [("Session ", f_lbl, T["dim"]),
             (f"{disp['session_pct']}%", f_num, sev_color(T, disp["session_color"]))]
        left = _fmt_left(disp.get("session_resets_at"))
        if left:
            g.append(("   " + left, f_dim, T["faint"]))
        groups.append(g)
    if "weekly" in metrics and disp.get("weekly_pct") is not None:
        groups.append([("Weekly ", f_lbl, T["dim"]),
                       (f"{disp['weekly_pct']}%", f_num, sev_color(T, disp["weekly_color"]))])
    if not groups:
        groups.append([("Claude  " + (disp.get("session") or "—"), f_dim, T["dim"])])

    cand = []
    if disp.get("session_pct") is not None:
        cand.append((disp["session_pct"], disp["session_color"]))
    if disp.get("weekly_pct") is not None:
        cand.append((disp["weekly_pct"], disp["weekly_color"]))
    dot_color = sev_color(T, max(cand)[1]) if cand else T["dim"]

    tmp = ImageDraw.Draw(Image.new("RGB", (4, 4)))

    def gw(g):
        return sum(tmp.textlength(t, font=f) for (t, f, _) in g)

    total = PAD + (DOT_R * 2 + DOTGAP) + sum(gw(g) for g in groups) \
        + GGAP * (len(groups) - 1) + PAD
    img = Image.new("RGB", (int(total), H), bg_hex)
    d = ImageDraw.Draw(img)
    cy = H / 2

    dx = PAD + DOT_R
    d.ellipse([dx - DOT_R, cy - DOT_R, dx + DOT_R, cy + DOT_R], fill=dot_color)
    x = PAD + DOT_R * 2 + DOTGAP
    for gi, g in enumerate(groups):
        if gi > 0:
            x += GGAP
        for (t, f, c) in g:
            d.text((x, cy), t, font=f, fill=c, anchor="lm")
            x += tmp.textlength(t, font=f)

    return img.resize((max(1, round(total / S)), round(H / S)), Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Details popover — horizontal usage meters
# --------------------------------------------------------------------------- #
def render_popover(disp, theme, scale=3):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W = 344
    rows = disp.get("model_rows") or []

    S1_LBL, S1_VAL = 66, 94
    S2_LBL, S2_VAL = 130, 158
    has_cost = disp.get("cost_usd") is not None
    if rows:
        div_y = 188
        rows_top = div_y + 22
        content_bottom = rows_top + (len(rows) - 1) * 28
        base_gap = 18
    else:
        div_y = rows_top = None
        content_bottom = S2_VAL + 6
        base_gap = 14
    if has_cost:
        cost_y = content_bottom + 20
        foot_div = cost_y + 13
    else:
        cost_y = None
        foot_div = content_bottom + base_gap
    foot_y = foot_div + 20
    H = foot_y + 24

    Ws, Hs = W * S, H * S
    base = Image.new("RGB", (Ws, Hs), T["key"])
    grad = _vgrad(Ws, Hs, T["panel_top"], T["panel_bot"])
    mask = Image.new("L", (Ws, Hs), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=16 * S, fill=255)
    base.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=16 * S, outline=T["border"], width=max(1, S))

    P = 22 * S
    f_title = _font("sb", 15 * S)
    f_badge = _font("sb", 10 * S)
    f_big = _font("sb", 23 * S)
    f_pct = _font("sb", 12 * S)
    f_lbl = _font("reg", 12 * S)
    f_reset = _font("reg", 10 * S)
    f_row = _font("reg", 12 * S)
    f_rownum = _font("sb", 12 * S)
    f_foot = _font("reg", 10 * S)

    def sev(name):
        return sev_color(T, name)

    # header
    _spark(d, P + 8 * S, 28 * S, 8 * S, T["accent"])
    d.text((P + 24 * S, 28 * S), "Claudometer", font=f_title, fill=T["neutral"], anchor="lm")
    plan = (disp.get("plan") or "").replace("Plan: ", "") or "—"
    _badge_right(d, Ws - P, 28 * S, plan, f_badge, T, S)

    bar_x1, bar_x2 = P + 60 * S, Ws - P

    def metric(y_lbl, y_val, label, reset, pct, color_name):
        d.text((P, y_lbl * S), label, font=f_lbl, fill=T["dim"], anchor="lm")
        if reset:
            d.text((Ws - P, y_lbl * S), reset, font=f_reset, fill=T["faint"], anchor="rm")
        col = sev(color_name)
        _big_pct(d, P, y_val * S, pct, f_big, f_pct, col, T["dim"])
        h = 9 * S
        _rrbar(d, bar_x1, y_val * S - h / 2, bar_x2, y_val * S + h / 2, pct, col, T["track"])

    metric(S1_LBL, S1_VAL, "Current session",
           _fmt_left(disp.get("session_resets_at")),
           disp.get("session_pct"), disp.get("session_color", "grey"))
    metric(S2_LBL, S2_VAL, "Weekly · all models",
           _fmt_at(disp.get("weekly_resets_at")),
           disp.get("weekly_pct"), disp.get("weekly_color", "grey"))

    # per-model rows (e.g. Fable)
    if rows:
        d.line([P, div_y * S, Ws - P, div_y * S], fill=T["hair"], width=max(1, S))
        y = rows_top * S
        for row in rows:
            d.text((P, y), row["label"], font=f_row, fill=T["neutral"], anchor="lm")
            hh = 7 * S
            _rrbar(d, P + 66 * S, y - hh / 2, Ws - P - 46 * S, y + hh / 2,
                   row["pct"], sev(row["color"]), T["track"])
            d.text((Ws - P, y), f"{row['pct']}%", font=f_rownum,
                   fill=sev(row["color"]), anchor="rm")
            y += 28 * S

    # estimated cost (optional)
    if has_cost:
        d.text((P, cost_y * S), "Today", font=f_lbl, fill=T["dim"], anchor="lm")
        tok = _fmt_tokens(disp.get("cost_tokens", 0))
        usd = _fmt_usd(disp.get("cost_usd", 0.0))
        d.text((Ws - P, cost_y * S), f"{tok} tokens   ·   ~{usd}", font=f_rownum,
               fill=T["neutral"], anchor="rm")

    # footer
    d.line([P, foot_div * S, Ws - P, foot_div * S], fill=T["hair"], width=max(1, S))
    fy = foot_y * S
    d.ellipse([P, fy - 3 * S, P + 6 * S, fy + 3 * S], fill=T["green"])
    d.text((P + 13 * S, fy), "Auto-updating", font=f_foot, fill=T["faint"], anchor="lm")
    quit_w = d.textlength("Quit", font=f_foot)
    qx2, qx1 = Ws - P, Ws - P - quit_w
    d.text((qx2, fy), "Quit", font=f_foot, fill=T["dim"], anchor="rm")
    ref_w = d.textlength("Refresh", font=f_foot)
    rx2 = qx1 - 18 * S
    rx1 = rx2 - ref_w
    d.text((rx2, fy), "Refresh", font=f_foot, fill=T["accent"], anchor="rm")

    out = base.resize((W, H), Image.LANCZOS)
    hits = {
        "refresh": (rx1 / S, (fy - 12 * S) / S, rx2 / S, (fy + 12 * S) / S),
        "quit": (qx1 / S, (fy - 12 * S) / S, qx2 / S, (fy + 12 * S) / S),
    }
    return out, hits


# --------------------------------------------------------------------------- #
# Threshold alert toast
# --------------------------------------------------------------------------- #
def render_toast(pct, title, subtitle, color_name, theme, scale=3):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W, H = 322, 70
    Ws, Hs = W * S, H * S
    base = Image.new("RGB", (Ws, Hs), T["key"])
    grad = _vgrad(Ws, Hs, T["panel_top"], T["panel_bot"])
    mask = Image.new("L", (Ws, Hs), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=14 * S, fill=255)
    base.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=14 * S, outline=T["border"], width=max(1, S))

    col = sev_color(T, color_name)
    cx1, cy1, chip = 14 * S, 13 * S, 44 * S
    d.rounded_rectangle([cx1, cy1, cx1 + chip, cy1 + chip], radius=12 * S, fill=col)
    d.text((cx1 + chip / 2, cy1 + chip / 2), f"{pct}%", font=_font("sb", 15 * S),
           fill="#ffffff", anchor="mm")

    tx = cx1 + chip + 16 * S
    d.text((tx, 26 * S), title, font=_font("sb", 13 * S), fill=T["neutral"], anchor="lm")
    d.text((tx, 45 * S), subtitle, font=_font("reg", 11 * S), fill=T["dim"], anchor="lm")
    return base.resize((W, H), Image.LANCZOS)


def render_action_toast(title, subtitle, action_label, theme, scale=3):
    """A toast with a left play-icon and a right accent button (whole toast is
    clickable). Used for the resume-on-reset notification."""
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W, H = 348, 72
    Ws, Hs = W * S, H * S
    base = Image.new("RGB", (Ws, Hs), T["key"])
    grad = _vgrad(Ws, Hs, T["panel_top"], T["panel_bot"])
    mask = Image.new("L", (Ws, Hs), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=14 * S, fill=255)
    base.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=14 * S, outline=T["border"], width=max(1, S))
    accent = T["accent"]

    ix, iy, isz = 15 * S, 16 * S, 40 * S
    d.rounded_rectangle([ix, iy, ix + isz, iy + isz], radius=11 * S, fill=accent)
    cx, cy, tw = ix + isz / 2, iy + isz / 2, isz * 0.24
    d.polygon([(cx - tw * 0.65, cy - tw), (cx - tw * 0.65, cy + tw), (cx + tw, cy)], fill="#ffffff")

    f_btn = _font("sb", 11 * S)
    bw = d.textlength(action_label, font=f_btn) + 28 * S
    bx2, by1, by2 = Ws - 14 * S, (H / 2 - 14) * S, (H / 2 + 14) * S
    bx1 = bx2 - bw
    d.rounded_rectangle([bx1, by1, bx2, by2], radius=9 * S, fill=accent)
    d.text(((bx1 + bx2) / 2, (by1 + by2) / 2), action_label, font=f_btn, fill="#ffffff", anchor="mm")

    tx = ix + isz + 15 * S
    d.text((tx, 27 * S), title, font=_font("sb", 13 * S), fill=T["neutral"], anchor="lm")
    d.text((tx, 46 * S), subtitle, font=_font("reg", 11 * S), fill=T["dim"], anchor="lm")
    return base.resize((W, H), Image.LANCZOS)
