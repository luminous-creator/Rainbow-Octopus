from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import re


ALLOWED_ACTIONS = {
    "click",
    "fill",
    "wait",
    "selector_exists",
    "text_visible",
    "attribute_equals",
    "no_console_errors",
}
SELECTOR_PATTERN = re.compile(
    r"""^\[data-testid=(?:"[A-Za-z0-9_-]+"|'[A-Za-z0-9_-]+')\]$"""
)
TEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SpecValidationError(ValueError):
    """Raised when a planner returns an unsafe or malformed task specification."""


@dataclass(frozen=True)
class UIElement:
    test_id: str
    purpose: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UIElement":
        test_id = _required_text(data, "test_id", 64)
        if not TEST_ID_PATTERN.fullmatch(test_id):
            raise SpecValidationError(f"Invalid test_id: {test_id!r}")
        return cls(test_id=test_id, purpose=_required_text(data, "purpose", 240))


@dataclass(frozen=True)
class TestStep:
    action: str
    selector: str | None = None
    value: str | None = None
    expected: str | None = None
    attribute: str | None = None
    timeout_ms: int = 1000

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestStep":
        action = _required_text(data, "action", 40)
        if action not in ALLOWED_ACTIONS:
            raise SpecValidationError(f"Unsupported test action: {action}")

        selector = data.get("selector")
        if selector is not None:
            if not isinstance(selector, str) or not SELECTOR_PATTERN.fullmatch(selector):
                raise SpecValidationError(
                    "Selectors must be exact [data-testid=\"...\"] selectors"
                )
        if action in {"click", "fill", "selector_exists", "text_visible", "attribute_equals"}:
            if not selector:
                raise SpecValidationError(f"{action} requires selector")

        value = _optional_text(data.get("value"), "value", 1000)
        expected = _optional_text(data.get("expected"), "expected", 1000)
        attribute = _optional_text(data.get("attribute"), "attribute", 80)
        if action == "fill" and value is None:
            raise SpecValidationError("fill requires value")
        if action in {"text_visible", "attribute_equals"} and expected is None:
            raise SpecValidationError(f"{action} requires expected")
        if action == "attribute_equals" and attribute is None:
            raise SpecValidationError("attribute_equals requires attribute")

        timeout = data.get("timeout_ms", 1000)
        if not isinstance(timeout, int) or not 0 <= timeout <= 3000:
            raise SpecValidationError("timeout_ms must be an integer between 0 and 3000")
        return cls(action, selector, value, expected, attribute, timeout)


@dataclass(frozen=True)
class TestCase:
    name: str
    steps: tuple[TestStep, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestCase":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 20:
            raise SpecValidationError("Each test must contain 1-20 steps")
        return cls(
            name=_required_text(data, "name", 120),
            steps=tuple(TestStep.from_dict(step) for step in raw_steps),
        )


@dataclass(frozen=True)
class TaskSpec:
    title: str
    goal: str
    features: tuple[str, ...]
    constraints: tuple[str, ...]
    ui_contract: tuple[UIElement, ...]
    tests: tuple[TestCase, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        if not isinstance(data, dict):
            raise SpecValidationError("Task specification must be a JSON object")
        features = _text_list(data.get("features"), "features", 1, 12, 240)
        constraints = _text_list(data.get("constraints"), "constraints", 1, 12, 240)

        raw_ui = data.get("ui_contract")
        if not isinstance(raw_ui, list) or not 1 <= len(raw_ui) <= 30:
            raise SpecValidationError("ui_contract must contain 1-30 elements")
        ui = tuple(UIElement.from_dict(item) for item in raw_ui)
        if len({item.test_id for item in ui}) != len(ui):
            raise SpecValidationError("ui_contract contains duplicate test_id values")

        raw_tests = data.get("tests")
        if not isinstance(raw_tests, list) or not 1 <= len(raw_tests) <= 10:
            raise SpecValidationError("tests must contain 1-10 test cases")
        tests = tuple(TestCase.from_dict(item) for item in raw_tests)
        if sum(len(test.steps) for test in tests) > 50:
            raise SpecValidationError("Task specification may contain at most 50 test steps")

        known_ids = {item.test_id for item in ui}
        for test in tests:
            for step in test.steps:
                if step.selector:
                    selected = re.search(r"""['"]([A-Za-z0-9_-]+)['"]""", step.selector)
                    if not selected or selected.group(1) not in known_ids:
                        raise SpecValidationError(
                            f"Test selector is absent from ui_contract: {step.selector}"
                        )

        required_constraints = (
            "Create only a static HTML/CSS/JavaScript website",
            "Do not use external dependencies, CDNs, or network requests",
            "Create index.html, styles.css, script.js, and README.md",
        )
        merged_constraints = tuple(dict.fromkeys((*constraints, *required_constraints)))
        return cls(
            title=_required_text(data, "title", 120),
            goal=_required_text(data, "goal", 1000),
            features=features,
            constraints=merged_constraints,
            ui_contract=ui,
            tests=tests,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class AcceptanceReport:
    passed: bool
    checks: list[AcceptanceCheck] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    screenshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def failure_summary(self) -> str:
        failed = [check for check in self.checks if not check.passed]
        lines = [f"- {check.name}: {check.detail}" for check in failed]
        lines.extend(f"- console: {error}" for error in self.console_errors)
        return "\n".join(lines) or "Unknown verification failure"


def _required_text(data: dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecValidationError(f"{key} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise SpecValidationError(f"{key} exceeds {limit} characters")
    return value


def _optional_text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise SpecValidationError(f"{name} must be a string up to {limit} characters")
    return value


def _text_list(
    value: Any, name: str, minimum: int, maximum: int, item_limit: int
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise SpecValidationError(f"{name} must contain {minimum}-{maximum} items")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > item_limit:
            raise SpecValidationError(f"{name} contains an invalid item")
        result.append(item.strip())
    return tuple(result)

