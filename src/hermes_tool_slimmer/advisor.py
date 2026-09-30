from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from .config import ToolSlimmerConfig, config_path, hermes_home, load_config
from .corpus import tool_name
from .index_store import IndexStore
from .metrics import summarize_decisions
from .types import Schema

BASE_ALWAYS_INCLUDE = ["terminal", "read_file", "write_file", "patch", "search_files"]
TEXT_NOISE_TOOLS = ["terminal", "cronjob"]


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.dump(data, Dumper=_NoAliasDumper, sort_keys=False)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tool_names_from_index(index: dict[str, Any] | None) -> set[str]:
    documents = (index or {}).get("documents")
    if not isinstance(documents, list):
        return set()
    return {str(doc.get("name")) for doc in documents if isinstance(doc, dict) and doc.get("name")}


def _tool_names_from_schemas(schemas: list[Schema] | None) -> set[str]:
    return {tool_name(schema) for schema in schemas or [] if isinstance(schema, dict) and tool_name(schema)}


def _present(names: list[str], available: set[str]) -> list[str]:
    if not available:
        return names
    return [name for name in names if name in available]


def recommended_config(
    cfg: ToolSlimmerConfig,
    *,
    available_tools: set[str] | None = None,
) -> dict[str, Any]:
    tools = available_tools or set()
    base_always = _present(BASE_ALWAYS_INCLUDE, tools)
    if "tool_slimmer_request_full_tools" in tools:
        base_always.append("tool_slimmer_request_full_tools")
    base: dict[str, Any] = {
        "enabled": True,
        "mode": "keyword",
        "top_k": max(8, int(cfg.top_k or 8)),
        "always_include": base_always,
        "always_exclude": cfg.disabled_tools,
        "disabled_toolsets": cfg.disabled_toolsets,
        "never_defer": _present(["terminal", "read_file"], tools),
        "include_mcp_tools": cfg.include_mcp_tools,
        "include_native_tools": cfg.include_native_tools,
        "log_decisions": True,
        "min_total_tools": 0,
        "min_estimated_reduction_percent": 5.0,
        "min_score": 0.25,
        "fail_open": True,
        "dry_run": False,
        "aliases": {
            **cfg.aliases,
            "browse": ["browser", "navigate", "url", "website"],
            "deploy": ["publish", "release", "terminal"],
        },
        "profiles": {
            "telegram": {
                "top_k": 4,
                "always_include": _present(["memory", "tool_slimmer_request_full_tools"], tools),
                "always_exclude": _present(TEXT_NOISE_TOOLS, tools),
            },
            "slack": {
                "top_k": 6,
                "always_include": _present(["memory", "read_file", "search_files", "tool_slimmer_request_full_tools"], tools),
                "always_exclude": _present(["cronjob"], tools),
            },
            "cli": {
                "top_k": 8,
                "always_include": base_always,
            },
            "cron": {
                "top_k": 10,
                "always_include": _present(["terminal", "read_file", "search_files", "tool_slimmer_request_full_tools"], tools),
            },
            "webhook": {
                "top_k": 8,
                "always_include": _present(["tool_slimmer_request_full_tools"], tools),
            },
        },
    }
    return base


