"""Tests for cost.py — today's token usage / cost estimation from local transcripts.

Hermetic: no network, no real credentials, no writes outside pytest's tmp_path.
The projects directory is faked under tmp_path and cost.py is pointed at it via
either the ``config_dir`` argument or the ``CLAUDE_CONFIG_DIR`` env var.
"""

import json
from datetime import datetime, timedelta

import pytest

import cost


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _now_local():
    """A timezone-aware 'now' in the same local zone cost.py uses."""
    return datetime.now().astimezone()


def _today_iso(hour=12, minute=0):
    """ISO timestamp for a moment *today* (after local midnight) with offset."""
    n = _now_local().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return n.isoformat()


def _make_projects_dir(base):
    """Create ``base/projects`` and return it as a Path."""
    proj = base / "projects"
    (proj / "some-project").mkdir(parents=True, exist_ok=True)
    return proj


def _write_jsonl(proj, name, records):
    """Write ``records`` (list of dicts) as JSONL under proj/some-project/name."""
    p = proj / "some-project" / name
    with p.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return p


def _assistant(model, usage, mid="m1", ts=None, **extra):
    """Build one assistant transcript line."""
    if ts is None:
        ts = _today_iso()
    msg = {"model": model, "usage": usage}
    if mid is not None:
        msg["id"] = mid
    rec = {"type": "assistant", "timestamp": ts, "message": msg}
    rec.update(extra)
    return rec


# --------------------------------------------------------------------------- #
# _key_for                                                                     #
# --------------------------------------------------------------------------- #

def test_key_for_opus():
    assert cost._key_for("claude-opus-4-20250101") == "opus"


def test_key_for_sonnet():
    assert cost._key_for("claude-sonnet-4-5") == "sonnet"


def test_key_for_haiku():
    assert cost._key_for("claude-haiku-3-5") == "haiku"


def test_key_for_fable():
    assert cost._key_for("fable-5") == "fable"


def test_key_for_mythos_maps_to_fable():
    # Mythos 5 shares Fable 5 pricing.
    assert cost._key_for("mythos-5") == "fable"


def test_key_for_is_case_insensitive():
    assert cost._key_for("CLAUDE-OPUS-4") == "opus"
    assert cost._key_for("Mythos-Deluxe") == "fable"


def test_key_for_unknown_model_is_none():
    assert cost._key_for("gpt-4o") is None
    assert cost._key_for("gemini-pro") is None


def test_key_for_none_or_empty_is_none():
    assert cost._key_for(None) is None
    assert cost._key_for("") is None


def test_key_for_fable_wins_over_opus_when_both_present():
    # fable/mythos is checked first in the function.
    assert cost._key_for("opus-fable-hybrid") == "fable"


# --------------------------------------------------------------------------- #
# _n  (numeric extraction, bool excluded)                                      #
# --------------------------------------------------------------------------- #

def test_n_returns_int_value():
    assert cost._n({"input_tokens": 42}, "input_tokens") == 42


def test_n_returns_float_value():
    assert cost._n({"input_tokens": 3.5}, "input_tokens") == 3.5


def test_n_missing_key_is_zero():
    assert cost._n({}, "input_tokens") == 0


def test_n_none_value_is_zero():
    assert cost._n({"input_tokens": None}, "input_tokens") == 0


def test_n_string_value_is_zero():
    assert cost._n({"input_tokens": "100"}, "input_tokens") == 0


def test_n_excludes_bool_true():
    # bool is a subclass of int, but must be treated as 0 here.
    assert cost._n({"input_tokens": True}, "input_tokens") == 0


def test_n_excludes_bool_false():
    assert cost._n({"input_tokens": False}, "input_tokens") == 0


# --------------------------------------------------------------------------- #
# _tok and _line_cost building blocks                                          #
# --------------------------------------------------------------------------- #

def test_tok_sums_all_four_token_fields():
    usage = {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 8,
    }
    assert cost._tok(usage) == 15


def test_tok_ignores_bool_fields():
    usage = {"input_tokens": True, "output_tokens": 5}
    assert cost._tok(usage) == 5


