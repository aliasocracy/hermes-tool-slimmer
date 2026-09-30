# Hermes core integration

Hermes v0.19+ ships `llm_request` middleware that can rewrite the provider request before each model call. Tool Slimmer v0.7.0+ registers there when the `select_tool_schemas` hook is not available, so current Hermes releases need no core patch. The adapter (`src/hermes_tool_slimmer/middleware.py`) normalizes OpenAI chat, Anthropic messages, and Codex responses payloads, only removes function tools, keeps provider server tools, keeps a forced `tool_choice` target, and moves the tool prompt-cache breakpoint onto the last kept tool. Bedrock Converse requests (`toolConfig`) pass through unchanged.

The rest of this page describes the core hook path, which is still preferred when a patched core both advertises and calls `select_tool_schemas`.

Hermes Tool Slimmer needs one upstream integration point before provider request construction. Current Hermes v0.14.0 source inspection found the main schema provider in `model_tools.get_tool_definitions(...)`, plugin hook registration in `hermes_cli.plugins.PluginContext.register_hook(...)`, turn orchestration in `agent/conversation_loop.py`, and provider kwargs construction in `agent/chat_completion_helpers.py`.

Compatibility note: Tool Slimmer v0.4.0+ is the supported line for Hermes Agent v0.14.0 active schema slimming. The installer patcher still carries a fallback for older monolithic `run_agent.py` Hermes cores, but older Tool Slimmer releases should not be used with Hermes v0.14.0.

`docs/hermes-core-selector-hook.patch` is a minimal upstreamable patch artifact for Hermes core development, not the normal user install path. Normal users should rerun `scripts/install-hermes-tool-slimmer.sh`; the installer detects the local Hermes layout and applies the compatibility patch for released Hermes versions. The patch artifact adds:

- `select_tool_schemas` to `VALID_HOOKS`.
- One turn-level invocation after `pre_llm_call` and before provider request construction.
- Request-local schema lists applied only while provider kwargs are built by temporarily swapping `self.tools`/`agent.tools`, then restoring the canonical catalog immediately. Selected subsets do not become sticky across turns, and an empty selected list remains a valid "send no tools" result.
- First-non-`None` selector behavior with warning for multiple selector results.
- Fail-open behavior because plugin hook exceptions are swallowed by `PluginManager.invoke_hook` and the caller keeps the original request-local schema list on malformed/no results.
- Contract tests for valid hook registration, first selector result, selector exception behavior, and non-sticky catalog semantics.

Validation evidence captured while preparing the patch artifact:

```bash
git -C /tmp/hermes-agent-core apply /path/to/hermes-tool-slimmer/docs/hermes-core-selector-hook.patch
python3 -m py_compile hermes_cli/plugins.py agent/conversation_loop.py tests/hermes_cli/test_tool_schema_selector_hook.py
PYTHONPATH=/tmp/hermes-agent-core pytest -q -o addopts='' tests/hermes_cli/test_tool_schema_selector_hook.py
# 4 passed
git -C /tmp/hermes-agent-core diff --check
# clean
```

MCP metadata evidence from current Hermes source: MCP tools are registered in `tools/mcp_tool.py` with `toolset_name = f"mcp-{name}"`, converted schema names use `mcp_{server}_{tool}`, and registry collision logic treats `existing_toolset.startswith("mcp-")` as MCP-origin. Hermes Tool Slimmer therefore classifies `mcp`, `mcp_tools`, `mcp:`, `mcp-`, `mcp_server`, and `mcp_` name-prefix shapes as MCP.

Preferred callback contract:

```python
def callback(
    user_message: str,
    conversation_history: list,
    schemas: list[dict],
    model: str,
    platform: str,
    provider: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> list[dict] | None:
    ...
```

Core behavior should be fail-open:

1. Build the normal enabled schema list.
2. Invoke `select_tool_schemas` callbacks with that list.
3. If no callback returns a list, keep the original schemas.
4. If one or more callbacks return lists, use only the first list and warn when there are multiple.
5. If a callback raises, log and keep processing other callbacks; if no valid list remains, keep the original schemas.

The plugin still probes compatibility methods (`register_tool_schema_selector`, `register_schema_selector`, or `register_hook("select_tool_schemas", ...)`). Active slimming requires one of those selector surfaces; otherwise the plugin can report diagnostics but cannot alter provider request schemas.
