# Context Tool

A Claude Code status line showing live context window usage and cumulative
session token totals, rendered on one line beneath the input prompt:

```
ctx 172k/1.0M 17% · in 282k (+11.9M cached) · out 62.1k · $10.31
```

| Segment | Meaning |
| --- | --- |
| `ctx 172k/1.0M 17%` | Tokens currently in the context window, the window size, and the percentage used. Coloured green below 50%, yellow below 80%, red above. |
| `in 282k` | Fresh input tokens sent this session: `input_tokens + cache_creation_input_tokens`. |
| `(+11.9M cached)` | Tokens re-read from the prompt cache. Omitted when zero. |
| `out 62.1k` | Output tokens generated, including thinking. |
| `$10.31` | Total session cost, taken from `cost.total_cost_usd`. Omitted only if the payload carries no cost at all; a genuine zero shows as `$0.00`. |

## Install

```json
{
  "statusLine": {
    "type": "command",
    "command": "python \"/path/to/statusline_context.py\""
  }
}
```

Add that to `~/.claude/settings.json`, pointing `command` at your checkout. Set
`NO_COLOR=1` to disable ANSI colour.

## Where the numbers come from

Context figures are read straight from the status line payload's
`context_window` object, so they track `/compact` and the model's real window
size (1M for `opus-5[1m]`, 200k otherwise) without any guessing.

Session totals are **not** in that payload, so they are summed from the session
transcript at `transcript_path`, plus any subagent transcripts in the sibling
`<session-id>/subagents/` directory.

Two details make that sum correct rather than merely plausible:

- **Deduplication by request id.** A single API response is written to the
  transcript as several lines — text, thinking, tool_use — each repeating an
  identical `usage` block. In one measured session that was 182 entries for 100
  real API calls; summing blind reported 19.4M cache-read tokens against a true
  11.9M.
- **Fresh input is kept separate from cache reads.** They differ by three orders
  of magnitude, so collapsing them into one "input" figure is meaningless.

## Known limitation

Session totals run roughly 2-6% below Claude Code's own accounting, because
internal and Haiku-model calls (session title generation and similar) never
appear in the transcript. Measured against a `cost-state` record: 277k vs 284k
fresh input, 11.1M vs 11.9M cached, 61.7k vs 62.1k output.

Claude Code writes an exact `cost-state` entry, but only at session end, so it
cannot be used for a live display. Treat the session figures as indicative and
the context figures as exact.

The cost figure comes from Claude Code rather than from this script, but it is
also an estimate: it is computed client-side at list price and may differ from
an actual bill. It resets to `$0` when `/clear` starts a new session, whereas
the token totals are read from the transcript and so follow that same session
boundary.

## Tests

```bash
python -m unittest test_statusline_context -v
```

Covers token formatting, request-id deduplication, subagent discovery,
malformed and truncated transcript lines, the null `context_window` that
appears before the first API call and immediately after `/compact`, and
end-to-end subprocess behaviour including UTF-8 output on a legacy console
code page.
