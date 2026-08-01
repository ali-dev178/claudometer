"""High-quality Pillow rendering for the taskbar readout and details popover.

Everything is drawn supersampled (scale x3) and downscaled with LANCZOS for
crisp anti-aliasing — rounded gradient card, sleek horizontal usage meters,
badges, and refined typography. tkinter's canvas can't do this natively, so the
UI is composed as images and shown through ImageTk.
"""

import math
import os
from datetime import datetime, timezone

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------- #
# Palettes
# --------------------------------------------------------------------------- #
THEMES = {
    "light": {
        "key": "#f3f5f8",
        "panel_top": "#ffffff", "panel_bot": "#f5f8fc",
        "border": "#e5e9ef", "hair": "#eef1f5",
        "neutral": "#161b22", "dim": "#6b7480", "faint": "#98a1ad", "grey": "#6b7480",
        "track": "#e9edf2", "toggle_off": "#c4ccd6",
        "accent": "#d97757", "accent_soft": "#f8ece5",
        "green": "#12a150", "amber": "#d99400", "red": "#e5484d",
    },
    "dark": {
        "key": "#0b0e14",
        "panel_top": "#191f2b", "panel_bot": "#12161f",
        "border": "#2a3342", "hair": "#222a37",
        "neutral": "#e9edf4", "dim": "#9aa4b3", "faint": "#69727e", "grey": "#9aa4b3",
        "track": "#262f3d", "toggle_off": "#3b4552",
        "accent": "#e58e6f", "accent_soft": "#2d241f",
        "green": "#2ec26a", "amber": "#e2a53c", "red": "#ff6b6e",
    },
}


def sev_color(T, name):
    return T.get(name, T["dim"])


# --------------------------------------------------------------------------- #
# Keeping the strip legible on whatever it is sitting on
# --------------------------------------------------------------------------- #
#: WCAG AA for text this size. The dots are shapes, not text, and take 3.0.
TEXT_CONTRAST = 4.5
SHAPE_CONTRAST = 3.0


def relative_luminance(rgb):
    """WCAG relative luminance, for judging contrast."""
    out = []
    for channel in rgb[:3]:
        c = min(max(channel, 0), 255) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast_ratio(rgb_a, rgb_b):
    """WCAG contrast ratio between two colours (1.0 … 21.0)."""
    a, b = relative_luminance(rgb_a), relative_luminance(rgb_b)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


def _rgb(color):
    return ImageColor.getrgb(color)[:3] if isinstance(color, str) else tuple(color[:3])


def outline_for(bg):
    """Black or white — whichever separates a shape from *bg*.

    Used instead of recolouring anything whose COLOUR is the message. Red,
    amber and green mean blocked, working and done; pushing them until they
    contrast turns all three into the same near-black on a grey taskbar and
    the same white on a teal one, which is a worse failure than being hard to
    see. A hairline outline makes the shape visible and leaves the fill alone.
    """
    back = _rgb(bg)
    return "#ffffff" if contrast_ratio(back, (255, 255, 255)) >= \
        contrast_ratio(back, (0, 0, 0)) else "#000000"


def readable(color, bg, target=TEXT_CONTRAST):
    """*color*, lightened or darkened just enough to be legible on *bg*.

    The strip paints itself in the colour of whatever it sits on, so it can
    end up on a saturated wallpaper where the theme's own greys all but
    vanish — the labels measured 1.2:1 on mid grey, which is not dim, it is
    absent. Picking the better of the two themes is not enough on its own.

    The fix is applied to the TEXT, never the background: the strip blends
    into the taskbar on purpose, and a plate behind the words would undo the
    one thing it is trying to do.

    Hue is preserved by moving straight toward white or black, so a red still
    reads as red — which matters, because the severity colours carry meaning.
    """
    rgb, back = _rgb(color), _rgb(bg)
    if contrast_ratio(rgb, back) >= target:
        return color
    # Toward whichever end the background is furthest from; on a mid grey both
    # are poor, so take the better one and go as far as it allows.
    end = (255, 255, 255) if contrast_ratio(back, (255, 255, 255)) >= \
        contrast_ratio(back, (0, 0, 0)) else (0, 0, 0)
    best = rgb
    for step in range(1, 21):
        t = step / 20.0
        cand = tuple(round(rgb[i] + (end[i] - rgb[i]) * t) for i in range(3))
        best = cand
        if contrast_ratio(cand, back) >= target:
            break
    return "#%02x%02x%02x" % best


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
_FCACHE = {}
_FONT_FILES = {
    "reg": "segoeui.ttf", "sb": "segoeuisb.ttf",
    "bold": "segoeuib.ttf", "light": "segoeuil.ttf",
}


