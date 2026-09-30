from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from time import perf_counter
from typing import Any

from .anthropic_tool_search import maybe_anthropic_tools
from .config import ToolSlimmerConfig, load_config
from .corpus import tool_name
from .index_store import IndexStore
from .metrics import record_decision, reduction_metrics
from .native import native_tool_search_active, native_tool_search_bridge_names
from .policy import eligible_schemas
from .selector import TOOL_MENTIONS_MARKER, ToolSelector
from .tools import FULL_TOOLS_REQUEST_MARKER
from .two_pass import (
    HYDRATE_TOOL_NAME,
    compact_catalog,
    compact_catalog_metrics,
    hydrate_tool_schema,
    requested_hydration_tools,
)
from .types import Schema, SelectionResult

LOG = logging.getLogger(__name__)

FALLBACK_INSTRUCTION = (
    "Tool Slimmer may hide tools. If a skill or task requires a missing tool, "
    "call tool_slimmer_request_full_tools; do not invent replacement tools."
)

_TOOL_NAME_RE = re.compile(r"\b[a-z][a-z0-9_]{2,}\b")
# Long-lived gateways see many sessions; keep only the most recent hydration sets.
_HYDRATED_SESSION_LIMIT = 256
_HYDRATED_BY_SESSION: OrderedDict[str, set[str]] = OrderedDict()
_LAST_LIVE_SNAPSHOT_KEY: tuple[object, ...] | None = None
_TURN_USER_MESSAGES: OrderedDict[str, str] = OrderedDict()


def _session_hydration_cache(session_id: str) -> set[str]:
    cached = _HYDRATED_BY_SESSION.pop(session_id, None)
    if cached is None:
        cached = set()
    _HYDRATED_BY_SESSION[session_id] = cached
    while len(_HYDRATED_BY_SESSION) > _HYDRATED_SESSION_LIMIT:
        _HYDRATED_BY_SESSION.popitem(last=False)
    return cached


def _log_decision(cfg: ToolSlimmerConfig, message: str, metrics: dict[str, object], context: dict[str, object]) -> None:
    if not cfg.log_decisions:
        return
    LOG.info(message, extra={"tool_slimmer": metrics})
    try:
        record_decision(metrics, context)
    except Exception as exc:
        LOG.warning("tool-slimmer decision logging failed: %s", exc)


def _load_config_for_hook() -> ToolSlimmerConfig:
    try:
        return load_config(strict=True)
    except Exception as exc:
        LOG.warning("tool-slimmer config load failed; disabling selector for this request: %s", exc)
        return ToolSlimmerConfig(enabled=False)


def _sync_live_index(schemas: list[Schema], min_total_tools: int, context: dict[str, Any] | None = None) -> None:
    if len(schemas) < min_total_tools:
        return
    global _LAST_LIVE_SNAPSHOT_KEY
    try:
        store = IndexStore()
        if context and context.get("session_id"):
            # Every model call in a tool loop sends the same catalog; only
            # rewrite the snapshot when the catalog or request context changes.
            snapshot_key = (str(store.root), IndexStore.checksum(schemas), *sorted((str(k), str(v)) for k, v in context.items()))
            if snapshot_key != _LAST_LIVE_SNAPSHOT_KEY:
                store.save_live_schemas(schemas, context)
                _LAST_LIVE_SNAPSHOT_KEY = snapshot_key
        current = store.load() or {}
        current_total = current.get("total_tools")
        if isinstance(current_total, int) and current_total > len(schemas):
            return
        store.ensure(schemas)
    except Exception as exc:
        LOG.warning("tool-slimmer live index sync failed: %s", exc)


