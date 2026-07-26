"""Run the five frozen Rainbow Octopus v0.1 acceptance prompts.

Release gate: all five must produce an openable page within two repairs, and at
least four must pass automated interaction verification.

Also records, per case, which executor the router chose and why the others were
passed over. That table is the evidence a smarter router would need in v0.2 —
right now nobody has it, which is exactly why the router is fixed-priority.

Usage:
    python scripts/run_benchmarks.py [--executor auto|claude|codex|deepseek]
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _inspect(output: Path) -> dict:
    """Pull the interesting facts out of one finished build."""
    facts: dict = {
        "page_openable": (output / "index.html").is_file(),
        "screenshot": (output / "screenshot.png").is_file(),
        "executor": None,
        "passed_over": [],
        "attempts": None,
        "failed_checks": [],
        "verified": False,
    }

    for attempt in range(1, 4):
        router = _read_json(output / ".rocto" / "logs" / f"router-attempt-{attempt}.json")
        if router:
            facts["executor"] = router.get("winner")
            facts["passed_over"] = router.get("skipped_or_failed", [])

    state = _read_json(output / ".rocto" / "run.json")
    if state:
        facts["attempts"] = state.get("attempt")

    report = _read_json(output / "acceptance-report.json")
    if report:
        facts["verified"] = bool(report.get("passed"))
        facts["failed_checks"] = [
            f"{check['name']}: {check['detail'][:120]}"
            for check in report.get("checks", [])
            if not check.get("passed")
        ]
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", default="auto")
    parser.add_argument("--max-retries", type=int, default=2, choices=range(0, 3))
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "benchmarks" / "cases.json").read_text(encoding="utf-8"))
    run_dir = root / "benchmark-runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)

    results = []
    for index, case in enumerate(cases, start=1):
        output = run_dir / case["id"]
        command = [
            sys.executable, "-m", "rainbow_octopus", "build", case["idea"],
            "--output", str(output),
            "--executor", args.executor,
            "--max-retries", str(args.max_retries),
        ]
        print(f"\n=== [{index}/{len(cases)}] {case['id']} ===", flush=True)
        started = time.monotonic()
        completed = subprocess.run(command, check=False)
        elapsed = round(time.monotonic() - started, 1)

        facts = _inspect(output)
        results.append({
            "id": case["id"],
            "exit_code": completed.returncode,
            "seconds": elapsed,
            "output": str(output),
            **facts,
        })
        mark = "PASS" if facts["verified"] else ("PAGE" if facts["page_openable"] else "FAIL")
        print(
            f"--- {case['id']}: {mark} in {elapsed}s "
            f"via {facts['executor'] or args.executor}",
            flush=True,
        )

    openable = sum(item["page_openable"] for item in results)
    verified = sum(item["verified"] for item in results)
    gate = openable == len(results) and verified >= 4

    summary = {
        "executor": args.executor,
        "gate_passed": gate,
        "openable": f"{openable}/{len(results)}",
        "verified": f"{verified}/{len(results)}",
        "requirement": "all openable, >=4 verified",
        "results": results,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 66)
    print(f"{'case':<18}{'result':<8}{'executor':<12}{'secs':<8}attempts")
    print("-" * 66)
    for item in results:
        mark = "PASS" if item["verified"] else ("PAGE" if item["page_openable"] else "FAIL")
        print(
            f"{item['id']:<18}{mark:<8}{str(item['executor'] or '-'):<12}"
            f"{item['seconds']:<8}{item['attempts'] or '-'}"
        )
    print("-" * 66)
    print(f"openable {openable}/{len(results)}   verified {verified}/{len(results)}")
    print(f"RELEASE GATE: {'PASS' if gate else 'FAIL'} (all openable, >=4 verified)")
    print(f"Report: {run_dir / 'summary.json'}")
    return 0 if gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
