"""User configuration loaded from ~/.claudometer.toml (optional).

Zero-dependency: uses the stdlib ``tomllib`` when present (Python 3.11+),
otherwise a tiny built-in parser for the flat keys we support. Everything has a
sensible default, so the file is entirely optional. Changes apply on restart.

The built-in fallback parser (Python < 3.11 only) is line-oriented: it does NOT
support multiline strings/arrays, [tables], or string escape sequences (\\n, \\t,
\\uXXXX, ...). Keep values single-line. Python 3.11+ uses tomllib for full TOML.
"""

import os
from pathlib import Path

DEFAULTS = {
    "poll": 90,                       # seconds between polls (clamped 60-300)
    "theme": "auto",                  # auto | light | dark
    "metrics": ["session", "weekly"],  # which meters to show on the strip
    "alerts": True,                   # desktop toast when crossing a threshold
    "alert_thresholds": [80, 90],     # percentages that trigger an alert
    "show_cost": False,               # show an estimated-cost line in the popover
    "accent": None,                   # hex override for the accent color, or None
    "hide_on_fullscreen": True,       # hide over fullscreen apps; false = always show
    # Resume-on-reset (Tier 1 = notify + one click; Tier 2 = unattended)
    "resume_notify": True,            # notify + one-click resume when the session limit resets
    "resume_auto": False,             # Tier 2: automatically resume, unattended (opt-in, risky)
    "resume_prompt": "Continue where you left off.",  # continuation prompt used for Tier 2
    "resume_skip_permissions": False,  # Tier 2: pass --dangerously-skip-permissions (dangerous)
    "resume_max_turns": 30,           # Tier 2: cap agentic turns per unattended resume
}

_VALID_THEMES = ("auto", "light", "dark")
_VALID_METRICS = ("session", "weekly")


def config_path() -> Path:
    return Path(os.environ.get("CLAUDOMETER_CONFIG") or (Path.home() / ".claudometer.toml"))


