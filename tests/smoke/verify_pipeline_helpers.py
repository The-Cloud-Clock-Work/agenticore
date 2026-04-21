"""Pure-function helpers for the SSE pipeline audit script.

Imported by tests/smoke/verify_streaming_pipeline.sh via `python3 -m` for the
parsing + cross-validation passes, and by tests/unit/test_verify_pipeline_helpers.py
for isolated correctness tests.

No I/O, no kubectl, no redis — just string → structured data transforms.
"""

import json
import sys
from typing import Any, Iterable


SSE_DATA_PREFIX = "data: "
DONE_MARKER = "[DONE]"


def parse_sse_chunk(raw_line: str) -> dict[str, Any] | None:
    """Parse a single `data: {json}\\n\\n` SSE line into a structured chunk dict.

    Returns None for non-data lines (blank, event:, etc.). Returns the special
    {'event_type': 'done_marker'} for the literal `data: [DONE]`.
    """
    line = raw_line.strip()
    if not line.startswith(SSE_DATA_PREFIX):
        return None
    payload = line[len(SSE_DATA_PREFIX) :].strip()
    if payload == DONE_MARKER:
        return {"event_type": "done_marker", "raw": line}
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return None

    choice = (body.get("choices") or [{}])[0]
    delta = choice.get("delta", {}) or {}
    finish_reason = choice.get("finish_reason")
    x_type = choice.get("x_agenticore_event_type")

    if x_type in ("thinking", "tool_use", "tool_result", "stream_config"):
        event_type = x_type
    elif delta.get("role") == "assistant":
        event_type = "role_open"
    elif finish_reason == "stop":
        event_type = "stop_chunk"
    elif "content" in delta or "tool_calls" in delta:
        event_type = "assistant_text"
    else:
        event_type = "unknown"

    out = {
        "event_type": event_type,
        "id": body.get("id", ""),
        "finish_reason": finish_reason,
        "raw": line,
    }
    if "content" in delta:
        out["content"] = delta["content"] or ""
    if "tool_calls" in delta:
        calls = delta["tool_calls"] or []
        out["tool_calls"] = []
        for c in calls:
            fn = (c or {}).get("function", {}) or {}
            out["tool_calls"].append(
                {
                    "id": (c or {}).get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                }
            )
    if "usage" in body:
        out["usage"] = body["usage"]
    if x_type:
        out["x_type"] = x_type
    tu_id = choice.get("x_agenticore_tool_use_id")
    if tu_id:
        out["tool_use_id"] = tu_id
    return out


def parse_sse_stream(text: str) -> list[dict[str, Any]]:
    """Parse a full raw SSE response into an ordered list of chunks."""
    chunks: list[dict[str, Any]] = []
    for line in text.splitlines():
        parsed = parse_sse_chunk(line)
        if parsed is not None:
            chunks.append(parsed)
    return chunks


