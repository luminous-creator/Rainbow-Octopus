from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from .models import TaskSpec

#: The only files any executor may create inside an output directory.
GENERATED_FILES = ("index.html", "styles.css", "script.js", "README.md")

#: Refuse absurdly large model output (bytes, per file).
MAX_FILE_BYTES = 400_000


class ExecutionError(RuntimeError):
    pass


@dataclass
class ExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    command: list[str]


Runner = Callable[..., subprocess.CompletedProcess[str]]


#: Binaries bundled inside the Codex desktop app. They answer ``--version`` but
#: ship without ``codex-windows-sandbox-setup.exe``, so ``--sandbox
#: workspace-write`` cannot write files (KI-002). Real CLI installs win.
APP_BUNDLED_CODEX = (
    Path(".codex") / ".sandbox-bin" / "codex.exe",
    Path(".codex") / "plugins" / ".plugin-appserver" / "codex.exe",
)

_SANDBOX_HELPER_ERRORS = (
    "orchestrator_helper_launch_failed",
    "codex-windows-sandbox-setup.exe",
    "sandbox setup refresh failed",
)


def find_codex() -> Path | None:
    """Prefer a real CLI on PATH; fall back to the app-bundled binary."""
    explicit = os.environ.get("ROCTO_CODEX_BIN")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    found = shutil.which("codex")
    if found:
        return Path(found)
    for relative in APP_BUNDLED_CODEX:
        candidate = Path.home() / relative
        if candidate.is_file():
            return candidate
    return None


def is_app_bundled_codex(path: Path | None) -> bool:
    if not path:
        return False
    return any(str(path).endswith(str(rel)) for rel in APP_BUNDLED_CODEX)


class CodexExecutor:
    def __init__(
        self,
        codex_path: Path | None = None,
        timeout: int = 1200,
        runner: Runner = subprocess.run,
        sandbox: str = "workspace-write",
        allow_sandbox_fallback: bool | None = None,
    ):
        self.codex_path = codex_path or find_codex()
        self.timeout = timeout
        self.runner = runner
        self.sandbox = sandbox
        if allow_sandbox_fallback is None:
            allow_sandbox_fallback = (
                os.environ.get("ROCTO_CODEX_SANDBOX_FALLBACK", "1") != "0"
            )
        self.allow_sandbox_fallback = allow_sandbox_fallback

    def healthcheck(self) -> tuple[bool, str]:
        if not self.codex_path:
            return False, "Codex CLI not found"
        try:
            result = self.runner(
                [str(self.codex_path), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            detail = (result.stdout or result.stderr).strip()
            if is_app_bundled_codex(self.codex_path):
                detail = f"{detail} (app-bundled binary; sandbox helper may be missing)"
            return result.returncode == 0, detail or f"exit {result.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)

    def _command(self, project_dir: Path, sandbox: str) -> list[str]:
        return [
            str(self.codex_path),
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox,
            "-c",
            'approval_policy="never"',
            "--json",
            "--color",
            "never",
            "-C",
            str(project_dir),
            "-",
        ]

    def _invoke(self, command: list[str], prompt: str):
        child_env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "HOME": os.environ.get("HOME") or str(Path.home()),
            "CODEX_HOME": os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
        }
        return self.runner(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=self.timeout,
            check=False,
        )

    def execute(
        self,
        project_dir: Path,
        spec: TaskSpec,
        attempt: int,
        previous_failure: str | None = None,
    ) -> ExecutionResult:
        if not self.codex_path:
            raise ExecutionError(
                "Codex CLI not found. Set ROCTO_CODEX_BIN or install Codex CLI."
            )
        task_path = project_dir / ".rocto" / "task.json"
        before_hash = _sha256(task_path)
        prompt = _build_prompt(spec, attempt, previous_failure)
        command = self._command(project_dir, self.sandbox)
        try:
            completed = self._invoke(command, prompt)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError(f"Codex timed out after {self.timeout} seconds") from exc
        except OSError as exc:
            raise ExecutionError(f"Cannot start Codex: {exc}") from exc

        # KI-002: `--sandbox workspace-write` needs codex-windows-sandbox-setup.exe,
        # which the app-bundled binary does not ship. Every file write then fails
        # while the process still exits 0. Detect that exact signature and retry
        # once without Codex's own sandbox — rocto still confines the run with
        # `-C <output>` and deletes anything outside the four contract files.
        if self.allow_sandbox_fallback and _has_sandbox_helper_error(completed):
            command = self._command(project_dir, "danger-full-access")
            try:
                completed = self._invoke(command, prompt)
            except subprocess.TimeoutExpired as exc:
                raise ExecutionError(
                    f"Codex timed out after {self.timeout} seconds"
                ) from exc
            except OSError as exc:
                raise ExecutionError(f"Cannot start Codex: {exc}") from exc

        if _sha256(task_path) != before_hash:
            raise ExecutionError("Codex modified the protected .rocto/task.json")
        _enforce_output_boundary(project_dir)
        result = ExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=command,
        )
        _write_execution_log(
            project_dir,
            attempt,
            result,
            extra={"sandbox": command[command.index("--sandbox") + 1]},
        )
        if completed.returncode != 0:
            detail = _last_event_message(completed.stdout) or completed.stderr[-1000:]
            raise ExecutionError(
                f"Codex exited with {completed.returncode}: {detail.strip()}"
            )
        # Codex can exit 0 while every write was blocked (KI-002), so success is
        # decided by what is on disk, not by the exit code.
        _require_generated_files(project_dir, "Codex")
        return result


