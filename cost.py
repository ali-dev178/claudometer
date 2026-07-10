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
from datetime import datetime
from pathlib import Path

PRICING = {
    "opus":   {"input": 5,  "output": 25, "cache_write": 6.25,  "cache_read": 0.50},
    "sonnet": {"input": 3,  "output": 15, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1,  "output": 5,  "cache_write": 1.25,  "cache_read": 0.10},
    "fable":  {"input": 10, "output": 50, "cache_write": 12.50, "cache_read": 1.00},
}


def _key_for(model: str):
    m = str(model or "").lower()
    if "fable" in m:
        return "fable"
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return None


def _line_cost(usage: dict, key: str) -> float:
    p = PRICING[key]
    return (usage.get("input_tokens", 0) * p["input"]
            + usage.get("output_tokens", 0) * p["output"]
            + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]
            + usage.get("cache_read_input_tokens", 0) * p["cache_read"]) / 1_000_000.0


def compute_today(config_dir=None):
    """Return {"tokens": int, "cost": float} for usage since local midnight,
    or None if there's nothing to read."""
    base = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    proj = base / "projects"
    if not proj.exists():
        return None

    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = start.timestamp()
    total_tokens, total_cost = 0, 0.0
    seen = set()  # message ids already counted (Claude Code repeats usage per block-line)

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
                        msg = obj.get("message", {})
                        # Claude Code writes one line per content block, each
                        # carrying the SAME cumulative usage — count each id once.
                        mid = msg.get("id")
                        if mid is not None:
                            if mid in seen:
                                continue
                            seen.add(mid)
                        key = _key_for(msg.get("model", ""))
                        if not key:
                            continue
                        u = msg.get("usage", {})
                        total_tokens += (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                                         + u.get("cache_creation_input_tokens", 0)
                                         + u.get("cache_read_input_tokens", 0))
                        total_cost += _line_cost(u, key)
                    except Exception:
                        continue
        except OSError:
            continue

    return {"tokens": total_tokens, "cost": total_cost}


if __name__ == "__main__":
    print(compute_today())
