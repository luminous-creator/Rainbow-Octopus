"""A local, append-only record of every build (ADR-006).

ADR-002 keeps the router at fixed priority on purpose: routing by success
rate needs success rates, and nobody had any. ``run_benchmarks.py`` collected
a table per release; day-to-day builds collected nothing.

Every build and resume now appends one line to ``<ROCTO_HOME>/ledger.jsonl``:
who planned, who wrote each attempt, how verification went, how long it took,
what it cost. ``rocto stats`` summarises it. The idea itself is stored only
as a hash — the ledger is evidence about the tools, not a copy of what the
user asked for.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any
import hashlib
import json

from . import __version__
from .config import rocto_home
from .state import RunState, utc_now


class Ledger:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def default(cls) -> "Ledger":
        return cls(rocto_home() / "ledger.jsonl")

    def record(
        self,
        project_dir: Path,
        state: RunState,
        outcome: str,
        seconds: float,
        since: str | None = None,
    ) -> dict[str, Any]:
        attempts = [
            {
                "attempt": item.get("attempt"),
                "executor": item.get("executor"),
                "outcome": item.get("outcome"),
                "seconds": item.get("seconds"),
                "cost_usd": item.get("cost_usd"),
                "failed_checks": item.get("failed_checks", [])[:10],
            }
            for item in state.attempts
            if since is None or str(item.get("started_at", "")) >= since
        ]
        entry = {
            "at": utc_now(),
            "version": __version__,
            "project": str(project_dir),
            "idea_sha256": hashlib.sha256(state.idea.encode("utf-8")).hexdigest()[:16],
            "kind": state.kind,
            "outcome": outcome,
            "phase": state.phase,
            "planner": state.planner,
            "executor_choice": state.executor,
            "attempts": attempts,
            "seconds": round(seconds, 1),
            "cost_usd": state.cost_usd,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        result = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn write must not hide every other record
            if isinstance(item, dict):
                result.append(item)
        return result


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-executor verification pass rates, durations and spend."""
    builds = len(entries)
    completed = sum(1 for item in entries if item.get("outcome") == "completed")
    durations = [item["seconds"] for item in entries if isinstance(item.get("seconds"), (int, float))]
    costs = [item["cost_usd"] for item in entries if isinstance(item.get("cost_usd"), (int, float))]

    executors: dict[str, dict[str, Any]] = {}
    first_attempts = {"total": 0, "passed": 0}
    for item in entries:
        attempts = item.get("attempts") or []
        verified = [a for a in attempts if a.get("outcome") in {"passed", "verification_failed"}]
        if verified and verified[0].get("attempt") == 1:
            first_attempts["total"] += 1
            first_attempts["passed"] += verified[0].get("outcome") == "passed"
        for attempt in attempts:
            name = attempt.get("executor") or "unknown"
            row = executors.setdefault(
                name,
                {"attempts": 0, "verified": 0, "passed": 0, "crashed": 0, "seconds": [], "cost_usd": 0.0},
            )
            row["attempts"] += 1
            outcome = attempt.get("outcome")
            if outcome in {"passed", "verification_failed"}:
                row["verified"] += 1
                row["passed"] += outcome == "passed"
            elif outcome == "execution_failed":
                row["crashed"] += 1
            if isinstance(attempt.get("seconds"), (int, float)):
                row["seconds"].append(attempt["seconds"])
            if isinstance(attempt.get("cost_usd"), (int, float)):
                row["cost_usd"] += attempt["cost_usd"]

    table = {}
    for name, row in sorted(executors.items()):
        table[name] = {
            "attempts": row["attempts"],
            "verified": row["verified"],
            "passed": row["passed"],
            "pass_rate": round(row["passed"] / row["verified"], 3) if row["verified"] else None,
            "execution_failures": row["crashed"],
            "median_seconds": round(median(row["seconds"]), 1) if row["seconds"] else None,
            "cost_usd": round(row["cost_usd"], 4),
        }
    return {
        "builds": builds,
        "completed": completed,
        "success_rate": round(completed / builds, 3) if builds else None,
        "first_attempt_pass_rate": (
            round(first_attempts["passed"] / first_attempts["total"], 3)
            if first_attempts["total"] else None
        ),
        "median_build_seconds": round(median(durations), 1) if durations else None,
        "total_cost_usd": round(sum(costs), 4) if costs else None,
        "executors": table,
    }
