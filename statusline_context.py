#!/usr/bin/env python3
"""Claude Code status line: context window usage and session token totals.

Reads the status line payload on stdin and prints one line, e.g.

    ctx 172k/1.0M 17% · in 282k (+11.9M cached) · out 62.1k

Context figures come straight from the payload's `context_window` object.
Session totals are summed from the session transcript, which the payload
does not carry.
"""

import glob
import json
import os
import sys

DEFAULT_CONTEXT_WINDOW = 200000

RESET = "\033[0m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def fmt_tokens(n):
    """Render a token count compactly: 999, 9.4k, 62.1k, 282k, 11.9M."""
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 100000:
        return "{:.1f}k".format(n / 1000)
    if n < 1000000:
        return "{:.0f}k".format(n / 1000)
    return "{:.1f}M".format(n / 1000000)


def transcript_files(transcript_path):
    """The main transcript plus any subagent transcripts beside it.

    Subagent transcripts live in `<transcript-stem>/subagents/agent-*.jsonl`
    rather than inline in the main file.
    """
    if not transcript_path:
        return []
    stem, _ = os.path.splitext(transcript_path)
    subagents = os.path.join(stem, "subagents", "*.jsonl")
    return [transcript_path] + sorted(glob.glob(subagents))


def sum_usage(paths):
    """Total the token usage recorded across the given transcript files.

    A single API response is written as several transcript lines (text,
    thinking, tool_use), each repeating an identical usage block, so entries
    are deduplicated by request id before summing.
    """
    totals = {"fresh": 0, "cached": 0, "out": 0}
    seen = set()
    for path in paths:
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message") or {}
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                key = entry.get("requestId") or message.get("id")
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                totals["fresh"] += usage.get("input_tokens", 0) or 0
                totals["fresh"] += usage.get("cache_creation_input_tokens", 0) or 0
                totals["cached"] += usage.get("cache_read_input_tokens", 0) or 0
                totals["out"] += usage.get("output_tokens", 0) or 0
    return totals


def _pct_color(pct):
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def render(payload, color=True):
    """Build the status line from a parsed status line payload."""
    window = payload.get("context_window") or {}
    used = window.get("total_input_tokens", 0) or 0
    size = window.get("context_window_size") or DEFAULT_CONTEXT_WINDOW
    pct = window.get("used_percentage")
    if pct is None:
        pct = (used / size * 100) if size else 0
    pct = int(pct + 0.5)

    totals = sum_usage(transcript_files(payload.get("transcript_path")))

    inbound = "in " + fmt_tokens(totals["fresh"])
    if totals["cached"]:
        inbound += " (+{} cached)".format(fmt_tokens(totals["cached"]))

    head = "ctx {}/{} ".format(fmt_tokens(used), fmt_tokens(size))
    tail = "% · {} · out {}".format(inbound, fmt_tokens(totals["out"]))

    cost = (payload.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        tail += " · ${:.2f}".format(cost)

    if not color:
        return head + str(pct) + tail
    return "{}{}{}{}{}{}{}{}".format(
        DIM, head, RESET, _pct_color(pct), pct, RESET, DIM + tail, RESET
    )


def use_color():
    return not os.environ.get("NO_COLOR")


def build_line(stdin_text, color=None):
    """Render the status line, degrading to an empty line on bad input."""
    if color is None:
        color = use_color()
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            return ""
        return render(payload, color=color)
    except Exception:
        return ""


def main():
    # Windows consoles default to a legacy code page that may not hold the
    # separator, so pin both streams to UTF-8 rather than inherit it.
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        # newline="\n" keeps Windows text mode from appending a carriage
        # return inside the rendered status line.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except (AttributeError, OSError):
        pass
    print(build_line(sys.stdin.read()))


if __name__ == "__main__":
    main()
