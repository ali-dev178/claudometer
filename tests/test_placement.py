"""Popover placement math (multi-monitor + auto up/down).

Regression coverage for the bug where the details popover jumped to the primary
monitor and only ever opened upward. `_popover_xy` is pure arithmetic, so these
run headlessly (no Tk / no real monitors needed).
"""

from widget_bar import _popover_xy

# A monitor's work area (left, top, right, bottom). rcWork excludes the taskbar.
PRIMARY = (0, 0, 1920, 1032)
RIGHT = (1920, 0, 3840, 1040)      # second monitor to the right
LEFT = (-1920, 0, 0, 1040)         # second monitor to the left (negative x)

W, H = 344, 222                    # a typical popover size


def test_opens_above_when_room():
    # Strip docked near the bottom -> popover opens upward, just above it.
    px, py = _popover_xy(600, 980, 1020, W, H, PRIMARY)
    assert py == 980 - H - 8
    assert px == 600


def test_drops_below_when_top_is_tight():
    # Strip near the top edge -> not enough room above -> open below the strip.
    px, py = _popover_xy(600, 10, 48, W, H, PRIMARY)
    assert py == 48 + 8
    assert px == 600


def test_stays_on_right_monitor():
    # Anchor on the right monitor must NOT be clamped back onto the primary.
    px, py = _popover_xy(2200, 900, 938, W, H, RIGHT)
    assert RIGHT[0] + 8 <= px <= RIGHT[2] - W - 8
    assert py == 900 - H - 8


def test_stays_on_left_monitor_negative_coords():
    # A monitor to the left uses negative x; the popover must land there.
    px, py = _popover_xy(-1500, 900, 938, W, H, LEFT)
    assert LEFT[0] + 8 <= px <= LEFT[2] - W - 8
    assert px == -1500


def test_clamps_inside_right_edge():
    # Strip flush to the right edge -> popover shifts left to fit the work area.
    px, py = _popover_xy(3838, 500, 538, W, H, RIGHT)
    assert px == RIGHT[2] - W - 8


def test_clamps_inside_left_edge():
    # Strip flush to the left edge of a left monitor -> clamp to left + 8.
    px, py = _popover_xy(-1919, 500, 538, W, H, LEFT)
    assert px == LEFT[0] + 8


def test_below_then_clamped_up_when_bottom_is_tight():
    # Short work area, strip at the top: open below, then clamp up so the
    # popover never runs past the bottom edge (the popover still fits).
    short = (0, 0, 1920, 260)            # 260 tall; popover is 222
    px, py = _popover_xy(600, 10, 48, W, H, short)
    assert py + H <= short[3] - 8        # fully inside the work area bottom
    assert py == max(short[1] + 8, short[3] - H - 8)


def test_fmt_age_buckets():
    from widget_bar import Popover
    assert Popover._fmt_age(0) == "just now"
    assert Popover._fmt_age(4) == "just now"
    assert Popover._fmt_age(5) == "5s ago"
    assert Popover._fmt_age(42) == "42s ago"
    assert Popover._fmt_age(60) == "1m ago"
    assert Popover._fmt_age(125) == "2m ago"
    assert Popover._fmt_age(-3) == "just now"  # clamps negatives