def _metrics_for_selection(
    mode: str,
    schemas: list[Schema],
    selected: list[Schema],
    hot_selected: list[Schema],
    always_included: list[str],
) -> dict[str, object]:
    metrics_selected = selected
    if mode == "anthropic_tool_search" and selected is not hot_selected:
        metrics_selected = hot_selected
    metrics = reduction_metrics(mode, schemas, metrics_selected, always_included)
    if mode == "anthropic_tool_search" and selected is not hot_selected:
        metrics["metric_basis"] = "hot_set"
        metrics["anthropic_payload_tools"] = len(selected)
        metrics["anthropic_deferred_tools"] = sum(
            1 for schema in selected if isinstance(schema, dict) and schema.get("defer_loading") is True
        )
    return metrics


def _schema_by_name(schemas: list[Schema]) -> dict[str, Schema]:
    out: dict[str, Schema] = {}
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        name = tool_name(schema)
        if name and name not in out:
            out[name] = schema
    return out


def _always_include_schemas(schemas_by_name: dict[str, Schema], cfg: ToolSlimmerConfig) -> tuple[list[Schema], list[str]]:
    selected: list[Schema] = []
    names: list[str] = []
    for name in [*cfg.always_include, "tool_slimmer_request_full_tools", HYDRATE_TOOL_NAME]:
        if name in schemas_by_name and name not in names:
            selected.append(schemas_by_name[name])
            names.append(name)
    return selected, names


def _two_pass_selected_schemas(
    schemas: list[Schema],
    cfg: ToolSlimmerConfig,
    conversation_history: list[Any] | None,
    session_id: str | None,
) -> tuple[list[Schema], dict[str, object], str | None]:
    schemas = eligible_schemas(schemas, cfg)
    schemas_by_name = _schema_by_name(schemas)
    catalog = compact_catalog(schemas, cfg)
    catalog_names = {item.name for item in catalog}
    hydrate_schema = schemas_by_name.get(HYDRATE_TOOL_NAME)
    if hydrate_schema is None:
        if cfg.two_pass.fallback_to_keyword:
            return [], compact_catalog_metrics(catalog, schemas), "missing_hydrate_tool"
        return schemas, compact_catalog_metrics(catalog, schemas), "missing_hydrate_tool_fail_open"

    selected, always_names = _always_include_schemas(schemas_by_name, cfg)
    selected_names = set(always_names)

    requested = [name for name in requested_hydration_tools(conversation_history) if name in schemas_by_name]
    requested = requested[: cfg.two_pass.hydrate_limit]
    cache_key = session_id or ""
    cached = _session_hydration_cache(cache_key) if cache_key else set()
    if requested and cfg.two_pass.cache_hydrated_tools and cache_key:
        cached.update(requested)
    hydrated = sorted((cached if cfg.two_pass.cache_hydrated_tools else set()) | set(requested))
    hydrated = [name for name in hydrated if name in schemas_by_name and name in catalog_names]
    hydrated = hydrated[: cfg.two_pass.hydrate_limit]

    for name in hydrated:
        if name not in selected_names:
            selected.append(schemas_by_name[name])
            selected_names.add(name)

    dynamic_hydrate = hydrate_tool_schema(hydrate_schema, catalog)
    selected = [dynamic_hydrate if _tool_schema_name(schema) == HYDRATE_TOOL_NAME else schema for schema in selected]
    metadata = compact_catalog_metrics(catalog, schemas)
    metadata.update(
        {
            "two_pass_requested_tools": requested,
            "two_pass_hydrated_tools": hydrated,
            "two_pass_cached_tools": sorted(cached) if cfg.two_pass.cache_hydrated_tools else [],
            "two_pass_cache_scope": "session" if cache_key and cfg.two_pass.cache_hydrated_tools else "request",
            "two_pass_phase": "hydrate" if hydrated else "catalog",
        }
    )
    return selected, metadata, None


def _tool_schema_name(schema: Schema) -> str:
    function = schema.get("function") if isinstance(schema, dict) else {}
    return str(schema.get("name") or (function.get("name") if isinstance(function, dict) else "") or "")


