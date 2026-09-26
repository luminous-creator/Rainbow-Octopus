"""Where the chat-completions calls go.

Two components make HTTPS calls: the planner, which turns an idea into a task
specification, and the DeepSeek executor, which is the zero-install fallback
backend. Both were pinned to `https://api.deepseek.com/chat/completions` and to
the `DEEPSEEK_API_KEY` variable.

That made a DeepSeek account a hard requirement for *every* user, including
someone who already holds a Claude subscription, has Claude Code signed in, and
only needs something to write the task specification. It is also a real barrier
outside China, where DeepSeek's billing is harder to reach than most.

Nothing in either call is DeepSeek-specific — it is a plain OpenAI-compatible
`/chat/completions` request with `response_format: json_object`. So the
endpoint and the key are configuration, not constants. DeepSeek stays the
default because it is cheap, it does not consume a Claude or ChatGPT
subscription quota, and it honours `json_object`.

Resolution order, most specific first:

1. an explicit argument passed in code
2. `ROCTO_API_BASE` / `ROCTO_API_KEY`
3. `DEEPSEEK_API_KEY` (so existing setups keep working untouched)
4. the DeepSeek default, for the base URL only

Set the base URL to whatever sits in front of `/chat/completions`:

    ROCTO_API_BASE=https://api.openai.com/v1        ROCTO_API_KEY=sk-...
    ROCTO_API_BASE=https://openrouter.ai/api/v1     ROCTO_API_KEY=sk-or-...
    ROCTO_API_BASE=http://localhost:11434/v1        ROCTO_API_KEY=ollama

A provider that does not honour `response_format: json_object` will fail
loudly at the planner's JSON parse rather than silently producing a bad
specification.
"""

from __future__ import annotations

from typing import Callable
import os
import socket
import time
import urllib.error

#: DeepSeek's endpoint has no `/v1` segment; OpenAI-compatible ones usually do.
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"

BASE_URL_ENV = "ROCTO_API_BASE"
API_KEY_ENV = "ROCTO_API_KEY"
LEGACY_API_KEY_ENV = "DEEPSEEK_API_KEY"


def resolve_base_url(explicit: str | None = None) -> str:
    """The prefix that `/chat/completions` is appended to, without a trailing slash."""
    value = explicit or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    return value.rstrip("/")


def resolve_api_key(explicit: str | None = None) -> str | None:
    return (
        explicit
        or os.environ.get(API_KEY_ENV)
        or os.environ.get(LEGACY_API_KEY_ENV)
        or None
    )


def completions_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def missing_key_message() -> str:
    return (
        f"No API key. Set {API_KEY_ENV} (or {LEGACY_API_KEY_ENV}), and set "
        f"{BASE_URL_ENV} too if you are not using DeepSeek."
    )


def is_default_provider(base_url: str | None = None) -> bool:
    return resolve_base_url(base_url) == DEFAULT_BASE_URL


# --------------------------------------------------------------------------
# Transient-failure retry (ADR-005)
# --------------------------------------------------------------------------

#: Status codes worth a second try: rate limiting and server-side trouble.
#: Everything else in 4xx (a bad key, a bad model name) fails immediately —
#: retrying a 401 just delays the same message.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

#: Upper bound for one wait, whatever Retry-After asks for. An unattended
#: build should slow down on a rate limit, not stall for an hour.
MAX_BACKOFF_SECONDS = 30.0


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_STATUS
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (TimeoutError, socket.timeout, ConnectionError))


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"unreachable ({exc.reason})"
    return type(exc).__name__


def with_retries(
    call: Callable[[], bytes],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    notify: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    what: str = "request",
) -> bytes:
    """Run ``call``; retry transient network failures with backoff.

    A planner or executor request that dies on one 503 used to fail the whole
    build. Attended, that is an annoyance; unattended — a batch, a nightly
    job, an issue-triggered workflow — it is the difference between a result
    and a red run nobody can act on.
    """
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - filtered by is_transient
            if attempt >= attempts or not is_transient(exc):
                raise
            delay = _retry_after(exc)
            if delay is None:
                delay = base_delay * (2 ** (attempt - 1))
            delay = min(delay, MAX_BACKOFF_SECONDS)
            if notify:
                try:
                    notify(
                        f"{what} failed ({describe_error(exc)}); retry "
                        f"{attempt + 1}/{attempts} in {delay:.0f}s"
                    )
                except Exception:  # noqa: BLE001 - reporting must never break a call
                    pass
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def usage_tokens(usage: object) -> int | None:
    """Total tokens from an OpenAI-style or Anthropic-style ``usage`` object.

    Reported so a user can see what a build cost even when the provider
    reports tokens rather than money (DeepSeek and most compatible APIs).
    """
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    keys = (
        "prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    )
    values = [usage.get(key) for key in keys if isinstance(usage.get(key), int)]
    return sum(values) if values else None


def add_tokens(total: int | None, more: int | None) -> int | None:
    if more is None:
        return total
    return (total or 0) + more
