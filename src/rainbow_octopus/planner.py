from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from . import __version__
from .contract import ContractReport, check_contract
from .models import SpecValidationError, TaskSpec
from .provider import (
    completions_url,
    missing_key_message,
    resolve_api_key,
    resolve_base_url,
    with_retries,
)


class PlanningError(RuntimeError):
    pass


Transport = Callable[[str, dict[str, str], bytes, float], bytes]


def _urlopen_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class _RepairingPlanner:
    """The plan-check-repair loop shared by every planner backend.

    The executor already gets its failures handed back to it as evidence and
    retries. The planner did not: whatever it produced first became the
    definition of "done" for the whole build. That asymmetry is what let
    ``pomodoro-2`` ship a contract that could be satisfied without building
    the requested feature, so the same repair loop applies here.

    Subclasses implement :meth:`_complete`, which turns a conversation into
    the model's raw text reply.
    """

    #: Shown in progress output and the run log.
    label = "planner"

    def __init__(self, max_attempts: int = 3):
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.max_attempts = max_attempts
        #: Non-blocking coverage notes from the accepted plan. Read by the
        #: orchestrator so a passing report cannot imply more than it verified.
        self.last_warnings: tuple[str, ...] = ()
        #: How many requests the accepted plan took, for the run log.
        self.last_attempts: int = 0
        #: Reported spend of the last plan() call, when the backend knows it.
        self.last_cost_usd: float | None = None
        #: Progress sink for retries; wired up by the orchestrator.
        self.notify: Callable[[str], None] | None = None

    def ensure_ready(self) -> None:
        """Raise PlanningError early when this planner cannot possibly work."""

    def plan(self, idea: str) -> TaskSpec:
        """Return a spec that is structurally valid *and* a usable contract."""
        return self._plan_loop([{"role": "user", "content": idea}])

    def refine(self, spec: TaskSpec, request: str) -> TaskSpec:
        """Return an updated specification for a change to an existing build."""
        current = json.dumps(spec.to_dict(), ensure_ascii=False, indent=2)
        return self._plan_loop(
            [
                {
                    "role": "user",
                    "content": _REFINE_TEMPLATE.format(spec=current, request=request),
                }
            ]
        )

    def _plan_loop(self, conversation: list[dict[str, str]]) -> TaskSpec:
        self.ensure_ready()
        self.last_cost_usd = None
        last_problem = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            content = self._complete(conversation)
            spec, report, last_problem = self._evaluate(content)
            if spec is not None and report is not None:
                self.last_warnings = report.warnings
                return spec
            # The rejected reply goes back into the conversation, so "keep
            # everything that was not listed" refers to something the model
            # can actually see.
            conversation = [
                *conversation,
                {"role": "assistant", "content": _as_text(content)},
                {"role": "user", "content": _REPAIR_TEMPLATE.format(problems=last_problem)},
            ]

        raise PlanningError(
            f"Planner could not produce a usable contract in {self.max_attempts} "
            f"attempts. Last problem:\n{last_problem}"
        )

    def _evaluate(
        self, content: Any
    ) -> tuple[TaskSpec | None, ContractReport | None, str]:
        try:
            spec = TaskSpec.from_dict(_extract_json(content))
        except SpecValidationError as exc:
            return None, None, f"the specification was rejected as invalid: {exc}"
        except (json.JSONDecodeError, PlanningError) as exc:
            return None, None, f"the reply was not a single JSON object: {exc}"
        report = check_contract(spec)
        if not report.ok:
            return None, None, report.feedback()
        return spec, report, ""

    def _complete(self, conversation: list[dict[str, str]]) -> Any:
        raise NotImplementedError


