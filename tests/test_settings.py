"""Tests for settings.py — config load/validation, TOML round-trip, and the
built-in mini-parser fallback used on Python < 3.11.

Hermetic: every test that touches disk points ``CLAUDOMETER_CONFIG`` at a file
under pytest's ``tmp_path``, so nothing outside the tmp dir is ever read or
written. No network, no real home directory, no credentials.
"""

import builtins
import copy

import pytest

import settings


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """Point CLAUDOMETER_CONFIG at a fresh (non-existent) file in tmp_path."""
    p = tmp_path / "claudometer.toml"
    monkeypatch.setenv("CLAUDOMETER_CONFIG", str(p))
    return p


@pytest.fixture
def force_mini_parser(monkeypatch):
    """Make ``import tomllib`` raise ModuleNotFoundError so ``_parse`` falls
    back to the built-in mini-parser (the Python < 3.11 code path)."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("forced: no tomllib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # sanity: the fallback is actually active
    with pytest.raises(ModuleNotFoundError):
        __import__("tomllib")


def write_toml(path, text):
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# config_path()
# --------------------------------------------------------------------------- #
def test_config_path_uses_env(tmp_path, monkeypatch):
    target = tmp_path / "custom.toml"
    monkeypatch.setenv("CLAUDOMETER_CONFIG", str(target))
    assert settings.config_path() == target


def test_config_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDOMETER_CONFIG", raising=False)
    p = settings.config_path()
    # Falls back to ~/.claudometer.toml (we don't read it, just check the name).
    assert p.name == ".claudometer.toml"


def test_config_path_empty_env_falls_back_to_home(monkeypatch):
    monkeypatch.setenv("CLAUDOMETER_CONFIG", "")
    assert settings.config_path().name == ".claudometer.toml"


# --------------------------------------------------------------------------- #
# load() — defaults
# --------------------------------------------------------------------------- #
def test_load_no_file_returns_defaults(cfg_path):
    assert not cfg_path.exists()
    cfg = settings.load()
    assert cfg == settings.DEFAULTS


def test_load_returns_a_copy_not_defaults_object(cfg_path):
    cfg = settings.load()
    assert cfg is not settings.DEFAULTS
    cfg["poll"] = 999
    assert settings.DEFAULTS["poll"] == 90  # mutation didn't leak


def test_load_empty_file_returns_defaults(cfg_path):
    write_toml(cfg_path, "")
    assert settings.load() == settings.DEFAULTS


def test_load_default_accent_is_none(cfg_path):
    assert settings.load()["accent"] is None


# --------------------------------------------------------------------------- #
# load() — poll clamping (60..300)
# --------------------------------------------------------------------------- #
def test_poll_clamped_low(cfg_path):
    write_toml(cfg_path, "poll = 10\n")
    assert settings.load()["poll"] == 60


def test_poll_clamped_high(cfg_path):
    write_toml(cfg_path, "poll = 9999\n")
    assert settings.load()["poll"] == 300


def test_poll_within_range_unchanged(cfg_path):
    write_toml(cfg_path, "poll = 120\n")
    assert settings.load()["poll"] == 120


def test_poll_boundaries(cfg_path):
    write_toml(cfg_path, "poll = 60\n")
    assert settings.load()["poll"] == 60
    write_toml(cfg_path, "poll = 300\n")
    assert settings.load()["poll"] == 300


def test_poll_bool_true_falls_back_to_default(cfg_path):
    # bools must NOT be treated as ints (True == 1) — fall back to default.
    write_toml(cfg_path, "poll = true\n")
    assert settings.load()["poll"] == settings.DEFAULTS["poll"]


def test_poll_non_numeric_string_falls_back(cfg_path):
    write_toml(cfg_path, 'poll = "abc"\n')
    assert settings.load()["poll"] == settings.DEFAULTS["poll"]


def test_poll_float_string_is_clamped_as_int(cfg_path):
    write_toml(cfg_path, "poll = 250.9\n")
    # int(250.9) == 250, within range
    assert settings.load()["poll"] == 250


# --------------------------------------------------------------------------- #
# load() — theme whitelist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("theme", ["auto", "light", "dark"])
def test_theme_valid_values_kept(cfg_path, theme):
    write_toml(cfg_path, f'theme = "{theme}"\n')
    assert settings.load()["theme"] == theme


def test_theme_invalid_falls_back_to_auto(cfg_path):
    write_toml(cfg_path, 'theme = "neon"\n')
    assert settings.load()["theme"] == "auto"


def test_theme_non_string_falls_back_to_auto(cfg_path):
    write_toml(cfg_path, "theme = 5\n")
    assert settings.load()["theme"] == "auto"


# --------------------------------------------------------------------------- #
# load() — metrics dedup / filter
# --------------------------------------------------------------------------- #
def test_metrics_dedup_and_filter_preserves_order(cfg_path):
    write_toml(cfg_path, 'metrics = ["weekly", "weekly", "bogus", "session"]\n')
    assert settings.load()["metrics"] == ["weekly", "session"]


def test_metrics_all_invalid_falls_back_to_default(cfg_path):
    write_toml(cfg_path, 'metrics = ["bogus", "nope"]\n')
    assert settings.load()["metrics"] == ["session", "weekly"]


def test_metrics_empty_list_falls_back_to_default(cfg_path):
    write_toml(cfg_path, "metrics = []\n")
    assert settings.load()["metrics"] == ["session", "weekly"]


def test_metrics_non_list_falls_back_to_default(cfg_path):
    write_toml(cfg_path, 'metrics = "session"\n')
    assert settings.load()["metrics"] == ["session", "weekly"]


def test_metrics_single_valid_kept(cfg_path):
    write_toml(cfg_path, 'metrics = ["weekly"]\n')
    assert settings.load()["metrics"] == ["weekly"]


# --------------------------------------------------------------------------- #
# load() — alert_thresholds (1..100, dedup, sorted, no bools)
# --------------------------------------------------------------------------- #
def test_thresholds_sorted_deduped_and_range_filtered(cfg_path):
    write_toml(cfg_path, "alert_thresholds = [90, 90, 150, 0, 50, 1, 100]\n")
    assert settings.load()["alert_thresholds"] == [1, 50, 90, 100]


def test_thresholds_boundaries_kept(cfg_path):
    write_toml(cfg_path, "alert_thresholds = [1, 100]\n")
    assert settings.load()["alert_thresholds"] == [1, 100]


def test_thresholds_out_of_range_dropped(cfg_path):
    write_toml(cfg_path, "alert_thresholds = [0, 101, -5, 200]\n")
    # nothing valid -> default
    assert settings.load()["alert_thresholds"] == settings.DEFAULTS["alert_thresholds"]


def test_thresholds_bools_ignored(cfg_path):
    # true/false must be skipped even though bool is an int subclass.
    write_toml(cfg_path, "alert_thresholds = [true, false, 42]\n")
    assert settings.load()["alert_thresholds"] == [42]


def test_thresholds_non_list_falls_back(cfg_path):
    write_toml(cfg_path, "alert_thresholds = 80\n")
    assert settings.load()["alert_thresholds"] == settings.DEFAULTS["alert_thresholds"]


def test_thresholds_empty_list_falls_back(cfg_path):
    write_toml(cfg_path, "alert_thresholds = []\n")
    assert settings.load()["alert_thresholds"] == settings.DEFAULTS["alert_thresholds"]


# --------------------------------------------------------------------------- #
# load() — bool coercions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    [
        "alerts",
        "show_cost",
        "hide_on_fullscreen",
        "resume_notify",
        "resume_auto",
        "resume_skip_permissions",
    ],
)
def test_bool_keys_true(cfg_path, key):
    write_toml(cfg_path, f"{key} = true\n")
    assert settings.load()[key] is True


@pytest.mark.parametrize(
    "key",
    [
        "alerts",
        "show_cost",
        "hide_on_fullscreen",
        "resume_notify",
        "resume_auto",
        "resume_skip_permissions",
    ],
)
def test_bool_keys_false(cfg_path, key):
    write_toml(cfg_path, f"{key} = false\n")
    assert settings.load()[key] is False


def test_bool_coercion_from_int_zero(cfg_path):
    write_toml(cfg_path, "alerts = 0\n")
    assert settings.load()["alerts"] is False


def test_bool_coercion_from_int_nonzero(cfg_path):
    write_toml(cfg_path, "show_cost = 1\n")
    assert settings.load()["show_cost"] is True


def test_bool_coercion_from_nonempty_string(cfg_path):
    write_toml(cfg_path, 'hide_on_fullscreen = "x"\n')
    assert settings.load()["hide_on_fullscreen"] is True


# --------------------------------------------------------------------------- #
# load() — accent
# --------------------------------------------------------------------------- #
def test_accent_valid_hex_kept(cfg_path):
    write_toml(cfg_path, 'accent = "#5b61ea"\n')
    assert settings.load()["accent"] == "#5b61ea"


def test_accent_without_hash_becomes_none(cfg_path):
    write_toml(cfg_path, 'accent = "red"\n')
    assert settings.load()["accent"] is None


def test_accent_non_string_becomes_none(cfg_path):
    write_toml(cfg_path, "accent = 123\n")
    assert settings.load()["accent"] is None


def test_accent_none_in_file_uses_default_none(cfg_path):
    # An explicit accent line absent entirely -> default None.
    write_toml(cfg_path, "poll = 90\n")
    assert settings.load()["accent"] is None


# --------------------------------------------------------------------------- #
# load() — resume_prompt / resume_max_turns
# --------------------------------------------------------------------------- #
def test_resume_prompt_blank_falls_back(cfg_path):
    write_toml(cfg_path, 'resume_prompt = "   "\n')
    assert settings.load()["resume_prompt"] == settings.DEFAULTS["resume_prompt"]


def test_resume_prompt_empty_falls_back(cfg_path):
    write_toml(cfg_path, 'resume_prompt = ""\n')
    assert settings.load()["resume_prompt"] == settings.DEFAULTS["resume_prompt"]


def test_resume_prompt_custom_kept(cfg_path):
    write_toml(cfg_path, 'resume_prompt = "Resume now."\n')
    assert settings.load()["resume_prompt"] == "Resume now."


def test_resume_prompt_non_string_falls_back(cfg_path):
    write_toml(cfg_path, "resume_prompt = 5\n")
    assert settings.load()["resume_prompt"] == settings.DEFAULTS["resume_prompt"]


def test_resume_max_turns_clamped_low(cfg_path):
    write_toml(cfg_path, "resume_max_turns = 0\n")
    assert settings.load()["resume_max_turns"] == 1


def test_resume_max_turns_clamped_high(cfg_path):
    write_toml(cfg_path, "resume_max_turns = 9999\n")
    assert settings.load()["resume_max_turns"] == 200


def test_resume_max_turns_within_range(cfg_path):
    write_toml(cfg_path, "resume_max_turns = 42\n")
    assert settings.load()["resume_max_turns"] == 42


def test_resume_max_turns_bool_falls_back(cfg_path):
    write_toml(cfg_path, "resume_max_turns = true\n")
    assert settings.load()["resume_max_turns"] == settings.DEFAULTS["resume_max_turns"]


def test_resume_max_turns_non_numeric_falls_back(cfg_path):
    write_toml(cfg_path, 'resume_max_turns = "lots"\n')
    assert settings.load()["resume_max_turns"] == settings.DEFAULTS["resume_max_turns"]


# --------------------------------------------------------------------------- #
# load() — unknown keys from file are not surfaced
# --------------------------------------------------------------------------- #
def test_load_ignores_unknown_keys_from_file(cfg_path):
    write_toml(cfg_path, 'custom = "hi"\npoll = 100\n')
    cfg = settings.load()
    assert "custom" not in cfg  # only DEFAULTS keys are surfaced by load()
    assert cfg["poll"] == 100


# --------------------------------------------------------------------------- #
# save() / to_toml() round-trip via a real tmp file
# --------------------------------------------------------------------------- #
def test_save_creates_file(cfg_path):
    settings.save(dict(settings.DEFAULTS))
    assert cfg_path.exists()


def test_save_load_defaults_round_trip(cfg_path):
    settings.save(dict(settings.DEFAULTS))
    assert settings.load() == settings.DEFAULTS


def test_save_load_custom_round_trip(cfg_path):
    cfg = dict(settings.DEFAULTS)
    cfg.update(
        poll=120,
        theme="dark",
        metrics=["weekly", "session"],
        alerts=False,
        alert_thresholds=[50, 95],
        show_cost=True,
        accent="#123abc",
        hide_on_fullscreen=False,
        resume_notify=False,
        resume_auto=True,
        resume_prompt="Pick up the thread.",
        resume_skip_permissions=True,
        resume_max_turns=15,
    )
    settings.save(cfg)
    loaded = settings.load()
    assert loaded == cfg


def test_save_thresholds_get_sorted_on_reload(cfg_path):
    cfg = dict(settings.DEFAULTS)
    cfg["alert_thresholds"] = [95, 50]
    settings.save(cfg)
    assert settings.load()["alert_thresholds"] == [50, 95]


def test_to_toml_is_parseable_string():
    text = settings.to_toml(dict(settings.DEFAULTS))
    assert isinstance(text, str)
    assert text.endswith("\n")
    # tomllib (or mini-parser fallback) recovers the managed keys.
    parsed = settings._parse(text)
    assert parsed["poll"] == 90
    assert parsed["theme"] == "auto"
    assert parsed["metrics"] == ["session", "weekly"]


def test_to_toml_accent_none_is_commented_out():
    cfg = dict(settings.DEFAULTS)
    cfg["accent"] = None
    lines = settings.to_toml(cfg).splitlines()
    # No live `accent =` assignment...
    assert not any(ln.startswith("accent =") for ln in lines)
    # ...but the commented example is present.
    assert any(ln.strip() == '# accent = "#d97757"' for ln in lines)


def test_to_toml_accent_set_emits_assignment():
    cfg = dict(settings.DEFAULTS)
    cfg["accent"] = "#abcdef"
    lines = settings.to_toml(cfg).splitlines()
    assert 'accent = "#abcdef"' in lines


def test_to_toml_accent_missing_key_treated_as_none():
    cfg = dict(settings.DEFAULTS)
    del cfg["accent"]
    lines = settings.to_toml(cfg).splitlines()
    assert not any(ln.startswith("accent =") for ln in lines)


def test_save_accent_none_round_trips_to_none(cfg_path):
    cfg = dict(settings.DEFAULTS)
    cfg["accent"] = None
    settings.save(cfg)
    assert settings.load()["accent"] is None


def test_save_is_atomic_no_tmp_left_behind(cfg_path):
    settings.save(dict(settings.DEFAULTS))
    leftovers = list(cfg_path.parent.glob("*.tmp"))
    assert leftovers == []


def test_save_creates_parent_directory(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "cfg.toml"
    monkeypatch.setenv("CLAUDOMETER_CONFIG", str(nested))
    settings.save(dict(settings.DEFAULTS))
    assert nested.exists()


# --------------------------------------------------------------------------- #
# save() — unknown-key preservation
# --------------------------------------------------------------------------- #
def test_save_preserves_unknown_string_key(cfg_path):
    write_toml(cfg_path, 'poll = 90\ncustom_key = "hello"\n')
    settings.save(dict(settings.DEFAULTS))
    text = cfg_path.read_text(encoding="utf-8")
    assert 'custom_key = "hello"' in text


def test_save_preserves_unknown_numeric_key(cfg_path):
    write_toml(cfg_path, "my_num = 7\n")
    settings.save(dict(settings.DEFAULTS))
    text = cfg_path.read_text(encoding="utf-8")
    assert "my_num = 7" in text


def test_save_preserves_multiple_unknown_keys(cfg_path):
    write_toml(cfg_path, 'a = "x"\nb = 2\nc = true\n')
    settings.save(dict(settings.DEFAULTS))
    parsed = settings._parse(cfg_path.read_text(encoding="utf-8"))
    assert parsed["a"] == "x"
    assert parsed["b"] == 2
    assert parsed["c"] is True


def test_save_unknown_key_in_cfg_arg_is_kept(cfg_path):
    cfg = dict(settings.DEFAULTS)
    cfg["extra"] = "kept"
    settings.save(cfg)
    text = cfg_path.read_text(encoding="utf-8")
    assert 'extra = "kept"' in text


def test_save_explicit_arg_wins_over_disk_for_unknown_key(cfg_path):
    # If the same unknown key is on disk AND in the arg, the arg value is used
    # (merged[k] already set -> disk copy skipped).
    write_toml(cfg_path, 'shared = "from_disk"\n')
    cfg = dict(settings.DEFAULTS)
    cfg["shared"] = "from_arg"
    settings.save(cfg)
    parsed = settings._parse(cfg_path.read_text(encoding="utf-8"))
    assert parsed["shared"] == "from_arg"


def test_save_does_not_duplicate_known_keys_from_disk(cfg_path):
    write_toml(cfg_path, "poll = 200\n")
    settings.save(dict(settings.DEFAULTS))
    text = cfg_path.read_text(encoding="utf-8")
    # Known key appears exactly once as an assignment.
    assign_lines = [ln for ln in text.splitlines() if ln.startswith("poll =")]
    assert assign_lines == ["poll = 90"]


# --------------------------------------------------------------------------- #
# _strip_comment
# --------------------------------------------------------------------------- #
def test_strip_comment_plain():
    assert settings._strip_comment("90 # poll seconds") == "90"


def test_strip_comment_respects_double_quotes():
    assert settings._strip_comment('"#5b61ea"  # accent') == '"#5b61ea"'


def test_strip_comment_respects_single_quotes():
    assert settings._strip_comment("'#5b61ea' # x") == "'#5b61ea'"


def test_strip_comment_escaped_quote_inside_string():
    # A backslash-escaped quote does not end the "…" string.
    assert settings._strip_comment(r'"a\"b" # c') == r'"a\"b"'


def test_strip_comment_no_comment():
    assert settings._strip_comment("hello world") == "hello world"


def test_strip_comment_only_comment():
    assert settings._strip_comment("# just a comment") == ""


# --------------------------------------------------------------------------- #
# _split_top
# --------------------------------------------------------------------------- #
def test_split_top_basic():
    assert settings._split_top("1, 2, 3") == ["1", " 2", " 3"]


def test_split_top_respects_quoted_comma():
    assert settings._split_top('1, "a,b", 3') == ["1", ' "a,b"', " 3"]


def test_split_top_escaped_quote_in_string():
    # backslash-escaped quote keeps us inside the string
    assert settings._split_top(r'"a\"b", c') == [r'"a\"b"', " c"]


def test_split_top_trailing_empty_dropped():
    # trailing part that is only whitespace is not appended
    assert settings._split_top("1, 2, ") == ["1", " 2"]


def test_split_top_empty_string():
    assert settings._split_top("") == []


# --------------------------------------------------------------------------- #
# _unescape
# --------------------------------------------------------------------------- #
def test_unescape_backslash_and_quote():
    # \\ -> \ , \" -> "
    assert settings._unescape(r'a\\b\"c') == r'a\b' + '"c'


def test_unescape_removes_lone_backslash_before_char():
    # _unescape simply drops the backslash and keeps the next char.
    assert settings._unescape(r'a\b') == "ab"


def test_unescape_trailing_backslash_kept():
    # a trailing backslash with no following char is kept verbatim
    assert settings._unescape("abc\\") == "abc\\"


def test_unescape_no_escapes():
    assert settings._unescape("plain") == "plain"


# --------------------------------------------------------------------------- #
# _mini_val
# --------------------------------------------------------------------------- #
def test_mini_val_int():
    assert settings._mini_val("42") == 42
    assert isinstance(settings._mini_val("42"), int)


def test_mini_val_negative_int():
    assert settings._mini_val("-7") == -7


def test_mini_val_float():
    assert settings._mini_val("3.5") == 3.5


def test_mini_val_true_false_case_insensitive():
    assert settings._mini_val("true") is True
    assert settings._mini_val("TRUE") is True
    assert settings._mini_val("False") is False


def test_mini_val_double_quoted_unescaped():
    assert settings._mini_val(r'"a\"b"') == 'a"b'


def test_mini_val_single_quoted_literal():
    # single-quoted strings are literal — no unescaping
    assert settings._mini_val(r"'a\b'") == r"a\b"


def test_mini_val_empty_list():
    assert settings._mini_val("[]") == []


def test_mini_val_list_of_strings():
    assert settings._mini_val('["a", "b"]') == ["a", "b"]


def test_mini_val_list_of_ints():
    assert settings._mini_val("[1, 2, 3]") == [1, 2, 3]


def test_mini_val_bare_word_returns_string():
    assert settings._mini_val("hello") == "hello"


# --------------------------------------------------------------------------- #
# _mini_parse
# --------------------------------------------------------------------------- #
def test_mini_parse_skips_comments_and_blanks():
    text = "\n# comment\n\npoll = 90\n"
    assert settings._mini_parse(text) == {"poll": 90}


def test_mini_parse_multiple_keys():
    text = 'poll = 120\ntheme = "dark"\nalerts = true\n'
    out = settings._mini_parse(text)
    assert out == {"poll": 120, "theme": "dark", "alerts": True}


def test_mini_parse_strips_inline_comment():
    out = settings._mini_parse("poll = 90 # seconds\n")
    assert out == {"poll": 90}


def test_mini_parse_ignores_lines_without_equals():
    out = settings._mini_parse("this has no equals sign\npoll = 90\n")
    assert out == {"poll": 90}


def test_mini_parse_value_with_equals_kept():
    # only the first '=' splits key/value
    out = settings._mini_parse('resume_prompt = "a = b"\n')
    assert out["resume_prompt"] == "a = b"


# --------------------------------------------------------------------------- #
# CRITICAL: double-quoted string with embedded quotes/backslashes must survive
# _mini_parse(to_toml(...)) — the Python < 3.11 fallback round-trip.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        'He said "hi" and a back\\slash',
        'quote " only',
        "backslash \\ only",
        'both \\ and " together',
        'trailing backslash \\',
        r'C:\Users\path with "quotes"',
        'newline-less but with # hash and , comma',
        "plain value",
        '',  # empty string is a valid resume_prompt after we bypass load() validation
    ],
)
def test_mini_parse_roundtrip_tricky_strings(value):
    cfg = dict(settings.DEFAULTS)
    cfg["resume_prompt"] = value
    text = settings.to_toml(cfg)
    parsed = settings._mini_parse(text)
    assert parsed["resume_prompt"] == value, (
        f"round-trip failed: emitted={parsed['resume_prompt']!r} expected={value!r}"
    )


def test_mini_parse_roundtrip_accent_with_hash():
    cfg = dict(settings.DEFAULTS)
    cfg["accent"] = "#5b61ea"
    parsed = settings._mini_parse(settings.to_toml(cfg))
    # the '#' inside the quoted string must not be treated as a comment
    assert parsed["accent"] == "#5b61ea"


def test_mini_parse_roundtrip_full_config():
    cfg = dict(settings.DEFAULTS)
    cfg.update(
        poll=150,
        theme="light",
        metrics=["session"],
        alert_thresholds=[70, 85],
        show_cost=True,
        accent="#abc123",
        resume_prompt='say "go" now',
        resume_max_turns=12,
    )
    parsed = settings._mini_parse(settings.to_toml(cfg))
    assert parsed["poll"] == 150
    assert parsed["theme"] == "light"
    assert parsed["metrics"] == ["session"]
    assert parsed["alert_thresholds"] == [70, 85]
    assert parsed["show_cost"] is True
    assert parsed["accent"] == "#abc123"
    assert parsed["resume_prompt"] == 'say "go" now'
    assert parsed["resume_max_turns"] == 12


# --------------------------------------------------------------------------- #
# _parse fallback path (forced mini-parser via monkeypatched import)
# --------------------------------------------------------------------------- #
def test_parse_uses_mini_parser_when_tomllib_missing(force_mini_parser):
    cfg = dict(settings.DEFAULTS)
    cfg["resume_prompt"] = 'a "b" c\\d'
    cfg["poll"] = 120
    text = settings.to_toml(cfg)
    parsed = settings._parse(text)
    assert parsed["resume_prompt"] == 'a "b" c\\d'
    assert parsed["poll"] == 120


def test_parse_recovers_from_malformed_toml(force_mini_parser):
    # The tolerant mini-parser skips the junk line and keeps the valid ones.
    text = 'poll = 120\nthis is not valid toml !!!\ntheme = "dark"\n'
    parsed = settings._parse(text)
    assert parsed["poll"] == 120
    assert parsed["theme"] == "dark"


def test_full_load_via_mini_parser(cfg_path, force_mini_parser):
    # End-to-end: load() through the fallback parser, with validation applied.
    write_toml(
        cfg_path,
        'poll = 5\ntheme = "dark"\naccent = "#010203"\n'
        'metrics = ["weekly", "weekly", "session"]\n',
    )
    cfg = settings.load()
    assert cfg["poll"] == 60  # clamped
    assert cfg["theme"] == "dark"
    assert cfg["accent"] == "#010203"
    assert cfg["metrics"] == ["weekly", "session"]


def test_save_then_load_via_mini_parser(cfg_path, force_mini_parser):
    cfg = dict(settings.DEFAULTS)
    cfg.update(theme="light", accent="#ffeedd", resume_prompt='use "quotes"')
    settings.save(cfg)
    loaded = settings.load()
    assert loaded["theme"] == "light"
    assert loaded["accent"] == "#ffeedd"
    assert loaded["resume_prompt"] == 'use "quotes"'


# --------------------------------------------------------------------------- #
# Defaults integrity
# --------------------------------------------------------------------------- #
def test_defaults_unmodified_after_full_cycle(cfg_path):
    snapshot = copy.deepcopy(settings.DEFAULTS)
    settings.save(dict(settings.DEFAULTS))
    settings.load()
    assert settings.DEFAULTS == snapshot


def test_valid_theme_and_metric_constants():
    assert settings._VALID_THEMES == ("auto", "light", "dark")
    assert settings._VALID_METRICS == ("session", "weekly")