def analyze_config(
    cfg: ToolSlimmerConfig,
    summary: dict[str, object] | None = None,
    indexed_tools: int = 0,
    *,
    available_tools: set[str] | None = None,
    live_schemas: list[Schema] | None = None,
) -> dict[str, object]:
    totals = (summary or {}).get("totals") if isinstance(summary, dict) else {}
    averages = (summary or {}).get("averages") if isinstance(summary, dict) else {}
    platforms = (summary or {}).get("platforms") if isinstance(summary, dict) else {}
    top_selected = (summary or {}).get("top_selected_tools") if isinstance(summary, dict) else {}
    totals = totals if isinstance(totals, dict) else {}
    averages = averages if isinstance(averages, dict) else {}
    platforms = platforms if isinstance(platforms, dict) else {}
    top_selected = top_selected if isinstance(top_selected, dict) else {}
    events = _safe_int(totals.get("events"))
    skipped = _safe_int(totals.get("skipped_events"))
    tools = available_tools or set()
    recommendation = recommended_config(cfg, available_tools=tools)

    recommendations: list[dict[str, object]] = []
    checklist: list[dict[str, object]] = []
    if events == 0:
        recommendations.append(
            {
                "id": "collect_data",
                "severity": "info",
                "message": "No real selector events are available yet. Leave decision logging on and run a few normal requests before tuning aggressively.",
            }
        )
    if len(cfg.always_include) > max(1, cfg.top_k):
        recommendations.append(
            {
                "id": "review_always_include",
                "severity": "warn",
                "message": "always_include is larger than top_k. Keep only tools that should be present on nearly every turn.",
                "tools": cfg.always_include,
            }
        )
    if cfg.top_k < 6:
        recommendations.append(
            {
                "id": "low_top_k",
                "severity": "warn",
                "message": "top_k below 6 saves more schema tokens but increases tool-miss risk. Use a platform profile instead of making every entry point this narrow.",
            }
        )
    if events and skipped / events > 0.5:
        recommendations.append(
            {
                "id": "review_guardrails",
                "severity": "warn",
                "message": "More than half of recent selections were skipped by guardrails. Review min_total_tools and min_estimated_reduction_percent.",
            }
        )
    if cfg.mode == "keyword" and not cfg.aliases:
        recommendations.append(
            {
                "id": "add_aliases",
                "severity": "info",
                "message": "Keyword mode is deterministic. Add aliases for common wording that differs from tool names.",
            }
        )
    if indexed_tools == 0:
        recommendations.append(
            {
                "id": "rebuild_index",
                "severity": "info",
                "message": "The persisted tool index is empty. Use the dashboard Rebuild button after installing or removing toolsets.",
            }
        )
    if not cfg.profiles:
        recommendations.append(
            {
                "id": "add_profiles",
                "severity": "info",
                "message": "Add platform profiles so Telegram, Slack, CLI, cron, and webhook traffic can use different tool budgets.",
            }
        )
    telegram_profile = cfg.profiles.get("telegram") if isinstance(cfg.profiles, dict) else {}
    telegram_excluded = []
    if isinstance(telegram_profile, dict):
        excluded_raw = telegram_profile.get("always_exclude") or telegram_profile.get("disabled_tools")
        if isinstance(excluded_raw, list):
            telegram_excluded = [str(item) for item in excluded_raw]
    cronjob_excluded_for_text = "cronjob" in cfg.disabled_tools or "cronjob" in telegram_excluded
    if top_selected.get("cronjob") and "telegram" in platforms and not cronjob_excluded_for_text:
        recommendations.append(
            {
                "id": "cronjob_profile_review",
                "severity": "warn",
                "message": "cronjob appears in Telegram selections. If Telegram is mostly chat, add cronjob to the Telegram profile's always_exclude list.",
            }
        )

    checklist.append({"id": "config_valid", "label": "Config loads", "status": "pass", "message": "Tool Slimmer config can be read."})
    checklist.append(
        {
            "id": "index_ready",
            "label": "Tool index",
            "status": "pass" if indexed_tools else "warn",
            "message": f"{indexed_tools} tools indexed." if indexed_tools else "No persisted tool index yet.",
        }
    )
    checklist.append(
        {
            "id": "fallback_tool",
            "label": "Full-tool fallback",
            "status": "pass" if not tools or "tool_slimmer_request_full_tools" in tools else "warn",
            "message": "Fallback tool is available." if not tools or "tool_slimmer_request_full_tools" in tools else "Fallback tool was not found in the current index.",
        }
    )
    checklist.append(
        {
            "id": "profiles",
            "label": "Entry-point profiles",
            "status": "pass" if cfg.profiles else "warn",
            "message": "Profiles are configured." if cfg.profiles else "Profiles are not configured yet.",
        }
    )
    if live_schemas is not None:
        store = IndexStore()
        index = store.load() or {}
        live_checksum = store.checksum(live_schemas)
        checklist.append(
            {
                "id": "catalog_current",
                "label": "Catalog current",
                "status": "pass" if index.get("checksum") == live_checksum else "warn",
                "message": "Index matches live schemas." if index.get("checksum") == live_checksum else "Live tools changed; rebuild the index.",
            }
        )
    setup_status = "active" if all(item.get("status") == "pass" for item in checklist) else "needs_setup"

    return {
        "ok": True,
        "status": setup_status,
        "summary": "Tool Slimmer is safest when it uses the live Hermes schemas, keeps the full-tool fallback available, and applies narrower budgets only to entry points that need them.",
        "config": {
            "mode": cfg.mode,
            "top_k": cfg.top_k,
            "always_include": cfg.always_include,
            "disabled_tools": cfg.disabled_tools,
            "disabled_toolsets": cfg.disabled_toolsets,
            "profiles": cfg.profiles,
            "min_total_tools": cfg.min_total_tools,
            "min_estimated_reduction_percent": cfg.min_estimated_reduction_percent,
            "min_score": cfg.min_score,
            "aliases": cfg.aliases,
        },
        "observed": {
            "events": events,
            "skipped_events": skipped,
            "average_reduction_percent": averages.get("reduction_percent", 0),
            "indexed_tools": indexed_tools,
            "platforms": platforms,
        },
        "recommended_config": recommendation,
        "recommended_yaml": dump_yaml({"tool_slimmer": recommendation}),
        "setup_checklist": checklist,
        "recommendations": recommendations,
    }