_MAC_FONTS = {  # macOS system fonts (Helvetica Neue is reliably present)
    "sb": "/System/Library/Fonts/HelveticaNeue.ttc",
    "bold": "/System/Library/Fonts/HelveticaNeue.ttc",
    "light": "/System/Library/Fonts/HelveticaNeue.ttc",
}

#: HelveticaNeue.ttc is a COLLECTION: every weight is one file, and asking for
#: it gives you face 0 — Regular — whichever weight you wanted. Semibold
#: headings, bold numbers and light captions all came out identical on macOS.
#: Faces are chosen by name rather than index because the order inside the
#: collection is not guaranteed across macOS versions.
_MAC_FACES = {
    "sb": ("HelveticaNeue-Medium", "HelveticaNeue-Bold"),
    "bold": ("HelveticaNeue-Bold",),
    "light": ("HelveticaNeue-Light", "HelveticaNeue-Thin"),
}


def _mac_face(path, kind, size):
    """Pick the right face out of a .ttc, or None to fall through."""
    wanted = _MAC_FACES.get(kind)
    if not wanted or not path.endswith(".ttc"):
        return None
    for index in range(12):          # collections are small; 12 is generous
        try:
            face = ImageFont.truetype(path, size, index=index)
        except Exception:
            break
        try:
            name = "-".join(p for p in face.getname() if p).replace(" ", "")
        except Exception:
            continue
        if any(w.replace(" ", "") in name for w in wanted):
            return face
    return None


def _font(kind, size):
    size = int(size)
    k = (kind, size)
    if k in _FCACHE:
        return _FCACHE[k]
    win = _FONT_FILES.get(kind, "segoeui.ttf")
    candidates = (
        "C:/Windows/Fonts/" + win,                                  # Windows (preferred)
        "C:/Windows/Fonts/segoeui.ttf",                             # Windows regular fallback
        _MAC_FONTS.get(kind, "/System/Library/Fonts/HelveticaNeue.ttc"),  # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",          # Linux
        "C:/Windows/Fonts/arial.ttf", win, "arial.ttf",            # last resort
    )
    f = None
    for path in candidates:
        try:
            f = _mac_face(path, kind, size) or ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size)  # Pillow >=10.1: a sized FreeType font
        except TypeError:                     # older Pillow: legacy bitmap font
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
    if secs < 60:
        return "<1m left"  # not "0m left" — that reads as already reset
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
    df = "%#d" if os.name == "nt" else "%-d"
    # Include the date — a bare weekday is ambiguous up to a week out.
    return loc.strftime(f"resets %a %b {df}, {hf}:%M %p")


def _fmt_tokens(n):
    n = int(n or 0)
    if n < 0:
        n = 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _elide(draw, text, font, max_w):
    """Truncate text with an ellipsis so it fits within max_w pixels."""
    text = str(text)
    if max_w <= 0:
        return ""  # fail closed: no room at all
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    if text:
        return text + "…"
    return "…" if draw.textlength("…", font=font) <= max_w else ""


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


def _gear(d, cx, cy, r, color, w):
    """A small settings gear icon (ring + teeth + hub)."""
    dirs = [(1, 0), (0.707, 0.707), (0, 1), (-0.707, 0.707),
            (-1, 0), (-0.707, -0.707), (0, -1), (0.707, -0.707)]
    for dx, dy in dirs:
        d.line([cx + dx * r * 0.72, cy + dy * r * 0.72,
                cx + dx * r * 1.28, cy + dy * r * 1.28], fill=color, width=w)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
    d.ellipse([cx - r * 0.34, cy - r * 0.34, cx + r * 0.34, cy + r * 0.34], fill=color)


