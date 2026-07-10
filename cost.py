"""Estimate today's token usage and cost from local Claude Code transcripts.

Reads ``~/.claude/projects/**/*.jsonl`` (the session logs Claude Code writes),
sums per-message token counts, and applies Anthropic list prices. This is an
ESTIMATE of what the same tokens would cost at API rates — it is not your actual
plan billing (Pro/Max are flat-rate).

Prices: USD per million tokens, verified from the official pricing page
(platform.claude.com) on 2026-07-10. `cache_write` = 5-minute cache write
(1.25x input); `cache_read` = cache hit (0.1x input). Prices are matched by model
family (opus/sonnet/haiku/fable) and assume the current generation — a future
model sharing a family name but priced differently would use these rates.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

PRICING = {
    "opus":   {"input": 5,  "output": 25, "cache_write": 6.25,  "cache_read": 0.50},
    "sonnet": {"input": 3,  "output": 15, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1,  "output": 5,  "cache_write": 1.25,  "cache_read": 0.10},
    "fable":  {"input": 10, "output": 50, "cache_write": 12.50, "cache_read": 1.00},
}


def _key_for(model: str):
    m = str(model or "").lower()
    if "fable" in m or "mythos" in m:  # Mythos 5 shares Fable 5 pricing
        return "fable"
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return None


def _n(usage: dict, k: str) -> float:
    v = usage.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _tok(usage: dict) -> float:
    return (_n(usage, "input_tokens") + _n(usage, "output_tokens")
            + _n(usage, "cache_creation_input_tokens") + _n(usage, "cache_read_input_tokens"))


def _line_cost(usage: dict, key: str) -> float:
    p = PRICING[key]
    return (_n(usage, "input_tokens") * p["input"]
            + _n(usage, "output_tokens") * p["output"]
            + _n(usage, "cache_creation_input_tokens") * p["cache_write"]
            + _n(usage, "cache_read_input_tokens") * p["cache_read"]) / 1_000_000.0


def compute_today(config_dir=None):
    """Return {"tokens": int, "cost": float} for usage since local midnight,
    or None if there's nothing to read."""
    base = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    proj = base / "projects"
    if not proj.exists():
        return None

    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = start.timestamp()
    # Claude Code writes one line per content block for an assistant message,
    # all sharing the message id. Input/cache tokens are identical across them
    # but output_tokens GROW (streaming snapshots), so keep the LAST occurrence
    # per id (the complete cumulative usage) rather than summing or keeping first.
    by_id = {}                          # message id -> (usage, key), last wins
    extra_tokens, extra_cost = 0, 0.0   # lines without an id (counted as-is)

    for f in proj.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:  # file untouched today -> skip fast
                continue
        except OSError:
            continue
        try:
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:  # one malformed line must not abort the whole scan
                        obj = json.loads(line)
                        if obj.get("type") != "assistant":
                            continue
                        ts = obj.get("timestamp")
                        if isinstance(ts, str):
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if dt < start:
                                continue
                        else:
                            continue  # no valid timestamp -> not counted as "today"
                        msg = obj.get("message", {})
                        key = _key_for(msg.get("model", ""))
                        if not key:
                            continue
                        u = msg.get("usage", {})
                        mid = msg.get("id")
                        if mid is not None:
                            by_id[mid] = (u, key)
                        else:
                            extra_tokens += _tok(u)
                            extra_cost += _line_cost(u, key)
                    except Exception:
                        continue
        except OSError:
            continue

    total_tokens = extra_tokens + sum(_tok(u) for u, _ in by_id.values())
    total_cost = extra_cost + sum(_line_cost(u, key) for u, key in by_id.values())
    return {"tokens": int(total_tokens), "cost": total_cost}


if __name__ == "__main__":
    print(compute_today())
