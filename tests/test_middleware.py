import json
import sys
import types

import pytest

from hermes_tool_slimmer import integration
from hermes_tool_slimmer.integration import (
    SURFACE_HOOK,
    SURFACE_MIDDLEWARE,
    maybe_register_selector_hook,
    resolve_selector_surface,
)
from hermes_tool_slimmer.middleware import apply_selection, llm_request_middleware, normalize_history
from hermes_tool_slimmer.tools import FULL_TOOLS_REQUEST_MARKER
from hermes_tool_slimmer.two_pass import HYDRATE_TOOL_NAME, hydrate_tool_schema


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    return tmp_path


def _write_config(home, **settings):
    lines = ["tool_slimmer:"] + [f"  {key}: {json.dumps(value)}" for key, value in settings.items()]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _openai_tool(name, description):
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": {}}},
    }


def _anthropic_tool(name, description):
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": {}}}


def _codex_tool(name, description):
    return {"type": "function", "name": name, "description": description, "parameters": {"type": "object", "properties": {}}}


CATALOG = [
    ("read_file", "Read a file from disk"),
    ("web_search", "Search the web for pages"),
    ("send_email", "Send an email message"),
    ("image_generate", "Generate an image from a prompt"),
    ("calendar_create", "Create a calendar event"),
    ("tool_slimmer_request_full_tools", "Request the full tool list"),
]


class MiddlewareCtx:
    def __init__(self, valid_hooks=None, valid_middleware=None):
        self.calls = []
        if valid_hooks is not None:
            self.valid_hooks = valid_hooks
        if valid_middleware is not None:
            self.valid_middleware = valid_middleware

    def register_hook(self, name, callback):
        self.calls.append(("hook", name))

    def register_middleware(self, kind, callback):
        self.calls.append(("middleware", kind))


def test_issue_5_unpatched_core_uses_llm_request_middleware():
    # Hermes v0.19+ without the local core patch: the hook is not advertised
    # but llm_request middleware is, so slimming must still be registered.
    ctx = MiddlewareCtx(valid_hooks={"pre_llm_call"}, valid_middleware={"llm_request", "tool_request"})

    assert maybe_register_selector_hook(ctx) is True
    assert ctx.calls == [("hook", "pre_llm_call"), ("middleware", "llm_request")]


def test_patched_core_prefers_hook_over_middleware(monkeypatch):
    monkeypatch.setattr(integration, "_core_invokes_selector_hook", lambda: True)
    ctx = MiddlewareCtx(valid_hooks={"pre_llm_call", "select_tool_schemas"}, valid_middleware={"llm_request"})

    assert maybe_register_selector_hook(ctx) is True
    assert ctx.calls == [("hook", "pre_llm_call"), ("hook", "select_tool_schemas")]


def test_partially_patched_core_falls_back_to_middleware(monkeypatch):
    # VALID_HOOKS advertises the hook but no turn-loop module calls it.
    monkeypatch.setattr(integration, "_core_invokes_selector_hook", lambda: False)
    ctx = MiddlewareCtx(valid_hooks={"pre_llm_call", "select_tool_schemas"}, valid_middleware={"llm_request"})

    assert resolve_selector_surface(ctx) == SURFACE_MIDDLEWARE
    assert maybe_register_selector_hook(ctx) is True
    assert ("middleware", "llm_request") in ctx.calls
    assert ("hook", "select_tool_schemas") not in ctx.calls


def test_no_surface_reports_diagnostics_only():
    class Ctx:
        valid_hooks = {"pre_llm_call"}
        valid_middleware = {"tool_request"}

        def register_hook(self, name, callback):
            pass

        def register_middleware(self, kind, callback):
            raise AssertionError("must not register unsupported middleware")

    assert maybe_register_selector_hook(Ctx()) is False