def _contains_full_tools_request(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("role") != "tool":
        return False
    content = value.get("content")
    if isinstance(content, dict):
        return content.get(FULL_TOOLS_REQUEST_MARKER) is True
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get(FULL_TOOLS_REQUEST_MARKER) is True
    return False


def _full_tools_requested(conversation_history: list[Any] | None) -> bool:
    history = conversation_history or []
    last_marker_index: int | None = None
    for index, item in enumerate(history):
        if _contains_full_tools_request(item):
            last_marker_index = index
    if last_marker_index is None:
        return False

    # Keep full tools through the current tool-call chain and the first user
    # retry after the marker. Some models call the fallback, then answer with
    # "send anything again" instead of immediately retrying the task. Expiring
    # at the first user message recreates the same missing-tool loop.
    user_messages_after_marker = sum(
        1
        for item in history[last_marker_index + 1 :]
        if isinstance(item, dict) and item.get("role") == "user"
    )
    if user_messages_after_marker <= 1:
        return True
    return False


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text_content(item) for item in value.values())
    if isinstance(value, list | tuple):
        return " ".join(_text_content(item) for item in value)
    return ""


def _recent_tool_mentions(conversation_history: list[Any] | None, schemas: list[Schema]) -> list[str]:
    known_names = {
        _tool_schema_name(schema)
        for schema in schemas
        if isinstance(schema, dict)
    }
    known_names.discard("")
    if not known_names:
        return []

    history = conversation_history or []
    # conversation_history already includes the current user message in Hermes.
    # Look at assistant/tool context immediately before that current user turn.
    index = len(history) - 1
    if index >= 0 and isinstance(history[index], dict) and history[index].get("role") == "user":
        index -= 1

    mentioned: list[str] = []
    seen: set[str] = set()
    while index >= 0:
        item = history[index]
        if isinstance(item, dict) and item.get("role") == "user":
            break
        text = _text_content(item)
        for token in _TOOL_NAME_RE.findall(text):
            if token in known_names and token not in seen:
                mentioned.append(token)
                seen.add(token)
        index -= 1
    return mentioned


def _selection_query(user_message: str, conversation_history: list[Any] | None, schemas: list[Schema]) -> str:
    mentions = _recent_tool_mentions(conversation_history, schemas)
    if not mentions:
        return user_message
    return f"{user_message}{TOOL_MENTIONS_MARKER}{' '.join(mentions)}"


