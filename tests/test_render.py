"""Hermetic pytest suite for render.py (pure Pillow, cross-platform).

These tests exercise the taskbar strip, the details popover (including its
click hit-map), the toast variants, and the settings-panel controls. They are
fully self-contained: no network, no credentials, no writes outside tmp. We
assert on return types and image .size rather than exact pixels, since the
exact rasterisation depends on the available system fonts.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

import render


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _in(secs):
    """A UTC datetime `secs` seconds from now (negative = in the past)."""
    return datetime.now(timezone.utc) + timedelta(seconds=secs)


def _is_image(obj):
    return isinstance(obj, Image.Image)


def _positive_size(img):
    w, h = img.size
    return w >= 1 and h >= 1


ALL_THEMES = ["light", "dark"]


# --------------------------------------------------------------------------- #
# _fmt_left
# --------------------------------------------------------------------------- #
def test_fmt_left_none_is_empty():
    assert render._fmt_left(None) == ""


def test_fmt_left_falsy_is_empty():
    # Guard clause is `if not dt`, so any falsy value returns "".
    assert render._fmt_left(0) == ""


def test_fmt_left_resetting_at_zero_or_past():
    # <= 0 seconds remaining reads as "resetting".
    assert render._fmt_left(_in(-5)) == "resetting"
    assert render._fmt_left(_in(-3600)) == "resetting"


def test_fmt_left_under_60s_is_lt_1m():
    # Explicitly not "0m left" — that would read as already reset.
    assert render._fmt_left(_in(30)) == "<1m left"


def test_fmt_left_minutes_only():
    out = render._fmt_left(_in(5 * 60 + 5))
    assert re.fullmatch(r"\d+m left", out), out
    assert "h" not in out


def test_fmt_left_hours_and_minutes():
    out = render._fmt_left(_in(2 * 3600 + 15 * 60 + 5))
    assert re.fullmatch(r"\d+h \d+m left", out), out
    assert out.startswith("2h")


def test_fmt_left_days_and_hours():
    out = render._fmt_left(_in(2 * 86400 + 3 * 3600 + 30))
    assert re.fullmatch(r"\d+d \d+h left", out), out
    assert out.startswith("2d")


# --------------------------------------------------------------------------- #
# _fmt_at
# --------------------------------------------------------------------------- #
def test_fmt_at_none_is_empty():
    assert render._fmt_at(None) == ""


def test_fmt_at_includes_a_date_and_time():
    out = render._fmt_at(_in(3 * 86400))
    assert out.startswith("resets ")
    # A bare weekday is ambiguous, so the format includes a numeric day...
    assert re.search(r"\d", out), out
    # ...and a clock time with AM/PM.
    assert re.search(r"\d+:\d\d [AP]M", out), out


def test_fmt_at_starts_with_weekday_abbrev():
    out = render._fmt_at(_in(86400))
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert any(wd in out for wd in weekdays), out


# --------------------------------------------------------------------------- #
# render_strip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
def test_strip_returns_image(theme):
    disp = {"session_pct": 42, "weekly_pct": 10,
            "session_color": "green", "weekly_color": "amber"}
    img = render.render_strip(disp, "#ffffff", theme)
    assert _is_image(img)
    assert _positive_size(img)


@pytest.mark.parametrize("theme", ALL_THEMES)
def test_strip_pct_zero(theme):
    disp = {"session_pct": 0, "weekly_pct": 0,
            "session_color": "green", "weekly_color": "green",
            "session_resets_at": _in(3600)}
    img = render.render_strip(disp, "#ffffff", theme)
    assert _is_image(img)
    assert _positive_size(img)


@pytest.mark.parametrize("theme", ALL_THEMES)
def test_strip_pct_100_limit_reached(theme):
    # At/over 100 the strip renders an explicit "limit reached" chip; must not raise.
    disp = {"session_pct": 100, "weekly_pct": 100,
            "session_color": "red", "weekly_color": "red"}
    img = render.render_strip(disp, "#ffffff", theme)
    assert _is_image(img)
    assert _positive_size(img)


def test_strip_missing_session_and_weekly_none():
    # No percentages at all -> falls back to a status group (Claude + session text).
    disp = {"session": None, "weekly_pct": None, "face_color": "grey"}
    img = render.render_strip(disp, "#ffffff", "light")
    assert _is_image(img)
    assert _positive_size(img)


def test_strip_status_offline_display():
    # A status/offline disp with no pct keys still renders (dot keyed off face_color).
    disp = {"session": "offline", "face_color": "red"}
    img = render.render_strip(disp, "#ffffff", "dark")
    assert _is_image(img)
    assert _positive_size(img)


def test_strip_empty_disp_does_not_raise():
    img = render.render_strip({}, "#ffffff", "light")
    assert _is_image(img)
    assert _positive_size(img)


def test_strip_height_is_scale_independent_of_content():
    # H = 30*S downscaled by S -> 30 regardless of theme/percentages.
    a = render.render_strip({"session_pct": 0}, "#fff", "light")
    b = render.render_strip({"session_pct": 100, "session_color": "red"}, "#fff", "light")
    assert a.size[1] == b.size[1] == 30


def test_strip_unknown_theme_falls_back_to_light():
    disp = {"session_pct": 50, "session_color": "green"}
    img = render.render_strip(disp, "#ffffff", "does-not-exist")
    assert _is_image(img)
    assert _positive_size(img)


def test_strip_session_only_metric():
    disp = {"session_pct": 77, "session_color": "amber",
            "session_resets_at": _in(90 * 60)}
    img = render.render_strip(disp, "#ffffff", "light", metrics=("session",))
    assert _is_image(img)
    assert _positive_size(img)


def test_strip_custom_scale():
    disp = {"session_pct": 20, "session_color": "green"}
    img = render.render_strip(disp, "#ffffff", "light", scale=2)
    assert _is_image(img)
    assert _positive_size(img)


# --------------------------------------------------------------------------- #
# render_popover
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
def test_popover_returns_image_and_hits(theme):
    out, hits = render.render_popover({"session_pct": 50, "weekly_pct": 33}, theme)
    assert _is_image(out)
    assert _positive_size(out)
    assert isinstance(hits, dict)


def test_popover_width_is_fixed():
    out, _ = render.render_popover({"session_pct": 10, "weekly_pct": 10}, "light")
    assert out.size[0] == 344


def test_popover_pct_zero_and_hundred_edges():
    out, hits = render.render_popover(
        {"session_pct": 0, "weekly_pct": 100,
         "session_color": "green", "weekly_color": "red",
         "session_resets_at": _in(3600), "weekly_resets_at": _in(5 * 86400)},
        "light",
    )
    assert _is_image(out)
    assert set(hits) == {"settings", "refresh", "quit"}


def test_popover_missing_session_weekly_none():
    out, hits = render.render_popover(
        {"session_pct": None, "weekly_pct": None}, "dark")
    assert _is_image(out)
    assert set(hits) == {"settings", "refresh", "quit"}


def test_popover_empty_disp_does_not_raise():
    out, hits = render.render_popover({}, "light")
    assert _is_image(out)
    assert set(hits) == {"settings", "refresh", "quit"}


def test_popover_hits_contains_expected_keys():
    _, hits = render.render_popover({"session_pct": 5, "weekly_pct": 5}, "light")
    for key in ("settings", "refresh", "quit"):
        assert key in hits


@pytest.mark.parametrize("key", ["settings", "refresh", "quit"])
def test_popover_hit_rectangles_are_sane(key):
    out, hits = render.render_popover({"session_pct": 5, "weekly_pct": 5}, "light")
    rect = hits[key]
    assert len(rect) == 4
    x1, y1, x2, y2 = rect
    # Non-inverted rectangle.
    assert x1 < x2
    assert y1 < y2
    # Fully inside the returned image.
    w, h = out.size
    assert 0 <= x1 <= w and 0 <= x2 <= w
    assert 0 <= y1 <= h and 0 <= y2 <= h


def test_popover_hits_do_not_horizontally_overlap():
    # Layout order left->right is settings, refresh, quit.
    _, hits = render.render_popover({"session_pct": 5, "weekly_pct": 5}, "light")
    assert hits["settings"][2] <= hits["refresh"][0] + 1
    assert hits["refresh"][2] <= hits["quit"][0] + 1


def test_popover_with_model_rows_and_cost():
    disp = {
        "session_pct": 50, "weekly_pct": 33,
        "session_color": "green", "weekly_color": "amber",
        "model_rows": [
            {"label": "Fable", "pct": 80, "color": "amber"},
            {"label": "Opus", "pct": 12, "color": "green"},
        ],
        "cost_usd": 1.23, "cost_tokens": 123456,
        "plan": "Plan: Max",
    }
    out, hits = render.render_popover(disp, "dark")
    assert _is_image(out)
    assert out.size[0] == 344
    # Extra rows + cost make it taller than the plain two-metric layout.
    plain, _ = render.render_popover({"session_pct": 50, "weekly_pct": 33}, "dark")
    assert out.size[1] > plain.size[1]
    assert set(hits) == {"settings", "refresh", "quit"}


def test_popover_long_plan_is_elided_without_raising():
    disp = {"session_pct": 5, "weekly_pct": 5,
            "plan": "Plan: " + "SuperLongPlanName" * 6}
    out, _ = render.render_popover(disp, "light")
    assert _is_image(out)
    assert out.size[0] == 344


def test_popover_unknown_theme_falls_back():
    out, hits = render.render_popover({"session_pct": 5, "weekly_pct": 5}, "nope")
    assert _is_image(out)
    assert set(hits) == {"settings", "refresh", "quit"}


# --------------------------------------------------------------------------- #
# render_toast / render_action_toast
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
def test_toast_returns_image(theme):
    img = render.render_toast(88, "Approaching limit", "Session at 88%", "amber", theme)
    assert _is_image(img)
    assert img.size == (322, 70)


def test_toast_pct_extremes():
    for pct in (0, 100):
        img = render.render_toast(pct, "T", "S", "red", "light")
        assert _is_image(img)
        assert img.size == (322, 70)


def test_toast_long_text_is_elided():
    img = render.render_toast(50, "X" * 200, "Y" * 200, "green", "dark")
    assert _is_image(img)
    assert img.size == (322, 70)


def test_toast_empty_strings():
    img = render.render_toast(0, "", "", "grey", "light")
    assert _is_image(img)
    assert img.size == (322, 70)


@pytest.mark.parametrize("theme", ALL_THEMES)
def test_action_toast_returns_image(theme):
    img = render.render_action_toast("Session reset", "You can resume", "Resume", theme)
    assert _is_image(img)
    assert img.size == (348, 72)


def test_action_toast_long_label_is_capped():
    img = render.render_action_toast("Title", "Subtitle", "Resume " * 20, "dark")
    assert _is_image(img)
    assert img.size == (348, 72)


def test_action_toast_empty_strings():
    img = render.render_action_toast("", "", "", "light")
    assert _is_image(img)
    assert img.size == (348, 72)


# --------------------------------------------------------------------------- #
# render_toggle
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
@pytest.mark.parametrize("on", [True, False])
def test_toggle_returns_image(theme, on):
    img = render.render_toggle(on, theme)
    assert _is_image(img)
    assert img.size == (46, 26)


def test_toggle_has_alpha():
    # Controls are composited over the panel, so they keep an alpha channel.
    img = render.render_toggle(True, "light")
    assert img.mode == "RGBA"


# --------------------------------------------------------------------------- #
# render_segment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
def test_segment_returns_image_and_width(theme):
    out, segw = render.render_segment(["Off", "On", "Auto"], 1, theme)
    assert _is_image(out)
    assert _positive_size(out)
    assert isinstance(segw, int)
    assert segw >= 1


def test_segment_width_matches_slices():
    labels = ["A", "BB", "CCC"]
    out, segw = render.render_segment(labels, 0, "light")
    # segwidth is the per-slice width: image width // number of labels.
    assert segw == out.width // len(labels)


def test_segment_single_label():
    out, segw = render.render_segment(["Only"], 0, "light")
    assert _is_image(out)
    assert segw == out.width  # one slice spans the whole control


def test_segment_empty_labels_does_not_raise():
    # Empty input is coerced to a single blank label (no div-by-zero / max()).
    out, segw = render.render_segment([], 0, "light")
    assert _is_image(out)
    assert _positive_size(out)
    assert segw >= 1


def test_segment_selection_out_of_range_is_harmless():
    # `sel` never matches any index -> no highlight, but must still render.
    out, segw = render.render_segment(["A", "B"], 99, "dark")
    assert _is_image(out)
    assert segw >= 1


def test_segment_selection_none_index():
    out, _ = render.render_segment(["A", "B", "C"], -1, "light")
    assert _is_image(out)


# --------------------------------------------------------------------------- #
# render_slider
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
@pytest.mark.parametrize("frac", [0.0, 0.5, 1.0])
def test_slider_returns_image(theme, frac):
    img = render.render_slider(frac, theme, 180)
    assert _is_image(img)
    assert img.size == (180, 24)


def test_slider_frac_clamped_below_zero():
    img = render.render_slider(-3.0, "light", 120)
    assert _is_image(img)
    assert img.size == (120, 24)


def test_slider_frac_clamped_above_one():
    img = render.render_slider(5.0, "dark", 120)
    assert _is_image(img)
    assert img.size == (120, 24)


def test_slider_tiny_width_does_not_raise():
    # Track is kept non-inverted even at width 1.
    img = render.render_slider(0.5, "light", 1)
    assert _is_image(img)
    assert img.size == (1, 24)


def test_slider_width_two():
    img = render.render_slider(0.0, "dark", 2)
    assert _is_image(img)
    assert img.size == (2, 24)


# --------------------------------------------------------------------------- #
# render_stepper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ALL_THEMES)
def test_stepper_returns_image(theme):
    img = render.render_stepper(5, theme)
    assert _is_image(img)
    assert img.size == (94, 26)


def test_stepper_custom_width():
    img = render.render_stepper(12, "light", width=140)
    assert _is_image(img)
    assert img.size == (140, 26)


def test_stepper_tiny_width_does_not_raise():
    img = render.render_stepper(999, "light", width=2)
    assert _is_image(img)
    assert img.size == (2, 26)


def test_stepper_string_value():
    img = render.render_stepper("Auto", "dark")
    assert _is_image(img)
    assert img.size == (94, 26)


def test_stepper_long_value_is_elided():
    img = render.render_stepper("123456789", "light", width=40)
    assert _is_image(img)
    assert img.size == (40, 26)


# --------------------------------------------------------------------------- #
# Sanity: palettes and severity colour lookup
# --------------------------------------------------------------------------- #
def test_themes_are_present():
    assert "light" in render.THEMES
    assert "dark" in render.THEMES


def test_sev_color_falls_back_to_dim_for_unknown():
    T = render.THEMES["light"]
    assert render.sev_color(T, "totally-unknown") == T["dim"]
    assert render.sev_color(T, "green") == T["green"]
