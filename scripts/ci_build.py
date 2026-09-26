"""Run one rocto build inside GitHub Actions (build-from-issue.yml).

Kept out of the workflow YAML on purpose. Issue titles and bodies are
attacker-controllable text; interpolating them into a ``run:`` script is how
workflow command injection happens. Here they arrive only as environment
variables and are passed to rocto as a single argument — never through a
shell.

Always exits 0 so that the logs can be uploaded; the outcome travels in
``$GITHUB_OUTPUT`` (``exit_code``, ``name``, ``dir``, ``verdict``) and a
Markdown summary is appended to ``$GITHUB_STEP_SUMMARY``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainbow_octopus import cli  # noqa: E402
from rainbow_octopus.orchestrator import slugify  # noqa: E402
from rainbow_octopus.report import collect, render_markdown  # noqa: E402

#: Upper bound for the idea handed to the planner. An issue body can be long;
#: the planner needs a request, not an essay.
MAX_IDEA_CHARS = 2000

_TITLE_PREFIX = re.compile(r"^\s*(\[[^\]]*\]|rocto\s*:|build\s*:|idea\s*:)\s*", re.IGNORECASE)


def compose_idea(title: str, body: str) -> str:
    title = _TITLE_PREFIX.sub("", title or "").strip()
    body = (body or "").strip()
    idea = title if not body else f"{title}\n\n{body}"
    return idea[:MAX_IDEA_CHARS].strip()


def build_name(issue: str, idea: str, now: datetime | None = None) -> str:
    slug = slugify(idea, 32) or "build"
    if issue:
        return f"issue-{issue}-{slug}"
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{slug}"


def write_output(values: dict[str, str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in values.items()]
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def main() -> int:
    issue = os.environ.get("ISSUE_NUMBER", "").strip()
    idea = compose_idea(
        os.environ.get("ISSUE_TITLE") or os.environ.get("DISPATCH_IDEA") or "",
        os.environ.get("ISSUE_BODY") or "",
    )
    if not idea:
        print("::error::No idea: the issue title is empty.")
        write_output({"exit_code": "2", "name": "", "dir": "", "verdict": "failed"})
        return 0

    root = Path(os.environ.get("ROCTO_OUTPUT_ROOT") or "builds")
    name = build_name(issue, idea)
    output = root / name
    counter = 2
    while output.exists():  # a re-run must never clobber a published build
        output = root / f"{name}-{counter}"
        counter += 1

    code = cli.main(["build", idea, "--output", str(output)])
    verdict = "failed"
    if (output / ".rocto" / "run.json").is_file():
        summary = collect(output)
        verdict = summary.verdict
        markdown = render_markdown(summary)
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write(markdown + "\n")
    write_output(
        {"exit_code": str(code), "name": output.name, "dir": str(output), "verdict": verdict}
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