def parse_redis_event(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Normalize a Redis XADD entry into a structured event dict."""
    return {
        "entry_id": entry_id,
        "event_type": fields.get("event_type", "unknown"),
        "content": fields.get("content", ""),
        "ts": fields.get("ts", ""),
        "session_id": fields.get("session_id", ""),
        "correlation_id": fields.get("correlation_id", ""),
    }


def parse_transcript_blocks(jsonl_text: str) -> list[dict[str, Any]]:
    """Walk a claude transcript JSONL and extract per-block summaries.

    Returns a list of {seq, entry_type, block_type, preview, raw_input} where
    entry_type is 'assistant' or 'user' and block_type is one of
    text|thinking|tool_use|tool_result.
    """
    out: list[dict[str, Any]] = []
    seq = 0
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry_type = entry.get("type", "")
        if entry_type not in ("assistant", "user"):
            continue
        content = (entry.get("message", {}) or {}).get("content")
        if isinstance(content, str):
            out.append(
                {
                    "seq": seq,
                    "entry_type": entry_type,
                    "block_type": "text",
                    "preview": content[:200],
                }
            )
            seq += 1
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text":
                full_text = block.get("text", "")
            elif bt == "thinking":
                full_text = block.get("thinking", "")
            elif bt == "tool_use":
                full_text = json.dumps(
                    {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                )
            elif bt == "tool_result":
                raw = block.get("content", "")
                if isinstance(raw, list):
                    parts = []
                    for p in raw:
                        if isinstance(p, dict) and p.get("type") == "text":
                            parts.append(p.get("text", ""))
                    raw = "\n".join(parts)
                full_text = f"tool_use_id={block.get('tool_use_id', '')} :: {str(raw)}"
            else:
                full_text = f"<{bt}>"
            out.append(
                {
                    "seq": seq,
                    "entry_type": entry_type,
                    "block_type": bt,
                    "preview": full_text[:200],
                    "full": full_text,
                }
            )
            seq += 1
    return out


def count_by(items: Iterable[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for it in items:
        v = it.get(key, "")
        counts[v] = counts.get(v, 0) + 1
    return counts


def marker_in_sse_tool_use(chunks: list[dict], marker: str) -> bool:
    for c in chunks:
        if c["event_type"] != "tool_use":
            continue
        for call in c.get("tool_calls", []):
            if marker in (call.get("arguments") or ""):
                return True
    return False


def marker_in_sse_tool_result(chunks: list[dict], marker: str) -> bool:
    for c in chunks:
        if c["event_type"] != "tool_result":
            continue
        if marker in (c.get("content") or ""):
            return True
    return False


def marker_in_redis(events: list[dict], event_type: str, marker: str) -> bool:
    for e in events:
        if e["event_type"] != event_type:
            continue
        if marker in (e.get("content") or ""):
            return True
    return False


def marker_in_transcript(blocks: list[dict], block_type: str, marker: str) -> bool:
    for b in blocks:
        if b["block_type"] != block_type:
            continue
        haystack = b.get("full") or b.get("preview") or ""
        if marker in haystack:
            return True
    return False


def check_ordering(chunks: list[dict]) -> dict[str, bool]:
    """Verify the expected ordering pattern of SSE chunks."""
    types = [c["event_type"] for c in chunks]
    if not types:
        return {"has_role_open_first": False, "no_events_after_done": True, "stop_before_done": False}

    has_role_first = types[0] == "role_open"

    done_idx = -1
    for i, t in enumerate(types):
        if t == "done_marker":
            done_idx = i
            break
    no_after = done_idx == -1 or done_idx == len(types) - 1

    stop_before = False
    if done_idx >= 1 and types[done_idx - 1] == "stop_chunk":
        stop_before = True

    return {
        "has_role_open_first": has_role_first,
        "no_events_after_done": no_after,
        "stop_before_done": stop_before,
    }


def cross_validate(
    sse_chunks: list[dict],
    redis_events: list[dict],
    transcript_blocks: list[dict],
    marker: str,
    correlation_uuid: str,
) -> dict[str, Any]:
    """Compute the full audit report from parsed layer data.

    Pure function. Returns dict ready to dump to audit-report.json.
    """
    sse_counts = count_by(sse_chunks, "event_type")
    redis_counts = count_by(redis_events, "event_type")
    transcript_counts = count_by(transcript_blocks, "block_type")

    propagation = {
        "sse_tool_use": marker_in_sse_tool_use(sse_chunks, marker),
        "sse_tool_result": marker_in_sse_tool_result(sse_chunks, marker),
        "redis_tool_use": marker_in_redis(redis_events, "tool_use", marker),
        "redis_tool_result": marker_in_redis(redis_events, "tool_result", marker),
        "transcript_tool_use": marker_in_transcript(transcript_blocks, "tool_use", marker),
        "transcript_tool_result": marker_in_transcript(transcript_blocks, "tool_result", marker),
    }

    ordering = check_ordering(sse_chunks)

    redis_correlations = {e.get("correlation_id", "") for e in redis_events}
    all_correlated = not redis_events or (len(redis_correlations) == 1 and correlation_uuid in redis_correlations)

    sse_tu = sse_counts.get("tool_use", 0)
    sse_tr = sse_counts.get("tool_result", 0)
    redis_tu = redis_counts.get("tool_use", 0)
    redis_tr = redis_counts.get("tool_result", 0)
    transcript_tu = transcript_counts.get("tool_use", 0)
    transcript_tr = transcript_counts.get("tool_result", 0)

    consistency = {
        "sse_tool_use_count_matches_redis": sse_tu == redis_tu,
        "sse_tool_result_count_matches_redis": sse_tr == redis_tr,
        "redis_tool_use_count_matches_transcript": redis_tu == transcript_tu,
        "redis_tool_result_count_matches_transcript": redis_tr == transcript_tr,
    }

    has_done_marker = any(c["event_type"] == "done_marker" for c in sse_chunks)

    report = {
        "event_counts": {
            "sse": sse_counts,
            "redis": redis_counts,
            "transcript": transcript_counts,
        },
        "marker_propagation": propagation,
        "ordering": ordering,
        "consistency": consistency,
        "correlation_ids_in_redis": sorted(redis_correlations),
        "all_redis_events_correlated": all_correlated,
        "has_done_marker": has_done_marker,
    }

    report["overall_pass"] = (
        all(propagation.values())
        and all(consistency.values())
        and ordering["has_role_open_first"]
        and ordering["no_events_after_done"]
        and ordering["stop_before_done"]
        and all_correlated
        and has_done_marker
    )

    failing: list[str] = []
    for k, v in propagation.items():
        if not v:
            failing.append(f"marker_propagation.{k}")
    for k, v in consistency.items():
        if not v:
            failing.append(f"consistency.{k}")
    for k, v in ordering.items():
        if not v:
            failing.append(f"ordering.{k}")
    if not all_correlated:
        failing.append("correlation_ids_in_redis")
    if not has_done_marker:
        failing.append("has_done_marker")
    report["failing_checks"] = failing

    return report


def render_report_md(report: dict[str, Any], meta: dict[str, Any]) -> str:
    verdict = "**PASS**" if report.get("overall_pass") else "**FAIL**"
    lines: list[str] = []
    lines.append(f"# SSE Pipeline Audit — {verdict}")
    lines.append("")
    lines.append(f"- run_id: `{meta.get('run_id', '')}`")
    lines.append(f"- agent: `{meta.get('agent', '')}`")
    lines.append(f"- correlation_uuid: `{meta.get('correlation_uuid', '')}`")
    lines.append(f"- marker: `{meta.get('marker', '')}`")
    lines.append(f"- started_at: {meta.get('started_at', '')}")
    lines.append(f"- ended_at: {meta.get('ended_at', '')}")
    lines.append("")
    lines.append("## Event counts")
    lines.append("")
    lines.append("| Layer | Counts |")
    lines.append("|---|---|")
    for layer, counts in report.get("event_counts", {}).items():
        pretty = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(none)"
        lines.append(f"| {layer} | {pretty} |")
    lines.append("")
    lines.append("## Marker propagation")
    lines.append("")
    for k, v in report.get("marker_propagation", {}).items():
        icon = "✓" if v else "✗"
        lines.append(f"- {icon} `{k}`")
    lines.append("")
    lines.append("## Ordering")
    lines.append("")
    for k, v in report.get("ordering", {}).items():
        icon = "✓" if v else "✗"
        lines.append(f"- {icon} `{k}`")
    lines.append("")
    lines.append("## Consistency")
    lines.append("")
    for k, v in report.get("consistency", {}).items():
        icon = "✓" if v else "✗"
        lines.append(f"- {icon} `{k}`")
    lines.append("")
    if report.get("failing_checks"):
        lines.append("## Failing checks")
        lines.append("")
        for f in report["failing_checks"]:
            lines.append(f"- {f}")
        lines.append("")
    lines.append(f"Artifacts: `{meta.get('artifact_dir', '')}`")
    return "\n".join(lines) + "\n"


def _cli() -> int:
    """CLI entry point used by the shell script to run cross_validate.

    Reads four files named as positional args and writes report to two more:
      argv[1]  sse-stream.raw
      argv[2]  redis-events.jsonl  (one {entry_id, fields} JSON per line)
      argv[3]  transcript.jsonl
      argv[4]  run-meta.json
      argv[5]  audit-report.json   (output)
      argv[6]  audit-report.md     (output)
    """
    if len(sys.argv) != 7:
        print(
            "usage: verify_pipeline_helpers.py <sse> <redis> <transcript> <meta> <out.json> <out.md>", file=sys.stderr
        )
        return 2

    sse_path, redis_path, transcript_path, meta_path, out_json, out_md = sys.argv[1:7]

    with open(sse_path) as f:
        sse_text = f.read()
    sse_chunks = parse_sse_stream(sse_text)

    redis_events: list[dict] = []
    try:
        with open(redis_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fields = rec.get("fields", {}) or {}
                redis_events.append(parse_redis_event(rec.get("entry_id", ""), fields))
    except FileNotFoundError:
        pass

    try:
        with open(transcript_path) as f:
            transcript_text = f.read()
    except FileNotFoundError:
        transcript_text = ""
    transcript_blocks = parse_transcript_blocks(transcript_text)

    with open(meta_path) as f:
        meta = json.load(f)
    marker = meta.get("marker", "")
    correlation_uuid = meta.get("correlation_uuid", "")

    report = cross_validate(sse_chunks, redis_events, transcript_blocks, marker, correlation_uuid)

    with open(out_json, "w") as f:
        json.dump(report, f, indent=2)
    with open(out_md, "w") as f:
        f.write(render_report_md(report, meta))

    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
