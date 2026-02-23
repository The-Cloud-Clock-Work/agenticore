"""Pytest conftest — generates a markdown smoke test report."""

import os
from datetime import datetime, timezone
from pathlib import Path

# Accumulator
_results: list[dict] = []


def pytest_runtest_logreport(report):
    """Hook: called after each test phase (setup/call/teardown)."""
    if report.when != "call":
        return
    if report.failed:
        message = str(report.longrepr)[:200]
    elif report.skipped:
        message = getattr(report, "wasxfail", "") or ""
    else:
        message = ""

    _results.append(
        {
            "nodeid": report.nodeid,
            "name": report.nodeid.split("::")[-1],
            "outcome": report.outcome,  # "passed", "failed", "skipped"
            "duration": round(report.duration, 2),
            "message": message,
        }
    )


def pytest_sessionfinish(session, exitstatus):
    """Hook: called after all tests complete. Write the report."""
    report_path = os.getenv("SMOKE_REPORT_PATH", "")
    if not report_path:
        return

    passed = sum(1 for r in _results if r["outcome"] == "passed")
    failed = sum(1 for r in _results if r["outcome"] == "failed")
    skipped = sum(1 for r in _results if r["outcome"] == "skipped")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Agenticore Smoke Test Report",
        "",
        f"**Run date:** {now}",
        f"**Result:** {passed} passed, {failed} failed, {skipped} skipped",
        "",
        "## Results",
        "",
        "| # | Test | Result | Duration | Detail |",
        "|---|------|--------|----------|--------|",
    ]

    ICONS = {"passed": ":white_check_mark:", "failed": ":x:", "skipped": ":fast_forward:"}

    for i, r in enumerate(_results, 1):
        icon = ICONS.get(r["outcome"], "?")
        name = r["name"].replace("test_", "").replace("_", " ").title()
        detail = r["message"].replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(f"| {i} | {name} | {icon} {r['outcome'].title()} | {r['duration']}s | {detail} |")

    lines.append("")
    Path(report_path).write_text("\n".join(lines))
