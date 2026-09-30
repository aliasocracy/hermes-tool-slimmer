"""Re-rank a keyword shortlist with TypeSafe's Jev System One model.

Each candidate tool becomes one Noul (yes/no probability) question evaluated
against the user's request. Jev answers every question in parallel in one
request, so latency barely depends on the shortlist size. Only the request text
and the candidate tools' names and descriptions leave the machine.

Any failure raises :class:`JevUnavailable`; the selector then keeps the keyword
result. A failed call starts a cooldown so an outage does not add a timeout to
every model call in a tool loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any

from .corpus import tool_name
from .types import Schema

if TYPE_CHECKING:
    from .config import JevConfig

MAX_REQUEST_CHARS = 6000
_CACHE_LIMIT = 256
_CACHE: OrderedDict[str, tuple[dict[str, float], dict[str, Any]]] = OrderedDict()
_LOCK = threading.Lock()
_cooldown_until = 0.0

QUESTION = "Would an assistant need to call `tool` to fulfill `user_request`?"
CRITERIA = {
    "true": "`tool` performs an action or lookup that `user_request` asks for or cannot be completed without.",
    "false": "`user_request` can be completed without calling `tool`.",
}


class JevUnavailable(RuntimeError):
    """Jev could not rank the shortlist; ``str(exc)`` is a short reason label."""


def _tool_description(schema: Schema, limit: int) -> str:
    nested = schema.get("function")
    function: dict[str, Any] = nested if isinstance(nested, dict) else schema
    description = " ".join(str(function.get("description") or "").split())
    return description if len(description) <= limit else description[: limit - 1].rstrip() + "…"


def build_payload(request_text: str, schemas: list[Schema], cfg: "JevConfig") -> dict[str, Any]:
    questions = {
        f"t{index}": {
            "type": "noul",
            "instructions": {
                "tool": {"name": tool_name(schema), "description": _tool_description(schema, cfg.max_description_chars)},
                "question": QUESTION,
            },
            "criteria": CRITERIA,
        }
        for index, schema in enumerate(schemas)
    }
    return {
        "model": cfg.model,
        "state": {"user_request": request_text[:MAX_REQUEST_CHARS]},
        "questions": questions,
    }


def _post(url: str, payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    from . import __version__

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"hermes-tool-slimmer/{__version__}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint from config
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("response is not an object")
    return body


def _error_label(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        return "connection_error"
    return "malformed_response"


def reset_state() -> None:
    """Clear the response cache and any active cooldown (tests and config reloads)."""
    global _cooldown_until
    with _LOCK:
        _CACHE.clear()
        _cooldown_until = 0.0


def score_tools(request_text: str, schemas: list[Schema], cfg: "JevConfig") -> tuple[dict[str, float], dict[str, Any]]:
    """Return ``{tool name: probability the request needs it}`` plus call metadata."""
    global _cooldown_until
    api_key = os.environ.get(cfg.api_key_env, "").strip()
    if not api_key:
        raise JevUnavailable("missing_api_key")
    if not schemas:
        return {}, {"jev_candidates": 0}
    payload = build_payload(request_text, schemas, cfg)
    cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            _CACHE.move_to_end(cache_key)
            return dict(cached[0]), {**cached[1], "jev_cache_hit": True, "jev_ms": 0.0}
        if monotonic() < _cooldown_until:
            raise JevUnavailable("cooldown")

    started = perf_counter()
    try:
        response = _post(f"{cfg.base_url.rstrip('/')}/v1/systemone", payload, api_key, cfg.timeout_seconds)
        answers = response["answers"]
        probabilities = {tool_name(schema): float(answers[f"t{index}"]["noul"]) for index, schema in enumerate(schemas)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # URLError, HTTPError and socket timeouts are all OSError subclasses.
        with _LOCK:
            _cooldown_until = monotonic() + cfg.cooldown_seconds
        raise JevUnavailable(_error_label(exc)) from exc

    raw_usage = response.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    metadata: dict[str, Any] = {
        "jev_model": response.get("model"),
        "jev_candidates": len(schemas),
        "jev_input_tokens": usage.get("input_tokens"),
        "jev_ms": round((perf_counter() - started) * 1000, 1),
        "jev_cache_hit": False,
    }
    with _LOCK:
        _CACHE[cache_key] = (probabilities, metadata)
        while len(_CACHE) > _CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return dict(probabilities), dict(metadata)
