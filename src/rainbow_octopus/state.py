from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunState:
    idea: str
    phase: str = "created"
    attempt: int = 0
    max_retries: int = 2
    model: str = "deepseek-v4-flash"
    started_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    error: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def transition(self, phase: str, detail: str = "", error: str | None = None) -> None:
        self.phase = phase
        self.error = error
        self.updated_at = utc_now()
        self.history.append(
            {"at": self.updated_at, "phase": phase, "detail": detail[:1000]}
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})


class StateStore:
    def __init__(self, project_dir: Path):
        self.internal_dir = project_dir / ".rocto"
        self.path = self.internal_dir / "run.json"

    def initialize(self, state: RunState) -> None:
        self.internal_dir.mkdir(parents=True, exist_ok=True)
        self.save(state)

    def save(self, state: RunState) -> None:
        self.internal_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    def load(self) -> RunState:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return RunState.from_dict(data)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp, path)