def _has_sandbox_helper_error(completed) -> bool:
    blob = f"{getattr(completed, 'stdout', '') or ''}\n{getattr(completed, 'stderr', '') or ''}"
    return any(marker in blob for marker in _SANDBOX_HELPER_ERRORS)


HttpTransport = Callable[[str, dict[str, str], bytes, float], bytes]


def _urlopen_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class DeepSeekExecutor:
    """Generate the static site with a single DeepSeek call.

    v0.1 only ever produces one self-contained static page, so an agentic CLI
    with its own sandbox is unnecessary. The model returns file contents as
    JSON and *rocto itself* writes them, which makes the "never touch anything
    outside --output" boundary absolute instead of merely requested.
    """

    API_URL = "https://api.deepseek.com/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 300,
        transport: HttpTransport = _urlopen_transport,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.model = (
            model
            or os.environ.get("ROCTO_DEEPSEEK_CODER_MODEL")
            or os.environ.get("ROCTO_DEEPSEEK_MODEL")
            or "deepseek-v4-flash"
        )
        self.timeout = timeout
        self.transport = transport

    def healthcheck(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "DEEPSEEK_API_KEY is not set"
        return True, f"deepseek executor ready (model={self.model})"

    def execute(
        self,
        project_dir: Path,
        spec: TaskSpec,
        attempt: int,
        previous_failure: str | None = None,
    ) -> ExecutionResult:
        if not self.api_key:
            raise ExecutionError("DEEPSEEK_API_KEY is not set")
        task_path = project_dir / ".rocto" / "task.json"
        before_hash = _sha256(task_path)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _CODER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_prompt(spec, attempt, previous_failure),
                },
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "rainbow-octopus/0.1.0",
        }
        try:
            raw = self.transport(self.API_URL, headers, body, float(self.timeout))
            response = json.loads(raw.decode("utf-8"))
            content = response["choices"][0]["message"]["content"]
            files = _extract_files(content)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ExecutionError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ExecutionError(f"Cannot reach DeepSeek: {exc.reason}") from exc
        except ExecutionError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ExecutionError(f"DeepSeek returned an invalid response: {exc}") from exc

        written = _write_generated_files(project_dir, files)

        if _sha256(task_path) != before_hash:
            raise ExecutionError("Executor modified the protected .rocto/task.json")

        result = ExecutionResult(
            returncode=0,
            stdout=json.dumps(
                {"files": {name: len(text) for name, text in written.items()}},
                ensure_ascii=False,
            ),
            stderr="",
            command=["<deepseek>", self.model, "chat/completions"],
        )
        _write_execution_log(project_dir, attempt, result, prefix="deepseek")
        return result


