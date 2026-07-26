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

import os

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