def test_line_cost_applies_all_pricing_multipliers_opus():
    # opus: input 5, output 25, cache_write 6.25, cache_read 0.50 per 1M.
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    expected = (5 + 25 + 6.25 + 0.50)  # each field is exactly 1M tokens
    assert cost._line_cost(usage, "opus") == pytest.approx(expected)


def test_line_cost_sonnet_input_only():
    usage = {"input_tokens": 2_000_000}
    assert cost._line_cost(usage, "sonnet") == pytest.approx(3 * 2)


def test_line_cost_haiku_cache_read_multiplier():
    usage = {"cache_read_input_tokens": 10_000_000}
    # haiku cache_read = 0.10 per 1M
    assert cost._line_cost(usage, "haiku") == pytest.approx(0.10 * 10)


def test_line_cost_fable_cache_write_multiplier():
    usage = {"cache_creation_input_tokens": 4_000_000}
    # fable cache_write = 12.50 per 1M
    assert cost._line_cost(usage, "fable") == pytest.approx(12.50 * 4)


# --------------------------------------------------------------------------- #
# compute_today — directory resolution                                         #
# --------------------------------------------------------------------------- #

def test_compute_today_none_when_projects_dir_absent(tmp_path):
    # base exists but has no 'projects' subdir.
    assert cost.compute_today(config_dir=str(tmp_path)) is None


def test_compute_today_empty_projects_dir_returns_zero(tmp_path):
    _make_projects_dir(tmp_path)
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result == {"tokens": 0, "cost": 0.0}


def test_compute_today_uses_env_var(tmp_path, monkeypatch):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"input_tokens": 100, "output_tokens": 200}),
    ])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    result = cost.compute_today()  # no explicit config_dir -> reads env var
    assert result["tokens"] == 300


def test_compute_today_arg_takes_precedence_over_env(tmp_path, monkeypatch):
    # env points at an empty base (no projects) -> would give None;
    # explicit arg points at a populated base -> must win.
    good = tmp_path / "good"
    good.mkdir()
    proj = _make_projects_dir(good)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-sonnet-4", {"input_tokens": 10, "output_tokens": 5}),
    ])
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    result = cost.compute_today(config_dir=str(good))
    assert result["tokens"] == 15


# --------------------------------------------------------------------------- #
# compute_today — dedup by message id (LAST occurrence wins)                   #
# --------------------------------------------------------------------------- #

