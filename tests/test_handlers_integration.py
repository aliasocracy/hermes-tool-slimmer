import json
import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

import hermes_tool_slimmer
from hermes_tool_slimmer.advisor import apply_recommended_config, apply_tool_preference, analyze_config, rollback_config
from hermes_tool_slimmer.commands import handle_slash_command
from hermes_tool_slimmer.config import ToolSlimmerConfig
from hermes_tool_slimmer.cli import _load_prompts, _load_schemas, _tool_names, diagnostic_report
from hermes_tool_slimmer.integration import FALLBACK_INSTRUCTION, maybe_register_selector_hook, pre_llm_diagnostic_hook, select_tool_schemas_callback
from hermes_tool_slimmer.metrics import read_decisions, summarize_decisions
from hermes_tool_slimmer.index_store import IndexStore
from hermes_tool_slimmer.tools import FULL_TOOLS_REQUEST_MARKER, _live_hermes_schemas, tool_slimmer_hydrate_tools, tool_slimmer_request_full_tools, tool_slimmer_select, tool_slimmer_status
from hermes_tool_slimmer.two_pass import HYDRATE_REQUEST_MARKER


def _patch_dashboard_modules(module, monkeypatch):
    from hermes_tool_slimmer.cli import eval_markdown, eval_prompts, privacy_inventory, run_doctor
    from hermes_tool_slimmer.config import load_config
    from hermes_tool_slimmer.metrics import read_decisions, summarize_decisions

    monkeypatch.setattr(
        module,
        "_load_modules",
        lambda: types.SimpleNamespace(
            analyze_config=analyze_config,
            apply_recommended_config=apply_recommended_config,
            apply_tool_preference=apply_tool_preference,
            rollback_config=rollback_config,
            eval_markdown=eval_markdown,
            eval_prompts=eval_prompts,
            privacy_inventory=privacy_inventory,
            run_doctor=run_doctor,
            load_config=load_config,
            IndexStore=IndexStore,
            read_decisions=read_decisions,
            summarize_decisions=summarize_decisions,
        ),
    )


def test_plugin_register_wires_tools_commands_and_hooks(monkeypatch):
    calls = []

    class Ctx:
        valid_hooks = {"pre_llm_call", "select_tool_schemas"}

        def register_tool(self, **kwargs):
            calls.append(("tool", kwargs["name"]))

        def register_command(self, name, **kwargs):
            calls.append(("command", name))

        def register_cli_command(self, name, **kwargs):
            calls.append(("cli", name))

        def register_hook(self, name, callback):
            calls.append(("hook", name))

    hermes_tool_slimmer.register(Ctx())

    assert ("tool", "tool_slimmer_status") in calls
    assert ("tool", "tool_slimmer_select") in calls
    assert ("tool", "tool_slimmer_request_full_tools") in calls
    assert ("tool", "tool_slimmer_hydrate_tools") in calls
    assert ("command", "tool-slimmer") in calls
    assert ("cli", "tool-slimmer") in calls
    assert ("hook", "select_tool_schemas") in calls


