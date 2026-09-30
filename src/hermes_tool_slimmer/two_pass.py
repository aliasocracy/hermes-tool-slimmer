from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import ToolSlimmerConfig
from .corpus import tool_description, tool_name, tool_toolset
from .metrics import approx_tokens, schema_bytes
from .policy import eligible_schemas
from .tokenizer import tokenize
from .types import Schema

HYDRATE_TOOL_NAME = "tool_slimmer_hydrate_tools"
HYDRATE_REQUEST_MARKER = "tool_slimmer_hydrate_tools_requested"
SAFETY_TOOL_NAMES = ("tool_slimmer_request_full_tools", HYDRATE_TOOL_NAME)


@dataclass(frozen=True)
class CompactTool:
    name: str
    description: str
    toolset: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


def compact_catalog(schemas: Iterable[Schema], cfg: ToolSlimmerConfig) -> list[CompactTool]:
    tools: list[CompactTool] = []
    seen: set[str] = set()
    for schema in eligible_schemas(schemas, cfg):
        name = tool_name(schema)
        if not name or name in seen:
            continue
        toolset = tool_toolset(schema)
        if name in {HYDRATE_TOOL_NAME, *SAFETY_TOOL_NAMES}:
            continue
        desc = _one_line(tool_description(schema) or name)
        tools.append(
            CompactTool(
                name=name,
                description=desc,
                toolset=toolset if cfg.two_pass.include_toolsets else None,
                tags=_tags(name, toolset, desc),
            )
        )
        seen.add(name)
    return sorted(tools, key=lambda item: item.name)[: cfg.two_pass.max_catalog_tools]


def render_compact_catalog(tools: list[CompactTool]) -> str:
    lines = [
        "Experimental Tool Slimmer two-pass mode is active.",
        "Use this compact catalog to decide which full tool schemas are needed.",
        f"Request schemas in one batch by calling {HYDRATE_TOOL_NAME} with tool names.",
        "",
        "Compact tool catalog:",
    ]
    for item in tools:
        suffix = ""
        if item.toolset:
            suffix += f" toolset={item.toolset}"
        if item.tags:
            suffix += f" tags={','.join(item.tags)}"
        lines.append(f"- {item.name}: {item.description}{suffix}")
    return "\n".join(lines)


def compact_catalog_metrics(tools: list[CompactTool], original: list[Schema]) -> dict[str, object]:
    rendered = render_compact_catalog(tools)
    catalog_bytes = len(rendered.encode("utf-8"))
    return {
        "two_pass_catalog_tools": len(tools),
        "two_pass_catalog_bytes": catalog_bytes,
        "two_pass_catalog_approx_tokens": approx_tokens(catalog_bytes),
        "two_pass_full_schema_bytes": schema_bytes(original),
        "two_pass_full_schema_approx_tokens": approx_tokens(schema_bytes(original)),
    }


def hydrate_tool_schema(base_schema: Schema | None, tools: list[CompactTool]) -> Schema:
    catalog = render_compact_catalog(tools)
    allowed_names = [item.name for item in tools]
    schema: Schema = copy.deepcopy(base_schema) if isinstance(base_schema, dict) else {}
    if not schema:
        schema = {
            "name": HYDRATE_TOOL_NAME,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        }
    function = schema.get("function") if isinstance(schema.get("function"), dict) else None
    target = function if function is not None else schema
    target["description"] = (
        f"{catalog}\n\n"
        "This does not execute tools. It only asks Hermes to expose the requested full schemas "
        "on the next model call. Batch all likely-needed tools together."
    )
    # Anthropic-shaped schemas (llm_request middleware path) use input_schema.
    params_key = "input_schema" if "input_schema" in target else "parameters"
    raw_params = target.get(params_key)
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if not params:
        params = {"type": "object", "properties": {}}
    raw_properties = params.get("properties")
    properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
    tools_property: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string", "enum": allowed_names},
        "description": "Tool names whose full schemas should be exposed on the next model call.",
    }
    properties["tools"] = tools_property
    properties["reason"] = {
        "type": "string",
        "description": "Short reason these schemas are needed.",
    }
    params["properties"] = properties
    params["required"] = ["tools"]
    target[params_key] = params
    if function is not None:
        schema["function"] = target
    return schema


def requested_hydration_tools(conversation_history: list[Any] | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in conversation_history or []:
        for name in _walk_hydration_markers(item):
            if name not in seen:
                names.append(name)
                seen.add(name)
    return names


def hydration_response(tools: list[str], *, reason: str | None = None, limit: int = 8) -> dict[str, object]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in tools:
        text = str(name).strip()
        if not text or text in seen:
            continue
        cleaned.append(text)
        seen.add(text)
        if len(cleaned) >= limit:
            break
    payload: dict[str, object] = {
        "ok": True,
        HYDRATE_REQUEST_MARKER: True,
        "tools": cleaned,
        "message": "Requested full schemas for the next model call. Retry the original task after schemas hydrate.",
    }
    if reason:
        payload["reason"] = str(reason)
    return payload


def _walk_hydration_markers(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        if value.get(HYDRATE_REQUEST_MARKER) is True:
            tools = value.get("tools")
            if isinstance(tools, list):
                for name in tools:
                    if name is not None:
                        yield str(name)
        for nested in value.values():
            yield from _walk_hydration_markers(nested)
    elif isinstance(value, list | tuple):
        for nested in value:
            yield from _walk_hydration_markers(nested)
    elif isinstance(value, str) and HYDRATE_REQUEST_MARKER in value:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk_hydration_markers(payload)


def _one_line(value: str, limit: int = 140) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _tags(name: str, toolset: str | None, description: str) -> tuple[str, ...]:
    raw = set(tokenize(f"{name} {toolset or ''} {description}"))
    priority = [
        "file",
        "read",
        "write",
        "search",
        "web",
        "browser",
        "github",
        "memory",
        "code",
        "image",
        "slack",
        "telegram",
        "cron",
        "skill",
        "database",
        "email",
    ]
    return tuple(tag for tag in priority if tag in raw)[:4]
