"""Hermetic unit tests for usage_core.

These tests build Usage/Limit dataclasses directly (or feed dicts to
parse_usage) and never touch the network, real credentials, or the real
filesystem outside pytest's tmp_path. Everything under test here is pure
formatting / parsing logic.

Run from the repo root: ``pytest test_usage_core.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import usage_core
from usage_core import Limit, Status, Usage


# Middle dot separator the formatter uses between fields.
DOT = "·"


def _now():
    return datetime.now(timezone.utc)


def _mk(kind, group, percent, resets_at=None, scope_label=None, is_active=False,
        severity="normal"):
    return Limit(
        kind=kind,
        group=group,
        percent=percent,
        severity=severity,
        resets_at=resets_at,
        scope_label=scope_label,
        is_active=is_active,
    )


# --------------------------------------------------------------------------- #
# parse_usage: canonical limits[] array
# --------------------------------------------------------------------------- #
def test_parse_usage_limits_array_basic_fields():
    data = {
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 42.4,
                "severity": "normal",
                "resets_at": "2026-07-10T20:00:00Z",
                "is_active": True,
            }
        ]
    }
    usage = usage_core.parse_usage(data, plan="max", rate_tier="default_claude_max_5x")

    assert len(usage.limits) == 1
    lim = usage.limits[0]
    assert lim.kind == "session"
    assert lim.group == "session"
    assert lim.percent == 42.4
    assert lim.severity == "normal"
    assert lim.is_active is True
    assert lim.resets_at == datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)


def test_parse_usage_passes_through_plan_rate_tier_and_raw():
    data = {"limits": [{"kind": "session", "group": "session", "percent": 1}]}
    usage = usage_core.parse_usage(data, plan="pro", rate_tier="default_5x")
    assert usage.plan == "pro"
    assert usage.rate_tier == "default_5x"
    # raw is the exact dict handed in.
    assert usage.raw is data


def test_parse_usage_percent_coerced_from_string():
    data = {"limits": [{"kind": "weekly_all", "group": "weekly", "percent": "90"}]}
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].percent == 90.0
    assert isinstance(usage.limits[0].percent, float)


def test_parse_usage_percent_none_becomes_zero():
    data = {"limits": [{"kind": "weekly_scoped", "group": "weekly", "percent": None}]}
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].percent == 0.0


def test_parse_usage_missing_fields_use_defaults():
    data = {"limits": [{}]}
    usage = usage_core.parse_usage(data)
    lim = usage.limits[0]
    assert lim.kind == ""
    assert lim.group == ""
    assert lim.percent == 0.0
    assert lim.severity == "normal"
    assert lim.resets_at is None
    assert lim.scope_label is None
    assert lim.is_active is False


def test_parse_usage_scope_label_prefers_model_display_name():
    data = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 10,
                "scope": {"model": {"display_name": "Fable"}, "surface": "code"},
            }
        ]
    }
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].scope_label == "Fable"


def test_parse_usage_scope_label_falls_back_to_surface():
    data = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 10,
                "scope": {"surface": "code"},
            }
        ]
    }
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].scope_label == "code"


def test_parse_usage_scope_none_gives_no_label():
    data = {
        "limits": [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 5, "scope": None}
        ]
    }
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].scope_label is None


def test_parse_usage_is_active_truthy_and_falsey():
    data = {
        "limits": [
            {"kind": "a", "group": "session", "percent": 1, "is_active": 1},
            {"kind": "b", "group": "session", "percent": 1, "is_active": 0},
            {"kind": "c", "group": "session", "percent": 1},  # missing -> False
        ]
    }
    usage = usage_core.parse_usage(data)
    assert [l.is_active for l in usage.limits] == [True, False, False]


# --------------------------------------------------------------------------- #
# parse_usage: flat five_hour / seven_day / seven_day_* fallback
# --------------------------------------------------------------------------- #
def test_parse_usage_flat_fallback_when_no_limits_key():
    data = {
        "five_hour": {"utilization": 55, "resets_at": "2026-07-10T21:00:00Z"},
        "seven_day": {"utilization": 12},
        "seven_day_opus": {"utilization": 3},
        "seven_day_sonnet": {"utilization": 0},
        "seven_day_fable": {"utilization": 7},
    }
    usage = usage_core.parse_usage(data)

    by_kind = [(l.kind, l.group, l.percent, l.scope_label, l.is_active)
               for l in usage.limits]
    assert by_kind == [
        ("session", "session", 55.0, None, True),
        ("weekly_all", "weekly", 12.0, None, False),
        ("weekly_scoped", "weekly", 3.0, "Opus", False),
        ("weekly_scoped", "weekly", 0.0, "Sonnet", False),
        ("weekly_scoped", "weekly", 7.0, "Fable", False),
    ]


def test_parse_usage_flat_fallback_session_reset_parsed():
    data = {"five_hour": {"utilization": 10, "resets_at": "2026-07-10T21:00:00Z"}}
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].resets_at == datetime(2026, 7, 10, 21, 0, tzinfo=timezone.utc)


def test_parse_usage_flat_fallback_severity_always_normal():
    data = {"five_hour": {"utilization": 99}}
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].severity == "normal"


def test_parse_usage_flat_fallback_skips_absent_blocks():
    # Only seven_day present -> exactly one limit produced.
    data = {"seven_day": {"utilization": 20}}
    usage = usage_core.parse_usage(data)
    assert len(usage.limits) == 1
    assert usage.limits[0].kind == "weekly_all"


def test_parse_usage_empty_limits_list_triggers_fallback():
    # An empty list is falsey -> we fall through to the flat branch.
    data = {"limits": [], "five_hour": {"utilization": 33}}
    usage = usage_core.parse_usage(data)
    assert len(usage.limits) == 1
    assert usage.limits[0].kind == "session"
    assert usage.limits[0].percent == 33.0


def test_parse_usage_empty_dict_yields_no_limits():
    usage = usage_core.parse_usage({})
    assert usage.limits == []


def test_parse_usage_flat_utilization_none_becomes_zero():
    data = {"five_hour": {"utilization": None}}
    usage = usage_core.parse_usage(data)
    assert usage.limits[0].percent == 0.0


# --------------------------------------------------------------------------- #
# _parse_dt
# --------------------------------------------------------------------------- #
def test_parse_dt_none_and_empty():
    assert usage_core._parse_dt(None) is None
    assert usage_core._parse_dt("") is None


def test_parse_dt_invalid_string_returns_none():
    assert usage_core._parse_dt("not-a-date") is None
    assert usage_core._parse_dt(123) is None  # str(123) is not iso


def test_parse_dt_zulu_is_utc():
    dt = usage_core._parse_dt("2026-07-10T20:00:00Z")
    assert dt == datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    assert dt.tzinfo is not None


def test_parse_dt_naive_becomes_utc():
    dt = usage_core._parse_dt("2026-07-10T20:00:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_dt_preserves_explicit_offset():
    dt = usage_core._parse_dt("2026-07-10T20:00:00+05:00")
    assert dt.utcoffset() == timedelta(hours=5)


# --------------------------------------------------------------------------- #
# color_for thresholds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pct", [0, 1, 25, 49, 49.99])
def test_color_for_green_below_50(pct):
    assert usage_core.color_for(pct) == "green"


@pytest.mark.parametrize("pct", [50, 60, 79.9, 80])
def test_color_for_amber_50_to_80(pct):
    assert usage_core.color_for(pct) == "amber"


@pytest.mark.parametrize("pct", [80.01, 81, 99, 100, 150])
def test_color_for_red_above_80(pct):
    assert usage_core.color_for(pct) == "red"


def test_color_for_exact_boundaries():
    # 50 is the first amber value; 80 is the last amber value; 81 is red.
    assert usage_core.color_for(50) == "amber"
    assert usage_core.color_for(80) == "amber"
    assert usage_core.color_for(81) == "red"


# --------------------------------------------------------------------------- #
# critical_limit
# --------------------------------------------------------------------------- #
def test_critical_limit_picks_highest_percent_among_shown():
    u = Usage(limits=[
        _mk("session", "session", 42, is_active=True),
        _mk("weekly_all", "weekly", 90),
        _mk("weekly_scoped", "weekly", 10, scope_label="Fable"),
    ])
    crit = usage_core.critical_limit(u)
    assert crit.kind == "weekly_all"
    assert crit.percent == 90


def test_critical_limit_ignores_unshown_kinds():
    # A very high but unshown kind must not be selected.
    u = Usage(limits=[
        _mk("overflow", "weekly", 99),
        _mk("session", "session", 5, is_active=True),
    ])
    crit = usage_core.critical_limit(u)
    assert crit.kind == "session"


def test_critical_limit_none_when_only_unshown_kinds():
    u = Usage(limits=[_mk("overflow", "weekly", 99)])
    assert usage_core.critical_limit(u) is None


def test_critical_limit_none_for_empty():
    assert usage_core.critical_limit(Usage(limits=[])) is None


def test_critical_limit_weekly_scoped_counts():
    u = Usage(limits=[
        _mk("session", "session", 20, is_active=True),
        _mk("weekly_scoped", "weekly", 88, scope_label="Fable"),
    ])
    crit = usage_core.critical_limit(u)
    assert crit.kind == "weekly_scoped"


# --------------------------------------------------------------------------- #
# _reset_in
# --------------------------------------------------------------------------- #
def test_reset_in_none_is_empty():
    assert usage_core._reset_in(None) == ""


def test_reset_in_past_is_resetting():
    assert usage_core._reset_in(_now() - timedelta(seconds=5)) == "resetting"


def test_reset_in_zero_is_resetting():
    # secs <= 0 -> "resetting"; use a moment already elapsed.
    assert usage_core._reset_in(_now() - timedelta(milliseconds=1)) == "resetting"


def test_reset_in_sub_minute():
    assert usage_core._reset_in(_now() + timedelta(seconds=30)) == "resets in <1m"


def test_reset_in_minutes_only():
    assert usage_core._reset_in(_now() + timedelta(minutes=30, seconds=5)) == "resets in 30m"


def test_reset_in_hours_and_minutes_padded():
    out = usage_core._reset_in(_now() + timedelta(hours=2, minutes=4, seconds=5))
    assert out == "resets in 2h 04m"


def test_reset_in_days_and_hours():
    out = usage_core._reset_in(_now() + timedelta(days=3, hours=3, minutes=10))
    assert out == "resets in 3d 3h"


# --------------------------------------------------------------------------- #
# _reset_at (weekly date format)
# --------------------------------------------------------------------------- #
def test_reset_at_none_is_empty():
    assert usage_core._reset_at(None) == ""


def test_reset_at_has_weekly_date_shape():
    # Rendered in system-local tz, so we assert on shape not exact hour.
    dt = datetime(2026, 7, 15, 18, 5, tzinfo=timezone.utc)
    out = usage_core._reset_at(dt)
    assert out.startswith("resets ")
    # e.g. "resets Wed Jul 15, 8:05 PM" -> weekday, month, comma, AM/PM present.
    assert "," in out
    assert out.endswith("AM") or out.endswith("PM")
    # No leading zero on the day/hour (platform-specific strftime directive).
    assert ", 0" not in out


# --------------------------------------------------------------------------- #
# format_breakdown: session / weekly lines
# --------------------------------------------------------------------------- #
def test_format_breakdown_session_line_basic():
    u = Usage(limits=[
        _mk("session", "session", 42, resets_at=_now() + timedelta(hours=2, minutes=30),
            is_active=True),
    ])
    b = usage_core.format_breakdown(u)
    assert b["session"].startswith("Session   42%")
    assert "limit reached" not in b["session"]
    assert "resets in" in b["session"]


def test_format_breakdown_session_limit_reached_at_100():
    u = Usage(limits=[_mk("session", "session", 100, is_active=True)])
    b = usage_core.format_breakdown(u)
    assert "limit reached" in b["session"]
    assert f"   {DOT}   limit reached" in b["session"]


def test_format_breakdown_session_limit_reached_above_100():
    u = Usage(limits=[_mk("session", "session", 130, is_active=True)])
    b = usage_core.format_breakdown(u)
    assert "limit reached" in b["session"]


def test_format_breakdown_session_no_limit_reached_at_99():
    u = Usage(limits=[_mk("session", "session", 99, is_active=True)])
    b = usage_core.format_breakdown(u)
    assert "limit reached" not in b["session"]


def test_format_breakdown_session_none_when_absent():
    u = Usage(limits=[_mk("weekly_all", "weekly", 10)])
    b = usage_core.format_breakdown(u)
    assert b["session"] is None
    assert b["session_pct"] is None
    assert b["session_color"] == "grey"
    assert b["session_resets_at"] is None


def test_format_breakdown_weekly_line_basic():
    reset = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    u = Usage(limits=[_mk("weekly_all", "weekly", 72, resets_at=reset)])
    b = usage_core.format_breakdown(u)
    assert b["weekly"].startswith("Weekly (all)   72%")
    assert "resets " in b["weekly"]  # uses _reset_at (date form)


def test_format_breakdown_weekly_limit_reached_at_100():
    u = Usage(limits=[_mk("weekly_all", "weekly", 100)])
    b = usage_core.format_breakdown(u)
    assert "limit reached" in b["weekly"]


def test_format_breakdown_weekly_none_when_absent():
    u = Usage(limits=[_mk("session", "session", 10, is_active=True)])
    b = usage_core.format_breakdown(u)
    assert b["weekly"] is None
    assert b["weekly_pct"] is None
    assert b["weekly_color"] == "grey"
    assert b["weekly_resets_at"] is None


# --------------------------------------------------------------------------- #
# format_breakdown: per-model (weekly_scoped) rows
# --------------------------------------------------------------------------- #
def test_format_breakdown_model_rows_from_scoped():
    u = Usage(limits=[
        _mk("weekly_scoped", "weekly", 0, scope_label="Fable"),
        _mk("weekly_scoped", "weekly", 85, scope_label="Opus"),
    ])
    b = usage_core.format_breakdown(u)
    assert b["models"] == ["Fable   0%", "Opus   85%"]
    assert b["model_rows"] == [
        {"label": "Fable", "pct": 0, "color": "green"},
        {"label": "Opus", "pct": 85, "color": "red"},
    ]


def test_format_breakdown_scoped_label_defaults_to_scoped():
    u = Usage(limits=[_mk("weekly_scoped", "weekly", 33, scope_label=None)])
    b = usage_core.format_breakdown(u)
    assert b["models"] == ["Scoped   33%"]
    assert b["model_rows"][0]["label"] == "Scoped"


def test_format_breakdown_scoped_shown_even_at_zero():
    u = Usage(limits=[_mk("weekly_scoped", "weekly", 0, scope_label="Fable")])
    b = usage_core.format_breakdown(u)
    assert len(b["model_rows"]) == 1


def test_format_breakdown_no_scoped_rows():
    u = Usage(limits=[_mk("session", "session", 5, is_active=True)])
    b = usage_core.format_breakdown(u)
    assert b["models"] == []
    assert b["model_rows"] == []


# --------------------------------------------------------------------------- #
# format_breakdown: face_pct / face_color / tooltip / plan
# --------------------------------------------------------------------------- #
def test_format_breakdown_face_reflects_critical():
    u = Usage(limits=[
        _mk("session", "session", 100, is_active=True),
        _mk("weekly_all", "weekly", 72),
    ])
    b = usage_core.format_breakdown(u)
    assert b["face_pct"] == "100%"
    assert b["face_color"] == "red"
    assert b["critical_kind"] == "session"


def test_format_breakdown_face_color_amber_and_green():
    u_amber = Usage(limits=[_mk("session", "session", 65, is_active=True)])
    assert usage_core.format_breakdown(u_amber)["face_color"] == "amber"
    u_green = Usage(limits=[_mk("session", "session", 10, is_active=True)])
    assert usage_core.format_breakdown(u_green)["face_color"] == "green"


def test_format_breakdown_face_pct_when_no_shown_limits():
    # No shown kinds -> crit is None -> face defaults to 0%.
    u = Usage(limits=[_mk("overflow", "weekly", 99)])
    b = usage_core.format_breakdown(u)
    assert b["face_pct"] == "0%"
    assert b["face_color"] == "green"
    assert b["critical_kind"] is None


def test_format_breakdown_face_pct_empty_usage():
    b = usage_core.format_breakdown(Usage(limits=[]))
    assert b["face_pct"] == "0%"
    assert b["face_color"] == "green"


def test_format_breakdown_tooltip_with_session_and_weekly():
    u = Usage(limits=[
        _mk("session", "session", 42, is_active=True),
        _mk("weekly_all", "weekly", 72),
    ])
    b = usage_core.format_breakdown(u)
    assert b["tooltip"] == f"Claude {DOT} Session 42% {DOT} Weekly 72%"


def test_format_breakdown_tooltip_default_when_no_windows():
    u = Usage(limits=[_mk("weekly_scoped", "weekly", 10, scope_label="Opus")])
    b = usage_core.format_breakdown(u)
    assert b["tooltip"] == "Claude usage"


def test_format_breakdown_plan_label_max_with_rate_tier():
    u = Usage(limits=[_mk("session", "session", 1, is_active=True)],
              plan="max", rate_tier="default_claude_max_5x")
    b = usage_core.format_breakdown(u)
    assert b["plan"] == "Plan: Max (5x)"


def test_format_breakdown_plan_label_pro_without_rate_tier():
    u = Usage(limits=[_mk("session", "session", 1, is_active=True)], plan="pro")
    b = usage_core.format_breakdown(u)
    assert b["plan"] == "Plan: Pro"


def test_format_breakdown_plan_label_unknown_plan_capitalized():
    u = Usage(limits=[_mk("session", "session", 1, is_active=True)], plan="scale")
    b = usage_core.format_breakdown(u)
    assert b["plan"] == "Plan: Scale"


def test_format_breakdown_plan_label_empty_defaults_to_claude():
    u = Usage(limits=[_mk("session", "session", 1, is_active=True)])
    b = usage_core.format_breakdown(u)
    assert b["plan"] == "Plan: Claude"


def test_format_breakdown_numeric_window_fields():
    reset_s = _now() + timedelta(hours=1)
    reset_w = datetime(2026, 7, 13, tzinfo=timezone.utc)
    u = Usage(limits=[
        _mk("session", "session", 49.6, resets_at=reset_s, is_active=True),
        _mk("weekly_all", "weekly", 80.4, resets_at=reset_w),
    ])
    b = usage_core.format_breakdown(u)
    assert b["session_pct"] == 50  # display pct is rounded
    # Color uses the raw (unrounded) percent: 49.6 < 50 -> green.
    assert b["session_color"] == "green"
    assert b["session_resets_at"] == reset_s
    assert b["weekly_pct"] == 80  # rounded from 80.4
    # 80.4 > 80 -> red (color uses the raw float, not the rounded value).
    assert b["weekly_color"] == "red"
    assert b["weekly_resets_at"] == reset_w


# --------------------------------------------------------------------------- #
# status_display
# --------------------------------------------------------------------------- #
def test_status_display_rate_limited_is_red_and_limit_reached():
    d = usage_core.status_display(Status.RATE_LIMITED)
    assert d["face_pct"] == "!"
    assert d["face_color"] == "red"
    assert d["session"] == "usage limit reached"
    assert d["tooltip"] == f"Claude {DOT} usage limit reached"


def test_status_display_offline():
    d = usage_core.status_display(Status.OFFLINE)
    assert d["face_pct"] == "-"
    assert d["face_color"] == "grey"
    assert d["session"] == "offline"


def test_status_display_no_creds():
    d = usage_core.status_display(Status.NO_CREDS)
    assert d["face_pct"] == "!"
    assert d["face_color"] == "grey"
    assert d["session"] == "not logged in to Claude Code"


def test_status_display_no_data():
    d = usage_core.status_display(Status.NO_DATA)
    assert d["face_pct"] == "-"
    assert d["face_color"] == "grey"
    assert d["session"] == "no usage data yet"


def test_status_display_error():
    d = usage_core.status_display(Status.ERROR)
    assert d["face_pct"] == "!"
    assert d["face_color"] == "grey"
    assert d["session"] == "error fetching usage"


def test_status_display_common_null_fields():
    # Every status dict zeroes out the usage-specific fields.
    for st in Status:
        d = usage_core.status_display(st)
        assert d["plan"] is None
        assert d["weekly"] is None
        assert d["models"] == []
        assert d["critical_kind"] is None
        assert d["session_pct"] is None
        assert d["session_color"] == "grey"
        assert d["session_resets_at"] is None
        assert d["weekly_pct"] is None
        assert d["weekly_color"] == "grey"
        assert d["weekly_resets_at"] is None
        assert d["model_rows"] == []


def test_status_display_keys_match_format_breakdown_keys():
    # Both producers must emit the same shape so the UI can render either.
    u = Usage(limits=[_mk("session", "session", 10, is_active=True)])
    fb_keys = set(usage_core.format_breakdown(u).keys())
    sd_keys = set(usage_core.status_display(Status.OFFLINE).keys())
    assert fb_keys == sd_keys