def test_compute_today_dedups_by_id_keeping_last(tmp_path):
    # Streaming snapshots: same id, input/cache constant, output GROWS.
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "stream.jsonl", [
        _assistant("claude-opus-4",
                   {"input_tokens": 1000, "output_tokens": 10,
                    "cache_read_input_tokens": 500}, mid="abc"),
        _assistant("claude-opus-4",
                   {"input_tokens": 1000, "output_tokens": 50,
                    "cache_read_input_tokens": 500}, mid="abc"),
        _assistant("claude-opus-4",
                   {"input_tokens": 1000, "output_tokens": 200,
                    "cache_read_input_tokens": 500}, mid="abc"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # Only the LAST snapshot counts: input 1000 + output 200 + cache_read 500.
    assert result["tokens"] == 1000 + 200 + 500


def test_compute_today_dedup_cost_uses_last_snapshot(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "stream.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 10}, mid="abc"),
        _assistant("claude-opus-4", {"output_tokens": 1_000_000}, mid="abc"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # opus output = 25 per 1M; last snapshot has exactly 1M output tokens.
    assert result["cost"] == pytest.approx(25.0)
    assert result["tokens"] == 1_000_000


def test_compute_today_dedup_across_multiple_files(tmp_path):
    # Same id appearing in two files still dedups (dict is global to the scan).
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "a.jsonl", [
        _assistant("claude-haiku-3", {"output_tokens": 5}, mid="shared"),
    ])
    _write_jsonl(proj, "b.jsonl", [
        _assistant("claude-haiku-3", {"output_tokens": 999}, mid="shared"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # Exactly one of the two wins (last in scan order) -> tokens is 5 or 999,
    # never 1004. Whichever wins, it must be a single record's value.
    assert result["tokens"] in (5, 999)


def test_compute_today_distinct_ids_are_summed(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 100}, mid="id1"),
        _assistant("claude-opus-4", {"output_tokens": 200}, mid="id2"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result["tokens"] == 300


# --------------------------------------------------------------------------- #
# compute_today — pricing per model                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model,key", [
    ("claude-opus-4", "opus"),
    ("claude-sonnet-4", "sonnet"),
    ("claude-haiku-4", "haiku"),
    ("fable-5", "fable"),
    ("mythos-5", "fable"),
])
def test_compute_today_pricing_matches_family(tmp_path, model, key):
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [_assistant(model, usage, mid="x")])
    result = cost.compute_today(config_dir=str(tmp_path))
    p = cost.PRICING[key]
    expected = p["input"] + p["output"] + p["cache_write"] + p["cache_read"]
    assert result["cost"] == pytest.approx(expected)
    assert result["tokens"] == 4_000_000


def test_compute_today_cache_read_and_write_multipliers(tmp_path):
    # Verify cache_read vs cache_write get DIFFERENT multipliers for sonnet.
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-sonnet-4",
                   {"cache_creation_input_tokens": 1_000_000,
                    "cache_read_input_tokens": 1_000_000}, mid="c"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # sonnet cache_write 3.75 + cache_read 0.30
    assert result["cost"] == pytest.approx(3.75 + 0.30)


# --------------------------------------------------------------------------- #
# compute_today — unpriced models count tokens but add no cost                 #
# --------------------------------------------------------------------------- #

def test_compute_today_unpriced_model_counts_tokens_no_cost(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("gpt-4o",
                   {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                   mid="u"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result["tokens"] == 2_000_000
    assert result["cost"] == 0.0


def test_compute_today_mixes_priced_and_unpriced(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("unknown-model", {"output_tokens": 1_000_000}, mid="u1"),
        _assistant("claude-opus-4", {"output_tokens": 1_000_000}, mid="p1"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # Both count for tokens; only opus contributes cost (25 per 1M output).
    assert result["tokens"] == 2_000_000
    assert result["cost"] == pytest.approx(25.0)


def test_compute_today_missing_model_field_is_unpriced(tmp_path):
    proj = _make_projects_dir(tmp_path)
    # message with usage but no 'model' key -> _key_for("") -> None.
    rec = {
        "type": "assistant",
        "timestamp": _today_iso(),
        "message": {"id": "nm", "usage": {"output_tokens": 500}},
    }
    _write_jsonl(proj, "s.jsonl", [rec])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result["tokens"] == 500
    assert result["cost"] == 0.0


# --------------------------------------------------------------------------- #
# compute_today — lines without an id (extra) counted as-is                    #
# --------------------------------------------------------------------------- #

def test_compute_today_lines_without_id_are_summed(tmp_path):
    proj = _make_projects_dir(tmp_path)
    # No id -> not deduped; both lines add up.
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 100}, mid=None),
        _assistant("claude-opus-4", {"output_tokens": 200}, mid=None),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result["tokens"] == 300


def test_compute_today_idless_priced_contributes_cost(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-haiku-4", {"output_tokens": 1_000_000}, mid=None),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # haiku output = 5 per 1M
    assert result["cost"] == pytest.approx(5.0)
    assert result["tokens"] == 1_000_000


def test_compute_today_idless_unpriced_no_cost(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("mystery", {"output_tokens": 777}, mid=None),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result["tokens"] == 777
    assert result["cost"] == 0.0


# --------------------------------------------------------------------------- #
# compute_today — filtering (type, usage presence, timestamp)                  #
# --------------------------------------------------------------------------- #

def test_compute_today_ignores_non_assistant_type(tmp_path):
    proj = _make_projects_dir(tmp_path)
    rec = {
        "type": "user",
        "timestamp": _today_iso(),
        "message": {"id": "x", "model": "claude-opus-4",
                    "usage": {"output_tokens": 999}},
    }
    _write_jsonl(proj, "s.jsonl", [rec])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result == {"tokens": 0, "cost": 0.0}


def test_compute_today_ignores_lines_without_usage_substring(tmp_path):
    proj = _make_projects_dir(tmp_path)
    # Line has no '"usage"' substring at all -> skipped before JSON parse.
    rec = {"type": "assistant", "timestamp": _today_iso(),
           "message": {"id": "x", "model": "claude-opus-4"}}
    _write_jsonl(proj, "s.jsonl", [rec])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result == {"tokens": 0, "cost": 0.0}


def test_compute_today_ignores_records_before_midnight(tmp_path):
    proj = _make_projects_dir(tmp_path)
    yesterday = (_now_local() - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0).isoformat()
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 500},
                   mid="old", ts=yesterday),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result == {"tokens": 0, "cost": 0.0}


def test_compute_today_ignores_records_without_timestamp(tmp_path):
    proj = _make_projects_dir(tmp_path)
    rec = {
        "type": "assistant",
        "message": {"id": "x", "model": "claude-opus-4",
                    "usage": {"output_tokens": 500}},
    }
    _write_jsonl(proj, "s.jsonl", [rec])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result == {"tokens": 0, "cost": 0.0}


def test_compute_today_handles_z_suffix_timestamp(tmp_path):
    # A UTC 'Z' timestamp from just after local midnight should still count.
    proj = _make_projects_dir(tmp_path)
    from datetime import timezone
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    ts = now_utc.isoformat().replace("+00:00", "Z")
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 42}, mid="z", ts=ts),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # 'now' is always >= today's local midnight, so this counts.
    assert result["tokens"] == 42


def test_compute_today_non_string_timestamp_is_skipped(tmp_path):
    proj = _make_projects_dir(tmp_path)
    rec = {
        "type": "assistant",
        "timestamp": 1700000000,  # int, not ISO string
        "message": {"id": "x", "model": "claude-opus-4",
                    "usage": {"output_tokens": 500}},
    }
    _write_jsonl(proj, "s.jsonl", [rec])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result == {"tokens": 0, "cost": 0.0}


# --------------------------------------------------------------------------- #
# compute_today — robustness                                                   #
# --------------------------------------------------------------------------- #

def test_compute_today_skips_malformed_lines(tmp_path):
    proj = _make_projects_dir(tmp_path)
    good = _assistant("claude-opus-4", {"output_tokens": 100}, mid="g")
    p = proj / "some-project" / "mixed.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        fh.write('{"usage": broken json here\n')      # malformed but has "usage"
        fh.write(json.dumps(good) + "\n")             # valid
    result = cost.compute_today(config_dir=str(tmp_path))
    # Malformed line skipped, good line counted.
    assert result["tokens"] == 100


def test_compute_today_reads_nested_project_dirs(tmp_path):
    # rglob should find files in deeply nested project folders.
    proj = tmp_path / "projects"
    nested = proj / "a" / "b" / "c"
    nested.mkdir(parents=True)
    p = nested / "deep.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(
            _assistant("claude-opus-4", {"output_tokens": 7}, mid="d")) + "\n")
    result = cost.compute_today(config_dir=str(tmp_path))
    assert result["tokens"] == 7


def test_compute_today_return_shape(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 1}, mid="s"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    assert set(result.keys()) == {"tokens", "cost"}
    assert isinstance(result["tokens"], int)
    assert isinstance(result["cost"], float)


def test_compute_today_tokens_is_int_even_with_float_usage(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4", {"output_tokens": 10.9}, mid="f"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # total_tokens is cast to int() -> truncation toward zero.
    assert result["tokens"] == 10
    assert isinstance(result["tokens"], int)


def test_compute_today_bool_usage_values_ignored(tmp_path):
    proj = _make_projects_dir(tmp_path)
    _write_jsonl(proj, "s.jsonl", [
        _assistant("claude-opus-4",
                   {"input_tokens": True, "output_tokens": 5}, mid="b"),
    ])
    result = cost.compute_today(config_dir=str(tmp_path))
    # 'True' must NOT be counted as 1 token.
    assert result["tokens"] == 5
