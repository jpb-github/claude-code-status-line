"""Tests for statusline_context."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import statusline_context as sc


def write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def assistant(request_id, usage, msg_id="msg_1"):
    return {
        "type": "assistant",
        "requestId": request_id,
        "message": {"id": msg_id, "usage": usage},
    }


def usage(inp=0, cache_create=0, cache_read=0, out=0):
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
        "output_tokens": out,
    }


class FormatTokensTest(unittest.TestCase):
    def test_under_one_thousand_is_a_plain_integer(self):
        self.assertEqual(sc.fmt_tokens(0), "0")
        self.assertEqual(sc.fmt_tokens(999), "999")

    def test_below_one_hundred_thousand_keeps_one_decimal(self):
        self.assertEqual(sc.fmt_tokens(1000), "1.0k")
        self.assertEqual(sc.fmt_tokens(9449), "9.4k")
        self.assertEqual(sc.fmt_tokens(10000), "10.0k")
        self.assertEqual(sc.fmt_tokens(62100), "62.1k")

    def test_at_or_above_one_hundred_thousand_drops_the_decimal(self):
        self.assertEqual(sc.fmt_tokens(100000), "100k")
        self.assertEqual(sc.fmt_tokens(282499), "282k")

    def test_millions_use_m_with_one_decimal(self):
        self.assertEqual(sc.fmt_tokens(1000000), "1.0M")
        self.assertEqual(sc.fmt_tokens(11890397), "11.9M")


class SumUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_counts_one_api_call_once_despite_repeated_lines(self):
        # A single API response is written as several transcript lines
        # (text, thinking, tool_use), each carrying an identical usage block.
        p = self.path("t.jsonl")
        write_jsonl(p, [
            assistant("req_A", usage(inp=10, cache_read=1000, out=50)),
            assistant("req_A", usage(inp=10, cache_read=1000, out=50)),
            assistant("req_A", usage(inp=10, cache_read=1000, out=50)),
        ])
        totals = sc.sum_usage([p])
        self.assertEqual(totals["cached"], 1000)
        self.assertEqual(totals["out"], 50)

    def test_separates_fresh_input_from_cache_reads(self):
        p = self.path("t.jsonl")
        write_jsonl(p, [
            assistant("req_A", usage(inp=10, cache_create=90, cache_read=5000, out=7)),
        ])
        totals = sc.sum_usage([p])
        self.assertEqual(totals["fresh"], 100)  # input + cache_creation
        self.assertEqual(totals["cached"], 5000)
        self.assertEqual(totals["out"], 7)

    def test_accumulates_across_distinct_requests_and_files(self):
        a, b = self.path("a.jsonl"), self.path("b.jsonl")
        write_jsonl(a, [assistant("req_A", usage(inp=1, out=2))])
        write_jsonl(b, [assistant("req_B", usage(inp=10, out=20))])
        totals = sc.sum_usage([a, b])
        self.assertEqual(totals["fresh"], 11)
        self.assertEqual(totals["out"], 22)

    def test_ignores_entries_that_are_not_assistant_responses(self):
        p = self.path("t.jsonl")
        write_jsonl(p, [
            {"type": "user", "message": {"usage": usage(inp=999)}},
            {"type": "cost-state", "modelUsage": {}},
            assistant("req_A", usage(inp=5)),
        ])
        self.assertEqual(sc.sum_usage([p])["fresh"], 5)

    def test_tolerates_malformed_and_partial_lines(self):
        p = self.path("t.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("not json at all\n")
            fh.write(json.dumps({"type": "assistant", "message": {}}) + "\n")
            fh.write(json.dumps(assistant("req_A", usage(inp=5))) + "\n")
            fh.write('{"type": "assistant", "truncated"')  # no trailing newline
        self.assertEqual(sc.sum_usage([p])["fresh"], 5)

    def test_falls_back_to_message_id_when_request_id_is_absent(self):
        p = self.path("t.jsonl")
        write_jsonl(p, [
            {"type": "assistant", "message": {"id": "m1", "usage": usage(inp=5)}},
            {"type": "assistant", "message": {"id": "m1", "usage": usage(inp=5)}},
        ])
        self.assertEqual(sc.sum_usage([p])["fresh"], 5)

    def test_missing_file_contributes_nothing(self):
        totals = sc.sum_usage([self.path("nope.jsonl")])
        self.assertEqual(totals, {"fresh": 0, "cached": 0, "out": 0})


class TranscriptFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_includes_subagent_transcripts_from_the_sibling_directory(self):
        main = os.path.join(self.tmp.name, "sess.jsonl")
        open(main, "w").close()
        subs = os.path.join(self.tmp.name, "sess", "subagents")
        os.makedirs(subs)
        agent = os.path.join(subs, "agent-abc.jsonl")
        open(agent, "w").close()
        # A sidecar metadata file that must not be treated as a transcript.
        open(os.path.join(subs, "agent-abc.meta.json"), "w").close()

        found = sc.transcript_files(main)
        self.assertEqual(found, [main, agent])

    def test_returns_just_the_main_file_when_no_subagents_ran(self):
        main = os.path.join(self.tmp.name, "sess.jsonl")
        open(main, "w").close()
        self.assertEqual(sc.transcript_files(main), [main])

    def test_returns_nothing_for_a_missing_transcript_path(self):
        self.assertEqual(sc.transcript_files(""), [])
        self.assertEqual(sc.transcript_files(None), [])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transcript = os.path.join(self.tmp.name, "sess.jsonl")

    def payload(self, **overrides):
        data = {
            "transcript_path": self.transcript,
            "context_window": {
                "total_input_tokens": 172032,
                "context_window_size": 1000000,
                "used_percentage": 17.2,
            },
        }
        data.update(overrides)
        return data

    def test_renders_context_and_session_totals_on_one_line(self):
        write_jsonl(self.transcript, [
            assistant("req_A", usage(inp=2000, cache_create=280000,
                                     cache_read=11890397, out=62100)),
        ])
        self.assertEqual(
            sc.render(self.payload(), color=False),
            "ctx 172k/1.0M 17% · in 282k (+11.9M cached) · out 62.1k",
        )

    def test_omits_the_cached_suffix_when_nothing_was_read_from_cache(self):
        write_jsonl(self.transcript, [assistant("req_A", usage(inp=500, out=100))])
        self.assertEqual(
            sc.render(self.payload(), color=False),
            "ctx 172k/1.0M 17% · in 500 · out 100",
        )

    def test_reports_zeroed_context_before_the_first_api_response(self):
        # context_window is absent until the session's first API call.
        self.assertEqual(
            sc.render({"transcript_path": self.transcript}, color=False),
            "ctx 0/200k 0% · in 0 · out 0",
        )

    def test_computes_the_percentage_when_the_payload_omits_it(self):
        line = sc.render(
            self.payload(context_window={
                "total_input_tokens": 50000,
                "context_window_size": 200000,
            }),
            color=False,
        )
        self.assertTrue(line.startswith("ctx 50.0k/200k 25% "), line)

    def test_survives_a_null_context_window_after_compact(self):
        line = sc.render(self.payload(context_window=None), color=False)
        self.assertTrue(line.startswith("ctx 0/200k 0% "), line)

    def test_appends_the_session_cost_rounded_to_cents(self):
        write_jsonl(self.transcript, [assistant("req_A", usage(inp=500, out=100))])
        line = sc.render(
            self.payload(cost={"total_cost_usd": 10.312850499999996}),
            color=False,
        )
        self.assertEqual(
            line, "ctx 172k/1.0M 17% · in 500 · out 100 · $10.31")

    def test_shows_a_zero_cost_rather_than_hiding_it(self):
        line = sc.render(self.payload(cost={"total_cost_usd": 0}), color=False)
        self.assertTrue(line.endswith(" · $0.00"), line)

    def test_omits_the_cost_when_the_payload_carries_none(self):
        line = sc.render(self.payload(cost={}), color=False)
        self.assertNotIn("$", line)

    def test_colorizes_the_percentage_when_color_is_enabled(self):
        write_jsonl(self.transcript, [assistant("req_A", usage(inp=1))])
        line = sc.render(self.payload(), color=True)
        self.assertIn("\033[", line)
        self.assertTrue(line.endswith("\033[0m"))


class MainTest(unittest.TestCase):
    def test_never_raises_on_garbage_stdin(self):
        # The status line must degrade quietly rather than spew a traceback.
        self.assertEqual(sc.build_line("this is not json"), "")

    def test_builds_a_line_from_a_json_payload(self):
        line = sc.build_line(json.dumps({"context_window": {
            "total_input_tokens": 5000,
            "context_window_size": 200000,
            "used_percentage": 2.5,
        }}), color=False)
        self.assertEqual(line, "ctx 5.0k/200k 3% · in 0 · out 0")


class SubprocessTest(unittest.TestCase):
    """The script runs as a shell command, so exercise it as one."""

    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "statusline_context.py")

    def run_script(self, stdin_text, **env_overrides):
        env = dict(os.environ, NO_COLOR="1", **env_overrides)
        return subprocess.run(
            [sys.executable, self.SCRIPT],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            env=env,
        )

    def test_emits_utf8_even_when_the_console_encoding_cannot_hold_it(self):
        # Windows consoles default to a legacy code page; the separator must
        # not blow up the status line there.
        result = self.run_script(json.dumps({}), PYTHONIOENCODING="ascii")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stderr, b"")
        self.assertIn("·", result.stdout.decode("utf-8"))

    def test_terminates_the_line_with_a_bare_newline(self):
        # Windows text mode would otherwise emit CRLF and leave a stray
        # carriage return inside the rendered status line.
        result = self.run_script(json.dumps({}))
        self.assertNotIn(b"\r", result.stdout)
        self.assertTrue(result.stdout.endswith(b"\n"), result.stdout)

    def test_prints_an_empty_line_and_succeeds_on_garbage_input(self):
        result = self.run_script("}{ not json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"")


if __name__ == "__main__":
    unittest.main()