def test_resolve_surface_without_ctx_reads_hermes_modules(monkeypatch):
    hermes_cli = types.ModuleType("hermes_cli")
    plugins = types.ModuleType("hermes_cli.plugins")
    middleware = types.ModuleType("hermes_cli.middleware")
    plugins.VALID_HOOKS = {"pre_llm_call"}
    middleware.VALID_MIDDLEWARE = {"llm_request"}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setitem(sys.modules, "hermes_cli.middleware", middleware)

    assert resolve_selector_surface() == SURFACE_MIDDLEWARE

    plugins.VALID_HOOKS = {"pre_llm_call", "select_tool_schemas"}
    monkeypatch.setattr(integration, "_core_invokes_selector_hook", lambda: None)
    assert resolve_selector_surface() == SURFACE_HOOK


def test_core_call_site_detection(monkeypatch, tmp_path):
    loop = tmp_path / "agent" / "conversation_loop.py"
    loop.parent.mkdir()
    (loop.parent / "__init__.py").write_text("", encoding="utf-8")
    loop.write_text("def run():\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("agent", "agent.conversation_loop"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    assert integration._core_invokes_selector_hook() is False

    loop.write_text('invoke_hook("select_tool_schemas", schemas=[])\n', encoding="utf-8")
    assert integration._core_invokes_selector_hook() is True


def test_middleware_slims_openai_chat_request(isolated_home):
    _write_config(isolated_home, top_k=1, always_include=[], min_estimated_reduction_percent=0)
    tools = [_openai_tool(name, desc) for name, desc in CATALOG]
    request = {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "search the web for hermes release notes"},
        ],
        "tools": tools,
        "tool_choice": "auto",
    }

    result = llm_request_middleware(request=request, original_request=request, session_id="s1", platform="cli", model="gpt-test", provider="openrouter")

    assert result is not None
    assert result["source"] == "tool-slimmer"
    names = [tool["function"]["name"] for tool in result["request"]["tools"]]
    assert "web_search" in names
    assert "send_email" not in names
    assert "tool_slimmer_request_full_tools" in names
    assert len(request["tools"]) == len(CATALOG), "original request must not be mutated"


def test_middleware_slims_anthropic_request_and_keeps_server_tools(isolated_home):
    _write_config(isolated_home, top_k=1, always_include=[], min_estimated_reduction_percent=0)
    server_tool = {"type": "web_search_20250305", "name": "web_search_server", "max_uses": 3}
    tools = [_anthropic_tool(name, desc) for name, desc in CATALOG]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    request = {
        "model": "claude-test",
        "system": "sys",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "send an email to the team"}]}],
        "tools": [server_tool, *tools],
    }

    result = llm_request_middleware(request=request, session_id="s1", platform="cli", model="claude-test", provider="anthropic")

    assert result is not None
    new_tools = result["request"]["tools"]
    assert new_tools[0] == server_tool
    names = [tool["name"] for tool in new_tools[1:]]
    assert "send_email" in names
    assert "image_generate" not in names
    assert new_tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert sum(1 for tool in new_tools if "cache_control" in tool) == 1


def test_middleware_honors_full_tools_marker_in_anthropic_tool_result(isolated_home):
    _write_config(isolated_home, top_k=1, always_include=[], min_estimated_reduction_percent=0)
    tools = [_anthropic_tool(name, desc) for name, desc in CATALOG]
    marker = json.dumps({FULL_TOOLS_REQUEST_MARKER: True})
    request = {
        "messages": [
            {"role": "user", "content": "send an email"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "tool_slimmer_request_full_tools", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": marker}]},
        ],
        "tools": tools,
    }

    # Full catalog requested: nothing to slim, so the request passes through.
    assert llm_request_middleware(request=request, session_id="s1", platform="cli", provider="anthropic") is None


def test_middleware_handles_codex_responses_input(isolated_home):
    _write_config(isolated_home, top_k=1, always_include=[], min_estimated_reduction_percent=0)
    tools = [_codex_tool(name, desc) for name, desc in CATALOG]
    request = {
        "model": "gpt-codex",
        "instructions": "sys",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "generate an image of a cat"}]},
            {"type": "function_call", "name": "image_generate", "call_id": "c1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
        ],
        "tools": tools,
    }

    result = llm_request_middleware(request=request, session_id="s1", platform="cli", provider="openai-codex")

    assert result is not None
    names = [tool["name"] for tool in result["request"]["tools"]]
    assert "image_generate" in names
    assert "calendar_create" not in names


