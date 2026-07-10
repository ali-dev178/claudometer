"""User configuration loaded from ~/.claudometer.toml (optional).

Zero-dependency: uses the stdlib ``tomllib`` when present (Python 3.11+),
otherwise a tiny built-in parser for the flat keys we support. Everything has a
sensible default, so the file is entirely optional. Changes apply on restart.
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
}

_VALID_THEMES = ("auto", "light", "dark")
_VALID_METRICS = ("session", "weekly")


def config_path() -> Path:
    return Path(os.environ.get("CLAUDOMETER_CONFIG") or (Path.home() / ".claudometer.toml"))


def _mini_val(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_mini_val(x) for x in inner.split(",") if x.strip()] if inner else []
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
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


def _parse(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = _mini_val(v)
    return out


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
        cfg["poll"] = max(60, min(300, int(cfg["poll"])))
    except (TypeError, ValueError):
        cfg["poll"] = DEFAULTS["poll"]
    if cfg["theme"] not in _VALID_THEMES:
        cfg["theme"] = "auto"
    if not isinstance(cfg["metrics"], list) or not cfg["metrics"]:
        cfg["metrics"] = list(DEFAULTS["metrics"])
    cfg["metrics"] = [m for m in cfg["metrics"] if m in _VALID_METRICS] or ["session", "weekly"]
    cfg["alerts"] = bool(cfg["alerts"])
    try:
        cfg["alert_thresholds"] = sorted({int(t) for t in cfg["alert_thresholds"] if 1 <= int(t) <= 100})
    except (TypeError, ValueError):
        cfg["alert_thresholds"] = list(DEFAULTS["alert_thresholds"])
    cfg["show_cost"] = bool(cfg["show_cost"])
    if not (isinstance(cfg["accent"], str) and cfg["accent"].startswith("#")):
        cfg["accent"] = None
    return cfg