def select_tool_schemas_callback(
    user_message: str,
    conversation_history: list[Any] | None,
    schemas: list[Schema],
    model: str,
    platform: str,
    provider: str | None = None,
    session_id: str | None = None,
    config: ToolSlimmerConfig | None = None,
    **kwargs: Any,
) -> list[Schema] | None:
    cfg = config or _load_config_for_hook()
    cfg = cfg.for_context(platform=platform)
    if not cfg.enabled:
        return None
    decision_context: dict[str, object] = {
        "provider": provider,
        "model": model,
        "platform": platform,
        "session_id": session_id,
        "dry_run": cfg.dry_run,
        "schema_count": len(schemas),
    }
    try:
        started = perf_counter()
        _sync_live_index(
            schemas,
            cfg.min_total_tools,
            {key: value for key, value in decision_context.items() if key != "dry_run"},
        )
        if native_tool_search_active(schemas):
            bridge_tools = native_tool_search_bridge_names(schemas)
            metrics = reduction_metrics(cfg.mode, schemas, schemas, [])
            metrics["selection_ms"] = round((perf_counter() - started) * 1000, 3)
            metrics["skipped"] = True
            metrics["skip_reason"] = "native_hermes_tool_search_active"
            metrics["native_hermes_tool_search"] = True
            metrics["native_hermes_bridge_tools"] = bridge_tools
            _log_decision(cfg, "tool-slimmer skipped; Hermes native Tool Search is active", metrics, decision_context)
            return None
        policy_schemas = eligible_schemas(schemas, cfg)
        if _full_tools_requested(conversation_history):
            metrics = reduction_metrics(cfg.mode, schemas, policy_schemas, [])
            metrics["selection_ms"] = round((perf_counter() - started) * 1000, 3)
            metrics["skipped"] = True
            metrics["skip_reason"] = "full_tools_requested"
            _log_decision(cfg, "tool-slimmer full schema fallback", metrics, decision_context)
            return None if cfg.dry_run else policy_schemas
        if len(schemas) < cfg.min_total_tools:
            metrics = reduction_metrics(cfg.mode, schemas, policy_schemas, [])
            metrics["selection_ms"] = round((perf_counter() - started) * 1000, 3)
            metrics["skipped"] = True
            metrics["skip_reason"] = "below_min_total_tools"
            metrics["min_total_tools"] = cfg.min_total_tools
            _log_decision(cfg, "tool-slimmer skipped", metrics, decision_context)
            if cfg.dry_run or len(policy_schemas) == len(schemas):
                return None
            return policy_schemas

        effective_cfg = cfg
        query = _selection_query(user_message, conversation_history, policy_schemas)
        if cfg.mode == "two_pass":
            selected, two_pass_metrics, two_pass_fallback = _two_pass_selected_schemas(
                policy_schemas,
                cfg,
                conversation_history,
                session_id,
            )
            if two_pass_fallback == "missing_hydrate_tool" and cfg.two_pass.fallback_to_keyword:
                fallback_cfg = cfg.with_mode("keyword")
                result = ToolSelector(fallback_cfg).select(
                    query,
                    policy_schemas,
                    conversation_history=conversation_history,
                    model=model,
                    platform=platform,
                    provider=provider,
                    session_id=session_id,
                    **kwargs,
                )
                selected = result.selected
                effective_cfg = fallback_cfg
                two_pass_metrics["two_pass_fallback"] = two_pass_fallback
            else:
                selected_names = [_tool_schema_name(schema) for schema in selected]
                selected_name_set = set(selected_names)
                result = SelectionResult(
                    mode=cfg.mode,
                    selected=selected,
                    selected_names=selected_names,
                    scores={},
                    total_tools=len(schemas),
                    always_included=[
                        name for name in [*cfg.always_include, "tool_slimmer_request_full_tools", HYDRATE_TOOL_NAME]
                        if name in selected_name_set
                    ],
                    reason=two_pass_fallback,
                    metadata=two_pass_metrics,
                )
        else:
            result = ToolSelector(effective_cfg).select(
                query,
                policy_schemas,
                conversation_history=conversation_history,
                model=model,
                platform=platform,
                provider=provider,
                session_id=session_id,
                **kwargs,
            )
            selected = maybe_anthropic_tools(
                provider,
                model,
                policy_schemas if cfg.mode == "anthropic_tool_search" else result.selected,
                result.selected_names,
                effective_cfg,
                explicit_capability=cfg.anthropic.tool_search_supported,
            )
        if cfg.mode == "anthropic_tool_search" and selected is policy_schemas:
            # Unsupported provider path: fall back to deterministic keyword selection,
            # not the full catalog, unless the user explicitly chose eager mode.
            fallback_cfg = cfg.with_mode("keyword")
            result = ToolSelector(fallback_cfg).select(
                query,
                policy_schemas,
                conversation_history=conversation_history,
                model=model,
                platform=platform,
                provider=provider,
                session_id=session_id,
                **kwargs,
            )
            selected = result.selected
            effective_cfg = fallback_cfg
        metrics = _metrics_for_selection(effective_cfg.mode, schemas, selected, result.selected, result.always_included)
        if result.metadata:
            metrics.update(result.metadata)
        metrics["selection_ms"] = round((perf_counter() - started) * 1000, 3)
        metrics["selected_scores"] = {name: result.score_details.get(name, {}) for name in result.selected_names}
        metrics["top_candidates"] = [
            {"name": name, "score": score, "details": result.score_details.get(name, {})}
            for name, score in sorted(result.scores.items(), key=lambda item: item[1], reverse=True)[:10]
        ]
        metrics["expanded_query_token_count"] = len(result.expanded_query_tokens)
        raw_reduction = metrics["estimated_reduction_percent"]
        reduction_percent = raw_reduction if isinstance(raw_reduction, (int, float)) else 0.0
        if effective_cfg.mode != "two_pass" and reduction_percent < cfg.min_estimated_reduction_percent:
            selected = policy_schemas
            metrics = reduction_metrics(effective_cfg.mode, schemas, selected, result.always_included)
            metrics["selection_ms"] = round((perf_counter() - started) * 1000, 3)
            metrics["selected_scores"] = {}
            metrics["top_candidates"] = []
            metrics["pre_skip_selected"] = result.selected_names
            metrics["expanded_query_token_count"] = len(result.expanded_query_tokens)
            metrics["skipped"] = True
            metrics["skip_reason"] = "below_min_estimated_reduction_percent"
            metrics["min_estimated_reduction_percent"] = cfg.min_estimated_reduction_percent
        _log_decision(cfg, "tool-slimmer selection", metrics, decision_context)
        if cfg.dry_run:
            return None
        return selected
    except Exception:
        LOG.exception("tool-slimmer selector failed; using original schemas")
        if cfg.fail_open:
            return None
        raise


