"""Quality checks on a planner-produced acceptance contract.

``models.py`` decides whether a specification is *structurally valid* and
*safe to execute*. This module decides something different and softer: whether
the specification is a **useful acceptance contract** — one that an executor
cannot satisfy without actually building the thing that was asked for.

Both rules here exist because of a real failure. ``demo-output/pomodoro-2``
passed 31 of 31 assertions, and yet:

1. One assertion (``text_visible "24:58"`` after a 2000 ms ``wait``) was only
   satisfiable if the page's clock ticked at exactly the rate the planner had
   assumed. The executor did not fix its clock; it inserted a 1100 ms offset
   around the measurement so the assertion would land. The delivered timer runs
   0.1 s long per session. The product was tuned to the measurement.
2. ``tomato-count`` — the counter named in the one-line request, "a pomodoro
   timer *with stats*" — was asserted twice, both times as ``"0"``. A page
   whose counter is permanently zero scores full marks.

The verifier was not at fault in either case; it faithfully checked what it was
given. The gap is that nothing was checking the *planner*. Rainbow Octopus
constrains the executor (allowlisted filenames, no shell) and the verifier
(seven actions, exact testid selectors), but the component that decides what
"done" means was, until now, unconstrained model output.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .models import TaskSpec


#: ``mm:ss``, ``m:ss`` or ``hh:mm:ss`` — the shape of a value that a running
#: clock produces. Deliberately narrow: a bare integer such as a counter is not
#: matched, because counters change on discrete user actions rather than with
#: elapsed time.
CLOCK_PATTERN = re.compile(r"^\d{1,3}:[0-5]\d(?::[0-5]\d)?$")

#: Steps that assert something, as opposed to driving the page.
ASSERTION_ACTIONS = frozenset({"selector_exists", "text_visible", "attribute_equals"})

#: Assertion steps that compare against an expected value.
VALUE_ASSERTIONS = frozenset({"text_visible", "attribute_equals"})


@dataclass(frozen=True)
class ContractReport:
    """The outcome of checking a contract.

    ``errors`` block the build and are fed back to the planner for repair.
    ``warnings`` never block: they describe coverage that is thin but not
    provably wrong, and some of them are not fixable within the test DSL at
    all (no sequence of at most 3000 ms waits can observe a 25 minute timer
    reach zero). They are recorded so that a passing report cannot quietly
    imply more than it verified.
    """

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def feedback(self) -> str:
        return "\n".join(f"- {problem}" for problem in self.errors)


def check_contract(spec: TaskSpec) -> ContractReport:
    errors: list[str] = []
    warnings: list[str] = []
    _check_wall_clock_assertions(spec, errors)
    _check_contract_coverage(spec, errors, warnings)
    return ContractReport(tuple(errors), tuple(warnings))


def _check_wall_clock_assertions(spec: TaskSpec, errors: list[str]) -> None:
    """Reject clock-shaped assertions whose value depends on elapsed time.

    An assertion is judged safe when the same clock value is also asserted
    somewhere that no ``wait`` precedes it. Such a value is an *anchor*: a
    resting state the page returns to (``25:00`` on load, or after Reset), so
    asserting it is a statement about behaviour rather than about timing.

    A clock value that appears only after a ``wait`` is the planner having done
    arithmetic — 25:00 minus two seconds is 24:58 — and it silently converts
    the executor's job from "build a correct clock" into "produce this string
    at this moment".
    """
    anchors: set[str] = set()
    for test in spec.tests:
        waited = False
        for step in test.steps:
            if step.action == "wait":
                waited = True
            elif (
                not waited
                and step.action in VALUE_ASSERTIONS
                and step.expected is not None
            ):
                anchors.add(step.expected)

    for test in spec.tests:
        waited = False
        for step in test.steps:
            if step.action == "wait":
                waited = True
                continue
            if step.action not in VALUE_ASSERTIONS or step.expected is None:
                continue
            if not waited or not CLOCK_PATTERN.fullmatch(step.expected):
                continue
            if step.expected in anchors:
                continue
            errors.append(
                f"test {test.name!r} asserts the time-shaped value "
                f"{step.expected!r} on {step.selector} after a wait. That value "
                "depends on how long the page ran, so it can be satisfied by "
                "adjusting the clock instead of by correct behaviour. Assert a "
                "resting state instead (a value the page also shows with no "
                "preceding wait), or assert a non-time property."
            )


def _check_contract_coverage(
    spec: TaskSpec, errors: list[str], warnings: list[str]
) -> None:
    """Every declared element must be exercised; flag ones that never change."""
    referenced: set[str] = set()
    asserted_values: dict[str, set[str]] = {}

    for test in spec.tests:
        for step in test.steps:
            test_id = _selector_id(step.selector)
            if test_id is None:
                continue
            referenced.add(test_id)
            if step.action in VALUE_ASSERTIONS and step.expected is not None:
                asserted_values.setdefault(test_id, set()).add(step.expected)

    declared = [element.test_id for element in spec.ui_contract]
    unreferenced = [test_id for test_id in declared if test_id not in referenced]
    if unreferenced:
        errors.append(
            "ui_contract declares "
            + ", ".join(repr(test_id) for test_id in unreferenced)
            + " but no test ever selects "
            + ("them" if len(unreferenced) > 1 else "it")
            + ". Every declared element must appear in at least one test step, "
            "or it must be removed from ui_contract."
        )

    for test_id in declared:
        values = asserted_values.get(test_id)
        if values and len(values) == 1:
            only = next(iter(values))
            warnings.append(
                f"{test_id!r} is only ever asserted as {only!r}; no test observes "
                "it change, so a page that hard-codes this value would pass."
            )


def _selector_id(selector: str | None) -> str | None:
    if not selector:
        return None
    found = re.search(r"""['"]([A-Za-z0-9_-]+)['"]""", selector)
    return found.group(1) if found else None
