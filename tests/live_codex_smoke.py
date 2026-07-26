"""Manual live test for the Codex subprocess adapter.

Usage from the repository root:
    $env:PYTHONPATH = "src;."
    python tests/live_codex_smoke.py demo-output/codex-smoke
"""

from pathlib import Path
import sys

from rainbow_octopus.executor import CodexExecutor
from rainbow_octopus.orchestrator import prepare_output_directory
from rainbow_octopus.state import write_json_atomic
from tests.helpers import sample_spec


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tests/live_codex_smoke.py OUTPUT_DIR")
        return 2
    project = prepare_output_directory(Path(sys.argv[1]))
    spec = sample_spec()
    write_json_atomic(project / ".rocto" / "task.json", spec.to_dict())
    result = CodexExecutor(timeout=300).execute(project, spec, attempt=1)
    expected = ("index.html", "styles.css", "script.js", "README.md")
    missing = [name for name in expected if not (project / name).is_file()]
    if missing:
        print("missing:", ", ".join(missing))
        return 1
    print("codex smoke test passed:", project)
    print("returncode:", result.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