def _remember_turn_user_message(session_id: Any, user_message: Any) -> None:
    if not session_id or not isinstance(user_message, str):
        return
    key = str(session_id)
    _TURN_USER_MESSAGES.pop(key, None)
    _TURN_USER_MESSAGES[key] = user_message
    while len(_TURN_USER_MESSAGES) > _HYDRATED_SESSION_LIMIT:
        _TURN_USER_MESSAGES.popitem(last=False)


def turn_user_message(session_id: Any) -> str | None:
    """The clean user message Hermes passed to ``pre_llm_call`` for this session's turn."""
    return _TURN_USER_MESSAGES.get(str(session_id)) if session_id else None


def pre_llm_diagnostic_hook(**kwargs: Any) -> dict[str, str] | None:
    # Hermes appends memory recall and every plugin's pre_llm_call context to the
    # user message it sends, which is all the llm_request middleware can see.
    # Keep the clean message so selection ranks against what the user typed.
    _remember_turn_user_message(kwargs.get("session_id"), kwargs.get("user_message"))
    cfg = _load_config_for_hook()
    if not cfg.enabled:
        return None
    if not cfg.dry_run:
        return {"context": FALLBACK_INSTRUCTION}
    return {
        "context": (
            f"{FALLBACK_INSTRUCTION} "
            "Hermes Tool Slimmer dry-run is enabled; "
            "schema selection is diagnostic-only for this turn."
        )
    }


SELECTOR_HOOK = "select_tool_schemas"
LLM_REQUEST_MIDDLEWARE = "llm_request"
SURFACE_HOOK = "select_tool_schemas_hook"
SURFACE_MIDDLEWARE = "llm_request_middleware"

# Modules that carry the turn loop across Hermes layouts; the installer's core
# patch inserts the select_tool_schemas call site into one of them.
_CORE_LOOP_MODULES = ("agent.conversation_loop", "agent.turn_api_request", "run_agent")


def _as_name_set(value: Any) -> set[str] | None:
    if value is None:
        return None
    try:
        return {str(item) for item in value}
    except TypeError:
        return None


def _known_valid_hooks(ctx: Any = None) -> set[str] | None:
    """Hook names Hermes advertises, or None when they cannot be discovered."""
    for owner in (ctx, getattr(ctx, "_manager", None)):
        for attr in ("valid_hooks", "VALID_HOOKS"):
            found = _as_name_set(getattr(owner, attr, None)) if owner is not None else None
            if found is not None:
                return found
    try:
        from hermes_cli.plugins import VALID_HOOKS  # type: ignore[import-not-found]
    except Exception:
        return None
    return _as_name_set(VALID_HOOKS)


