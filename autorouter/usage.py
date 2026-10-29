"""Token accounting: turn Claude Code usage records into weighted cost.

Weighted cost approximates relative spend: input = 1, cache write = 1.25,
cache read = 0.1, output = 5, all multiplied by the model family weight.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

TAIL_BYTES = 2_000_000  # read at most the last ~2 MB of a transcript


def weighted(usage: dict, model: str | None, settings) -> float:
    if not isinstance(usage, dict):
        return 0.0
    tokens = (usage.get("input_tokens", 0) or 0) \
        + 1.25 * (usage.get("cache_creation_input_tokens", 0) or 0) \
        + 0.1 * (usage.get("cache_read_input_tokens", 0) or 0) \
        + 5 * (usage.get("output_tokens", 0) or 0)
    return tokens * config.model_weight(settings, model)


def agent_run(tool_response, agent_model: str, settings) -> dict:
    """Extract tokens, tool uses, duration, text and cost from an Agent/Task result.

    Field names vary across Claude Code versions, so every field is optional.
    """
    r = tool_response if isinstance(tool_response, dict) else {}
    text = _text(tool_response)
    usage = r.get("usage") if isinstance(r.get("usage"), dict) else None
    total = r.get("totalTokens") or r.get("total_tokens")
    if usage:
        cost = weighted(usage, agent_model, settings)
        total = total or sum(usage.get(k, 0) or 0 for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    elif total:
        # Totals only: assume a typical agent mix (mostly cached input, ~5% output).
        cost = total * 0.5 * config.model_weight(settings, agent_model)
    else:
        total = len(text) // 4
        cost = total * 5 * config.model_weight(settings, agent_model)
    return {
        "tokens": int(total or 0),
        "tool_uses": r.get("totalToolUseCount") or r.get("tool_uses"),
        "duration_ms": r.get("totalDurationMs") or r.get("duration_ms"),
        "text": text,
        "cost": float(cost),
    }


def _text(resp) -> str:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        content = resp.get("content", resp.get("result", ""))
        return _text(content)
    if isinstance(resp, list):
        return "\n".join(_text(x.get("text", x) if isinstance(x, dict) else x) for x in resp)
    return ""


def last_turn_cost(transcript_path: str | None, settings) -> float | None:
    """Weighted cost of the main session since the last real user prompt."""
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    entries = []
    for line in raw.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue

    total, seen = 0.0, set()
    for e in reversed(entries):
        if _is_user_prompt(e):
            break
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid = msg.get("id")
        if mid in seen:  # streamed messages repeat their usage per content block
            continue
        seen.add(mid)
        total += weighted(msg.get("usage") or {}, msg.get("model"), settings)
    return total


def _is_user_prompt(e) -> bool:
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    return False