def find_claude() -> Path | None:
    explicit = os.environ.get("ROCTO_CLAUDE_BIN")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    found = shutil.which("claude")
    return Path(found) if found else None


class ClaudeCodeExecutor:
    """Drive Claude Code headlessly to write the site.

    Notes on the flags, all verified against ``claude --help``:

    ``-p``                     non-interactive, print and exit
    ``--output-format json``   one JSON result object (gives us cost/usage)
    ``--permission-mode``      ``acceptEdits`` so file writes are not prompted
    ``--tools "Read,Write,Edit"``
                               removes Bash entirely, so the model physically
                               cannot run a shell command. This is how hard
                               constraint #4 ("never execute LLM-generated
                               shell") is enforced for this backend.
    ``--max-budget-usd``       hard spend ceiling per attempt
    ``--no-session-persistence`` every attempt starts clean

    The process runs with ``cwd`` set to the output directory, and anything it
    leaves behind outside the four contract files is deleted afterwards.
    """

    #: Deliberately no Bash. Verified against claude 2.1.219: unknown names are
    #: ignored rather than fatal, so this list is safe across CLI versions.
    TOOLS = "Read,Write,Edit,Glob"

    def __init__(
        self,
        claude_path: Path | None = None,
        timeout: int = 900,
        model: str | None = None,
        max_budget_usd: float = 1.5,
        runner: Runner = subprocess.run,
    ):
        self.claude_path = claude_path or find_claude()
        self.timeout = timeout
        self.model = model or os.environ.get("ROCTO_CLAUDE_MODEL")
        self.max_budget_usd = max_budget_usd
        self.runner = runner

    def healthcheck(self) -> tuple[bool, str]:
        """Report available only when the CLI exists *and* is signed in.

        ``claude --version`` succeeds even when logged out, and a logged-out
        build fails with "Not logged in · Please run /login" after burning a
        turn. ``claude auth status --json`` gives ``{"loggedIn": bool}``, so the
        router can skip this backend instead of wasting an attempt.
        """
        if not self.claude_path:
            return False, "Claude Code CLI not found (install it or set ROCTO_CLAUDE_BIN)"
        try:
            version = self.runner(
                [str(self.claude_path), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if version.returncode != 0:
            return False, (version.stdout or version.stderr or "").strip() or "unusable"
        label = (version.stdout or version.stderr or "").strip()

        try:
            status = self.runner(
                [str(self.claude_path), "auth", "status", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            payload = json.loads((status.stdout or "{}").strip() or "{}")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            # Older builds may not have `auth status`; assume usable.
            return True, label
        if payload.get("loggedIn") is False:
            return False, f"{label} — not signed in (run: claude auth login)"
        return True, f"{label} ({payload.get('authMethod', 'authenticated')})"

    def _command(self) -> list[str]:
        command = [
            str(self.claude_path),
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            self.TOOLS,
            "--no-session-persistence",
            "--max-budget-usd",
            str(self.max_budget_usd),
        ]
        if self.model:
            command += ["--model", self.model]
        return command

    def execute(
        self,
        project_dir: Path,
        spec: TaskSpec,
        attempt: int,
        previous_failure: str | None = None,
    ) -> ExecutionResult:
        if not self.claude_path:
            raise ExecutionError(
                "Claude Code CLI not found. Install it or set ROCTO_CLAUDE_BIN."
            )
        task_path = project_dir / ".rocto" / "task.json"
        before_hash = _sha256(task_path)
        prompt = _build_prompt(spec, attempt, previous_failure)
        command = self._command()
        try:
            completed = self.runner(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(project_dir),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError(
                f"Claude Code timed out after {self.timeout} seconds"
            ) from exc
        except OSError as exc:
            raise ExecutionError(f"Cannot start Claude Code: {exc}") from exc

        if _sha256(task_path) != before_hash:
            raise ExecutionError("Claude Code modified the protected .rocto/task.json")

        removed = _enforce_output_boundary(project_dir)
        result = ExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=["<claude>", *command[1:]],
        )
        _write_execution_log(project_dir, attempt, result, prefix="claude", extra={
            "removed_stray_paths": removed,
            "cost_usd": _claude_cost(completed.stdout),
        })
        verdict = _claude_result_message(completed.stdout)
        if completed.returncode != 0:
            detail = verdict or (completed.stderr or completed.stdout)[-800:].strip()
            raise ExecutionError(f"Claude Code exited with {completed.returncode}: {detail}")
        _require_generated_files(project_dir, "Claude Code")
        return result


def _claude_events(stdout: str):
    """Yield JSON objects from --output-format json (or stream-json) output."""
    text = (stdout or "").strip()
    if not text:
        return
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        yield from (parsed if isinstance(parsed, list) else [parsed])
        return
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _claude_cost(stdout: str) -> float | None:
    """Best-effort cost extraction from --output-format json."""
    for event in reversed(list(_claude_events(stdout))):
        if not isinstance(event, dict):
            continue
        for key in ("total_cost_usd", "cost_usd"):
            value = event.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _claude_result_message(stdout: str) -> str:
    """Pull the human-readable failure reason out of the result event.

    A logged-out CLI returns ``{"is_error": true, "result": "Not logged in ·
    Please run /login"}``; surfacing that beats dumping raw JSON at the user.
    """
    for event in reversed(list(_claude_events(stdout))):
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" or "is_error" in event:
            message = event.get("result") or event.get("error")
            if isinstance(message, str) and message.strip():
                return message.strip()[:500]
    return ""


class RouterExecutor:
    """Try each backend in order; fall through on failure.

    This is the "编排" the project is actually about: one requirement, several
    independent coding agents, automatic failover, and a record of who was
    tried and why they lost. Order is deliberate — the strongest agentic coder
    first, the cheapest always-available one last, so a build never dies just
    because a vendor CLI is broken on this machine.
    """

    def __init__(self, backends: list[tuple[str, Any]]):
        if not backends:
            raise ExecutionError("Router needs at least one backend")
        self.backends = backends
        self.last_used: str | None = None
        self._health: dict[str, tuple[bool, str]] = {}

    def _availability(self, name: str, backend) -> tuple[bool, str]:
        """Probe once per build.

        Health checks shell out (``claude --version``, ``claude auth status``,
        ``codex --version``). With up to three repair attempts that is a dozen
        subprocess launches for information that cannot change mid-run.
        """
        if name not in self._health:
            try:
                self._health[name] = backend.healthcheck()
            except Exception as exc:  # noqa: BLE001 - a probe must never abort a build
                self._health[name] = (False, f"health check failed: {exc}")
        return self._health[name]

    def healthcheck(self) -> tuple[bool, str]:
        details = []
        healthy = False
        for name, backend in self.backends:
            ok, _ = self._availability(name, backend)
            healthy = healthy or ok
            details.append(f"{name}={'ok' if ok else 'unavailable'}")
        return healthy, "; ".join(details)

    def execute(
        self,
        project_dir: Path,
        spec: TaskSpec,
        attempt: int,
        previous_failure: str | None = None,
    ) -> ExecutionResult:
        errors: list[str] = []
        for name, backend in self.backends:
            available, detail = self._availability(name, backend)
            if not available:
                errors.append(f"{name}: skipped ({detail})")
                continue
            try:
                result = backend.execute(project_dir, spec, attempt, previous_failure)
            except ExecutionError as exc:
                errors.append(f"{name}: {exc}")
                _clear_generated_files(project_dir)
                continue
            self.last_used = name
            _write_router_log(project_dir, attempt, name, errors)
            return result
        _write_router_log(project_dir, attempt, None, errors)
        raise ExecutionError(
            "every executor failed:\n  " + "\n  ".join(errors or ["no backend available"])
        )


def _write_router_log(
    project_dir: Path, attempt: int, winner: str | None, errors: list[str]
) -> None:
    log_dir = project_dir / ".rocto" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"router-attempt-{attempt}.json").write_text(
        json.dumps(
            {"winner": winner, "skipped_or_failed": errors},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


#: Files the verifier legitimately writes into the output directory.
VERIFIER_ARTIFACTS = ("screenshot.png", "acceptance-report.json")


def _enforce_output_boundary(project_dir: Path) -> list[str]:
    """Delete anything inside the output directory that is not in the contract.

    An agentic CLI may leave scratch files, notes, or its own dot-directories
    behind. The output directory belongs to rocto, so removing strays is safe,
    keeps the delivered project clean, and stops the retry loop from tripping
    over leftovers. Everything removed is recorded in the execution log.
    """
    removed: list[str] = []
    root = project_dir.resolve()
    if not root.is_dir():
        return removed
    keep = {".rocto", *GENERATED_FILES, *VERIFIER_ARTIFACTS}
    for entry in sorted(root.iterdir()):
        if entry.name in keep:
            continue
        removed.append(entry.name)
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    return removed


def _clear_generated_files(project_dir: Path) -> None:
    """Remove a failed backend's partial output before trying the next one."""
    for name in GENERATED_FILES:
        try:
            (project_dir / name).unlink()
        except OSError:
            pass


def _require_generated_files(project_dir: Path, who: str) -> None:
    missing = [name for name in GENERATED_FILES if not (project_dir / name).is_file()]
    if missing:
        raise ExecutionError(f"{who} did not produce: {', '.join(missing)}")


def _extract_files(content: Any) -> dict[str, str]:
    if not isinstance(content, str):
        raise ExecutionError("Executor content is not text")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExecutionError(f"Executor did not return JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExecutionError("Executor JSON root must be an object")
    files = parsed.get("files", parsed)
    if not isinstance(files, dict):
        raise ExecutionError("Executor 'files' must be an object")
    return files


def _write_generated_files(project_dir: Path, files: dict) -> dict[str, str]:
    """Write only the whitelisted filenames, directly under project_dir.

    Any other key is a hard failure: this is the enforcement point for
    "never create anything outside the output directory".
    """
    cleaned: dict[str, str] = {}
    for name, text in files.items():
        if name not in GENERATED_FILES:
            raise ExecutionError(
                f"Executor tried to write a file outside the allowed set: {name!r}"
            )
        if not isinstance(text, str):
            raise ExecutionError(f"Content for {name} is not text")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise ExecutionError(f"{name} is too large ({len(encoded)} bytes)")
        cleaned[name] = text

    missing = [name for name in GENERATED_FILES if name not in cleaned]
    if missing:
        raise ExecutionError(f"Executor did not produce: {', '.join(missing)}")

    root = project_dir.resolve()
    for name, text in cleaned.items():
        target = (root / name).resolve()
        if target.parent != root:
            raise ExecutionError(f"Refusing to write outside the output dir: {name}")
        target.write_text(text, encoding="utf-8", newline="\n")
    return cleaned


#: Default failover order for ``--executor auto``.
DEFAULT_AUTO_ORDER = ("claude", "codex", "deepseek")
KNOWN_BACKENDS = ("claude", "codex", "deepseek")


def auto_order() -> tuple[str, ...]:
    """Failover order, overridable with ``ROCTO_EXECUTOR_ORDER``.

    Subscription-backed CLIs draw on a quota you cannot top up mid-month, so
    the cheap always-available backend should lead during routine work and the
    expensive one should lead when the output is going in front of someone:

        $env:ROCTO_EXECUTOR_ORDER = "deepseek,codex,claude"   # save quota
        $env:ROCTO_EXECUTOR_ORDER = "claude,deepseek"         # best effort
    """
    raw = os.environ.get("ROCTO_EXECUTOR_ORDER", "")
    names = [part.strip().lower() for part in raw.replace(" ", ",").split(",")]
    chosen = tuple(name for name in names if name in KNOWN_BACKENDS)
    # dedupe, keep order
    seen: list[str] = []
    for name in chosen:
        if name not in seen:
            seen.append(name)
    return tuple(seen) or DEFAULT_AUTO_ORDER


#: Backwards-compatible alias; prefer auto_order().
AUTO_ORDER = DEFAULT_AUTO_ORDER


def make_executor(backend: str = "auto", timeout: int = 1200):
    """Single place where an external code generator is chosen.

    ``auto`` builds a RouterExecutor over :func:`auto_order`. Any single name
    pins one backend.
    """
    if backend == "auto":
        return RouterExecutor(
            [(name, make_executor(name, timeout)) for name in auto_order()]
        )
    if backend == "deepseek":
        return DeepSeekExecutor(timeout=min(timeout, 600))
    if backend == "codex":
        return CodexExecutor(timeout=timeout)
    if backend == "claude":
        return ClaudeCodeExecutor(timeout=min(timeout, 900))
    raise ExecutionError(f"Unknown executor backend: {backend}")


_CODER_SYSTEM_PROMPT = r"""
You are the implementation component of Rainbow Octopus. You write one small,
polished, fully offline static website.

Return exactly one JSON object of this shape and nothing else:
{"files": {"index.html": "...", "styles.css": "...", "script.js": "...", "README.md": "..."}}

Rules:
- Exactly these four keys. No other keys, no paths, no directories.
- Vanilla HTML/CSS/JavaScript only. No build step, package manager, module
  syntax, CDN, external font, analytics, image URL, or any network request.
- index.html must link styles.css and script.js with relative paths.
- Put every data-testid from the ui_contract on the matching visible element,
  spelled exactly as given.
- All declared acceptance tests must pass deterministically, including after a
  page reload. Never write to localStorage or sessionStorage.
- No uncaught exceptions and nothing logged to console.error.
- Make it look good: sensible layout, spacing, contrast, responsive, keyboard
  accessible.
- README.md briefly documents what the page does and how to open it.
- JSON strings must escape newlines correctly. Output no Markdown fences.
""".strip()


def _build_prompt(
    spec: TaskSpec, attempt: int, previous_failure: str | None
) -> str:
    spec_json = json.dumps(spec.to_dict(), ensure_ascii=False, indent=2)
    retry = ""
    if previous_failure:
        retry = f"""
This is repair attempt {attempt}. The deterministic verifier reported:
{previous_failure}
Fix every reported failure without removing already working behavior.
"""
    return f"""
Build the static website described by the task specification below.

Hard boundaries:
- Work only in the current directory.
- Never read or modify .rocto/.
- Create or update exactly index.html, styles.css, script.js, and README.md.
- Use vanilla HTML/CSS/JavaScript. No dependencies, CDNs, external URLs,
  network requests, modules, build tools, or package managers.
- Put every required data-testid on the matching interactive or visible element.
- Make the interface polished, responsive, accessible, and usable offline.
- Ensure the test steps remain deterministic after page reload.
- Do not merely describe the solution: write the files.

TASK SPECIFICATION:
{spec_json}
{retry}
At the end, briefly state which files were created and what was verified.
""".strip()


def _write_execution_log(
    project_dir: Path,
    attempt: int,
    result: ExecutionResult,
    prefix: str = "codex",
    extra: dict | None = None,
) -> None:
    log_dir = project_dir / ".rocto" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if prefix == "codex":
        command = ["<codex>", *result.command[1:-1], "<prompt-from-stdin>"]
    else:
        command = list(result.command)
    payload = {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": command,
        **(extra or {}),
    }
    (log_dir / f"{prefix}-attempt-{attempt}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _last_event_message(output: str) -> str:
    for line in reversed(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key in ("message", "error"):
            value = event.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return ""


def _sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()