def _refresh_icon(d, cx, cy, r, color, w):
    """A two-arrow 'sync/reload' icon: two opposing arcs, each arrow-tipped."""
    box = [cx - r, cy - r, cx + r, cy + r]

    def head(ang, sweep=34):
        a = math.radians(ang)
        tip = a + math.radians(sweep)
        tx, ty = cx + r * math.cos(tip), cy + r * math.sin(tip)
        ix, iy = cx + (r - w * 1.7) * math.cos(a), cy + (r - w * 1.7) * math.sin(a)
        ox, oy = cx + (r + w * 1.7) * math.cos(a), cy + (r + w * 1.7) * math.sin(a)
        d.polygon([(tx, ty), (ix, iy), (ox, oy)], fill=color)

    d.arc(box, 20, 150, fill=color, width=w)
    d.arc(box, 200, 330, fill=color, width=w)
    head(20)
    head(200)


def _rrbar(d, x1, y1, x2, y2, pct, color, track):
    r = (y2 - y1) / 2
    d.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=track)
    fw = (x2 - x1) * min(max(pct or 0, 0), 100) / 100.0
    if fw > 0:
        d.rounded_rectangle([x1, y1, min(x2, x1 + max(fw, 2 * r)), y2], radius=r, fill=color)


def _big_pct(d, x, ymid, pct, fbig, fpct, color, dim):
    """Large percentage value with a smaller raised percent sign."""
    if pct is None:
        d.text((x, ymid), "—", font=fbig, fill=dim, anchor="lm")
        return
    num = str(pct)
    nw = d.textlength(num, font=fbig)
    d.text((x, ymid), num, font=fbig, fill=color, anchor="lm")
    d.text((x + nw + 2, ymid - getattr(fbig, "size", 20) * 0.17), "%", font=fpct,
           fill=color, anchor="lm")


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

    # Each item is (text, font, colour, carries_meaning). The flag decides how
    # an illegible colour is rescued: a label can simply be recoloured, but a
    # severity colour has to keep its hue and gets an outline instead.
    groups = []
    if "session" in metrics and disp.get("session_pct") is not None:
        sp = disp["session_pct"]
        g = [("Session ", f_lbl, T["dim"], False),
             (f"{sp}%", f_num, sev_color(T, disp.get("session_color", "grey")),
              True)]
        if sp >= 100:  # be explicit when you're actually blocked
            g.append(("   limit reached", f_dim, sev_color(T, "red"), True))
        else:
            left = _fmt_left(disp.get("session_resets_at"))
            if left:
                g.append(("   " + left, f_dim, T["faint"], False))
        groups.append(g)
    if "weekly" in metrics and disp.get("weekly_pct") is not None:
        groups.append([
            ("Weekly ", f_lbl, T["dim"], False),
            (f"{disp['weekly_pct']}%", f_num,
             sev_color(T, disp.get("weekly_color", "grey")), True)])
    # NOTE: sessions_* (plural) are the LIVE CLI sessions; session_* (singular)
    # is the 5-hour usage meter. The two dicts are merged before rendering.
    if "sessions" in metrics and disp.get("sessions_count") is not None:
        # "Live", not "Sessions": the strip already says "Session 61%" for the
        # 5-hour usage window, and the two side by side read as the same thing.
        g = [("Live ", f_lbl, T["dim"], False)]
        dots = disp.get("sessions_dots") or []
        if dots:
            # One dot per session, coloured by its state — readable without
            # reading. Blocked sessions sort first, so red leads.
            f_dot = _font("reg", 13 * S)
            for i, color in enumerate(dots):
                g.append(("●" + (" " if i < len(dots) - 1 else ""),
                          f_dot, sev_color(T, color), True))
            over = disp.get("sessions_dots_overflow") or 0
            if over:
                g.append((f" +{over}", f_dim, T["dim"], False))
        else:
            g.append(("0", f_num, T["dim"], False))
        groups.append(g)
    if not groups:
        groups.append([("Claude  " + (disp.get("session") or "—"), f_dim,
                        T["dim"], False)])
    if disp.get("_demo"):  # unmistakable marker so simulated data isn't mistaken for real
        groups.insert(0, [("DEMO", f_num, sev_color(T, "amber"), True)])

    cand = []
    if disp.get("session_pct") is not None:
        cand.append((disp["session_pct"], disp.get("session_color", "grey")))
    if disp.get("weekly_pct") is not None:
        cand.append((disp["weekly_pct"], disp.get("weekly_color", "grey")))
    if cand:
        dot_color = sev_color(T, max(cand, key=lambda c: c[0])[1])
    else:  # status state (no percentages) — key the dot off face_color so a
        dot_color = sev_color(T, disp.get("face_color", "grey"))  # 429 shows red, not grey
    # A session blocked on you outranks usage severity: usage is something to
    # pace, a blocked session is something to go and do.
    if "sessions" in metrics and (disp.get("sessions_blocked") or 0):
        dot_color = sev_color(T, "red")


    tmp = ImageDraw.Draw(Image.new("RGB", (4, 4)))

    def gw(g):
        return sum(tmp.textlength(t, font=f) for (t, f, _c, _m) in g)

    total = PAD + (DOT_R * 2 + DOTGAP) + sum(gw(g) for g in groups) \
        + GGAP * (len(groups) - 1) + PAD
    img = Image.new("RGB", (int(total), H), bg_hex)
    d = ImageDraw.Draw(img)
    cy = H / 2

    dx = PAD + DOT_R
    # The attention pulse swells the status dot only. The strip's background is
    # sampled from whatever sits behind it so the widget blends in — repainting
    # that would flash the entire strip, which is not what this is for.
    # The strip paints itself in whatever it sits on, so on a saturated
    # wallpaper the theme's own greys all but vanish — measured at 1.2:1,
    # which is not dim, it is absent. Fixed on the TEXT, never the background:
    # blending into the taskbar is the point of the strip.
    ring = outline_for(bg_hex)
    faint_dot = contrast_ratio(_rgb(dot_color), _rgb(bg_hex)) < SHAPE_CONTRAST
    r = DOT_R * (1.7 if disp.get("_pulse") else 1.0)
    d.ellipse([dx - r, cy - r, dx + r, cy + r], fill=dot_color,
              outline=ring if faint_dot else None,
              width=max(1, S // 2) if faint_dot else 0)
    x = PAD + DOT_R * 2 + DOTGAP
    for gi, g in enumerate(groups):
        if gi > 0:
            x += GGAP
        for (t, f, c, means) in g:
            # Every glyph passes through here, the one place that knows both
            # the colour and what it will sit on.
            if not means:
                d.text((x, cy), t, font=f, fill=readable(c, bg_hex),
                       anchor="lm")
            elif contrast_ratio(_rgb(c), _rgb(bg_hex)) < SHAPE_CONTRAST:
                # Keep the hue — it is what red, amber and green MEAN — and
                # separate it from the background with a hairline instead.
                # Scaled with S, because the strip is drawn at S× and then
                # downsampled: a flat 1px stroke here would come out a third
                # of a pixel and vanish in the resize. Kept under a full pixel
                # once resolved — enough to separate the glyph, not so much
                # that you read the outline instead of the number.
                d.text((x, cy), t, font=f, fill=c, anchor="lm",
                       stroke_width=max(1, round(S * 0.7)), stroke_fill=ring)
            else:
                d.text((x, cy), t, font=f, fill=c, anchor="lm")
            x += tmp.textlength(t, font=f)

    return img.resize((max(1, round(total / S)), round(H / S)), Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Details popover — horizontal usage meters
# --------------------------------------------------------------------------- #
#: Vertical pitch of one live-session row in the popover.
SESSION_ROW_H = 24


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
        content_bottom = cost_y
        base_gap = 14
    else:
        cost_y = None

    # Live Claude Code sessions. Present only when the feature is on; an empty
    # list still renders (as a single "none running" line) so the section
    # doesn't blink in and out as sessions start and stop.
    sess_rows = disp.get("sessions_rows")
    show_sessions = sess_rows is not None
    sess_overflow = int(disp.get("sessions_overflow") or 0)
    if show_sessions:
        sess_div = content_bottom + base_gap
        sess_hdr = sess_div + 20
        sess_top = sess_hdr + 22
        n_lines = max(1, len(sess_rows)) + (1 if sess_overflow else 0)
        content_bottom = sess_top + (n_lines - 1) * SESSION_ROW_H
        base_gap = 16
    else:
        sess_div = sess_hdr = sess_top = None

    foot_div = content_bottom + (13 if has_cost and not show_sessions else base_gap)
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
    title_right = P + 24 * S + d.textlength("Claudometer", font=f_title)
    max_plan_w = (Ws - P) - title_right - 14 * S - 18 * S  # gap + badge padding
    plan = _elide(d, plan, f_badge, max(max_plan_w, 24 * S))
    _badge_right(d, Ws - P, 28 * S, plan, f_badge, T, S)

    bar_x1, bar_x2 = P + 60 * S, Ws - P

    def metric(y_lbl, y_val, label, reset, pct, color_name):
        d.text((P, y_lbl * S), label, font=f_lbl, fill=T["dim"], anchor="lm")
        at_limit = pct is not None and pct >= 100
        if at_limit:  # make the blocked state unmistakable
            reset = "limit reached" + (" · " + reset if reset else "")
        if reset:
            d.text((Ws - P, y_lbl * S), reset, font=f_reset,
                   fill=T["red"] if at_limit else T["faint"], anchor="rm")
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
            r_lbl = row.get("label", "")
            r_pct = int(row.get("pct") or 0)
            r_col = row.get("color", "grey")
            d.text((P, y), _elide(d, r_lbl, f_row, 58 * S), font=f_row,
                   fill=T["neutral"], anchor="lm")
            hh = 7 * S
            _rrbar(d, P + 66 * S, y - hh / 2, Ws - P - 46 * S, y + hh / 2,
                   r_pct, sev(r_col), T["track"])
            d.text((Ws - P, y), f"{r_pct}%", font=f_rownum, fill=sev(r_col), anchor="rm")
            y += 28 * S

    # estimated cost (optional)
    if has_cost:
        d.text((P, cost_y * S), "Today", font=f_lbl, fill=T["dim"], anchor="lm")
        tok = _fmt_tokens(disp.get("cost_tokens") or 0)
        usd = _fmt_usd(disp.get("cost_usd") or 0.0)
        cw = (Ws - P) - (P + d.textlength("Today", font=f_lbl)) - 16 * S
        d.text((Ws - P, cost_y * S), _elide(d, f"{tok} tokens   ·   ~{usd}", f_rownum, cw),
               font=f_rownum, fill=T["neutral"], anchor="rm")

    # live Claude Code sessions
    if show_sessions:
        d.line([P, sess_div * S, Ws - P, sess_div * S], fill=T["hair"],
               width=max(1, S))
        d.text((P, sess_hdr * S), "Live sessions", font=f_lbl, fill=T["dim"],
               anchor="lm")
        summary = disp.get("sessions_summary") or ""
        if summary:
            sum_max = ((Ws - P) - (P + d.textlength("Live sessions", font=f_lbl))
                       - 16 * S)
            d.text((Ws - P, sess_hdr * S), _elide(d, summary, f_reset, sum_max),
                   font=f_reset, fill=T["faint"], anchor="rm")

        dot_r = 3.5 * S
        label_x = P + dot_r * 2 + 9 * S
        y = sess_top * S
        sess_hits = []      # one clickable rect per row, in display pixels
        if not sess_rows:
            d.text((P, y), disp.get("sessions_empty") or "No sessions running",
                   font=f_row, fill=T["faint"], anchor="lm")
        for row in sess_rows:
            col = sev(row.get("color", "grey"))
            d.ellipse([P, y - dot_r, P + dot_r * 2, y + dot_r], fill=col)
            detail = row.get("detail", "")
            detail_w = d.textlength(detail, font=f_reset)
            avail = (Ws - P) - label_x - detail_w - 14 * S
            proj = row.get("project") or ""
            proj_txt = f"  {proj}" if proj else ""
            # The project is context, not the headline. Show it only when it
            # fits WHOLE — a stub like "medborgarpr…" costs the title width and
            # tells you nothing, so in that case drop it and let the title,
            # which is what you actually scan for, use the space.
            proj_w = d.textlength(proj_txt, font=f_reset) if proj_txt else 0
            if proj_w > avail * 0.40:
                proj_txt, proj_w = "", 0
            lbl = _elide(d, row.get("label", ""), f_row,
                         max(avail - proj_w, 30 * S))
            d.text((label_x, y), lbl, font=f_row, fill=T["neutral"], anchor="lm")
            if proj_txt:
                d.text((label_x + d.textlength(lbl, font=f_row), y), proj_txt,
                       font=f_reset, fill=T["faint"], anchor="lm")
            d.text((Ws - P, y), detail, font=f_reset, fill=col, anchor="rm")
            half = (SESSION_ROW_H * S) / 2
            sess_hits.append((P / S, (y - half) / S, (Ws - P) / S, (y + half) / S))
            y += SESSION_ROW_H * S
        if sess_overflow:
            d.text((label_x, y), f"+{sess_overflow} more", font=f_reset,
                   fill=T["faint"], anchor="lm")

    # footer
    d.line([P, foot_div * S, Ws - P, foot_div * S], fill=T["hair"], width=max(1, S))
    fy = foot_y * S
    # Left status: normally "Auto-updating"; a manual Refresh briefly shows
    # "Refreshing…" then "Updated just now" so the click is never invisible.
    foot = disp.get("foot") or {}
    foot_text = foot.get("text", "Auto-updating")
    dot_col = T.get(foot.get("dot", "green"), T["green"])
    d.ellipse([P, fy - 3 * S, P + 6 * S, fy + 3 * S], fill=dot_col)
    d.text((P + 13 * S, fy), foot_text, font=f_foot, fill=T["faint"], anchor="lm")
    quit_w = d.textlength("Quit", font=f_foot)
    qx2, qx1 = Ws - P, Ws - P - quit_w
    d.text((qx2, fy), "Quit", font=f_foot, fill=T["dim"], anchor="rm")
    # Refresh — a circular reload icon (replaces the old "Refresh" label),
    # left of Quit, in the accent color so it reads as the primary action.
    rr = 5 * S
    rcx = qx1 - 20 * S - rr
    _refresh_icon(d, rcx, fy, rr, T["accent"], max(1, int(1.4 * S)))
    rx1, rx2 = rcx - rr, rcx + rr
    # Settings (gear + label), left of the refresh icon
    set_w = d.textlength("Settings", font=f_foot)
    sx2 = rx1 - 18 * S
    sx1 = sx2 - set_w
    d.text((sx2, fy), "Settings", font=f_foot, fill=T["dim"], anchor="rm")
    gr = 5 * S
    gx = sx1 - 7 * S - gr
    _gear(d, gx, fy, gr, T["dim"], max(1, int(1.4 * S)))

    out = base.resize((W, H), Image.LANCZOS)
    hits = {
        "settings": ((gx - gr * 1.3) / S, (fy - 12 * S) / S, sx2 / S, (fy + 12 * S) / S),
        "refresh": ((rx1 - 4 * S) / S, (fy - 12 * S) / S, (rx2 + 4 * S) / S, (fy + 12 * S) / S),
        "quit": (qx1 / S, (fy - 12 * S) / S, qx2 / S, (fy + 12 * S) / S),
    }
    if show_sessions:
        # "session:<row index>" — the caller maps the index back to the row it
        # passed in, so the renderer never has to know what a session is.
        for index, rect in enumerate(sess_hits):
            hits["session:%d" % index] = rect
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
    ccx, ccy = cx1 + chip / 2, cy1 + chip / 2
    if pct is None:
        # Live-session alerts have no percentage; a white status dot reads as a
        # light and keeps the toast geometry identical to the usage alerts.
        dr = 7 * S
        d.ellipse([ccx - dr, ccy - dr, ccx + dr, ccy + dr], fill="#ffffff")
    else:
        d.text((ccx, ccy), f"{pct}%", font=_font("sb", 15 * S),
               fill="#ffffff", anchor="mm")

    tx = cx1 + chip + 16 * S
    avail = max(0, W * S - tx - 14 * S)
    ft, fs = _font("sb", 13 * S), _font("reg", 11 * S)
    d.text((tx, 26 * S), _elide(d, title, ft, avail), font=ft, fill=T["neutral"], anchor="lm")
    d.text((tx, 45 * S), _elide(d, subtitle, fs, avail), font=fs, fill=T["dim"], anchor="lm")
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
    action_label = _elide(d, action_label, f_btn, int(W * S * 0.4) - 28 * S)  # cap button
    bw = d.textlength(action_label, font=f_btn) + 28 * S
    bx2, by1, by2 = Ws - 14 * S, (H / 2 - 14) * S, (H / 2 + 14) * S
    bx1 = bx2 - bw
    d.rounded_rectangle([bx1, by1, bx2, by2], radius=9 * S, fill=accent)
    d.text(((bx1 + bx2) / 2, (by1 + by2) / 2), action_label, font=f_btn, fill="#ffffff", anchor="mm")

    tx = ix + isz + 15 * S
    avail = max(0, bx1 - tx - 10 * S)
    ft, fs = _font("sb", 13 * S), _font("reg", 11 * S)
    d.text((tx, 27 * S), _elide(d, title, ft, avail), font=ft, fill=T["neutral"], anchor="lm")
    d.text((tx, 46 * S), _elide(d, subtitle, fs, avail), font=fs, fill=T["dim"], anchor="lm")
    return base.resize((W, H), Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Premium settings-panel controls (drawn supersampled, like everything else)
# --------------------------------------------------------------------------- #
def _soft_disc(size, cx, cy, r, S, alpha=60):
    """A blurred shadow disc for knobs."""
    sh = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([cx - r, cy - r + S, cx + r, cy + r + S], fill=(0, 0, 0, alpha))
    return sh.filter(ImageFilter.GaussianBlur(2 * S))


def render_toggle(on, theme, scale=3):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W, H = 46, 26
    Ws, Hs = W * S, H * S
    img = Image.new("RGBA", (Ws, Hs), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = Hs / 2
    d.rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=r,
                        fill=T["accent"] if on else T.get("toggle_off", T["track"]))
    kr = r - 3 * S
    kcx = (Ws - r) if on else r
    img.alpha_composite(_soft_disc((Ws, Hs), kcx, r, kr, S))
    ImageDraw.Draw(img).ellipse([kcx - kr, r - kr, kcx + kr, r + kr], fill="#ffffff")
    return img.resize((W, H), Image.LANCZOS)


def render_segment(labels, sel, theme, scale=3):
    """A pill segmented control; returns (image, segment_width_px)."""
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    labels = list(labels) or [""]  # never divide by / max() over an empty list
    f = _font("sb", 11 * S)
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    segw = int(max(scratch.textlength(l, font=f) for l in labels)) + 22 * S
    H = 26
    Ws, Hs = segw * len(labels), H * S
    img = Image.new("RGBA", (Ws, Hs), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=Hs / 2, fill=T["track"])
    for i, label in enumerate(labels):
        x1 = i * segw
        if i == sel:
            m = 2 * S
            d.rounded_rectangle([x1 + m, m, x1 + segw - m, Hs - m],
                                radius=(Hs - 2 * m) / 2, fill=T["accent"])
        d.text((x1 + segw / 2, Hs / 2), label, font=f,
               fill="#ffffff" if i == sel else T["dim"], anchor="mm")
    out = img.resize((Ws // S, H), Image.LANCZOS)
    return out, out.width // len(labels)


def render_slider(frac, theme, width, scale=3):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W, H = width, 24
    Ws, Hs = W * S, H * S
    img = Image.new("RGBA", (Ws, Hs), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    th, m = 6 * S, 10 * S
    y1, y2 = (Hs - th) / 2, (Hs + th) / 2
    x1, x2 = m, max(m + 1, Ws - m)  # keep the track non-inverted at tiny widths
    frac = min(max(frac, 0.0), 1.0)
    kx = x1 + (x2 - x1) * frac
    d.rounded_rectangle([x1, y1, x2, y2], radius=th / 2, fill=T.get("toggle_off", T["track"]))
    if kx > x1:
        d.rounded_rectangle([x1, y1, kx, y2], radius=th / 2, fill=T["accent"])
    kr = 9 * S
    img.alpha_composite(_soft_disc((Ws, Hs), kx, Hs / 2, kr, S, alpha=55))
    d = ImageDraw.Draw(img)
    d.ellipse([kx - kr, Hs / 2 - kr, kx + kr, Hs / 2 + kr], fill="#ffffff")
    d.ellipse([kx - kr, Hs / 2 - kr, kx + kr, Hs / 2 + kr], outline=T["accent"], width=2 * S)
    return img.resize((W, H), Image.LANCZOS)


def render_stepper(value, theme, scale=3, width=94):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W, H = width, 26
    Ws, Hs = W * S, H * S
    img = Image.new("RGBA", (Ws, Hs), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, Ws - 1, Hs - 1], radius=7 * S, fill=T["track"])
    third = Ws / 3
    for gx in (third, 2 * third):
        d.line([gx, 6 * S, gx, Hs - 6 * S], fill=T["hair"], width=max(1, S))
    fsym, fval = _font("sb", 15 * S), _font("sb", 12 * S)
    d.text((third / 2, Hs / 2 - S), "−", font=fsym, fill=T["dim"], anchor="mm")
    d.text((Ws / 2, Hs / 2), _elide(d, str(value), fval, third - 6 * S),
           font=fval, fill=T["neutral"], anchor="mm")
    d.text((2.5 * third, Hs / 2 - S), "+", font=fsym, fill=T["accent"], anchor="mm")
    return img.resize((W, H), Image.LANCZOS)


def render_settings_header(theme, width, scale=3):
    T = THEMES.get(theme, THEMES["light"])
    S = scale
    W, H = width, 62
    Ws, Hs = W * S, H * S
    base = _vgrad(Ws, Hs, T["panel_top"], T["panel_bot"]).convert("RGBA")
    base.alpha_composite(_radial(Ws, Hs, int(22 * S), int(24 * S), int(150 * S),
                                 ImageColor.getrgb(T["accent"]), 46))
    d = ImageDraw.Draw(base)
    P = 22 * S
    _spark(d, P + 6 * S, 26 * S, 7 * S, T["accent"])
    d.text((P + 22 * S, 26 * S), "Settings", font=_font("sb", 16 * S),
           fill=T["neutral"], anchor="lm")
    d.text((P, 46 * S), "Applies immediately · saved to ~/.claudometer.toml",
           font=_font("reg", 9 * S), fill=T["dim"], anchor="lm")
    d.line([0, Hs - 1, Ws, Hs - 1], fill=T["hair"], width=max(1, S))
    return base.resize((W, H), Image.LANCZOS)


def _radial(w, h, cx, cy, radius, color, max_alpha):
    g = Image.new("L", (w, h), 0)
    ImageDraw.Draw(g).ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=max_alpha)
    g = g.filter(ImageFilter.GaussianBlur(radius * 0.5))
    col = Image.new("RGBA", (w, h), color + (0,))
    col.putalpha(g)
    return col
