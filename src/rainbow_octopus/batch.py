"""Build a list of ideas unattended (``rocto batch``).

``scripts/run_benchmarks.py`` used to shell out to ``rocto build`` once per
case and then dig the outcome back out of each build's files. The batch
runner does the same job in-process, for any list of ideas, and reuses
:func:`report.collect` so the batch table and every build's own report agree.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import json
import time

from .orchestrator import BuildError, slugify
from .report import collect


def load_cases(path: Path) -> list[dict[str, str]]:
    """A JSON list of ``{"id", "idea"}`` or of strings, or one idea per line."""
    text = path.read_text(encoding="utf-8")
    cases: list[dict[str, str]] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        for index, item in enumerate(data, start=1):
            if isinstance(item, str):
                cases.append({"id": "", "idea": item})
            elif isinstance(item, dict) and isinstance(item.get("idea"), str):
                cases.append({"id": str(item.get("id") or ""), "idea": item["idea"]})
            else:
                raise ValueError(f"entry {index} has no 'idea'")
    elif data is not None:
        raise ValueError("expected a JSON list")
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cases.append({"id": "", "idea": line})
    if not cases:
        raise ValueError("no ideas found")

    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        base = slugify(case["id"]) or slugify(case["idea"], 24) or f"case-{index}"
        name = base
        counter = 2
        while name in seen:
            name = f"{base}-{counter}"
            counter += 1
        seen.add(name)
        case["id"] = name
    return cases


def run_batch(
    cases: list[dict[str, str]],
    root: Path,
    make_orchestrator: Callable[[], Any],
    *,
    model: str = "",
    min_verified: int | None = None,
    echo: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    batch_dir = root / f"batch-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for index, case in enumerate(cases, start=1):
        output = batch_dir / case["id"]
        if echo:
            print(f"\n=== [{index}/{len(cases)}] {case['id']}: {case['idea'][:80]} ===", flush=True)
        started = time.monotonic()
        exit_code = 0
        message = ""
        try:
            make_orchestrator().build(case["idea"], output, model)
        except BuildError as exc:
            exit_code = exc.exit_code
            message = str(exc).splitlines()[0][:300]
        seconds = round(time.monotonic() - started, 1)

        facts: dict[str, Any] = {
            "id": case["id"],
            "idea": case["idea"],
            "output": str(output),
            "exit_code": exit_code,
            "seconds": seconds,
            "page_openable": (output / "index.html").is_file(),
            "verified": False,
            "attempts": 0,
            "executors": [],
            "failed_checks": [],
            "message": message,
        }
        if (output / ".rocto" / "run.json").is_file():
            summary = collect(output)
            facts.update(
                verified=summary.verdict == "passed",
                attempts=len([a for a in summary.attempts if a.get("outcome") != "running"]),
                executors=[a.get("executor") for a in summary.attempts if a.get("executor")],
                failed_checks=[
                    f"{line.description or line.name}: {line.detail[:120]}"
                    for group in summary.groups
                    for line in group.lines
                    if not line.passed
                ][:10],
                checks=f"{summary.checks_passed}/{summary.checks_total}",
                cost_usd=summary.cost_usd,
            )
        results.append(facts)
        if echo:
            print(f"--- {case['id']}: {_mark(facts)} in {seconds}s", flush=True)

    openable = sum(item["page_openable"] for item in results)
    verified = sum(item["verified"] for item in results)
    if min_verified is None:
        gate = verified == len(results)
        requirement = "every idea verified"
    else:
        gate = openable == len(results) and verified >= min_verified
        requirement = f"all openable, >={min_verified} verified"
    summary = {
        "batch_dir": str(batch_dir),
        "gate_passed": gate,
        "requirement": requirement,
        "openable": f"{openable}/{len(results)}",
        "verified": f"{verified}/{len(results)}",
        "results": results,
    }
    (batch_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (batch_dir / "summary.md").write_text(render_batch_markdown(summary), encoding="utf-8")
    if echo:
        print(render_batch_table(summary))
    return summary


def _mark(item: dict[str, Any]) -> str:
    if item["verified"]:
        return "PASS"
    return "PAGE" if item["page_openable"] else "FAIL"


def render_batch_table(summary: dict[str, Any]) -> str:
    rows = summary["results"]
    lines = ["", "=" * 72, f"{'case':<22}{'result':<8}{'checks':<9}{'secs':<8}executors", "-" * 72]
    for item in rows:
        executors = ",".join(dict.fromkeys(item.get("executors") or [])) or "-"
        lines.append(
            f"{item['id'][:21]:<22}{_mark(item):<8}{item.get('checks', '-'):<9}"
            f"{item['seconds']:<8}{executors}"
        )
    lines.append("-" * 72)
    lines.append(f"openable {summary['openable']}   verified {summary['verified']}")
    lines.append(f"GATE: {'PASS' if summary['gate_passed'] else 'FAIL'} ({summary['requirement']})")
    lines.append(f"Summary: {Path(summary['batch_dir']) / 'summary.json'}")
    return "\n".join(lines)


def render_batch_markdown(summary: dict[str, Any]) -> str:
    icon = "✅" if summary["gate_passed"] else "❌"
    out = [
        f"### {icon} Batch: {summary['verified']} verified, {summary['openable']} openable",
        "",
        f"Gate: **{'PASS' if summary['gate_passed'] else 'FAIL'}** ({summary['requirement']})",
        "",
        "| case | result | checks | seconds | executors |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in summary["results"]:
        executors = ", ".join(dict.fromkeys(item.get("executors") or [])) or "-"
        out.append(
            f"| {item['id']} | {_mark(item)} | {item.get('checks', '-')} | "
            f"{item['seconds']} | {executors} |"
        )
    failures = [item for item in summary["results"] if not item["verified"]]
    if failures:
        out += ["", "**Failures**", ""]
        for item in failures:
            reason = item["failed_checks"][0] if item["failed_checks"] else item["message"] or "-"
            out.append(f"- `{item['id']}`: {reason}")
    return "\n".join(out) + "\n"
