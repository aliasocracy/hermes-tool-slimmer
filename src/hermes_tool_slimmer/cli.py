from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import yaml

from .advisor import apply_recommended_config, analyze_config as _advisor_analyze_config, current_advisor, dump_yaml, rollback_config
from .anthropic_tool_search import supports_anthropic_tool_search
from .config import ToolSlimmerConfig, config_path, load_config
from .corpus import tool_name
from .index_store import IndexStore
from .integration import SURFACE_HOOK, SURFACE_MIDDLEWARE, resolve_selector_surface
from .metrics import read_decisions, reduction_metrics, summarize_decisions
from .native import native_tool_search_status
from .selector import ToolSelector


def _load_schemas(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    target = Path(path).expanduser()
    if not target.is_file():
        return []
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if isinstance(data, dict):
        schemas = data.get("tools") or data.get("schemas")
        if isinstance(schemas, list):
            return schemas
        indexed_schemas = _schemas_from_index(data)
        if indexed_schemas:
            return indexed_schemas
        return []
    return data if isinstance(data, list) else []


def _load_cli_schemas(path: str | None) -> tuple[list[dict[str, Any]], str]:
    schemas = _load_schemas(path)
    if schemas:
        return schemas, "file"
    store = IndexStore()
    indexed_schemas = _schemas_from_index(store.load())
    if indexed_schemas:
        return indexed_schemas, "index"
    live_schemas = store.load_live_schemas(require_session=False)
    if live_schemas:
        return live_schemas, "live"
    return [], "none"


def _schemas_from_index(index: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(index, dict):
        return []
    documents = index.get("documents")
    if not isinstance(documents, list):
        return []
    out = []
    for doc in documents:
        if not isinstance(doc, dict) or not doc.get("name"):
            continue
        tokens = doc.get("tokens")
        token_text = " ".join(str(token) for token in tokens) if isinstance(tokens, list) else ""
        out.append(
            {
                "name": doc.get("name"),
                "toolset": doc.get("toolset"),
                "description": doc.get("text") or token_text,
            }
        )
    return out


def _load_prompts(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    target = Path(path).expanduser()
    if not target.is_file():
        return []
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if isinstance(data, dict):
        prompts = data.get("prompts")
        return prompts if isinstance(prompts, list) else []
    return data if isinstance(data, list) else []


def _tool_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {tool_name(schema) for schema in schemas}


def _check(status: str, message: str, detail: object | None = None) -> dict[str, object]:
    item: dict[str, object] = {"status": status, "message": message}
    if detail is not None:
        item["detail"] = detail
    return item


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def analyze_config(cfg: ToolSlimmerConfig, summary: dict[str, object] | None = None, indexed_tools: int = 0) -> dict[str, object]:
    return _advisor_analyze_config(cfg, summary, indexed_tools)


def privacy_inventory(cfg: ToolSlimmerConfig | None = None) -> dict[str, object]:
    cfg = cfg or load_config()
    return {
        "ok": True,
        "raw_prompts_logged": False,
        "decision_log_path": str(IndexStore().root / "decisions.jsonl"),
        "event_fields": ["timestamp", "metrics", "context"],
        "context_fields": ["provider", "model", "platform", "session_id", "dry_run", "schema_count"],
        "metric_fields": [
            "mode",
            "total_tools",
            "selected_tools",
            "schema_bytes_before",
            "schema_bytes_after",
            "schema_bytes_saved",
            "approx_tokens_before",
            "approx_tokens_after",
            "approx_tokens_saved",
            "estimated_reduction_percent",
            "always_included",
            "selected",
            "selection_ms",
            "skipped",
            "skip_reason",
            "selected_scores",
            "top_candidates",
            "expanded_query_token_count",
            "two_pass_catalog_tools",
            "two_pass_catalog_approx_tokens",
            "two_pass_hydrated_tools",
            "two_pass_requested_tools",
            "two_pass_phase",
            "native_hermes_tool_search",
            "native_hermes_bridge_tools",
            "jev_model",
            "jev_candidates",
            "jev_input_tokens",
            "jev_ms",
            "jev_cache_hit",
            "jev_fallback",
        ],
        "external_services": {
            "typesafe_jev": {
                "active": cfg.mode == "jev",
                "endpoint": cfg.jev.base_url,
                "sends": ["user request text for the turn", "candidate tool names", "candidate tool descriptions"],
            }
        },
        "notes": [
            "Raw user prompts are not written to decisions.jsonl.",
            "mode: jev sends each turn's user request and candidate tool descriptions to TypeSafe; other modes make no network calls.",
            "Dashboard headline totals exclude events without a session_id.",
            "Score details include tool names and numeric ranking components.",
        ],
    }


def diagnostic_report(limit: int = 200) -> dict[str, object]:
    """Return a sanitized support report safe to paste into a GitHub issue."""
    store = IndexStore()
    cfg_error: str | None = None
    try:
        cfg = load_config()
    except Exception as exc:
        cfg = ToolSlimmerConfig(enabled=False)
        cfg_error = str(exc)
    index = store.load() or {}
    live_schemas = store.load_live_schemas(require_session=False)
    status_schemas = live_schemas or _schemas_from_index(index)
    events = read_decisions(limit=limit)
    summary = summarize_decisions(limit=limit, require_session=True)
    summary.pop("recent", None)
    last_event = events[-1] if events else {}
    last_context = last_event.get("context") if isinstance(last_event, dict) else {}
    last_metrics = last_event.get("metrics") if isinstance(last_event, dict) else {}
    if not isinstance(last_context, dict):
        last_context = {}
    if not isinstance(last_metrics, dict):
        last_metrics = {}
    return {
        "ok": cfg_error is None,
        "report_version": 1,
        "privacy": {
            "raw_prompts_included": False,
            "env_values_included": False,
            "paths_include_home": True,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "package": _package_info(),
        "native_hermes_tool_search": native_tool_search_status(status_schemas),
        "config": {
            "error": cfg_error,
            "enabled": cfg.enabled,
            "mode": cfg.mode,
            "top_k": cfg.top_k,
            "min_total_tools": cfg.min_total_tools,
            "dry_run": cfg.dry_run,
            "fail_open": cfg.fail_open,
            "always_include": cfg.always_include,
            "always_exclude": cfg.disabled_tools,
            "profiles": sorted(cfg.profiles),
            "two_pass": cfg.two_pass.__dict__,
        },
        "doctor": run_doctor(),
        "index": {
            "path": str(store.path),
            "exists": bool(index),
            "total_tools": index.get("total_tools", 0),
            "checksum": str(index.get("checksum") or "")[:12],
            "live_snapshots": [_sanitize_snapshot(snapshot) for snapshot in store.live_schema_summaries()],
        },
        "decisions": {
            "summary": summary,
            "last_event": {
                "context": {
                    "provider": last_context.get("provider"),
                    "model": last_context.get("model"),
                    "platform": last_context.get("platform"),
                    "schema_count": last_context.get("schema_count"),
                    "dry_run": last_context.get("dry_run"),
                    "has_session_id": bool(last_context.get("session_id")),
                },
                "metrics": {
                    "mode": last_metrics.get("mode"),
                    "total_tools": last_metrics.get("total_tools"),
                    "selected_tools": last_metrics.get("selected_tools"),
                    "estimated_reduction_percent": last_metrics.get("estimated_reduction_percent"),
                    "skipped": last_metrics.get("skipped"),
                    "skip_reason": last_metrics.get("skip_reason"),
                    "selection_ms": last_metrics.get("selection_ms"),
                    "selected": last_metrics.get("selected"),
                    "two_pass_phase": last_metrics.get("two_pass_phase"),
                    "two_pass_fallback": last_metrics.get("two_pass_fallback"),
                },
            },
        },
    }


def _sanitize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": snapshot.get("label"),
        "platform": snapshot.get("platform"),
        "total_tools": snapshot.get("total_tools"),
        "schema_count": snapshot.get("schema_count"),
        "has_session_id": bool(snapshot.get("has_session_id")),
        "checksum": str(snapshot.get("checksum") or "")[:12],
        "updated_at": snapshot.get("updated_at"),
    }


def _package_info() -> dict[str, object]:
    try:
        import importlib.metadata as md
    except Exception:
        return {"version": "unknown", "module": None}
    spec = importlib.util.find_spec("hermes_tool_slimmer")
    try:
        version = md.version("hermes-tool-slimmer")
    except md.PackageNotFoundError:
        version = "unknown"
    return {"version": version, "module": spec.origin if spec else None}


def eval_prompts(cfg: ToolSlimmerConfig, schemas: list[dict[str, Any]], prompts: list[dict[str, Any]]) -> dict[str, object]:
    rows = []
    hits = 0
    fail_open_count = 0
    selector = ToolSelector(cfg)
    total_reduction = 0.0
    total_selected = 0
    for prompt in prompts:
        if not isinstance(prompt, dict):
            continue
        result = selector.select(str(prompt.get("text") or ""), schemas)
        metrics = reduction_metrics(cfg.mode, schemas, result.selected, result.always_included)
        expected = set(prompt.get("expected_any", []))
        hit = bool(expected & set(result.selected_names)) if expected else None
        if hit:
            hits += 1
        if result.fail_open:
            fail_open_count += 1
        total_selected += len(result.selected_names)
        total_reduction += _safe_float(metrics.get("estimated_reduction_percent"))
        rows.append({"name": prompt.get("name"), "selected": result.selected_names, "expected_included": hit, "reduction_percent": metrics["estimated_reduction_percent"], "fail_open": result.fail_open, "reason": result.reason})
    expected_rows = [row for row in rows if row["expected_included"] is not None]
    return {
        "summary": {
            "prompts": len(rows),
            "expected_prompts": len(expected_rows),
            "hit_rate": round(hits / len(expected_rows), 3) if expected_rows else None,
            "average_reduction_percent": round(total_reduction / len(rows), 1) if rows else 0.0,
            "average_selected_tools": round(total_selected / len(rows), 1) if rows else 0.0,
            "fail_open_count": fail_open_count,
        },
        "rows": rows,
    }


def eval_markdown(report: dict[str, object]) -> str:
    summary = report.get("summary") if isinstance(report, dict) else {}
    rows = report.get("rows") if isinstance(report, dict) else []
    summary = summary if isinstance(summary, dict) else {}
    rows = rows if isinstance(rows, list) else []
    lines = [
        "# Tool Slimmer Eval Report",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Prompts | {summary.get('prompts', 0)} |",
        f"| Expected prompts | {summary.get('expected_prompts', 0)} |",
        f"| Hit rate | {summary.get('hit_rate')} |",
        f"| Average selected tools | {summary.get('average_selected_tools', 0)} |",
        f"| Average reduction | {summary.get('average_reduction_percent', 0)}% |",
        f"| Fail-open count | {summary.get('fail_open_count', 0)} |",
        "",
        "| Prompt | Expected hit | Reduction | Fail-open | Selected |",
        "|---|---:|---:|---:|---|",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        selected = ", ".join(str(item) for item in row.get("selected", []))
        lines.append(f"| {row.get('name') or ''} | {row.get('expected_included')} | {row.get('reduction_percent')}% | {row.get('fail_open')} | {selected} |")
    lines.append("")
    return "\n".join(lines)


def run_doctor(
    config_arg: str | None = None,
    schemas_path: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    checks: dict[str, dict[str, object]] = {}
    cfg: ToolSlimmerConfig | None = None
    target = Path(config_arg).expanduser() if config_arg else config_path()
    try:
        if config_arg and not target.is_file():
            raise FileNotFoundError(str(target))
        cfg = load_config(config_arg)
        checks["config"] = _check(
            "pass",
            "tool_slimmer config is valid",
            {"mode": cfg.mode, "top_k": cfg.top_k, "two_pass": cfg.two_pass.__dict__},
        )
    except Exception as exc:
        checks["config"] = _check("fail", "tool_slimmer config is invalid", str(exc))
        cfg = ToolSlimmerConfig(enabled=False)

    checks["hermes_importable"] = _check(
        "pass" if importlib.util.find_spec("hermes_cli") else "warn",
        "Hermes Python modules are importable" if importlib.util.find_spec("hermes_cli") else "Hermes Python modules were not found in this environment",
    )

    enabled_detail: object = "config file not found"
    enabled_status = "warn"
    if target.is_file():
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            enabled = data.get("plugins", {}).get("enabled", []) if isinstance(data, dict) else []
            enabled_status = "pass" if "tool-slimmer" in enabled else "warn"
            enabled_detail = enabled
        except Exception as exc:
            enabled_status = "warn"
            enabled_detail = f"config unreadable: {exc}"
    checks["plugin_enabled"] = _check(
        enabled_status,
        "tool-slimmer is listed in plugins.enabled"
        if enabled_status == "pass"
        else "tool-slimmer is not listed in plugins.enabled",
        enabled_detail,
    )

    store = IndexStore()
    try:
        probe = store.root / ".doctor-write-test" if store.root else store.path.parent / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        index = store.load()
        checks["index_store"] = _check("pass", "index directory is readable/writable", {"path": str(store.path), "indexed_tools": (index or {}).get("total_tools", 0)})
    except Exception as exc:
        index = None
        checks["index_store"] = _check("fail", "index directory is not readable/writable", str(exc))

    schemas = _load_schemas(schemas_path)
    schema_source = "supplied schemas"
    if not schemas:
        schemas = _schemas_from_index(index)
        schema_source = "tool index"
    if schemas:
        names = _tool_names(schemas)
        missing = [name for name in cfg.always_include if name not in names]
        checks["always_include"] = _check(
            "pass" if not missing else "warn",
            f"always-included tools exist in {schema_source}" if not missing else f"some always-included tools are absent from {schema_source}",
            missing,
        )
    else:
        checks["always_include"] = _check("warn", "no schemas supplied; cannot validate always_include")

    native_schemas = store.load_live_schemas(require_session=False) or schemas
    native_status = native_tool_search_status(native_schemas)
    checks["native_hermes_tool_search"] = _check(
        "pass" if native_status["available"] or native_status["active"] else "warn",
        "Hermes native Tool Search is available; Tool Slimmer skips active slimming when bridge tools are already assembled"
        if native_status["available"] or native_status["active"]
        else "Hermes native Tool Search was not found in this Python environment",
        native_status,
    )

    if importlib.util.find_spec("hermes_cli") is None:
        checks["core_selector_hook"] = _check(
            "warn",
            "Hermes core not importable here; rerun the installer with the Hermes venv launcher",
        )
    else:
        surface = resolve_selector_surface()
        messages = {
            SURFACE_HOOK: "Hermes core calls select_tool_schemas; Tool Slimmer uses the core hook",
            SURFACE_MIDDLEWARE: "Hermes llm_request middleware is available; Tool Slimmer slims requests without a core patch",
            None: "Hermes exposes no selector surface; upgrade Hermes to v0.19+ or rerun scripts/install-hermes-tool-slimmer.sh",
        }
        checks["core_selector_hook"] = _check(
            "pass" if surface else "warn",
            messages[surface],
            {"surface": surface},
        )

    if cfg.mode == "anthropic_tool_search":
        supported = supports_anthropic_tool_search(
            provider, model, cfg.anthropic.tool_search_supported
        )
        if supported:
            checks["anthropic_tool_search"] = _check(
                "pass",
                "provider path supports Anthropic Tool Search",
                {"provider": provider, "model": model},
            )
        else:
            checks["anthropic_tool_search"] = _check(
                "fail",
                "anthropic_tool_search mode requires native Anthropic provider or explicit tool_search_supported for this provider path",
                {"provider": provider, "model": model},
            )
    else:
        checks["anthropic_tool_search"] = _check("pass", "Anthropic Tool Search mode is not active")
    checks["two_pass"] = _check(
        "pass",
        "Experimental two_pass mode is active" if cfg.mode == "two_pass" else "Experimental two_pass mode is not active",
        cfg.two_pass.__dict__,
    )
    if cfg.mode == "jev":
        has_key = bool(os.environ.get(cfg.jev.api_key_env, "").strip())
        checks["jev"] = _check(
            "pass" if has_key else "warn",
            f"jev mode will rank tools with TypeSafe {cfg.jev.model}"
            if has_key
            else f"jev mode is active but {cfg.jev.api_key_env} is not set; selection falls back to keyword",
            {"model": cfg.jev.model, "api_key_env": cfg.jev.api_key_env, "api_key_present": has_key, "threshold": cfg.jev.threshold},
        )
    return {"ok": all(v["status"] != "fail" for v in checks.values()), "checks": checks}


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Path to Hermes config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--schemas")
    doctor.add_argument("--provider")
    doctor.add_argument("--model")
    index = sub.add_parser("index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    rebuild = index_sub.add_parser("rebuild")
    rebuild.add_argument("--schemas", required=True)
    show = index_sub.add_parser("show")
    show.add_argument("--top", type=int, default=20)
    select = sub.add_parser("select")
    select.add_argument("query")
    select.add_argument("--schemas")
    bench = sub.add_parser("benchmark")
    bench.add_argument("--prompts", required=True)
    bench.add_argument("--schemas")
    eval_cmd = sub.add_parser("eval")
    eval_cmd.add_argument("--prompts", required=True)
    eval_cmd.add_argument("--schemas")
    eval_cmd.add_argument("--markdown", action="store_true")
    sub.add_parser("analyze-config")
    advisor = sub.add_parser("advisor")
    advisor.add_argument("--apply", action="store_true", help="Apply the recommended config with a timestamped backup")
    advisor.add_argument("--rollback", help="Restore a config backup created by advisor --apply")
    advisor.add_argument("--limit", type=int, default=1000)
    sub.add_parser("privacy")
    diagnostics = sub.add_parser("diagnostics")
    diagnostics.add_argument("--limit", type=int, default=200)
    sub.add_parser("recommend-config")


def handle_cli(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        print(
            json.dumps(
                run_doctor(
                    getattr(args, "config", None),
                    getattr(args, "schemas", None),
                    getattr(args, "provider", None),
                    getattr(args, "model", None),
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "privacy":
        print(json.dumps(privacy_inventory(), indent=2, sort_keys=True))
        return 0
    if args.command == "diagnostics":
        print(json.dumps(diagnostic_report(limit=getattr(args, "limit", 200)), indent=2, sort_keys=True))
        return 0

    cfg = load_config(getattr(args, "config", None))
    if args.command == "status":
        store = IndexStore()
        index = store.load() or {}
        live_schemas = store.load_live_schemas(require_session=False)
        live_snapshots = store.live_schema_summaries()
        latest_live = next((item for item in live_snapshots if item.get("label") == "latest"), None)
        print(
            json.dumps(
                {
                    "enabled": cfg.enabled,
                    "mode": cfg.mode,
                    "top_k": cfg.top_k,
                    "min_score": cfg.min_score,
                    "two_pass": cfg.two_pass.__dict__,
                    "native_hermes_tool_search": native_tool_search_status(live_schemas or _schemas_from_index(index)),
                    "index_path": str(store.path),
                    "total_tools_indexed": index.get("total_tools", 0),
                    "source_context": latest_live,
                    "live_snapshots": live_snapshots,
                    "core_integration": resolve_selector_surface(),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "index":
        store = IndexStore()
        if args.index_command == "rebuild":
            payload = store.rebuild(_load_schemas(args.schemas))
            print(json.dumps({"path": str(store.path), "checksum": payload["checksum"], "total_tools": payload["total_tools"]}, indent=2))
            return 0
        index = store.load() or {"documents": []}
        print(json.dumps(index.get("documents", [])[: args.top], indent=2))
        return 0
    if args.command == "select":
        schemas, schema_source = _load_cli_schemas(args.schemas)
        result = ToolSelector(cfg).select(args.query, schemas)
        print(
            json.dumps(
                {
                    "selected": result.selected_names,
                    "scores": result.scores,
                    "score_details": result.score_details,
                    "fail_open": result.fail_open,
                    "schema_count": len(schemas),
                    "schema_source": schema_source,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "benchmark":
        schemas = _load_schemas(args.schemas)
        prompts = _load_prompts(args.prompts)
        rows = []
        selector = ToolSelector(cfg)
        for prompt in prompts:
            if not isinstance(prompt, dict):
                continue
            result = selector.select(str(prompt.get("text") or ""), schemas)
            metrics = reduction_metrics(cfg.mode, schemas, result.selected, result.always_included)
            expected = set(prompt.get("expected_any", []))
            rows.append({"name": prompt.get("name"), "selected": result.selected_names, "expected_included": bool(expected & set(result.selected_names)) if expected else None, "metrics": metrics})
        print(json.dumps({"benchmarks": rows}, indent=2))
        return 0
    if args.command == "eval":
        schemas = _load_schemas(args.schemas)
        prompts = _load_prompts(args.prompts)
        report = eval_prompts(cfg, schemas, prompts)
        print(eval_markdown(report) if getattr(args, "markdown", False) else json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command == "analyze-config":
        from .metrics import summarize_decisions

        store = IndexStore()
        index = store.load() or {}
        print(json.dumps(analyze_config(cfg, summarize_decisions(require_session=True), int(index.get("total_tools") or 0)), indent=2, sort_keys=True))
        return 0
    if args.command == "advisor":
        if getattr(args, "rollback", None):
            print(json.dumps(rollback_config(args.rollback, path=getattr(args, "config", None)), indent=2, sort_keys=True))
            return 0
        report = current_advisor(limit=getattr(args, "limit", 1000))
        if getattr(args, "apply", False):
            recommended_raw = report.get("recommended_config")
            recommended = recommended_raw if isinstance(recommended_raw, dict) else None
            applied = apply_recommended_config(
                recommended,
                path=getattr(args, "config", None),
            )
            print(json.dumps({"advisor": report, "apply": applied}, indent=2, sort_keys=True))
            return 0
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command == "recommend-config":
        print(dump_yaml({"tool_slimmer": current_advisor().get("recommended_config", {})}))
        return 0
    raise ValueError(f"Unknown command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes tool-slimmer")
    setup_argparse(parser)
    return handle_cli(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
