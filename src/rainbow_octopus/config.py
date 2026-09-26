"""Configuration files, and where rocto keeps its own state (ADR-004).

Every knob rocto has was an environment variable. That works for a CI job and
is miserable for a person: settings vanish with the terminal, PowerShell and
POSIX shells spell them differently, and nothing tells you which value is in
force or where it came from.

The design keeps environment variables as the one mechanism the rest of the
code reads, and makes config files a *source of defaults for them*:

    CLI flag  >  environment variable  >  ./rocto.toml  >  user config  >  built-in

:func:`apply_config` runs once at CLI start-up and ``setdefault``s the
environment from the files. Nothing downstream changed how it reads settings,
every documented variable keeps working, and an explicit environment variable
always wins over a file — which is what a CI job overriding a checked-in
``rocto.toml`` expects.

API keys are never read from a file. ``api_key = "..."`` is rejected with an
explanation; ``api_key_env = "OPENROUTER_API_KEY"`` names the variable that
holds the key instead, so a config file can be committed safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping
import os
import platform
import re

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    _toml = None


PROJECT_CONFIG_NAME = "rocto.toml"
HOME_ENV = "ROCTO_HOME"
CONFIG_ENV = "ROCTO_CONFIG"


class ConfigError(ValueError):
    """A config file exists but cannot be used."""


@dataclass(frozen=True)
class Setting:
    key: str
    env: str
    kind: type
    default: Any
    help: str


#: The complete list of settings a config file may contain. Each one is a
#: default for the environment variable next to it.
SETTINGS: tuple[Setting, ...] = (
    Setting("api_base", "ROCTO_API_BASE", str, "https://api.deepseek.com",
            "OpenAI-compatible endpoint, the part before /chat/completions"),
    Setting("model", "ROCTO_DEEPSEEK_MODEL", str, "deepseek-v4-flash",
            "Model the API planner (and API executor) uses"),
    Setting("coder_model", "ROCTO_DEEPSEEK_CODER_MODEL", str, None,
            "Model the API executor uses; defaults to `model`"),
    Setting("planner", "ROCTO_PLANNER", str, "auto",
            "auto | api | claude — who writes the task specification"),
    Setting("mode", "ROCTO_MODE", str, "auto",
            "auto | cheap | best — cheap tries the lowest-cost backend first, "
            "best the strongest"),
    Setting("executor", "ROCTO_EXECUTOR", str, "auto",
            "auto | claude | codex | deepseek — who writes the site"),
    Setting("executor_order", "ROCTO_EXECUTOR_ORDER", str, "claude,codex,deepseek",
            "Failover order for executor=auto"),
    Setting("escalate_after", "ROCTO_ESCALATE_AFTER", int, 2,
            "Consecutive failed verifications before auto hands the repair "
            "to the next executor (0 = never)"),
    Setting("max_retries", "ROCTO_MAX_RETRIES", int, 2,
            "Repair attempts after the first one (0-4)"),
    Setting("timeout", "ROCTO_TIMEOUT", int, 1200,
            "Executor timeout per attempt, in seconds"),
    Setting("planner_timeout", "ROCTO_PLANNER_TIMEOUT", int, 180,
            "Planner request timeout, in seconds"),
    Setting("max_minutes", "ROCTO_MAX_MINUTES", float, None,
            "Stop starting new attempts after this many minutes"),
    Setting("max_cost_usd", "ROCTO_MAX_COST_USD", float, None,
            "Stop starting new attempts once reported spend reaches this"),
    Setting("output_root", "ROCTO_OUTPUT_ROOT", str, "rocto-builds",
            "Where builds go when --output is not given"),
    Setting("open_report", "ROCTO_OPEN", bool, False,
            "Open report.html in a browser when a build finishes"),
    Setting("browser", "ROCTO_BROWSER_BIN", str, None,
            "Chromium-family browser used for verification"),
    Setting("browser_no_sandbox", "ROCTO_BROWSER_NO_SANDBOX", bool, False,
            "Force --no-sandbox (automatic when running as root on Linux)"),
    Setting("claude_bin", "ROCTO_CLAUDE_BIN", str, None, "Claude Code CLI path"),
    Setting("claude_model", "ROCTO_CLAUDE_MODEL", str, None,
            "Model Claude Code uses as planner or executor"),
    Setting("claude_budget_usd", "ROCTO_CLAUDE_BUDGET_USD", float, 1.5,
            "Claude Code spend ceiling per executor attempt"),
    Setting("codex_bin", "ROCTO_CODEX_BIN", str, None, "Codex CLI path"),
)

SETTINGS_BY_KEY = {setting.key: setting for setting in SETTINGS}

#: Keys that look like they belong in a config file but must not be there.
_FORBIDDEN_KEYS = {
    "api_key": "rocto never reads API keys from files. Put the key in an "
    "environment variable and set api_key_env to that variable's name.",
    "deepseek_api_key": "rocto never reads API keys from files. Use "
    "DEEPSEEK_API_KEY in the environment, or api_key_env.",
}

#: Special key: names the variable that holds the API key.
API_KEY_ENV_KEY = "api_key_env"


def rocto_home(environ: Mapping[str, str] | None = None) -> Path:
    """Directory for the user config, the run ledger and the last-build pointer."""
    env = os.environ if environ is None else environ
    explicit = env.get(HOME_ENV)
    if explicit:
        return Path(explicit).expanduser()
    if platform.system() == "Windows":
        base = env.get("APPDATA")
        if base:
            return Path(base) / "rocto"
        return Path.home() / "AppData" / "Roaming" / "rocto"
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "rocto"
    return Path.home() / ".config" / "rocto"


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return rocto_home(env) / "config.toml"


def project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / PROJECT_CONFIG_NAME


@dataclass
class LoadedConfig:
    #: key -> (value, source label)
    values: dict[str, tuple[Any, str]]
    files: list[Path]


def load_config(
    cwd: Path | None = None, environ: Mapping[str, str] | None = None
) -> LoadedConfig:
    """Read the user config, then the project config on top of it."""
    values: dict[str, tuple[Any, str]] = {}
    files: list[Path] = []
    for path in (user_config_path(environ), project_config_path(cwd)):
        if not path.is_file():
            continue
        files.append(path)
        for key, value in read_config_file(path).items():
            values[key] = (value, str(path))
    return LoadedConfig(values, files)


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = _parse_toml(text)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    # Accept both a flat file and one with a [rocto] table.
    table = raw.get("rocto", raw) if isinstance(raw.get("rocto"), dict) else raw
    result: dict[str, Any] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            continue
        if key in _FORBIDDEN_KEYS:
            raise ConfigError(f"{path}: {key}: {_FORBIDDEN_KEYS[key]}")
        if key == API_KEY_ENV_KEY:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ConfigError(f"{path}: api_key_env must be a variable name")
            result[key] = value
            continue
        setting = SETTINGS_BY_KEY.get(key)
        if setting is None:
            known = ", ".join(sorted([*SETTINGS_BY_KEY, API_KEY_ENV_KEY]))
            raise ConfigError(f"{path}: unknown setting {key!r} (known: {known})")
        result[key] = _coerce(setting, value, path)
    return result


def _coerce(setting: Setting, value: Any, path: Path) -> Any:
    kind = setting.kind
    if kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            return value.lower() in {"1", "true", "yes", "on"}
    elif kind is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif kind is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif kind is str:
        if isinstance(value, str):
            return value
    raise ConfigError(
        f"{path}: {setting.key} must be a {kind.__name__}, got {value!r}"
    )


def _env_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def apply_config(
    loaded: LoadedConfig, environ: MutableMapping[str, str] | None = None
) -> dict[str, str]:
    """Fill unset environment variables from config. Returns env -> source."""
    env = os.environ if environ is None else environ
    applied: dict[str, str] = {}
    for key, (value, source) in loaded.values.items():
        if key == API_KEY_ENV_KEY:
            if env.get("ROCTO_API_KEY") or env.get("DEEPSEEK_API_KEY"):
                continue
            secret = env.get(value)
            if secret:
                env["ROCTO_API_KEY"] = secret
                applied["ROCTO_API_KEY"] = f"{source} (from ${value})"
            continue
        setting = SETTINGS_BY_KEY[key]
        if setting.env in env and env[setting.env] != "":
            continue
        env[setting.env] = _env_text(value)
        applied[setting.env] = source
    return applied


def describe(
    loaded: LoadedConfig,
    environ: Mapping[str, str] | None = None,
    applied: Mapping[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """(key, effective value, where it came from) for every setting."""
    env = os.environ if environ is None else environ
    applied = applied or {}
    rows: list[tuple[str, str, str]] = []
    for setting in SETTINGS:
        if setting.env in env and env[setting.env] != "":
            source = applied.get(setting.env, f"env ${setting.env}")
            rows.append((setting.key, env[setting.env], source))
        elif setting.default is not None:
            rows.append((setting.key, _env_text(setting.default), "built-in default"))
        else:
            rows.append((setting.key, "", "not set"))
    has_key = bool(env.get("ROCTO_API_KEY") or env.get("DEEPSEEK_API_KEY"))
    key_source = applied.get("ROCTO_API_KEY") or (
        "env $ROCTO_API_KEY" if env.get("ROCTO_API_KEY")
        else "env $DEEPSEEK_API_KEY" if env.get("DEEPSEEK_API_KEY")
        else "not set"
    )
    rows.append(("api_key", "<configured>" if has_key else "", key_source))
    return rows


def set_config_value(path: Path, key: str, raw_value: str | None) -> None:
    """Set (or with ``None`` remove) one key in a flat config file.

    Other lines, comments included, are kept as they are.
    """
    if key in _FORBIDDEN_KEYS:
        raise ConfigError(_FORBIDDEN_KEYS[key])
    if key != API_KEY_ENV_KEY and key not in SETTINGS_BY_KEY:
        raise ConfigError(f"unknown setting {key!r}")
    rendered = None
    if raw_value is not None:
        if key == API_KEY_ENV_KEY:
            value: Any = raw_value
        else:
            value = _parse_cli_value(SETTINGS_BY_KEY[key], raw_value, path)
        rendered = f"{key} = {_toml_literal(value)}"

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    kept: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if rendered and not replaced:
                kept.append(rendered)
                replaced = True
            continue
        kept.append(line)
    if rendered and not replaced:
        kept.append(rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")


def _parse_cli_value(setting: Setting, raw: str, path: Path) -> Any:
    if setting.kind is bool:
        return _coerce(setting, raw, path)
    if setting.kind is int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{setting.key} must be an integer") from exc
    if setting.kind is float:
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{setting.key} must be a number") from exc
    return raw


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def template() -> str:
    """A commented rocto.toml listing every setting at its default."""
    lines = [
        "# Rainbow Octopus project configuration.",
        "# Every value here is a default for an environment variable; an explicit",
        "# environment variable or CLI flag always wins. Uncomment what you need.",
        "#",
        "# API keys are never read from this file. Keep the key in the environment",
        "# and, if it is not ROCTO_API_KEY or DEEPSEEK_API_KEY, name it here:",
        '# api_key_env = "OPENROUTER_API_KEY"',
        "",
    ]
    for setting in SETTINGS:
        lines.append(f"# {setting.help}  (${setting.env})")
        default = setting.default
        shown = _toml_literal(default) if default is not None else '""'
        lines.append(f"# {setting.key} = {shown}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# TOML: stdlib tomllib when present, otherwise a flat-file subset for 3.10.
# --------------------------------------------------------------------------


def _parse_toml(text: str) -> dict[str, Any]:
    if _toml is not None:
        try:
            return _toml.loads(text)
        except _toml.TOMLDecodeError as exc:
            raise ValueError(str(exc)) from exc
    return _parse_flat_toml(text)


_KEY_VALUE = re.compile(r"^([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$")


def _parse_flat_toml(text: str) -> dict[str, Any]:
    """Enough TOML for rocto's flat config: strings, numbers, booleans, tables."""
    root: dict[str, Any] = {}
    current = root
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = root.setdefault(line[1:-1].strip(), {})
            continue
        match = _KEY_VALUE.match(line)
        if not match:
            raise ValueError(f"line {number}: expected key = value")
        key, value = match.groups()
        current[key] = _flat_value(value, number)
    return root


def _flat_value(value: str, number: int) -> Any:
    if value[:1] in {'"', "'"}:
        quote = value[0]
        end = value.find(quote, 1)
        while quote == '"' and end > 0 and value[end - 1] == "\\":
            end = value.find(quote, end + 1)
        if end < 0:
            raise ValueError(f"line {number}: unterminated string")
        rest = value[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise ValueError(f"line {number}: unexpected text after string")
        body = value[1:end]
        if quote == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    value = value.split("#", 1)[0].strip()
    if value in {"true", "false"}:
        return value == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"line {number}: unsupported value {value!r}") from exc
