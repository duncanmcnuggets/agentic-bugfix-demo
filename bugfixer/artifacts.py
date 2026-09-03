"""Artifact persistence for real controller and verifier evidence."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bugfixer.state import RunState, utc_now


@dataclass(frozen=True, slots=True)
class ArtifactStore:
    root: Path

    @classmethod
    def create(cls, repo_root: Path, run_id: str) -> ArtifactStore:
        root = repo_root.resolve() / "artifacts" / run_id
        for directory in (root, root / "prompts", root / "agents", root / "verification"):
            directory.mkdir(parents=True, exist_ok=False)
        return cls(root=root)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def save_state(self, state: RunState) -> None:
        state.save_atomic(self.state_path)

    def _resolve(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("Artifact path escapes run directory") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_text(self, relative_path: str, content: str) -> Path:
        target = self._resolve(relative_path)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
        return target

    def write_json(self, relative_path: str, value: Any) -> Path:
        return self.write_text(
            relative_path,
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str),
        )

    def append_event(self, event: str, **data: object) -> None:
        record = {"timestamp": utc_now(), "event": event, **data}
        target = self._resolve("controller.trace.jsonl")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
            handle.write("\n")

    def copy_runtime_prompts(self, prompt_dir: Path) -> None:
        for source in sorted(prompt_dir.glob("*.md")):
            shutil.copyfile(source, self._resolve(f"prompts/{source.stem}.txt"))