class DeepSeekPlanner(_RepairingPlanner):
    """Plans against any OpenAI-compatible `/chat/completions` endpoint.

    Named for its default provider, not for a dependency on one — see
    `provider.py` for how the endpoint and key are resolved.
    """

    label = "api"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "deepseek-v4-flash",
        timeout: float | None = None,
        transport: Transport = _urlopen_transport,
        max_attempts: int = 3,
        base_url: str | None = None,
        network_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(max_attempts)
        self.base_url = resolve_base_url(base_url)
        self.api_url = completions_url(self.base_url)
        self.api_key = resolve_api_key(api_key)
        self.model = model
        if timeout is None:
            timeout = float(os.environ.get("ROCTO_PLANNER_TIMEOUT") or 180)
        self.timeout = timeout
        self.transport = transport
        self.network_attempts = network_attempts
        self.sleep = sleep

    def describe(self) -> str:
        return self.model

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise PlanningError(missing_key_message())

    def _complete(self, conversation: list[dict[str, str]]) -> Any:
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *conversation]
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"rainbow-octopus/{__version__}",
        }
        try:
            raw = with_retries(
                lambda: self.transport(self.api_url, headers, body, self.timeout),
                attempts=self.network_attempts,
                notify=self.notify,
                sleep=self.sleep,
                what=f"planner request to {self.base_url}",
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise PlanningError(f"{self.base_url} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PlanningError(f"Cannot reach {self.base_url}: {exc.reason}") from exc
        except (TimeoutError, ConnectionError) as exc:
            raise PlanningError(f"{self.base_url} did not answer: {exc}") from exc
        try:
            response = json.loads(raw.decode("utf-8"))
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlanningError(
                f"{self.base_url} returned an invalid response: {exc}"
            ) from exc


class ClaudeCodePlanner(_RepairingPlanner):
    """Write the task specification with a signed-in Claude Code CLI.

    ADR-003 made the endpoint configurable, but planning still needed *some*
    API key. Someone whose only AI account is a Claude subscription had
    Claude Code signed in, able to write the whole site, and still could not
    run a build. This backend closes that gap: ``claude -p`` with every tool
    disabled (``--tools ""``) is a plain text-in, text-out call billed to the
    subscription that is already there.
    """

    label = "claude"

    def __init__(
        self,
        claude_path: Path | None = None,
        timeout: float | None = None,
        model: str | None = None,
        max_budget_usd: float = 0.5,
        max_attempts: int = 3,
        runner: Callable[..., Any] = subprocess.run,
    ):
        super().__init__(max_attempts)
        from .executor import find_claude  # local import: executor imports models only

        self.claude_path = claude_path or find_claude()
        if timeout is None:
            timeout = float(os.environ.get("ROCTO_PLANNER_TIMEOUT") or 180)
        self.timeout = timeout
        self.model = model or os.environ.get("ROCTO_CLAUDE_MODEL")
        self.max_budget_usd = max_budget_usd
        self.runner = runner

    def describe(self) -> str:
        return f"Claude Code{f' ({self.model})' if self.model else ''}"

    def ensure_ready(self) -> None:
        if not self.claude_path:
            raise PlanningError(
                "Claude Code CLI not found. Install it, set ROCTO_CLAUDE_BIN, "
                "or configure an API key for the api planner."
            )

    def _command(self) -> list[str]:
        command = [
            str(self.claude_path),
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--no-session-persistence",
            "--max-budget-usd",
            str(self.max_budget_usd),
            "--system-prompt",
            _SYSTEM_PROMPT,
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    def _complete(self, conversation: list[dict[str, str]]) -> Any:
        prompt = _flatten_conversation(conversation)
        try:
            completed = self.runner(
                self._command(),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlanningError(
                f"Claude Code planner timed out after {self.timeout:.0f} seconds"
            ) from exc
        except OSError as exc:
            raise PlanningError(f"Cannot start Claude Code: {exc}") from exc
        try:
            event = json.loads((completed.stdout or "").strip() or "{}")
        except json.JSONDecodeError:
            event = {}
        if not isinstance(event, dict):
            event = {}
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            self.last_cost_usd = (self.last_cost_usd or 0.0) + float(cost)
        result = event.get("result")
        if completed.returncode != 0 or event.get("is_error") or not isinstance(result, str):
            detail = (
                result if isinstance(result, str) and result.strip()
                else (completed.stderr or completed.stdout or "").strip()[-500:]
            )
            raise PlanningError(
                f"Claude Code planner failed (exit {completed.returncode}): "
                f"{detail or 'no output'}"
            )
        return result


def _flatten_conversation(conversation: list[dict[str, str]]) -> str:
    """Claude Code -p takes one prompt, so earlier turns are quoted into it."""
    if len(conversation) == 1:
        return conversation[0]["content"]
    parts = []
    for message in conversation:
        speaker = "Your previous reply" if message["role"] == "assistant" else "User"
        parts.append(f"=== {speaker} ===\n{message['content']}")
    return "\n\n".join(parts)


def _as_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


PLANNER_CHOICES = ("auto", "api", "claude")


def make_planner(choice: str | None = None, model: str | None = None) -> _RepairingPlanner:
    """Pick the planner backend (ADR-004).

    ``auto`` prefers the API planner when a key is configured — it is cheap
    and spends no subscription quota — and otherwise falls back to a Claude
    Code CLI that is installed and signed in.
    """
    from .executor import ClaudeCodeExecutor, find_claude

    choice = (choice or os.environ.get("ROCTO_PLANNER") or "auto").strip().lower()
    if choice not in PLANNER_CHOICES:
        raise PlanningError(
            f"Unknown planner {choice!r}; choose one of: {', '.join(PLANNER_CHOICES)}"
        )
    api_model = model or os.environ.get("ROCTO_DEEPSEEK_MODEL") or "deepseek-v4-flash"
    if choice == "api":
        return DeepSeekPlanner(model=api_model)
    if choice == "claude":
        return ClaudeCodePlanner()
    if resolve_api_key():
        return DeepSeekPlanner(model=api_model)
    claude = find_claude()
    if claude:
        ok, _ = ClaudeCodeExecutor(claude).healthcheck()
        if ok:
            return ClaudeCodePlanner(claude_path=claude)
    # Nothing usable: return the API planner so the error names the API key,
    # the one fix that works everywhere.
    return DeepSeekPlanner(model=api_model)


def _extract_json(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise PlanningError("Planner content is not text")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise PlanningError("Planner JSON root must be an object")
    return parsed


_SYSTEM_PROMPT = r"""
You are the planning component of Rainbow Octopus. Convert one user idea into
a small, polished, testable static website specification.

Return exactly one JSON object with this shape:
{
  "title": "short project title",
  "goal": "clear goal",
  "features": ["1-8 observable features"],
  "constraints": ["project-specific constraints"],
  "ui_contract": [
    {"test_id": "ascii-kebab-id", "purpose": "why this element exists"}
  ],
  "tests": [
    {
      "name": "observable behavior",
      "steps": [
        {
          "action": "click|fill|wait|selector_exists|text_visible|attribute_equals|no_console_errors",
          "selector": "[data-testid=\"known-id\"]",
          "value": "only for fill",
          "expected": "for text_visible or attribute_equals",
          "attribute": "only for attribute_equals",
          "timeout_ms": 0
        }
      ]
    }
  ]
}

Rules:
- The implementation must be vanilla HTML, CSS, and JavaScript with no build
  step, package manager, CDN, external font, analytics, or network request.
- Every selector must be an exact data-testid selector declared in ui_contract.
- Design 2-6 deterministic tests. Prefer stable state changes and visible text.
- Use fill/click actions before assertions when testing interactions.
- Include a final no_console_errors assertion.
- wait may be at most 3000 ms. Do not output code, Markdown, or shell commands.
- Each test is independent: it starts from a freshly loaded page with empty
  localStorage and sessionStorage. Never rely on state from an earlier test;
  create whatever a test needs (for example, add an item) inside that test.
- A selector matches the FIRST element with that data-testid. Elements that
  repeat (list rows, cards) may share one test_id; assert on the first one.
- attribute_equals with attribute "value" or "checked" reads the element's
  live state (what the user typed, whether the box is ticked).

Two rules about what makes an assertion worth writing. Both are enforced, and
a specification that breaks either one is sent back to you:

- NEVER assert a clock-shaped value (mm:ss) that you obtained by subtracting a
  wait from a starting time. Asserting "24:58" two seconds after starting a
  25:00 timer does not test the timer; it tests the tick rate, and it is
  satisfied more easily by adjusting the clock than by building it correctly.
  Assert resting states — the value on load, or the value after a reset — and
  assert them with no wait in between.
- Every test_id you declare in ui_contract must appear in at least one test
  step. Declaring an element you never test is worse than omitting it: it
  reads as coverage that does not exist. If you cannot test it, drop it.

Aim your tests at whatever the user actually asked for. If the request names a
feature, that feature is the one that most needs an assertion, and asserting a
counter is at zero is not a test of counting.
""".strip()


_REPAIR_TEMPLATE = """
The specification you just produced (quoted above) was rejected before any
code was written.

{problems}

Return a corrected JSON object. Keep everything that was not listed above,
change only what is needed to resolve each point, and output JSON only.
""".strip()



_REFINE_TEMPLATE = """
This is a change request for a website that already exists and already passes
the acceptance specification below.

CURRENT SPECIFICATION:
{spec}

CHANGE REQUEST:
{request}

Return the complete updated specification as one JSON object of the same
shape. Keep every existing ui_contract element and test that the change does
not make obsolete, so they keep guarding behaviour that already works. Add
elements and tests for what the change introduces; the change itself is what
most needs an assertion. Update title, goal and features to describe the
site after the change.
""".strip()