def test_plugin_handlers_return_json_strings(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    status = tool_slimmer_status({})
    select = tool_slimmer_select({"query": "read", "schemas": [{"name": "read_file", "description": "Read"}]})
    request_full = tool_slimmer_request_full_tools({"reason": "missing skill tool"})
    hydrate = tool_slimmer_hydrate_tools({"tools": ["web_search"], "reason": "need web"})
    slash = handle_slash_command("select read", schemas=[{"name": "read_file", "description": "Read"}])
    assert json.loads(status)["ok"] is True
    assert json.loads(select)["ok"] is True
    assert json.loads(request_full)[FULL_TOOLS_REQUEST_MARKER] is True
    assert json.loads(hydrate)[HYDRATE_REQUEST_MARKER] is True
    assert json.loads(slash)["ok"] is True


def test_slash_command_status_dry_run_unknown_and_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    status = json.loads(handle_slash_command({"text": "status"}))
    dry_run = json.loads(handle_slash_command("dry-run on"))
    unknown = json.loads(handle_slash_command("bogus"))

    assert status["ok"] is True
    assert dry_run["requested"] == "on"
    assert unknown["ok"] is False

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("hermes_tool_slimmer.commands.tool_slimmer_status", boom)
    failed = json.loads(handle_slash_command("status"))
    assert failed == {"error": "boom", "ok": False}


def test_tool_slimmer_select_rejects_eager_mode_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    result = json.loads(
        tool_slimmer_select(
            {
                "query": "search",
                "mode": "eager",
                "schemas": [
                    {"name": "read_file", "description": "Read"},
                    {"name": "search_files", "description": "Search"},
                ],
            }
        )
    )

    assert result["ok"] is False
    assert result["error"] == "mode_not_allowed"


def test_tool_slimmer_select_falls_back_to_index_when_schemas_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_tool_slimmer.tools._live_hermes_schemas", lambda: [])
    IndexStore().rebuild(
        [
            {"name": "execute_code", "description": "Run python scripts and execute code"},
            {"name": "image_generate", "description": "Generate images"},
        ]
    )

    result = json.loads(tool_slimmer_select({"query": "run a python script", "mode": "keyword", "allow_catalog_fallback": True}))

    assert result["ok"] is True
    assert result["schema_source"] == "index"
    assert result["schema_count"] == 2
    assert result["selected"][0] == "execute_code"


def test_tool_slimmer_select_reports_no_schemas_when_all_sources_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_tool_slimmer.tools._live_hermes_schemas", lambda: [])
    result = json.loads(tool_slimmer_select({"query": "search"}))

    assert result["ok"] is False
    assert result["error"] == "no_schemas_available"
    assert result["schema_source"] == "none"


def test_tool_slimmer_select_does_not_read_catalog_without_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_tool_slimmer.tools._live_hermes_schemas", lambda: [{"name": "live_tool", "description": "Live"}])
    IndexStore().rebuild([{"name": "indexed_tool", "description": "Indexed"}])

    result = json.loads(tool_slimmer_select({"query": "live indexed", "mode": "keyword"}))

    assert result["ok"] is False
    assert result["error"] == "no_schemas_available"
    assert result["schema_source"] == "none"


def test_tool_slimmer_status_handles_bad_config(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tool_slimmer:\n  top_k: -1\n")
    result = json.loads(tool_slimmer_status({"config_path": str(path)}))

    assert result["ok"] is False
    assert "top_k" in result["error"]


def test_live_hermes_schemas_typeerror_fallback_and_bad_payload(monkeypatch):
    module = types.ModuleType("model_tools")
    calls = []

    def get_tool_definitions(*args):
        calls.append(args)
        if args:
            raise TypeError("old signature")
        return [{"name": "fallback_tool"}]

    module.get_tool_definitions = get_tool_definitions
    monkeypatch.setitem(sys.modules, "model_tools", module)

    assert _live_hermes_schemas() == [{"name": "fallback_tool"}]
    assert calls == [(None, None, True), ()]

    module.get_tool_definitions = lambda *args: {"bad": "payload"}
    assert _live_hermes_schemas() == []


def test_tool_slimmer_select_prefers_runtime_live_schemas_before_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    IndexStore().rebuild([{"name": "indexed_tool", "description": "Indexed"}])
    snapshot_schemas = [{"name": "snapshot_tool", "description": "Snapshot"}, *[{"name": f"extra_{idx}"} for idx in range(19)]]
    IndexStore().save_live_schemas(snapshot_schemas, {"session_id": "session-1"})
    module = types.ModuleType("model_tools")
    module.get_tool_definitions = lambda *args: [{"name": "runtime_tool", "description": "Runtime"}]
    monkeypatch.setitem(sys.modules, "model_tools", module)

    result = json.loads(tool_slimmer_select({"query": "runtime", "mode": "keyword", "allow_catalog_fallback": True}))

    assert result["ok"] is True
    assert result["schema_source"] == "live"
    assert result["selected"] == ["runtime_tool"]


def test_cli_tool_names_tolerates_null_function_wrapper():
    assert _tool_names([{"function": None}]) == {""}


def test_cli_load_schemas_handles_missing_path(tmp_path):
    assert _load_schemas(str(tmp_path / "missing.yaml")) == []


def test_cli_load_schemas_accepts_tool_index_documents(tmp_path):
    path = tmp_path / "tool_index.json"
    path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "name": "execute_code",
                        "toolset": "native",
                        "tokens": ["execute", "code"],
                        "text": "execute_code\nRun Python scripts",
                    }
                ]
            }
        )
    )

    assert _load_schemas(str(path)) == [
        {
            "name": "execute_code",
            "toolset": "native",
            "description": "execute_code\nRun Python scripts",
        }
    ]