def current_advisor(limit: int = 1000, live_schemas: list[Schema] | None = None) -> dict[str, object]:
    cfg = load_config()
    store = IndexStore()
    index = store.load() or {}
    tools = _tool_names_from_schemas(live_schemas) or _tool_names_from_index(index)
    return analyze_config(
        cfg,
        summarize_decisions(limit=limit, require_session=True),
        _safe_int(index.get("total_tools")),
        available_tools=tools,
        live_schemas=live_schemas,
    )


def run_advisor(limit: int = 1000, live_schemas: list[Schema] | None = None) -> dict[str, object]:
    """Compatibility wrapper for scripts that look for an imperative advisor API."""
    return current_advisor(limit=limit, live_schemas=live_schemas)


def _read_hermes_config(target: Path) -> tuple[dict[str, Any], dict[str, object] | None]:
    """Load the whole Hermes config for a rewrite, refusing anything unparseable.

    Rewriting an unreadable config from an empty mapping would silently drop
    every non-Tool-Slimmer Hermes setting.
    """
    if not target.is_file():
        return {}, None
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return {}, {"ok": False, "error": "config_unreadable", "path": str(target), "message": str(exc)}
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, {"ok": False, "error": "config_not_mapping", "path": str(target)}
    return data, None


def backup_dir() -> Path:
    path = hermes_home() / "tool-slimmer" / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_config(path: str | Path | None = None) -> Path:
    target = Path(path).expanduser() if path else config_path()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Several edits can land in the same second; never overwrite an earlier backup.
    backup = backup_dir() / f"config-{stamp}.yaml"
    suffix = 1
    while backup.exists():
        backup = backup_dir() / f"config-{stamp}-{suffix}.yaml"
        suffix += 1
    if target.is_file():
        shutil.copy2(target, backup)
    else:
        backup.write_text("# No config file existed before Tool Slimmer advisor apply.\n", encoding="utf-8")
    return backup


def apply_recommended_config(
    recommended: dict[str, Any] | None = None,
    *,
    path: str | Path | None = None,
) -> dict[str, object]:
    target = Path(path).expanduser() if path else config_path()
    data, error = _read_hermes_config(target)
    if error:
        return error
    cfg = load_config(target) if target.is_file() else ToolSlimmerConfig()
    store = IndexStore()
    index = store.load() or {}
    payload = recommended or recommended_config(cfg, available_tools=_tool_names_from_index(index))
    backup = backup_config(target)

    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
    if "tool-slimmer" not in enabled:
        enabled.append("tool-slimmer")
    plugins["enabled"] = enabled
    data["plugins"] = plugins
    data["tool_slimmer"] = payload

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_yaml(data), encoding="utf-8")
    return {"ok": True, "path": str(target), "backup_path": str(backup), "applied": payload}


def apply_tool_preference(
    tool: str,
    action: str,
    *,
    profile: str = "default",
    path: str | Path | None = None,
) -> dict[str, object]:
    if action not in {"always_include", "always_exclude"}:
        return {"ok": False, "error": "invalid_action", "action": action}
    target = Path(path).expanduser() if path else config_path()
    data, error = _read_hermes_config(target)
    if error:
        return error
    section = data.get("tool_slimmer")
    if not isinstance(section, dict):
        section = {}
    profiles = section.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    destination = section if profile in {"", "default"} else profiles.setdefault(profile, {})
    if not isinstance(destination, dict):
        destination = {}
        profiles[profile] = destination
    backup = backup_config(target)
    field = "always_include" if action == "always_include" else "always_exclude"
    values = destination.get(field)
    if not isinstance(values, list):
        values = []
    if tool not in values:
        values.append(tool)
    destination[field] = values
    section["profiles"] = profiles
    data["tool_slimmer"] = section
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_yaml(data), encoding="utf-8")
    return {"ok": True, "path": str(target), "backup_path": str(backup), "profile": profile, "action": action, "tool": tool}


def rollback_config(backup_path: str | Path, *, path: str | Path | None = None) -> dict[str, object]:
    backup = Path(backup_path).expanduser()
    # Only restore files this advisor created; the dashboard forwards this path
    # from an HTTP body, so an arbitrary path must not overwrite the config.
    try:
        backup.resolve().relative_to(backup_dir().resolve())
    except ValueError:
        return {"ok": False, "error": "backup_outside_backup_dir", "backup_path": str(backup)}
    if not backup.is_file():
        return {"ok": False, "error": "backup_not_found", "backup_path": str(backup)}
    target = Path(path).expanduser() if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    return {"ok": True, "path": str(target), "backup_path": str(backup)}