def test_middleware_passes_through_when_disabled_or_toolless(isolated_home):
    _write_config(isolated_home, enabled=False)
    tools = [_openai_tool(name, desc) for name, desc in CATALOG]
    request = {"messages": [{"role": "user", "content": "search the web"}], "tools": tools}

    assert llm_request_middleware(request=request, session_id="s1", platform="cli") is None
    assert llm_request_middleware(request={"messages": []}, session_id="s1") is None
    assert llm_request_middleware(request=None) is None


def test_apply_selection_keeps_forced_tool_and_drops_empty_tool_settings():
    tools = [_openai_tool("read_file", "Read"), _openai_tool("web_search", "Search")]
    forced = {"tools": tools, "tool_choice": {"type": "function", "function": {"name": "web_search"}}}
    kept = apply_selection(forced, [tools[0]])
    assert [tool["function"]["name"] for tool in kept["tools"]] == ["read_file", "web_search"]

    emptied = apply_selection({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": True}, [])
    assert "tools" not in emptied
    assert "tool_choice" not in emptied
    assert "parallel_tool_calls" not in emptied


def test_normalize_history_maps_provider_tool_results_to_tool_role():
    history = normalize_history(
        [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": [{"type": "text", "text": "ok"}]}]},
            {"type": "function_call_output", "call_id": "c", "output": "out"},
        ]
    )
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "ok"},
        {"role": "tool", "content": "out"},
    ]


def test_hydrate_tool_schema_preserves_anthropic_input_schema():
    base = {"name": HYDRATE_TOOL_NAME, "description": "", "input_schema": {"type": "object", "properties": {}}}
    schema = hydrate_tool_schema(base, [])
    assert "parameters" not in schema
    assert schema["input_schema"]["required"] == ["tools"]


def test_middleware_ranks_clean_user_message_not_hermes_injections(isolated_home):
    from hermes_tool_slimmer.integration import FALLBACK_INSTRUCTION, pre_llm_diagnostic_hook
    from hermes_tool_slimmer.middleware import selection_query_text

    memory = "<memory-context>\n[System note: recalled]\nuser likes calendar events and email\n</memory-context>"
    wire = f"search the web for news\n\n{memory}\n\n{FALLBACK_INSTRUCTION}\n\nother plugin: generate image"

    # Without a recorded turn message, known injections are stripped.
    stripped = selection_query_text(wire, "unknown-session")
    assert "calendar" not in stripped
    assert FALLBACK_INSTRUCTION not in stripped

    # With the pre_llm_call record, the exact user text wins.
    pre_llm_diagnostic_hook(session_id="s-clean", user_message="search the web for news")
    assert selection_query_text(wire, "s-clean") == "search the web for news"

    _write_config(isolated_home, top_k=1, always_include=[], min_estimated_reduction_percent=0)
    tools = [_openai_tool(name, desc) for name, desc in CATALOG]
    request = {"messages": [{"role": "user", "content": wire}], "tools": tools}
    result = llm_request_middleware(request=request, session_id="s-clean", platform="cli")
    names = [tool["function"]["name"] for tool in result["request"]["tools"]]
    assert "web_search" in names
    assert "calendar_create" not in names
    assert "image_generate" not in names


def test_system_prompt_tool_mentions_do_not_boost_selection(isolated_home):
    _write_config(isolated_home, top_k=1, always_include=[], min_estimated_reduction_percent=0)
    tools = [_openai_tool(name, desc) for name, desc in CATALOG]
    request = {
        "messages": [
            {"role": "system", "content": "You can use send_email, calendar_create and image_generate."},
            {"role": "user", "content": "search the web for news"},
        ],
        "tools": tools,
    }

    result = llm_request_middleware(request=request, session_id="s-sys", platform="cli")

    names = [tool["function"]["name"] for tool in result["request"]["tools"]]
    assert "web_search" in names
    assert not {"send_email", "calendar_create", "image_generate"} & set(names)
