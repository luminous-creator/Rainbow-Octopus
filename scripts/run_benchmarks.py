"""Run the five frozen Rainbow Octopus v0.1 acceptance prompts.

Release gate: all five must produce an openable page within two repairs, and at
least four must pass automated interaction verification.

This is now a thin wrapper around ``rocto batch`` (which also records, per
case, which executors wrote the page). Every build lands in the local ledger,
so ``rocto stats`` afterwards shows the per-executor pass rates a smarter
router would need.

Usage:
    python scripts/run_benchmarks.py [--executor auto|claude|codex|deepseek]
                                     [--max-retries N] [any other batch option]
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rainbow_octopus import cli  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return cli.main(
        [
            "batch",
            str(ROOT / "benchmarks" / "cases.json"),
            "--output-root",
            str(ROOT / "benchmark-runs"),
            "--min-verified",
            "4",
            *args,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
