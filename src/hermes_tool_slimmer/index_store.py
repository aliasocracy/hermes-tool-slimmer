from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import hermes_home
from .corpus import build_corpus, tool_description, tool_name, tool_toolset
from .corpus import _schema_parameters
from .types import Schema


@dataclass
class IndexStore:
    root: Path
    path: Path

    def __init__(self, root: Path | str | None = None) -> None:
        root = Path(root or hermes_home() / "tool-slimmer").expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.path = root / "tool_index.json"
        self.live_schemas_path = root / "live_tool_schemas.json"
        self.live_schemas_dir = root / "live_tool_schemas"

    @staticmethod
    def checksum(schemas: list[Schema]) -> str:
        normalized = [
            {"name": tool_name(schema), "toolset": tool_toolset(schema), "description": tool_description(schema), "parameters": _schema_parameters(schema)}
            for schema in schemas
        ]
        normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str, separators=(",", ":")))
        payload = json.dumps(normalized, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_live_schemas(self, schemas: list[Schema], context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        payload = {
            "checksum": self.checksum(schemas),
            "total_tools": len(build_corpus(schemas)),
            "schemas": schemas,
            "context": context,
        }
        _write_private_json(self.live_schemas_path, payload)
        platform = _safe_snapshot_name(context.get("platform"))
        if platform:
            self.live_schemas_dir.mkdir(parents=True, exist_ok=True)
            _write_private_json(self.live_schemas_dir / f"{platform}.json", payload)
        return payload

    def load_live_schema_snapshot(self, platform: str | None = None) -> dict[str, Any] | None:
        path = self.live_schemas_path
        if platform:
            snapshot_name = _safe_snapshot_name(platform)
            if not snapshot_name:
                return None
            path = self.live_schemas_dir / f"{snapshot_name}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        schemas = payload.get("schemas")
        if not isinstance(schemas, list):
            return None
        checksum = payload.get("checksum")
        if isinstance(checksum, str) and checksum != self.checksum(schemas):
            return None
        return payload

    def live_schema_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        paths = []
        if self.live_schemas_path.exists():
            paths.append(("latest", self.live_schemas_path))
        if self.live_schemas_dir.exists():
            paths.extend((path.stem, path) for path in sorted(self.live_schemas_dir.glob("*.json")))
        seen: set[tuple[str, str]] = set()
        for label, path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            raw_context = payload.get("context")
            context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
            platform = str(context.get("platform") or label)
            key = (label, platform)
            if key in seen:
                continue
            seen.add(key)
            try:
                updated_at = path.stat().st_mtime
            except OSError:
                updated_at = None
            summaries.append(
                {
                    "label": label,
                    "platform": platform,
                    "total_tools": _safe_int(payload.get("total_tools")),
                    "schema_count": _safe_int(context.get("schema_count")),
                    "has_session_id": bool(context.get("session_id")),
                    "checksum": payload.get("checksum"),
                    "updated_at": updated_at,
                }
            )
        return summaries

    def load_live_schemas(self, min_total_tools: int = 0, require_session: bool = True, max_age_seconds: int | None = 3600) -> list[Schema]:
        if not self.live_schemas_path.exists():
            return []
        if max_age_seconds is not None:
            try:
                if time.time() - self.live_schemas_path.stat().st_mtime > max_age_seconds:
                    return []
            except OSError:
                return []
        try:
            payload = json.loads(self.live_schemas_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        if _safe_int(payload.get("total_tools")) < min_total_tools:
            return []
        context = payload.get("context")
        if require_session and (not isinstance(context, dict) or not context.get("session_id")):
            return []
        schemas = payload.get("schemas")
        if not isinstance(schemas, list):
            return []
        checksum = payload.get("checksum")
        if isinstance(checksum, str) and checksum != self.checksum(schemas):
            return []
        return schemas

    def rebuild(self, schemas: list[Schema]) -> dict[str, Any]:
        docs = build_corpus(schemas)
        payload = {
            "checksum": self.checksum(schemas),
            "total_tools": len(docs),
            "documents": [{"name": doc.name, "toolset": doc.toolset, "tokens": doc.tokens, "text": doc.text} for doc in docs],
        }
        _write_json(self.path, payload)
        return payload

    def ensure(self, schemas: list[Schema]) -> dict[str, Any]:
        current = self.load()
        checksum = self.checksum(schemas)
        if not current or current.get("checksum") != checksum:
            return self.rebuild(schemas)
        return current


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_snapshot_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    return safe.strip("._-")


def _write_json(path: Path, payload: dict[str, Any], *, private: bool = False) -> None:
    # Gateway and dashboard processes share these files; replace atomically so
    # a reader never sees a half-written snapshot.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    if private:
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload, private=True)
