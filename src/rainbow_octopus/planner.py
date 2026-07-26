from __future__ import annotations

from collections.abc import Callable
from typing import Any
import json
import urllib.error
import urllib.request

from .contract import ContractReport, check_contract
from .models import SpecValidationError, TaskSpec
from .provider import (
    completions_url,
    missing_key_message,
    resolve_api_key,
    resolve_base_url,
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


class DeepSeekPlanner:
    """Plans against any OpenAI-compatible `/chat/completions` endpoint.

    Named for its default provider, not for a dependency on one — see
    `provider.py` for how the endpoint and key are resolved.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "deepseek-v4-flash",
        timeout: float = 90,
        transport: Transport = _urlopen_transport,
        max_attempts: int = 3,
        base_url: str | None = None,
    ):
        self.base_url = resolve_base_url(base_url)
        self.api_url = completions_url(self.base_url)
        self.api_key = resolve_api_key(api_key)
        self.model = model
        self.timeout = timeout
        self.transport = transport
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.max_attempts = max_attempts
        #: Non-blocking coverage notes from the accepted plan. Read by the
        #: orchestrator so a passing report cannot imply more than it verified.
        self.last_warnings: tuple[str, ...] = ()
        #: How many requests the accepted plan took, for the run log.
        self.last_attempts: int = 0

    def plan(self, idea: str) -> TaskSpec:
        """Return a spec that is structurally valid *and* a usable contract.

        The executor already gets its failures handed back to it as evidence
        and retries. The planner did not: whatever it produced first became the
        definition of "done" for the whole build. That asymmetry is what let
        ``pomodoro-2`` ship a contract that could be satisfied without building
        the requested feature, so the same repair loop now applies here.
        """
        if not self.api_key:
            raise PlanningError(missing_key_message())

        feedback: str | None = None
        last_problem = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            spec, report, last_problem = self._attempt(idea, feedback)
            if spec is not None and report is not None:
                self.last_warnings = report.warnings
                return spec
            feedback = _REPAIR_TEMPLATE.format(problems=last_problem)

        raise PlanningError(
            f"Planner could not produce a usable contract in {self.max_attempts} "
            f"attempts. Last problem:\n{last_problem}"
        )

    def _attempt(
        self, idea: str, feedback: str | None
    ) -> tuple[TaskSpec | None, ContractReport | None, str]:
        """One request. Returns the spec on success, otherwise why it failed."""
        try:
            content = self._request(idea, feedback)
            spec = TaskSpec.from_dict(_extract_json(content))
        except SpecValidationError as exc:
            return None, None, f"the specification was rejected as invalid: {exc}"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise PlanningError(f"{self.base_url} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PlanningError(f"Cannot reach {self.base_url}: {exc.reason}") from exc
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlanningError(
                f"{self.base_url} returned an invalid response: {exc}"
            ) from exc

        report = check_contract(spec)
        if not report.ok:
            return None, None, report.feedback()
        return spec, report, ""

    def _request(self, idea: str, feedback: str | None) -> Any:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": idea},
        ]
        if feedback:
            messages.append({"role": "user", "content": feedback})
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
            "User-Agent": "rainbow-octopus/0.1.0",
        }
        raw = self.transport(self.api_url, headers, body, self.timeout)
        response = json.loads(raw.decode("utf-8"))
        return response["choices"][0]["message"]["content"]


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
The specification you just produced was rejected before any code was written.

{problems}

Return a corrected JSON object. Keep everything that was not listed above,
change only what is needed to resolve each point, and output JSON only.
""".strip()