def _known_valid_middleware(ctx: Any = None) -> set[str] | None:
    """Middleware kinds Hermes advertises, or None when they cannot be discovered."""
    for attr in ("valid_middleware", "VALID_MIDDLEWARE"):
        found = _as_name_set(getattr(ctx, attr, None)) if ctx is not None else None
        if found is not None:
            return found
    try:
        from hermes_cli.middleware import VALID_MIDDLEWARE  # type: ignore[import-not-found]
    except Exception:
        return None
    return _as_name_set(VALID_MIDDLEWARE)


def _core_invokes_selector_hook() -> bool | None:
    """Whether Hermes core source calls ``select_tool_schemas``.

    Returns None when no known turn-loop module is found (unknown layout). A
    ``False`` here with the hook advertised means a partially applied core patch:
    Hermes accepts the registration but never calls it.
    """
    import importlib.util
    from pathlib import Path

    found_any = False
    for module_name in _CORE_LOOP_MODULES:
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            continue
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            continue
        found_any = True
        try:
            if f'"{SELECTOR_HOOK}"' in Path(spec.origin).read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False if found_any else None


def resolve_selector_surface(ctx: Any = None) -> str | None:
    """Pick the Hermes surface that can apply schema selection, preferring the core hook.

    The core hook sees the canonical schema list and turn message; the
    ``llm_request`` middleware ships in Hermes v0.19+ and needs no core patch.
    Only one surface is used so requests are never slimmed twice.
    """
    valid_hooks = _known_valid_hooks(ctx)
    if valid_hooks is not None and SELECTOR_HOOK in valid_hooks and _core_invokes_selector_hook() is not False:
        return SURFACE_HOOK
    valid_middleware = _known_valid_middleware(ctx)
    middleware_ok = LLM_REQUEST_MIDDLEWARE in valid_middleware if valid_middleware is not None else None
    if middleware_ok and (ctx is None or callable(getattr(ctx, "register_middleware", None))):
        return SURFACE_MIDDLEWARE
    if ctx is not None and valid_hooks is None and valid_middleware is None:
        # Nothing advertised: try whichever registration method exists.
        if callable(getattr(ctx, "register_middleware", None)):
            return SURFACE_MIDDLEWARE
        if callable(getattr(ctx, "register_hook", None)):
            return SURFACE_HOOK
    return None


def maybe_register_selector_hook(ctx: Any) -> bool:
    """Register the selector with the best Hermes surface available.

    Returns True when a registration method accepted the callback. This avoids
    monkeypatching: unsupported Hermes versions keep diagnostics/CLI only.
    """
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        try:
            register_hook("pre_llm_call", pre_llm_diagnostic_hook)
        except Exception as exc:  # pragma: no cover - depends on Hermes version
            LOG.warning("pre_llm_call diagnostic hook registration failed: %s", exc)
    for method_name in ("register_tool_schema_selector", "register_schema_selector"):
        method = getattr(ctx, method_name, None)
        if callable(method):
            try:
                method(select_tool_schemas_callback)
                return True
            except Exception as exc:
                LOG.warning("%s registration failed: %s", method_name, exc)

    surface = resolve_selector_surface(ctx)
    try:
        if surface == SURFACE_HOOK and callable(register_hook):
            register_hook(SELECTOR_HOOK, select_tool_schemas_callback)
            return True
        if surface == SURFACE_MIDDLEWARE:
            from .middleware import llm_request_middleware

            ctx.register_middleware(LLM_REQUEST_MIDDLEWARE, llm_request_middleware)
            LOG.info("tool-slimmer selecting tool schemas through Hermes llm_request middleware")
            return True
    except Exception as exc:
        LOG.warning("tool-slimmer %s registration failed: %s", surface, exc)
    LOG.warning("Hermes selector surface is unavailable; tool-slimmer will run diagnostics only")
    return False
