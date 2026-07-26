"""Offline end-to-end rehearsal of the rocto pipeline.

Runs the real Planner, the real RouterExecutor, the real DeepSeekExecutor and
the real static half of the Verifier, with every network call and every CLI
subprocess replaced by a stub. Proves the whole chain wires up without needing
a DeepSeek key, Claude Code, Codex, or a browser.

Two scenarios:

  1. router  — Claude Code is signed out, Codex is broken, DeepSeek wins.
               This is the failover story the project exists to tell.
  2. direct  — DeepSeek pinned, single shot.

Usage:  python scripts/dry_run.py [router|direct|all]
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainbow_octopus.executor import (  # noqa: E402
    ClaudeCodeExecutor,
    CodexExecutor,
    DeepSeekExecutor,
    RouterExecutor,
)
from rainbow_octopus.orchestrator import Orchestrator  # noqa: E402
from rainbow_octopus.planner import DeepSeekPlanner  # noqa: E402
from rainbow_octopus.verifier import BrowserVerifier  # noqa: E402

SPEC = {
    "title": "Pomodoro",
    "goal": "A pomodoro timer with a completed-session counter",
    "features": ["25 minute countdown", "start and reset", "completed counter"],
    "constraints": ["Single page", "Data kept in memory"],
    "ui_contract": [
        {"test_id": "timer", "purpose": "Remaining time as MM:SS"},
        {"test_id": "start", "purpose": "Start or pause the countdown"},
        {"test_id": "reset", "purpose": "Reset back to 25:00"},
        {"test_id": "count", "purpose": "Completed sessions"},
    ],
    "tests": [
        {
            "name": "reset restores the initial time",
            "steps": [
                {"action": "selector_exists", "selector": '[data-testid="timer"]'},
                {"action": "selector_exists", "selector": '[data-testid="count"]'},
                {"action": "click", "selector": '[data-testid="start"]'},
                {"action": "click", "selector": '[data-testid="reset"]'},
                {
                    "action": "text_visible",
                    "selector": '[data-testid="timer"]',
                    "expected": "25:00",
                },
                {"action": "no_console_errors"},
            ],
        }
    ],
}

INDEX = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>番茄钟</title><link rel="stylesheet" href="styles.css"></head>
<body><main>
<h1>番茄钟</h1>
<output data-testid="timer">25:00</output>
<div><button data-testid="start">开始</button>
<button data-testid="reset">重置</button></div>
<p>已完成 <span data-testid="count">0</span> 个</p>
</main><script src="script.js"></script></body></html>
"""

SCRIPT = """const timer=document.querySelector('[data-testid="timer"]');
const count=document.querySelector('[data-testid="count"]');
let left=1500,handle=null;
function render(){const m=String(Math.floor(left/60)).padStart(2,'0');
const s=String(left%60).padStart(2,'0');timer.textContent=m+':'+s;}
document.querySelector('[data-testid="start"]').addEventListener('click',()=>{
if(handle){clearInterval(handle);handle=null;return;}
handle=setInterval(()=>{if(left>0){left--;render();}
else{clearInterval(handle);handle=null;left=1500;
count.textContent=String(Number(count.textContent)+1);render();}},1000);});
document.querySelector('[data-testid="reset"]').addEventListener('click',()=>{
if(handle){clearInterval(handle);handle=null;}left=1500;render();});
render();
"""

FILES = {
    "index.html": INDEX,
    "styles.css": "body{font-family:system-ui;display:grid;place-items:center}",
    "script.js": SCRIPT,
    "README.md": "# 番茄钟\n\n双击 index.html 即可打开。",
}


def chat_response(content: object) -> bytes:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


def make_planner() -> DeepSeekPlanner:
    return DeepSeekPlanner(api_key="sk-dry-run", transport=lambda *a: chat_response(SPEC))


def make_deepseek() -> DeepSeekExecutor:
    return DeepSeekExecutor(
        api_key="sk-dry-run", transport=lambda *a: chat_response({"files": FILES})
    )


def signed_out_claude() -> ClaudeCodeExecutor:
    def runner(command, **kwargs):
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="2.1.219 (Claude Code)", stderr="")
        return SimpleNamespace(
            returncode=1, stdout='{"loggedIn": false, "authMethod": "none"}', stderr=""
        )

    return ClaudeCodeExecutor(Path("claude"), runner=runner)


def broken_codex() -> CodexExecutor:
    def runner(command, **kwargs):
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="codex 0.9.0", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"item.completed","item":{"aggregated_output":'
                '"windows sandbox: orchestrator_helper_launch_failed: '
                'helper=codex-windows-sandbox-setup.exe, error=program not found"}}'
            ),
            stderr="",
        )

    return CodexExecutor(Path("codex"), runner=runner)


def report(project: Path, label: str) -> int:
    print(f"\n[{label}] files produced:")
    for path in sorted(project.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(project)}  ({path.stat().st_size} bytes)")

    index = project / "index.html"
    status = 1
    if index.is_file():
        html = index.read_text(encoding="utf-8")
        contract = json.loads(
            (project / ".rocto" / "task.json").read_text(encoding="utf-8")
        )["ui_contract"]
        missing = [
            c["test_id"] for c in contract if f'data-testid="{c["test_id"]}"' not in html
        ]
        print(f"\n[{label}] data-testid contract:")
        for item in contract:
            ok = f'data-testid="{item["test_id"]}"' in html
            print(f"  {'OK  ' if ok else 'MISS'} {item['test_id']}")
        status = 0 if not missing else 1

    router_log = project / ".rocto" / "logs" / "router-attempt-1.json"
    if router_log.is_file():
        data = json.loads(router_log.read_text(encoding="utf-8"))
        print(f"\n[{label}] router decision:")
        print(f"  winner: {data['winner']}")
        for line in data["skipped_or_failed"]:
            print(f"  passed over: {line[:110]}")

    acceptance = project / "acceptance-report.json"
    if acceptance.is_file():
        data = json.loads(acceptance.read_text(encoding="utf-8"))
        print(f"\n[{label}] acceptance checks:")
        for check in data.get("checks", []):
            mark = "PASS" if check["passed"] else "FAIL"
            print(f"  {mark} {check['name']}: {check['detail'][:80]}")
    return status


def scenario(label: str, executor) -> int:
    workdir = Path(tempfile.mkdtemp(prefix=f"rocto-{label}-"))
    project = workdir / "pomodoro"
    print(f"\n{'=' * 70}\n[{label}] output: {project}\n{'=' * 70}")

    orchestrator = Orchestrator(
        planner=make_planner(),
        executor=executor,
        verifier=BrowserVerifier(browser_path=Path("missing-browser")),
        max_retries=0,
    )
    try:
        orchestrator.build("做一个带统计功能的番茄钟网页", project, "dry-run")
        print(f"[{label}] build reported success")
    except Exception as exc:  # noqa: BLE001 - rehearsal harness
        print(f"[{label}] build stopped: {str(exc)[:200]}")

    status = report(project, label)
    shutil.rmtree(workdir, ignore_errors=True)
    return status


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    status = 0
    if which in ("router", "all"):
        router = RouterExecutor(
            [
                ("claude", signed_out_claude()),
                ("codex", broken_codex()),
                ("deepseek", make_deepseek()),
            ]
        )
        status |= scenario("router", router)
    if which in ("direct", "all"):
        status |= scenario("direct", make_deepseek())
    print(f"\n[dry-run] exit={status}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