def test_cli_loaders_handle_malformed_yaml_and_scalar_payloads(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schemas:\n  - [bad\n")
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("plain string")

    assert _load_schemas(str(bad)) == []
    assert _load_prompts(str(bad)) == []
    assert _load_schemas(str(scalar)) == []
    assert _load_prompts(str(scalar)) == []


def test_cli_analyze_config_and_eval(tmp_path, capsys):
    from argparse import Namespace
    from hermes_tool_slimmer.cli import handle_cli

    schemas = tmp_path / "schemas.yaml"
    prompts = tmp_path / "prompts.yaml"
    schemas.write_text("schemas:\n- name: search_files\n  description: Search files\n")
    prompts.write_text("prompts:\n- name: search\n  text: search files\n  expected_any: [search_files]\n")

    assert handle_cli(Namespace(command="eval", config=None, schemas=str(schemas), prompts=str(prompts))) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["hit_rate"] == 1.0
    assert handle_cli(Namespace(command="eval", config=None, schemas=str(schemas), prompts=str(prompts), markdown=True)) == 0
    assert "# Tool Slimmer Eval Report" in capsys.readouterr().out
    assert handle_cli(Namespace(command="analyze-config", config=None)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "recommendations" in out
    assert handle_cli(Namespace(command="privacy", config=None)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["raw_prompts_logged"] is False
    assert "metrics" in out["event_fields"]


def test_advisor_cronjob_warning_respects_telegram_profile_exclude():
    cfg = ToolSlimmerConfig(
        profiles={"telegram": {"always_exclude": ["cronjob"]}},
    )
    summary = {
        "totals": {"events": 2, "skipped_events": 0},
        "platforms": {"telegram": 2},
        "top_selected_tools": {"cronjob": 2},
    }

    report = analyze_config(cfg, summary, indexed_tools=53, available_tools={"cronjob"})

    assert "cronjob_profile_review" not in {item["id"] for item in report["recommendations"]}


def test_cli_status_index_select_recommend_and_main(tmp_path, capsys, monkeypatch):
    from argparse import Namespace
    from hermes_tool_slimmer.cli import handle_cli, main

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = tmp_path / "schemas.yaml"
    schemas.write_text("schemas:\n- name: search_files\n  description: Search files\n")

    assert handle_cli(Namespace(command="status", config=None)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["total_tools_indexed"] == 0
    assert status["source_context"] is None
    assert status["live_snapshots"] == []

    assert handle_cli(Namespace(command="index", index_command="rebuild", schemas=str(schemas), config=None)) == 0
    rebuilt = json.loads(capsys.readouterr().out)
    assert rebuilt["total_tools"] == 1

    assert handle_cli(Namespace(command="index", index_command="show", top=1, config=None)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown[0]["name"] == "search_files"

    assert handle_cli(Namespace(command="select", query="search files", schemas=str(schemas), config=None)) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["selected"] == ["search_files"]
    assert selected["schema_source"] == "file"

    assert handle_cli(Namespace(command="select", query="search files", schemas=None, config=None)) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["selected"] == ["search_files"]
    assert selected["schema_source"] == "index"
    assert selected["schema_count"] == 1

    assert handle_cli(Namespace(command="recommend-config", config=None)) == 0
    assert "tool_slimmer:" in capsys.readouterr().out

    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True


def test_cli_unknown_command_raises():
    from argparse import Namespace
    from hermes_tool_slimmer.cli import handle_cli

    with pytest.raises(ValueError, match="Unknown command"):
        handle_cli(Namespace(command="unknown", config=None))


def test_cli_eval_handles_non_yaml_prompt_payload(tmp_path, capsys):
    from argparse import Namespace
    from hermes_tool_slimmer.cli import handle_cli

    prompts = tmp_path / "prompts.yaml"
    prompts.write_text("plain string")

    assert handle_cli(Namespace(command="eval", config=None, schemas=None, prompts=str(prompts), markdown=False)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["prompts"] == 0


def test_cli_eval_and_benchmark_skip_non_dict_prompt_rows(tmp_path, capsys):
    from argparse import Namespace
    from hermes_tool_slimmer.cli import handle_cli

    prompts = tmp_path / "prompts.yaml"
    prompts.write_text("- plain string\n- name: search\n  text: search files\n")

    assert handle_cli(Namespace(command="eval", config=None, schemas=None, prompts=str(prompts), markdown=False)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["prompts"] == 1

    assert handle_cli(Namespace(command="benchmark", config=None, schemas=None, prompts=str(prompts))) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["benchmarks"]) == 1


def test_doctor_reports_missing_explicit_config_as_failure(tmp_path):
    from hermes_tool_slimmer.cli import run_doctor

    result = run_doctor(str(tmp_path / "missing.yaml"))
    assert result["ok"] is False
    assert result["checks"]["config"]["status"] == "fail"


def test_doctor_validates_always_include_against_index(monkeypatch, tmp_path):
    from hermes_tool_slimmer.cli import run_doctor
    from hermes_tool_slimmer.index_store import IndexStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    IndexStore().rebuild(
        [
            {"name": "terminal"},
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "patch"},
            {"name": "search_files"},
        ]
    )

    result = run_doctor()

    assert result["checks"]["always_include"]["status"] == "pass"
    assert result["checks"]["always_include"]["message"] == "always-included tools exist in tool index"


def test_integration_contract_returns_none_when_disabled():
    out = select_tool_schemas_callback("read", [], [{"name": "read_file"}], "model", "platform", config=ToolSlimmerConfig(enabled=False))
    assert out is None


def test_integration_hook_fails_open_on_malformed_config(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tool_slimmer:\n  mode: definitely_bad\n")
    monkeypatch.setenv("HERMES_CONFIG", str(path))

    out = select_tool_schemas_callback("read", [], [{"name": "read_file"}], "model", "platform")

    assert out is None


def test_integration_contract_dry_run_preserves_original_behavior():
    out = select_tool_schemas_callback(
        "read",
        [],
        [{"name": "read_file"}],
        "model",
        "platform",
        config=ToolSlimmerConfig(dry_run=True, log_decisions=False),
    )
    assert out is None


def test_selector_records_decision_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
        {"name": "slack_send_message", "description": "Send slack message"},
    ]
    out = select_tool_schemas_callback(
        "search files",
        [],
        schemas,
        "model",
        "dashboard",
        provider="test-provider",
        session_id="session-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )
    assert out == [schemas[1]]

    events = read_decisions()
    assert len(events) == 1
    assert events[0]["context"]["provider"] == "test-provider"
    assert events[0]["metrics"]["selected"] == ["search_files"]
    assert events[0]["metrics"]["approx_tokens_saved"] > 0
    summary = summarize_decisions()
    assert summary["totals"]["events"] == 1
    assert summary["totals"]["approx_tokens_saved"] > 0
    assert summarize_decisions(require_session=True)["totals"]["events"] == 1


def test_full_tools_request_marker_bypasses_slimming(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
        {"name": "terminal", "description": "Run commands"},
    ]
    conversation_history = [
        {
            "role": "tool",
            "content": json.dumps({FULL_TOOLS_REQUEST_MARKER: True}),
        }
    ]

    out = select_tool_schemas_callback(
        "search",
        conversation_history,
        schemas,
        "model",
        "tui",
        session_id="session-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )

    assert out == schemas
    event = read_decisions()[0]
    assert event["metrics"]["skipped"] is True
    assert event["metrics"]["skip_reason"] == "full_tools_requested"


def test_full_tools_request_marker_preserves_disabled_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "blocked_tool", "description": "Disabled"},
        {"name": "search_files", "description": "Search files"},
    ]
    conversation_history = [{"role": "tool", "content": json.dumps({FULL_TOOLS_REQUEST_MARKER: True})}]

    out = select_tool_schemas_callback(
        "search",
        conversation_history,
        schemas,
        "model",
        "tui",
        config=ToolSlimmerConfig(top_k=1, always_include=[], disabled_tools=["blocked_tool"], min_total_tools=0, log_decisions=False),
    )

    assert out == [schemas[0], schemas[2]]


def test_full_tools_request_marker_ignores_plain_text_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    conversation_history = [{"role": "assistant", "content": FULL_TOOLS_REQUEST_MARKER}]

    out = select_tool_schemas_callback(
        "search",
        conversation_history,
        schemas,
        "model",
        "tui",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert out == [schemas[1]]


def test_full_tools_request_marker_persists_through_tool_call_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    conversation_history = [
        {"role": "tool", "content": json.dumps({FULL_TOOLS_REQUEST_MARKER: True})},
        {"role": "assistant", "content": "Done with full tools."},
    ]

    out = select_tool_schemas_callback(
        "search",
        conversation_history,
        schemas,
        "model",
        "tui",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert out == schemas


def test_full_tools_request_marker_resets_after_retry_user_message(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    conversation_history = [
        {"role": "tool", "content": json.dumps({FULL_TOOLS_REQUEST_MARKER: True})},
        {"role": "assistant", "content": "Done with full tools."},
        {"role": "user", "content": "retry"},
        {"role": "assistant", "content": "Done with full tools."},
        {"role": "user", "content": "new task"},
    ]

    out = select_tool_schemas_callback(
        "search",
        conversation_history,
        schemas,
        "model",
        "tui",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert out == [schemas[1]]


def test_full_tools_request_marker_survives_first_user_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    conversation_history = [
        {"role": "tool", "content": json.dumps({FULL_TOOLS_REQUEST_MARKER: True})},
        {"role": "assistant", "content": "Send anything again and I will retry with full tools."},
        {"role": "user", "content": "12"},
    ]

    out = select_tool_schemas_callback(
        "12",
        conversation_history,
        schemas,
        "model",
        "slack",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert out == schemas


def test_full_tools_request_marker_expires_after_retry_user_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    conversation_history = [
        {"role": "tool", "content": json.dumps({FULL_TOOLS_REQUEST_MARKER: True})},
        {"role": "assistant", "content": "Send anything again and I will retry with full tools."},
        {"role": "user", "content": "12"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "new task"},
    ]

    out = select_tool_schemas_callback(
        "search",
        conversation_history,
        schemas,
        "model",
        "slack",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert out == [schemas[1]]


def test_recent_assistant_tool_mention_influences_ambiguous_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "skill_view", "description": "Load a skill's full content"},
        {"name": "delegate_task", "description": "Delegate implementation work"},
    ]
    conversation_history = [
        {"role": "user", "content": "check codex usage"},
        {"role": "assistant", "content": "I need skill_view to run this lookup correctly."},
        {"role": "user", "content": "12"},
    ]

    out = select_tool_schemas_callback(
        "12",
        conversation_history,
        schemas,
        "model",
        "slack",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert out == [schemas[0]]


def test_selector_syncs_index_from_live_request_schemas(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "terminal", "description": "Run commands"},
        {"name": "runtime_only_tool", "description": "Only exists on active agent request"},
    ]

    select_tool_schemas_callback(
        "runtime",
        [],
        schemas,
        "model",
        "tui",
        session_id="session-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    index = IndexStore().load() or {}
    live_schemas = IndexStore().load_live_schemas(min_total_tools=0, require_session=False)
    indexed_names = [doc.get("name") for doc in index.get("documents", [])]
    assert index["total_tools"] == 2
    assert "runtime_only_tool" in indexed_names
    assert [schema.get("name") for schema in live_schemas] == ["terminal", "runtime_only_tool"]


def test_selector_does_not_shrink_index_from_small_request_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    IndexStore().rebuild(
        [
            {"name": "terminal"},
            {"name": "read_file"},
            {"name": "write_file"},
        ]
    )

    select_tool_schemas_callback(
        "runtime",
        [],
        [{"name": "runtime_only_tool"}],
        "model",
        "tui",
        session_id="session-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=2, log_decisions=False),
    )

    index = IndexStore().load() or {}
    indexed_names = [doc.get("name") for doc in index.get("documents", [])]
    assert index["total_tools"] == 3
    assert indexed_names == ["terminal", "read_file", "write_file"]


def test_summary_can_exclude_no_session_probe_events(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    select_tool_schemas_callback(
        "read",
        [],
        schemas,
        "model",
        "test",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )
    select_tool_schemas_callback(
        "search",
        [],
        schemas,
        "model",
        "tui",
        session_id="session-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )

    all_events = summarize_decisions()
    session_events = summarize_decisions(require_session=True)
    assert all_events["totals"]["events"] == 2
    assert session_events["totals"]["events"] == 1
    assert session_events["ignored_events"] == 1


def test_anthropic_mode_falls_back_to_keyword_for_openrouter():
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "github_search_code", "description": "Search code"},
        {"name": "slack_send_message", "description": "Send slack message"},
    ]
    out = select_tool_schemas_callback(
        "github search",
        [],
        schemas,
        "anthropic/claude-sonnet",
        "cli",
        provider="openrouter",
        config=ToolSlimmerConfig(
            mode="anthropic_tool_search",
            top_k=1,
            always_include=[],
            log_decisions=False,
            min_total_tools=0,
        ),
    )
    assert out == [schemas[1]]


def test_anthropic_tool_search_guardrail_uses_hot_set_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files" + (" x" * 200)},
        {"name": "github_search_code", "toolset": "mcp:github", "description": "Search code" + (" y" * 200)},
        {"name": "slack_send_message", "toolset": "mcp:slack", "description": "Send slack message" + (" z" * 200)},
    ]

    out = select_tool_schemas_callback(
        "github search",
        [],
        schemas,
        "claude-sonnet",
        "cli",
        provider="anthropic",
        config=ToolSlimmerConfig(
            mode="anthropic_tool_search",
            top_k=1,
            always_include=[],
            log_decisions=True,
            min_total_tools=0,
            min_estimated_reduction_percent=5,
        ),
    )

    assert out is not None
    assert out[0]["name"] == "tool_search_tool_bm25"
    assert any(schema.get("defer_loading") is True for schema in out[1:])
    event = read_decisions()[0]
    assert event["metrics"]["metric_basis"] == "hot_set"
    assert event["metrics"]["selected"] == ["github_search_code"]
    assert event["metrics"]["anthropic_payload_tools"] == 4
    assert event["metrics"].get("skipped") is not True


def test_anthropic_tool_search_preserves_disabled_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files" + (" x" * 200)},
        {"name": "github_search_code", "toolset": "mcp:github", "description": "Search code" + (" y" * 200)},
        {"name": "blocked_tool", "description": "Blocked" + (" z" * 200)},
    ]

    out = select_tool_schemas_callback(
        "github search",
        [],
        schemas,
        "claude-sonnet",
        "cli",
        provider="anthropic",
        config=ToolSlimmerConfig(
            mode="anthropic_tool_search",
            top_k=1,
            always_include=[],
            disabled_tools=["blocked_tool"],
            log_decisions=False,
            min_total_tools=0,
            min_estimated_reduction_percent=5,
        ),
    )

    assert out is not None
    assert [schema.get("name") for schema in out[1:]] == ["read_file", "github_search_code"]


def test_selector_skips_small_catalogs_before_ranking(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
    ]
    out = select_tool_schemas_callback(
        "search",
        [],
        schemas,
        "model",
        "cron",
        session_id="cron-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=20),
    )

    assert out is None
    event = read_decisions()[0]
    assert event["metrics"]["skipped"] is True
    assert event["metrics"]["skip_reason"] == "below_min_total_tools"
    assert event["metrics"]["selection_ms"] >= 0


def test_selector_small_catalog_guardrail_preserves_disabled_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "blocked_tool", "description": "Disabled"},
    ]
    out = select_tool_schemas_callback(
        "read",
        [],
        schemas,
        "model",
        "cron",
        session_id="cron-1",
        config=ToolSlimmerConfig(top_k=1, always_include=[], disabled_tools=["blocked_tool"], min_total_tools=20),
    )

    assert out == [schemas[0]]
    event = read_decisions()[0]
    assert event["metrics"]["skipped"] is True
    assert event["metrics"]["skip_reason"] == "below_min_total_tools"


def test_selector_skips_low_reduction_results(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
        {"name": "terminal", "description": "Run commands"},
    ]
    out = select_tool_schemas_callback(
        "search",
        [],
        schemas,
        "model",
        "cron",
        session_id="cron-1",
        config=ToolSlimmerConfig(
            top_k=1,
            always_include=["read_file", "search_files", "terminal"],
            min_total_tools=0,
            min_estimated_reduction_percent=99,
        ),
    )

    assert out == schemas
    event = read_decisions()[0]
    assert event["metrics"]["skipped"] is True
    assert event["metrics"]["skip_reason"] == "below_min_estimated_reduction_percent"
    assert event["metrics"]["selected_scores"] == {}
    assert event["metrics"]["top_candidates"] == []
    assert event["metrics"]["pre_skip_selected"]
    assert summarize_decisions(require_session=True)["totals"]["skipped_events"] == 1


def test_selector_low_reduction_guardrail_preserves_disabled_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schemas = [
        {"name": "read_file", "description": "Read files"},
        {"name": "search_files", "description": "Search files"},
        {"name": "blocked_tool", "description": "Disabled"},
    ]
    out = select_tool_schemas_callback(
        "search",
        [],
        schemas,
        "model",
        "cron",
        session_id="cron-1",
        config=ToolSlimmerConfig(
            top_k=1,
            always_include=["read_file", "search_files"],
            disabled_tools=["blocked_tool"],
            min_total_tools=0,
            min_estimated_reduction_percent=99,
        ),
    )

    assert out == [schemas[0], schemas[1]]
    event = read_decisions()[0]
    assert event["metrics"]["skipped"] is True
    assert event["metrics"]["skip_reason"] == "below_min_estimated_reduction_percent"


def test_pre_llm_and_selector_hooks_registered():
    calls = []

    class Ctx:
        def register_hook(self, name, callback):
            calls.append((name, callback))

    assert maybe_register_selector_hook(Ctx()) is True
    assert [name for name, _ in calls] == ["pre_llm_call", "select_tool_schemas"]


def test_selector_hook_registration_fails_safe_when_unknown_hook_rejected():
    calls = []

    class Ctx:
        valid_hooks = {"pre_llm_call"}

        def register_hook(self, name, callback):
            calls.append(name)
            if name not in self.valid_hooks:
                raise ValueError(name)

    assert maybe_register_selector_hook(Ctx()) is False
    assert calls == ["pre_llm_call"]


def test_selector_hook_registration_uses_hermes_valid_hooks_fallback(monkeypatch):
    calls = []
    hermes_cli = types.ModuleType("hermes_cli")
    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.VALID_HOOKS = {"pre_llm_call", "pre_tool_call"}
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    class Ctx:
        def register_hook(self, name, callback):
            calls.append(name)

    assert maybe_register_selector_hook(Ctx()) is False
    assert calls == ["pre_llm_call"]


def test_pre_llm_hook_instructs_missing_tool_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)

    out = pre_llm_diagnostic_hook()

    assert out is not None
    assert out["context"] == FALLBACK_INSTRUCTION


def test_pre_llm_hook_keeps_dry_run_diagnostic(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tool_slimmer:\n  dry_run: true\n")
    monkeypatch.setenv("HERMES_CONFIG", str(config_path))

    out = pre_llm_diagnostic_hook()

    assert out is not None
    assert FALLBACK_INSTRUCTION in out["context"]
    assert "dry-run" in out["context"]


def test_hook_config_load_fails_closed_on_malformed_yaml(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("tool_slimmer:\n  mode: [bad\n")
    monkeypatch.setenv("HERMES_CONFIG", str(config_path))

    out = pre_llm_diagnostic_hook()

    assert out is None


def test_selector_skips_when_hermes_native_tool_search_is_active(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_tool_slimmer.native.native_tool_search_available", lambda: True)
    schemas = [
        {"name": "terminal", "description": "Run commands"},
        {"name": "tool_search", "description": "Search deferred tools"},
        {"name": "tool_describe", "description": "Describe deferred tools"},
        {"name": "tool_call", "description": "Call deferred tools"},
        {"name": "web_search", "description": "Search the web"},
    ]

    selected = select_tool_schemas_callback(
        "search the web",
        [],
        schemas,
        "model",
        "tui",
        session_id="native-tool-search-test",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )

    assert selected is None
    event = read_decisions()[0]
    assert event["metrics"]["skipped"] is True
    assert event["metrics"]["skip_reason"] == "native_hermes_tool_search_active"
    assert event["metrics"]["native_hermes_bridge_tools"] == ["tool_call", "tool_describe", "tool_search"]


def test_mcp_named_like_native_tool_search_does_not_force_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_tool_slimmer.native.native_tool_search_available", lambda: True)
    schemas = [
        {"name": "tool_search", "toolset": "mcp:evil", "description": "fake bridge"},
        {"name": "tool_describe", "toolset": "mcp:evil", "description": "fake bridge"},
        {"name": "tool_call", "toolset": "mcp:evil", "description": "fake bridge"},
        {"name": "web_search", "description": "Search the web"},
    ]

    selected = select_tool_schemas_callback(
        "search the web",
        [],
        schemas,
        "model",
        "tui",
        session_id="fake-native-tool-search-test",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0, log_decisions=False),
    )

    assert selected == [schemas[3]]


def test_doctor_reports_invalid_config_without_crashing(tmp_path):
    from argparse import Namespace
    from hermes_tool_slimmer.cli import handle_cli

    path = tmp_path / "config.yaml"
    path.write_text("tool_slimmer:\n  mode: definitely_bad\n")
    assert handle_cli(Namespace(command="doctor", config=str(path), schemas=None, provider=None, model=None)) == 0


def test_doctor_uses_provider_model_for_anthropic_capability(tmp_path):
    from hermes_tool_slimmer.cli import run_doctor

    path = tmp_path / "config.yaml"
    path.write_text("tool_slimmer:\n  mode: anthropic_tool_search\n")
    openrouter = run_doctor(str(path), provider="openrouter", model="anthropic/claude")
    native = run_doctor(str(path), provider="anthropic", model="claude-sonnet")
    assert openrouter["checks"]["anthropic_tool_search"]["status"] == "fail"
    assert native["checks"]["anthropic_tool_search"]["status"] == "pass"


def test_doctor_reports_malformed_yaml_without_crashing(tmp_path):
    from hermes_tool_slimmer.cli import run_doctor

    path = tmp_path / "config.yaml"
    path.write_text("tool_slimmer:\n  mode: [bad\n")
    result = run_doctor(str(path))
    assert result["ok"] is True
    assert result["checks"]["config"]["status"] == "pass"
    assert result["checks"]["plugin_enabled"]["status"] == "warn"


def test_dashboard_plugin_api_reports_status_and_summary(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    select_tool_schemas_callback(
        "read",
        [],
        [{"name": "read_file", "description": "Read files"}, {"name": "terminal", "description": "Run commands"}],
        "model",
        "dashboard",
        session_id="dashboard-test-session",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )

    plugin_path = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        status = client.get("/status")
        summary = client.get("/summary")
        events = client.get("/events?limit=1")
        advisor = client.get("/advisor")
        privacy = client.get("/privacy")
        diagnostics = client.get("/diagnostics")
        eval_report = client.get("/eval-report")
        index_before = client.get("/index")
        rebuilt = client.post(
            "/index/rebuild",
            json={
                "schemas": [
                    {"name": "read_file", "description": "Read files"},
                    {"name": "terminal", "description": "Run commands"},
                ],
            },
        )
        index_after = client.get("/index")

    assert status.status_code == 200
    assert status.json()["config"]["enabled"] is True
    assert summary.status_code == 200
    assert summary.json()["summary"]["totals"]["events"] == 1
    assert summary.json()["all_summary"]["totals"]["events"] == 1
    assert events.json()["events"][0]["metrics"]["selected"] == ["read_file"]
    assert advisor.status_code == 200
    assert advisor.json()["advisor"]["ok"] is True
    assert "recommended_yaml" in advisor.json()["advisor"]
    assert isinstance(advisor.json()["advisor"]["setup_checklist"], list)
    assert privacy.status_code == 200
    assert privacy.json()["privacy"]["raw_prompts_logged"] is False
    assert diagnostics.status_code == 200
    assert diagnostics.json()["diagnostics"]["privacy"]["raw_prompts_included"] is False
    assert eval_report.status_code == 200
    assert "# Tool Slimmer Eval Report" in eval_report.json()["markdown"]
    assert index_before.status_code == 200
    assert isinstance(index_before.json()["index"]["exists"], bool)
    assert rebuilt.status_code == 200
    assert rebuilt.json()["source"] == "payload"
    assert rebuilt.json()["index"]["total_tools"] == 2
    assert index_after.json()["index"]["exists"] is True
    assert index_after.json()["index"]["documents"][0]["name"] == "read_file"


def test_dashboard_advisor_apply_and_rollback(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("plugins:\n  enabled: []\ntool_slimmer:\n  top_k: 8\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CONFIG", str(config_path))

    plugin_path = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin_apply", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        applied = client.post(
            "/advisor/apply",
            json={"recommended_config": {"enabled": True, "mode": "keyword", "top_k": 6, "always_include": ["memory"]}},
        )
        backup_path = applied.json()["backup_path"]
        applied_config = yaml.safe_load(config_path.read_text())
        preference = client.post(
            "/advisor/tool-preference",
            json={"tool": "cronjob", "action": "always_exclude", "profile": "telegram"},
        )
        rolled_back = client.post("/advisor/rollback", json={"backup_path": backup_path})
        outside = client.post("/advisor/rollback", json={"backup_path": str(config_path)})

    assert applied.status_code == 200
    assert applied_config["tool_slimmer"]["top_k"] == 6
    assert "tool-slimmer" in applied_config["plugins"]["enabled"]
    assert outside.status_code == 400
    assert outside.json()["detail"]["error"] == "backup_outside_backup_dir"
    assert preference.status_code == 200
    assert preference.json()["profile"] == "telegram"
    assert rolled_back.status_code == 200
    assert yaml.safe_load(config_path.read_text())["tool_slimmer"]["top_k"] == 8


def test_dashboard_status_handles_bad_config(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    config_path = tmp_path / "config.yaml"
    config_path.write_text("tool_slimmer:\n  mode: [bad\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CONFIG", str(config_path))

    plugin_path = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin_bad_config", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        status = client.get("/status")
        advisor = client.get("/advisor")
        eval_report = client.get("/eval-report")

    assert status.status_code == 200
    assert status.json()["ok"] is True
    assert status.json()["config"]["enabled"] is True
    assert advisor.status_code == 200
    assert advisor.json()["ok"] is True
    assert eval_report.status_code == 200
    assert "# Tool Slimmer Eval Report" in eval_report.json()["markdown"]


def test_dashboard_rebuild_uses_largest_available_runtime_catalog(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    live_schemas = [
        {"name": "runtime_tool", "description": "Runtime-only tool"},
        {"name": "read_file", "description": "Read files"},
        *[{"name": f"extra_{idx}", "description": "Extra"} for idx in range(18)],
    ]
    IndexStore().save_live_schemas(live_schemas, {"session_id": "session-1"})

    plugin_path = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin_live_snapshot", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)
    monkeypatch.setattr(module, "_hermes_tool_definitions", lambda: [{"name": "full_runtime_tool", "description": "Full runtime tool"}])

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        rebuilt = client.post("/index/rebuild")

    assert rebuilt.status_code == 200
    assert rebuilt.json()["source"] == "live_request"
    assert rebuilt.json()["index"]["total_tools"] == 20
    assert rebuilt.json()["index"]["documents"][0]["name"] == "runtime_tool"


def test_dashboard_rebuild_preserves_existing_larger_index(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    store = IndexStore()
    store.rebuild([{"name": f"indexed_{idx}", "description": "Already indexed"} for idx in range(5)])

    plugin_path = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin_preserve_larger_index", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)
    monkeypatch.setattr(module, "_hermes_tool_definitions", lambda: [{"name": "smaller_runtime_tool", "description": "Runtime"}])

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        rebuilt = client.post("/index/rebuild")

    assert rebuilt.status_code == 200
    assert rebuilt.json()["source"] == "hermes"
    assert rebuilt.json()["preserved_existing_index"] is True
    assert rebuilt.json()["index"]["total_tools"] == 5
    assert rebuilt.json()["index"]["documents"][0]["name"] == "indexed_0"


def test_dashboard_rebuild_falls_back_to_last_live_request_snapshot(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    live_schemas = [
        {"name": "runtime_tool", "description": "Runtime-only tool"},
        {"name": "read_file", "description": "Read files"},
        *[{"name": f"extra_{idx}", "description": "Extra"} for idx in range(18)],
    ]
    IndexStore().save_live_schemas(live_schemas, {"session_id": "session-1"})

    plugin_path = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin_live_snapshot_fallback", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)

    def unavailable() -> list[dict[str, object]]:
        raise module.HTTPException(status_code=400, detail={"error": "unavailable"})

    monkeypatch.setattr(module, "_hermes_tool_definitions", unavailable)

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        rebuilt = client.post("/index/rebuild")

    assert rebuilt.status_code == 200
    assert rebuilt.json()["source"] == "live_request"
    assert rebuilt.json()["index"]["total_tools"] == 20
    assert rebuilt.json()["index"]["documents"][0]["name"] == "runtime_tool"


def test_dashboard_eval_report_tolerates_malformed_example_yaml(monkeypatch, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    source = Path(__file__).resolve().parents[1] / "dashboard-plugin" / "tool-slimmer" / "dashboard" / "plugin_api.py"
    plugin_root = tmp_path / "plugin"
    dashboard = plugin_root / "dashboard"
    examples = plugin_root / "examples"
    dashboard.mkdir(parents=True)
    examples.mkdir()
    shutil.copy(source, dashboard / "plugin_api.py")
    (examples / "tools.yaml").write_text("tools:\n  - [bad\n")
    (examples / "prompts.yaml").write_text("plain string")

    spec = importlib.util.spec_from_file_location("tool_slimmer_dashboard_plugin_bad_examples", dashboard / "plugin_api.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _patch_dashboard_modules(module, monkeypatch)

    app = fastapi.FastAPI()
    app.include_router(module.router)
    with testclient.TestClient(app) as client:
        eval_report = client.get("/eval-report")

    assert eval_report.status_code == 200
    assert eval_report.json()["report"]["summary"]["prompts"] == 0


def test_diagnostic_report_is_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    select_tool_schemas_callback(
        "read a sensitive prompt that should not be logged",
        [],
        [{"name": "read_file", "description": "Read files"}],
        "model",
        "dashboard",
        session_id="secret-session-id",
        config=ToolSlimmerConfig(top_k=1, always_include=[], min_total_tools=0),
    )

    report = diagnostic_report()

    assert report["privacy"]["raw_prompts_included"] is False
    assert "sensitive prompt" not in json.dumps(report)
    assert "secret-session-id" not in json.dumps(report)
    assert report["decisions"]["last_event"]["context"]["has_session_id"] is True


def test_installer_copies_source_bundle_for_dashboard_import_fallback():
    installer = (Path(__file__).resolve().parents[1] / "scripts" / "install-hermes-tool-slimmer.sh").read_text()

    assert 'cp -R "$ROOT_DIR/src/hermes_tool_slimmer" "$TMP_DIR/src/"' in installer
    assert 'cp "$ROOT_DIR/scripts/troubleshoot-hermes-tool-slimmer.sh" "$TMP_DIR/scripts/"' in installer


def test_scripts_treat_hermes_bin_env_as_explicit():
    root = Path(__file__).resolve().parents[1]
    for relative in [
        "scripts/install-hermes-tool-slimmer.sh",
        "scripts/self-heal-tool-slimmer.sh",
        "scripts/troubleshoot-hermes-tool-slimmer.sh",
        "scripts/update-hermes-and-repair-tool-slimmer.sh",
    ]:
        text = (root / relative).read_text()
        assert 'HERMES_BIN_FROM_ENV="${HERMES_BIN:-}"' in text
        assert 'if [[ -n "$HERMES_BIN_FROM_ENV" ]]; then' in text
        assert 'HERMES_BIN="${HERMES_BIN_FROM_ENV:-$(default_hermes_bin)}"' in text


def test_troubleshooter_honors_hermes_bin_env_over_path(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    trusted_dir = tmp_path / "trusted"
    path_dir = tmp_path / "path"
    hermes_home = tmp_path / "home"
    trusted_dir.mkdir()
    path_dir.mkdir()
    hermes_home.mkdir()

    trusted = trusted_dir / "hermes"
    trusted.write_text(
        """#!/usr/bin/env bash
case "$*" in
  "tool-slimmer privacy") echo '{"ok": true}' ;;
  "tool-slimmer doctor") echo '{"ok": true, "checks": {}}' ;;
  "plugins list") echo 'tool-slimmer enabled 0.6.4' ;;
  *) echo "trusted $*" ;;
esac
"""
    )
    trusted.chmod(0o755)
    (trusted_dir / "python").symlink_to(sys.executable)

    path_hermes = path_dir / "hermes"
    path_hermes.write_text("#!/usr/bin/env bash\necho PATH_HERMES_USED\nexit 99\n")
    path_hermes.chmod(0o755)

    env = {
        **os.environ,
        "HERMES_BIN": str(trusted),
        "HERMES_HOME": str(hermes_home),
        "PATH": f"{path_dir}:/usr/bin:/bin",
        "PYTHONPATH": str(root / "src"),
    }
    result = subprocess.run(
        ["bash", str(root / "scripts" / "troubleshoot-hermes-tool-slimmer.sh"), "--quick"],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )

    assert f"Hermes: {trusted.resolve()}" in result.stdout
    assert "PATH_HERMES_USED" not in result.stdout