def _strip_comment(v):
    """Drop an inline # comment, respecting quotes (so "#5b61ea" survives).
    A backslash-escaped quote inside a "…" string does not end the string."""
    out, quote, esc = [], None, False
    for ch in v:
        if esc:
            out.append(ch)
            esc = False
        elif quote:
            out.append(ch)
            if ch == "\\" and quote == '"':
                esc = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _split_top(s):
    """Split on top-level commas, respecting quotes and backslash escapes."""
    parts, buf, quote, esc = [], [], None, False
    for ch in s:
        if esc:
            buf.append(ch)
            esc = False
        elif quote:
            buf.append(ch)
            if ch == "\\" and quote == '"':
                esc = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def _unescape(s):
    """Reverse the escaping _toml_str emits (\\\\ -> \\, \\" -> ")."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _mini_val(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_mini_val(x) for x in _split_top(inner) if x.strip()] if inner else []
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        inner = v[1:-1]
        # double-quoted strings carry escapes; single-quoted are literal (TOML)
        return _unescape(inner) if v[0] == '"' else inner
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _mini_parse(text: str) -> dict:
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = _mini_val(_strip_comment(v.strip()))
    return out


def _parse(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass  # Python < 3.11
    except Exception:
        pass  # malformed TOML — recover what we can with the tolerant parser
    return _mini_parse(text)


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        p = config_path()
        if p.exists():
            data = _parse(p.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in data and data[k] is not None:
                    cfg[k] = data[k]
    except Exception:
        pass

    # normalize / validate
    try:
        cfg["poll"] = (DEFAULTS["poll"] if isinstance(cfg["poll"], bool)
                       else max(60, min(300, int(cfg["poll"]))))
    except (TypeError, ValueError):
        cfg["poll"] = DEFAULTS["poll"]
    if cfg["theme"] not in _VALID_THEMES:
        cfg["theme"] = "auto"
    if not isinstance(cfg["metrics"], list) or not cfg["metrics"]:
        cfg["metrics"] = list(DEFAULTS["metrics"])
    cfg["metrics"] = list(dict.fromkeys(m for m in cfg["metrics"] if m in _VALID_METRICS)) or ["session", "weekly"]
    cfg["alerts"] = bool(cfg["alerts"])
    thr = []
    for t in cfg["alert_thresholds"] if isinstance(cfg["alert_thresholds"], list) else []:
        if isinstance(t, bool):
            continue
        try:
            v = int(t)
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 100:
            thr.append(v)
    cfg["alert_thresholds"] = sorted(set(thr)) or list(DEFAULTS["alert_thresholds"])
    cfg["show_cost"] = bool(cfg["show_cost"])
    cfg["hide_on_fullscreen"] = bool(cfg["hide_on_fullscreen"])
    if not (isinstance(cfg["accent"], str) and cfg["accent"].startswith("#")):
        cfg["accent"] = None
    cfg["resume_notify"] = bool(cfg["resume_notify"])
    cfg["resume_auto"] = bool(cfg["resume_auto"])
    cfg["resume_skip_permissions"] = bool(cfg["resume_skip_permissions"])
    if not isinstance(cfg["resume_prompt"], str) or not cfg["resume_prompt"].strip():
        cfg["resume_prompt"] = DEFAULTS["resume_prompt"]
    try:
        cfg["resume_max_turns"] = (DEFAULTS["resume_max_turns"] if isinstance(cfg["resume_max_turns"], bool)
                                   else max(1, min(200, int(cfg["resume_max_turns"]))))
    except (TypeError, ValueError):
        cfg["resume_max_turns"] = DEFAULTS["resume_max_turns"]
    return cfg


# --------------------------------------------------------------------------- #
# Writing the config back (used by the in-app settings panel)
# --------------------------------------------------------------------------- #
def _toml_str(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return _toml_str(v)


def to_toml(cfg: dict) -> str:
    """Serialize a config dict to commented TOML matching the example file."""
    L = [
        "# Claudometer configuration — managed by the in-app settings panel.",
        "# You can still hand-edit this file; changes apply on restart (some live).",
        "",
        "# Seconds between usage polls (clamped 60-300).",
        f"poll = {_toml_val(cfg['poll'])}",
        "",
        '# "auto" follows your taskbar; force with "light" or "dark".',
        f"theme = {_toml_val(cfg['theme'])}",
        "",
        '# Meters shown on the strip: any of "session", "weekly".',
        f"metrics = {_toml_val(cfg['metrics'])}",
        "",
        "# Hide over fullscreen apps; false = always visible.",
        f"hide_on_fullscreen = {_toml_val(cfg['hide_on_fullscreen'])}",
        "",
        "# Desktop toast alerts when you cross a usage threshold.",
        f"alerts = {_toml_val(cfg['alerts'])}",
        f"alert_thresholds = {_toml_val(cfg['alert_thresholds'])}",
        "",
        "# Estimated token/cost line in the popover.",
        f"show_cost = {_toml_val(cfg['show_cost'])}",
        "",
        "# Accent color override (hex).",
    ]
    L.append(f"accent = {_toml_val(cfg['accent'])}" if cfg.get("accent") else '# accent = "#d97757"')
    L += [
        "",
        "# --- Resume when your 5-hour session limit resets ---",
        f"resume_notify = {_toml_val(cfg['resume_notify'])}",
        f"resume_auto = {_toml_val(cfg['resume_auto'])}   # Tier 2: unattended (risky)",
        f"resume_prompt = {_toml_val(cfg['resume_prompt'])}",
        f"resume_max_turns = {_toml_val(cfg['resume_max_turns'])}",
        f"resume_skip_permissions = {_toml_val(cfg['resume_skip_permissions'])}",
    ]
    extras = [k for k in cfg if k not in DEFAULTS]
    if extras:
        L.append("")
        for k in extras:
            L.append(f"{k} = {_toml_val(cfg[k])}")
    L.append("")
    return "\n".join(L)


def save(cfg: dict) -> None:
    """Atomically write the config to disk (temp file + os.replace)."""
    p = config_path()
    merged = dict(cfg)
    try:  # keep any keys we don't manage that the user added by hand
        if p.exists():
            for k, v in _parse(p.read_text(encoding="utf-8")).items():
                if k not in DEFAULTS and k not in merged:
                    merged[k] = v
    except Exception:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / ("%s.%d.tmp" % (p.name, os.getpid()))  # unique per process
    tmp.write_text(to_toml(merged), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # best-effort; POSIX-only, a no-op on Windows
    except OSError:
        pass
    try:
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            os.remove(str(tmp))  # don't leave the temp behind on failure
        except OSError:
            pass
        raise
