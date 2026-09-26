"""What a finished build tells a person (ADR-007).

A build used to end with three file paths. Whether it worked, what was
actually checked, what was *not* checked, who wrote the page and how many
tries it took were all there — spread over ``acceptance-report.json``,
``run.json``, ``task.json``, ``contract-warnings.json`` and one router log per
attempt. Reading them was a job.

:func:`collect` gathers them into one :class:`BuildSummary`, and three
renderers share it: ``report.html`` (self-contained, opens offline, the
screenshot inlined), a Markdown block for pull requests and issue comments,
and the terminal summary. One source means the three can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
import base64
import json

from . import __version__

REPORT_NAME = "report.html"


@dataclass
class CheckLine:
    name: str
    passed: bool
    detail: str
    #: Plain-language description of the step, when the spec says what it was.
    description: str = ""


@dataclass
class CheckGroup:
    title: str
    lines: list[CheckLine] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(line.passed for line in self.lines)


@dataclass
class BuildSummary:
    project_dir: Path
    idea: str = ""
    title: str = ""
    goal: str = ""
    features: list[str] = field(default_factory=list)
    phase: str = "unknown"
    passed: bool = False
    checks_passed: int = 0
    checks_total: int = 0
    groups: list[CheckGroup] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    planner: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    screenshot: Path | None = None
    started_at: str = ""
    updated_at: str = ""
    revision: int = 0
    changes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.passed and self.phase == "completed":
            return "passed"
        if self.phase in {"interrupted", "stopped", "cancelled"}:
            return self.phase
        if self.phase in {"failed", "verification_failed", "execution_failed"}:
            return "failed"
        return "in progress"

    @property
    def headline(self) -> str:
        return {
            "passed": "Works — every check passed",
            "failed": "Does not work yet — some checks failed",
            "interrupted": "Stopped before it finished (interrupted)",
            "stopped": "Stopped at the time or cost budget",
            "cancelled": "Cancelled at plan review",
        }.get(self.verdict, "Still running")

    def next_step(self) -> str:
        path = _quote_path(self.project_dir)
        verdict = self.verdict
        if verdict == "passed":
            return f"rocto open {path}"
        if verdict == "cancelled":
            return "rocto build \"<idea>\" --review-plan"
        return f"rocto resume {path}"


def collect(project_dir: Path) -> BuildSummary:
    project_dir = Path(project_dir)
    internal = project_dir / ".rocto"
    state = _read_json(internal / "run.json") or {}
    spec = _read_json(internal / "task.json") or {}
    acceptance = _read_json(project_dir / "acceptance-report.json") or {}
    warnings = (_read_json(internal / "contract-warnings.json") or {}).get("warnings", [])

    checks = acceptance.get("checks", []) if isinstance(acceptance, dict) else []
    screenshot = project_dir / "screenshot.png"
    summary = BuildSummary(
        project_dir=project_dir,
        idea=str(state.get("idea", "")),
        title=str(spec.get("title", "")) or _first_line(str(state.get("idea", ""))),
        goal=str(spec.get("goal", "")),
        features=[str(f) for f in spec.get("features", [])],
        phase=str(state.get("phase", "unknown")),
        passed=bool(acceptance.get("passed")) if acceptance else False,
        checks_passed=sum(1 for c in checks if c.get("passed")),
        checks_total=len(checks),
        groups=_group_checks(checks, spec),
        console_errors=[str(e) for e in acceptance.get("console_errors", [])] if acceptance else [],
        warnings=[str(w) for w in warnings],
        attempts=list(state.get("attempts", [])),
        planner=state.get("planner"),
        cost_usd=state.get("cost_usd"),
        error=state.get("error"),
        screenshot=screenshot if screenshot.is_file() else None,
        started_at=str(state.get("started_at", "")),
        updated_at=str(state.get("updated_at", "")),
        revision=int(state.get("revision") or 0),
        changes=[str(c) for c in state.get("changes", [])],
    )
    # Older runs have no per-attempt records; rebuild a minimal list from logs.
    if not summary.attempts:
        summary.attempts = _attempts_from_logs(internal, state)
    return summary


# ------------------------------------------------------------------ grouping

_STATIC_LABELS = {
    "offline_only": "Makes no network requests",
    "testid_contract": "Every element the tests need is on the page",
    "browser_available": "A browser was available to test with",
    "browser_run": "The page loaded and the test script ran",
    "browser_result": "The test script reported back",
    "screenshot": "A screenshot was taken",
}


def _group_checks(checks: list[dict[str, Any]], spec: dict[str, Any]) -> list[CheckGroup]:
    static = CheckGroup("Files and safety")
    by_test: dict[str, CheckGroup] = {}
    order: list[str] = []
    steps = [
        (test.get("name", ""), step)
        for test in spec.get("tests", [])
        for step in test.get("steps", [])
    ]
    step_index = 0
    for check in checks:
        name = str(check.get("name", ""))
        passed = bool(check.get("passed"))
        detail = str(check.get("detail", ""))
        if name.startswith("required_file:"):
            file = name.split(":", 1)[1]
            static.lines.append(CheckLine(name, passed, detail, f"{file} exists"))
            continue
        if name in _STATIC_LABELS or ":" not in name:
            static.lines.append(
                CheckLine(name, passed, detail, _STATIC_LABELS.get(name, name))
            )
            continue
        test_name, action = name.rsplit(":", 1)
        description = ""
        if step_index < len(steps) and steps[step_index][0] == test_name:
            description = describe_step(steps[step_index][1])
            step_index += 1
        else:
            description = action.replace("_", " ")
        if test_name not in by_test:
            by_test[test_name] = CheckGroup(test_name)
            order.append(test_name)
        by_test[test_name].lines.append(CheckLine(name, passed, detail, description))
    groups = [static] if static.lines else []
    groups.extend(by_test[name] for name in order)
    return groups


def describe_step(step: dict[str, Any]) -> str:
    """One test step in words a non-programmer can follow."""
    action = step.get("action")
    target = _target(step.get("selector"))
    if action == "click":
        return f"Click {target}"
    if action == "fill":
        return f"Type “{step.get('value', '')}” into {target}"
    if action == "wait":
        return f"Wait {step.get('timeout_ms', 0)} ms"
    if action == "selector_exists":
        return f"{target} is on the page"
    if action == "text_visible":
        return f"{target} shows “{step.get('expected', '')}”"
    if action == "attribute_equals":
        return (
            f"{target} has {step.get('attribute')} = "
            f"“{step.get('expected', '')}”"
        )
    if action == "no_console_errors":
        return "No errors in the browser console"
    return str(action)


def _target(selector: Any) -> str:
    if not isinstance(selector, str):
        return "the page"
    start = selector.find("=")
    value = selector[start + 1:].strip("[]\"' ") if start >= 0 else selector
    return f"[{value}]"


def _attempts_from_logs(internal: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = []
    for number in range(1, int(state.get("attempt") or 0) + 1):
        router = _read_json(internal / "logs" / f"router-attempt-{number}.json") or {}
        attempts.append({"attempt": number, "executor": router.get("winner"), "outcome": None})
    return attempts


# ------------------------------------------------------------------ terminal


def render_text(summary: BuildSummary) -> str:
    mark = {"passed": "PASS", "failed": "FAIL"}.get(summary.verdict, summary.verdict.upper())
    lines = [
        "",
        f"  {mark}  {summary.title or 'Build'} — {summary.headline}",
        f"        {summary.checks_passed}/{summary.checks_total} checks passed"
        + _attempt_phrase(summary),
    ]
    failed = [line for group in summary.groups for line in group.lines if not line.passed]
    if failed:
        lines.append("")
        lines.append("  What failed:")
        for line in failed[:8]:
            lines.append(f"    - {line.description or line.name}: {_short(line.detail, 110)}")
        if len(failed) > 8:
            lines.append(f"    ... and {len(failed) - 8} more (see report.html)")
    if summary.warnings:
        lines.append("")
        lines.append("  Not verified (a passing report does not cover these):")
        for warning in summary.warnings[:5]:
            lines.append(f"    - {_short(warning, 120)}")
    lines.append("")
    lines.append(f"  Report:  {summary.project_dir / REPORT_NAME}")
    if summary.verdict == "passed":
        lines.append(f"  Site:    {summary.project_dir / 'index.html'}")
    lines.append(f"  Next:    {summary.next_step()}")
    return "\n".join(lines)


def _attempt_phrase(summary: BuildSummary) -> str:
    tried = [a for a in summary.attempts if a.get("outcome") not in {None, "running"}]
    if not tried:
        return ""
    writers = []
    for attempt in tried:
        name = attempt.get("executor")
        if name and name not in writers:
            writers.append(name)
    phrase = f" · {len(tried)} attempt{'s' if len(tried) != 1 else ''}"
    if writers:
        phrase += f" · written by {', '.join(writers)}"
    if summary.cost_usd:
        phrase += f" · ${summary.cost_usd:.2f}"
    return phrase


# ------------------------------------------------------------------ markdown


def render_markdown(summary: BuildSummary, screenshot_url: str | None = None) -> str:
    icon = {"passed": "✅", "failed": "❌"}.get(summary.verdict, "⏸️")
    out = [
        f"### {icon} {summary.title or 'Rainbow Octopus build'} — {summary.headline}",
        "",
        f"**{summary.checks_passed}/{summary.checks_total} checks passed**"
        + _attempt_phrase(summary).replace(" · ", " · ", 1),
        "",
    ]
    if summary.idea:
        out += [f"> {_first_line(summary.idea)}", ""]
    if screenshot_url and summary.screenshot:
        out += [f"![screenshot]({screenshot_url})", ""]
    if summary.features:
        out.append("**What it does**")
        out += [f"- {feature}" for feature in summary.features]
        out.append("")
    failed = [line for group in summary.groups for line in group.lines if not line.passed]
    if failed:
        out.append("**What failed**")
        out += [
            f"- {line.description or line.name} — `{_short(line.detail, 140)}`"
            for line in failed[:15]
        ]
        out.append("")
    if summary.warnings:
        out.append("**Not verified** — a passing report does not cover these:")
        out += [f"- {warning}" for warning in summary.warnings]
        out.append("")
    out.append("<details><summary>Every check</summary>")
    out.append("")
    for group in summary.groups:
        out.append(f"**{group.title}**")
        out.append("")
        for line in group.lines:
            out.append(f"- {'✅' if line.passed else '❌'} {line.description or line.name}")
        out.append("")
    out.append("</details>")
    out.append("")
    out.append(f"<sub>Rainbow Octopus {__version__} · planner: {summary.planner or '-'}</sub>")
    return "\n".join(out)


# ---------------------------------------------------------------------- html


def write_html_report(project_dir: Path) -> Path:
    summary = collect(Path(project_dir))
    target = Path(project_dir) / REPORT_NAME
    target.write_text(render_html(summary), encoding="utf-8")
    return target


def render_html(summary: BuildSummary) -> str:
    verdict = summary.verdict
    tone = {"passed": "good", "failed": "bad"}.get(verdict, "warn")
    screenshot = ""
    if summary.screenshot:
        try:
            data = base64.b64encode(summary.screenshot.read_bytes()).decode("ascii")
            screenshot = (
                f'<figure class="shot"><img alt="Screenshot of the generated page" '
                f'src="data:image/png;base64,{data}"></figure>'
            )
        except OSError:
            screenshot = ""

    groups_html = []
    for group in summary.groups:
        rows = "".join(
            f'<li class="{"ok" if line.passed else "no"}">'
            f'<span class="mark" aria-label="{"passed" if line.passed else "failed"}">'
            f'{"✓" if line.passed else "✗"}</span>'
            f'<span class="what">{escape(line.description or line.name)}</span>'
            f'<span class="detail">{escape(_short(line.detail, 300))}</span></li>'
            for line in group.lines
        )
        state = "ok" if group.passed else "no"
        groups_html.append(
            f'<section class="group {state}"><h3>{escape(group.title)}</h3>'
            f'<ul class="checks">{rows}</ul></section>'
        )

    attempts_html = "".join(
        "<tr>"
        f"<td>#{escape(str(a.get('attempt', '')))}</td>"
        f"<td>{escape(str(a.get('executor') or '—'))}</td>"
        f"<td>{escape(_outcome_label(a.get('outcome')))}</td>"
        f"<td>{_checks_cell(a)}</td>"
        f"<td>{_seconds(a.get('seconds'))}</td>"
        f"<td>{_money(a.get('cost_usd'))}</td>"
        "</tr>"
        for a in summary.attempts
    )

    warnings_html = ""
    if summary.warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in summary.warnings)
        warnings_html = (
            '<section class="card notice"><h2>Not verified</h2>'
            "<p>These parts of the page were never observed changing, so a pass "
            "does not prove they work.</p>"
            f"<ul>{items}</ul></section>"
        )

    features_html = ""
    if summary.features:
        items = "".join(f"<li>{escape(f)}</li>" for f in summary.features)
        features_html = f'<section class="card"><h2>What it should do</h2><ul>{items}</ul></section>'

    changes_html = ""
    if summary.changes:
        items = "".join(f"<li>{escape(c)}</li>" for c in summary.changes)
        changes_html = (
            f'<section class="card"><h2>Changes applied (revision {summary.revision})</h2>'
            f"<ol>{items}</ol></section>"
        )

    error_html = ""
    if summary.error:
        error_html = (
            '<section class="card"><h2>Last problem</h2>'
            f"<pre>{escape(_short(summary.error, 4000))}</pre></section>"
        )

    console_html = ""
    if summary.console_errors:
        items = "".join(f"<li><code>{escape(e)}</code></li>" for e in summary.console_errors)
        console_html = f'<section class="card"><h2>Browser console errors</h2><ul>{items}</ul></section>'

    open_site = (
        '<a class="button" href="index.html">Open the page</a>'
        if (summary.project_dir / "index.html").is_file() else ""
    )
    meta = " · ".join(
        part for part in (
            f"{summary.checks_passed}/{summary.checks_total} checks passed",
            f"planner: {summary.planner}" if summary.planner else "",
            f"spent ${summary.cost_usd:.2f}" if summary.cost_usd else "",
            f"updated {summary.updated_at[:19].replace('T', ' ')} UTC" if summary.updated_at else "",
        ) if part
    )
    return _HTML.format(
        title=escape(summary.title or "Rainbow Octopus build"),
        tone=tone,
        headline=escape(summary.headline),
        idea=escape(summary.idea),
        goal=escape(summary.goal),
        meta=escape(meta),
        open_site=open_site,
        next_step=escape(summary.next_step()),
        screenshot=screenshot,
        features=features_html,
        changes=changes_html,
        warnings=warnings_html,
        error=error_html,
        console=console_html,
        groups="".join(groups_html) or "<p>No checks have run yet.</p>",
        attempts=attempts_html or '<tr><td colspan="6">No attempts yet.</td></tr>',
        version=escape(__version__),
    )


def _outcome_label(outcome: Any) -> str:
    return {
        "passed": "passed",
        "verification_failed": "checks failed",
        "execution_failed": "could not write the site",
        "executed": "written, not yet checked",
        "running": "running",
        "interrupted": "interrupted",
        "crashed": "internal error",
    }.get(str(outcome), str(outcome or "—"))


def _checks_cell(attempt: dict[str, Any]) -> str:
    total = attempt.get("checks_total")
    if not total:
        return "—"
    return f"{attempt.get('checks_passed', 0)}/{total}"


def _seconds(value: Any) -> str:
    return f"{value:.0f}s" if isinstance(value, (int, float)) else "—"


def _money(value: Any) -> str:
    return f"${value:.2f}" if isinstance(value, (int, float)) else "—"


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _quote_path(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — build report</title>
<style>
:root {{
  --bg: #f6f5f2; --panel: #ffffff; --ink: #1d1d1f; --muted: #6b6b70;
  --line: #e4e2dc; --good: #1f7a4d; --good-bg: #e6f4ec; --bad: #b3261e;
  --bad-bg: #fbe9e7; --warn: #8a5a00; --warn-bg: #fdf3dc; --accent: #3b5bdb;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #141416; --panel: #1d1d20; --ink: #ececef; --muted: #9c9ca3;
    --line: #2e2e33; --good: #5fd19b; --good-bg: #16301f; --bad: #ff8a80;
    --bad-bg: #3a1714; --warn: #f3c669; --warn-bg: #33280f; --accent: #8fa8ff;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", "PingFang SC",
  "Microsoft YaHei", sans-serif; }}
main {{ max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }}
header.verdict {{ border-radius: 14px; padding: 22px 24px; margin-bottom: 20px;
  border: 1px solid var(--line); background: var(--panel); }}
header.good {{ background: var(--good-bg); border-color: transparent; }}
header.bad {{ background: var(--bad-bg); border-color: transparent; }}
header.warn {{ background: var(--warn-bg); border-color: transparent; }}
.eyebrow {{ font-size: 13px; color: var(--muted); letter-spacing: .02em; }}
h1 {{ margin: 4px 0 6px; font-size: 26px; line-height: 1.25; }}
header.good h1 {{ color: var(--good); }}
header.bad h1 {{ color: var(--bad); }}
header.warn h1 {{ color: var(--warn); }}
.idea {{ margin: 0 0 10px; font-size: 16px; }}
.meta {{ color: var(--muted); font-size: 13px; }}
.actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 14px; }}
.button {{ display: inline-block; padding: 8px 14px; border-radius: 8px;
  background: var(--accent); color: #fff; text-decoration: none; font-weight: 600; }}
code, pre {{ font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 13px; }}
.next code {{ background: var(--panel); border: 1px solid var(--line); padding: 3px 8px;
  border-radius: 6px; word-break: break-all; }}
.shot {{ margin: 0 0 20px; border: 1px solid var(--line); border-radius: 12px;
  overflow: hidden; background: var(--panel); }}
.shot img {{ display: block; width: 100%; height: auto; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 720px) {{ .grid {{ grid-template-columns: 1fr; }} h1 {{ font-size: 22px; }} }}
.card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 16px 18px; margin-bottom: 16px; }}
.card h2 {{ margin: 0 0 8px; font-size: 16px; }}
.card ul, .card ol {{ margin: 0; padding-left: 20px; }}
.notice {{ background: var(--warn-bg); border-color: transparent; }}
.notice h2 {{ color: var(--warn); }}
pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; }}
h2.section {{ font-size: 18px; margin: 28px 0 10px; }}
.group {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 12px 16px; margin-bottom: 12px; }}
.group h3 {{ margin: 0 0 6px; font-size: 15px; }}
.group.no h3 {{ color: var(--bad); }}
.checks {{ list-style: none; margin: 0; padding: 0; }}
.checks li {{ display: grid; grid-template-columns: 22px minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px; padding: 6px 0; border-top: 1px solid var(--line); }}
.checks li:first-child {{ border-top: 0; }}
.checks .mark {{ font-weight: 700; }}
.checks li.ok .mark {{ color: var(--good); }}
.checks li.no .mark {{ color: var(--bad); }}
.checks li.no .what {{ font-weight: 600; }}
.checks .detail {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
@media (max-width: 720px) {{ .checks li {{ grid-template-columns: 22px 1fr; }}
  .checks .detail {{ grid-column: 2; }} }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
th, td {{ text-align: left; padding: 8px 12px; border-top: 1px solid var(--line);
  font-size: 14px; white-space: nowrap; }}
th {{ color: var(--muted); font-weight: 600; border-top: 0; }}
footer {{ margin-top: 32px; color: var(--muted); font-size: 12px; }}
</style>
</head>
<body>
<main>
<header class="verdict {tone}">
  <div class="eyebrow">Rainbow Octopus build report</div>
  <h1>{headline}</h1>
  <p class="idea"><strong>{title}</strong> — {idea}</p>
  <div class="meta">{meta}</div>
  <div class="actions">{open_site}<span class="next">Next: <code>{next_step}</code></span></div>
</header>
{screenshot}
{warnings}
{error}
<div class="grid">
{features}
<section class="card"><h2>Goal</h2><p>{goal}</p></section>
</div>
{changes}
{console}
<h2 class="section">What was checked</h2>
{groups}
<h2 class="section">Attempts</h2>
<div class="table-wrap"><table>
<thead><tr><th>Attempt</th><th>Written by</th><th>Result</th><th>Checks</th><th>Time</th><th>Cost</th></tr></thead>
<tbody>{attempts}</tbody>
</table></div>
<footer>Generated by Rainbow Octopus {version}. This page works offline.</footer>
</main>
</body>
</html>
"""
