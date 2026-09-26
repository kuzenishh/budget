#!/usr/bin/env python3
"""budget - see where your Claude Code session's context and tokens went.

Single file, no dependencies. Reads a .jsonl session transcript and shows
which tools ate the tokens, which files got re-read, what the biggest tool
outputs were, and where the agent looped retrying the same thing.

Read-only. Never modifies the transcript.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# parsing


def load_lines(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def content_text_len(content) -> int:
    """Rough char count of a tool_result's content, whatever shape it's in."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                total += len(part.get("text", "") or "")
            elif isinstance(part, str):
                total += len(part)
        return total
    return len(json.dumps(content, ensure_ascii=False))


def chars_to_tokens(n: int) -> int:
    # rough rule of thumb, not exact
    return n // 4


class Session:
    """One parsed transcript: turns, tool calls, and usage."""

    def __init__(self, path: Path, last_n: int = None):
        self.path = path
        self.usage_totals = defaultdict(int)  # input/output/cache_read/cache_creation
        self.usage_by_turn = []  # one dict per assistant message
        self.tool_calls = []  # {id, name, input, line, turn}
        self.tool_results = {}  # tool_use_id -> {chars, is_error}
        self.file_reads = defaultdict(int)  # file_path -> times Read was called

        pending_tool_use = {}  # tool_use_id -> tool_call dict, until its result shows up
        turn = 0

        for line_no, obj in enumerate(load_lines(path), 1):
            t = obj.get("type")

            if t == "assistant":
                turn += 1
                msg = obj.get("message", {})
                usage = msg.get("usage") or {}
                row = {
                    "turn": turn,
                    "line": line_no,
                    "input": usage.get("input_tokens", 0) or 0,
                    "output": usage.get("output_tokens", 0) or 0,
                    "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                    "cache_creation": usage.get("cache_creation_input_tokens", 0) or 0,
                }
                self.usage_by_turn.append(row)
                for k in ("input", "output", "cache_read", "cache_creation"):
                    self.usage_totals[k] += row[k]

                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            call = {
                                "id": part.get("id"),
                                "name": part.get("name", "?"),
                                "input": part.get("input", {}) or {},
                                "line": line_no,
                                "turn": turn,
                            }
                            self.tool_calls.append(call)
                            pending_tool_use[call["id"]] = call
                            if call["name"] == "Read":
                                fp = call["input"].get("file_path")
                                if fp:
                                    self.file_reads[fp] += 1

            elif t == "user":
                msg = obj.get("message", {})
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "tool_result":
                            tid = part.get("tool_use_id")
                            chars = content_text_len(part.get("content"))
                            self.tool_results[tid] = {
                                "chars": chars,
                                "is_error": bool(part.get("is_error")),
                                "line": line_no,
                            }

        if last_n:
            keep_turns = set(t["turn"] for t in self.usage_by_turn[-last_n:])
            self.usage_by_turn = [t for t in self.usage_by_turn if t["turn"] in keep_turns]
            self.tool_calls = [c for c in self.tool_calls if c["turn"] in keep_turns]
            self.usage_totals = defaultdict(int)
            for row in self.usage_by_turn:
                for k in ("input", "output", "cache_read", "cache_creation"):
                    self.usage_totals[k] += row[k]
            self.file_reads = defaultdict(int)
            for call in self.tool_calls:
                if call["name"] == "Read":
                    fp = call["input"].get("file_path")
                    if fp:
                        self.file_reads[fp] += 1

    def by_tool(self):
        """chars of tool_result output, grouped by tool name."""
        agg = defaultdict(lambda: {"calls": 0, "chars": 0, "errors": 0})
        for call in self.tool_calls:
            res = self.tool_results.get(call["id"], {})
            bucket = agg[call["name"]]
            bucket["calls"] += 1
            bucket["chars"] += res.get("chars", 0)
            bucket["errors"] += 1 if res.get("is_error") else 0
        return agg

    def biggest_outputs(self, n=10):
        rows = []
        for call in self.tool_calls:
            res = self.tool_results.get(call["id"])
            if not res:
                continue
            rows.append((res["chars"], call["name"], call, res))
        rows.sort(key=lambda r: r[0], reverse=True)
        return rows[:n]

    def repeated_reads(self, min_hits=2):
        return {fp: n for fp, n in self.file_reads.items() if n >= min_hits}

    def retry_loops(self, window=6):
        """Same tool + same input repeated within a short window, or an error
        immediately followed by the same call again."""
        loops = []
        n = len(self.tool_calls)
        for i, call in enumerate(self.tool_calls):
            key = (call["name"], json.dumps(call["input"], sort_keys=True, default=str))
            hits = [call]
            for j in range(i + 1, min(i + 1 + window, n)):
                other = self.tool_calls[j]
                okey = (other["name"], json.dumps(other["input"], sort_keys=True, default=str))
                if okey == key:
                    hits.append(other)
            if len(hits) >= 3:
                loops.append((call["name"], call["input"], len(hits), call["line"]))
        # dedupe: keep the first occurrence of each (name, input) loop
        seen = set()
        out = []
        for name, inp, count, line in loops:
            key = (name, json.dumps(inp, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            out.append((name, inp, count, line))
        return out


# --------------------------------------------------------------------------- #
# formatting


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def truncate(s: str, n=80) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_scan(args):
    sess = Session(Path(args.path), last_n=args.last)
    u = sess.usage_totals
    total_in = u["input"] + u["cache_read"] + u["cache_creation"]
    print(f"budget: {sess.path.name}")
    if args.last:
        print(f"(last {args.last} assistant turns)")
    print()
    print("== tokens (from real API usage, not estimated) ==")
    print(f"  input (fresh):     {fmt_int(u['input']):>12}")
    print(f"  input (cache read):{fmt_int(u['cache_read']):>12}")
    print(f"  input (cache write):{fmt_int(u['cache_creation']):>11}")
    print(f"  output:            {fmt_int(u['output']):>12}")
    print(f"  total input:       {fmt_int(total_in):>12}")
    print()

    print("== by tool (output size, not counting the model's own tokens) ==")
    by_tool = sess.by_tool()
    for name, agg in sorted(by_tool.items(), key=lambda kv: -kv[1]["chars"]):
        err = f"  ({agg['errors']} errors)" if agg["errors"] else ""
        print(f"  {name:<14} {agg['calls']:>4} calls  ~{fmt_int(chars_to_tokens(agg['chars'])):>8} tok{err}")
    print()

    rereads = sess.repeated_reads()
    if rereads:
        print("== files read more than once ==")
        for fp, n in sorted(rereads.items(), key=lambda kv: -kv[1]):
            print(f"  {n}x  {fp}")
        print()

    loops = sess.retry_loops()
    if loops:
        print("== possible retry loops (same tool + same input, repeated) ==")
        for name, inp, count, line in loops:
            print(f"  {name}  x{count}  (first at line {line})  {truncate(inp)}")
        print()

    if not rereads and not loops:
        print("no repeated reads or retry loops detected.")


def cmd_top(args):
    sess = Session(Path(args.path), last_n=args.last)
    rows = sess.biggest_outputs(args.n)
    if not rows:
        print("budget: no tool outputs found")
        return
    print(f"== biggest tool outputs (top {len(rows)}) ==")
    for chars, name, call, res in rows:
        flag = " [ERROR]" if res.get("is_error") else ""
        print(f"  ~{fmt_int(chars_to_tokens(chars)):>7} tok  {name:<12} line {call['line']}{flag}")
        preview = truncate(call["input"], 100)
        if preview and preview != "{}":
            print(f"           input: {preview}")


def build_parser():
    p = argparse.ArgumentParser(
        prog="budget",
        description="See where a Claude Code session's context and tokens went.",
    )
    p.add_argument("--version", action="version", version=f"budget {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="overview of a session transcript")
    s.add_argument("path", help=".jsonl session log")
    s.add_argument("--last", type=int, default=None, help="only the last N assistant turns")
    s.set_defaults(fn=cmd_scan)

    t = sub.add_parser("top", help="biggest tool outputs in a session")
    t.add_argument("path", help=".jsonl session log")
    t.add_argument("-n", type=int, default=10, help="how many to show (default 10)")
    t.add_argument("--last", type=int, default=None, help="only the last N assistant turns")
    t.set_defaults(fn=cmd_top)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except FileNotFoundError as exc:
        sys.exit(f"budget: {exc}")


if __name__ == "__main__":
    main()
