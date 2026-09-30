"""Adapter from Hermes ``llm_request`` middleware to the Tool Slimmer selector.

Hermes v0.19+ runs registered ``llm_request`` middleware on the provider kwargs
right before each model call. That surface ships in Hermes core, so it keeps
working after Hermes updates without the local ``select_tool_schemas`` core patch.

The payload is provider-shaped (OpenAI chat, Anthropic messages, or Codex
responses), so this module normalizes the message history into the chat shape
the selector already understands and only ever removes function tools. Anything
it does not recognize is left untouched.
"""

from __future__ import annotations

import re
from typing import Any

from .corpus import tool_name
from .types import Schema

MIDDLEWARE_SOURCE = "tool-slimmer"


def _is_function_tool(tool: Any) -> bool:
    if not isinstance(tool, dict) or not tool_name(tool):
        return False
    kind = tool.get("type")
    if kind == "function":
        # OpenAI chat nests the definition; Codex responses keeps it flat.
        return isinstance(tool.get("function"), dict) or "parameters" in tool or "name" in tool
    # Anthropic custom tools have no type (or type "custom") and an input_schema.
    return kind in {None, "custom"} and "input_schema" in tool


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}:
        text = block.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part for part in (_block_text(block) for block in content) if part)
    return ""


def _tool_output_text(value: Any) -> Any:
    if isinstance(value, list):
        return _content_text(value)
    return value


def normalize_history(items: Any) -> list[dict[str, Any]]:
    """Map provider message lists onto chat-style ``role``/``content`` dicts.

    Tool results become ``role: "tool"`` entries so full-tool fallback markers
    and the "stop at the previous user turn" logic behave the same as on the
    canonical Hermes history.
    """
    history: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return history
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        # Codex responses items.
        if kind == "function_call_output":
            history.append({"role": "tool", "content": _tool_output_text(item.get("output"))})
            continue
        if kind == "function_call":
            history.append({"role": "assistant", "content": "", "tool_calls": [item]})
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"system", "developer"}:
            # Hermes' canonical history excludes the system prompt, and its tool
            # mentions would otherwise read as "recently needed" tools.
            continue
        if role == "user" and isinstance(content, list):
            # Anthropic carries tool results inside user messages.
            tool_results = [
                block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            for block in tool_results:
                history.append({"role": "tool", "content": _tool_output_text(block.get("content"))})
            text = _content_text(content)
            if text or not tool_results:
                history.append({"role": "user", "content": text})
            continue
        if isinstance(role, str):
            history.append({**item, "content": content if isinstance(content, str) else _content_text(content) or content})
    return history


def last_user_message(history: list[dict[str, Any]]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            content = item.get("content")
            return content if isinstance(content, str) else _content_text(content)
    return ""


_MEMORY_CONTEXT_RE = re.compile(r"<memory-context>.*?</memory-context>", re.S)


def selection_query_text(wire_text: str, session_id: Any) -> str:
    """Strip Hermes turn injections so ranking sees what the user typed.

    Hermes sends ``user text + memory recall + pre_llm_call contexts``. Prefer the
    clean message captured by our pre_llm_call hook; otherwise drop the fenced
    memory block and our own fallback instruction.
    """
    from .integration import FALLBACK_INSTRUCTION, turn_user_message

    clean = turn_user_message(session_id)
    if clean and wire_text.startswith(clean):
        return clean
    text = _MEMORY_CONTEXT_RE.sub(" ", wire_text)
    return text.replace(FALLBACK_INSTRUCTION, " ").strip()


def _tool_choice_name(tool_choice: Any) -> str:
    if not isinstance(tool_choice, dict):
        return ""
    function = tool_choice.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(tool_choice.get("name") or "")


def apply_selection(request: dict[str, Any], selected: list[Schema]) -> dict[str, Any]:
    """Return a copy of ``request`` whose function tools are replaced by ``selected``."""
    tools = request.get("tools") or []
    pinned = [tool for tool in tools if not _is_function_tool(tool)]
    by_name = {tool_name(tool): tool for tool in tools if _is_function_tool(tool)}
    chosen = [schema for schema in selected if isinstance(schema, dict)]
    chosen_names = {tool_name(schema) for schema in chosen}

    # A forced tool_choice must stay resolvable or the provider rejects the call.
    forced = _tool_choice_name(request.get("tool_choice"))
    if forced and forced not in chosen_names and forced in by_name:
        chosen.append(by_name[forced])

    # Keep the prompt-cache breakpoint Hermes placed on the tool block, and keep
    # it on the last tool so the whole (reordered) tool prefix stays cached.
    cache_marker = next(
        (tool["cache_control"] for tool in reversed(tools) if isinstance(tool, dict) and "cache_control" in tool),
        None,
    )
    new_tools = [*pinned, *chosen]
    if cache_marker is not None and new_tools:
        new_tools = [
            {key: value for key, value in tool.items() if key != "cache_control"} if "cache_control" in tool else tool
            for tool in new_tools
        ]
        new_tools[-1] = {**new_tools[-1], "cache_control": cache_marker}

    out = dict(request)
    if new_tools:
        out["tools"] = new_tools
    else:
        # Providers reject an empty tools array and tool settings without tools.
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            out.pop(key, None)
    return out


def llm_request_middleware(request: Any = None, **context: Any) -> dict[str, Any] | None:
    """Hermes ``llm_request`` middleware: slim ``request["tools"]`` in place of the core hook."""
    from .integration import select_tool_schemas_callback

    if not isinstance(request, dict):
        return None
    tools = request.get("tools")
    if not isinstance(tools, list):
        return None
    function_tools = [tool for tool in tools if _is_function_tool(tool)]
    if not function_tools:
        return None

    messages = request.get("messages")
    if not isinstance(messages, list):
        messages = request.get("input")
    history = normalize_history(messages)
    if not history and isinstance(request.get("input"), str):
        history = [{"role": "user", "content": request["input"]}]

    session_id = context.get("session_id") or None
    selected = select_tool_schemas_callback(
        selection_query_text(last_user_message(history), session_id),
        history,
        function_tools,
        model=str(context.get("model") or request.get("model") or ""),
        platform=str(context.get("platform") or ""),
        provider=context.get("provider"),
        session_id=session_id,
    )
    if selected is None or [id(schema) for schema in selected] == [id(tool) for tool in function_tools]:
        return None
    return {
        "request": apply_selection(request, selected),
        "source": MIDDLEWARE_SOURCE,
        "reason": "tool_schema_selection",
    }
