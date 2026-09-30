from __future__ import annotations

import math
import os
import sys
from collections.abc import Collection
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml


VALID_MODES = {"eager", "keyword", "hybrid", "anthropic_tool_search", "two_pass", "jev"}
_LIST_FIELDS = {
    "always_exclude",
    "always_include",
    "never_defer",
    "disabled_tools",
    "disabled_toolsets",
}
_BOOL_FIELDS = {"enabled", "include_mcp_tools", "include_native_tools", "log_decisions", "fail_open", "dry_run"}
_ANTHROPIC_LIST_FIELDS = {"never_defer"}
_ANTHROPIC_BOOL_FIELDS = {"defer_mcp_tools", "defer_native_tools", "tool_search_supported"}
_TWO_PASS_BOOL_FIELDS = {"cache_hydrated_tools", "fallback_to_keyword", "include_toolsets"}
_NESTED_SECTIONS = ("anthropic", "two_pass", "jev")
_PROFILE_ALIASES = {
    "chat": "cli",
    "console": "cli",
    "terminal": "cli",
    "tui": "cli",
    "telegram_bot": "telegram",
    "slack_bot": "slack",
    "scheduled": "cron",
}


@dataclass
class AnthropicConfig:
    variant: str = "bm25"
    defer_mcp_tools: bool = True
    defer_native_tools: bool = False
    tool_search_supported: bool | None = None
    never_defer: list[str] = field(default_factory=lambda: ["terminal", "read_file", "search_files"])


@dataclass
class TwoPassConfig:
    max_catalog_tools: int = 120
    hydrate_limit: int = 8
    cache_hydrated_tools: bool = True
    fallback_to_keyword: bool = True
    include_toolsets: bool = True


@dataclass
class JevConfig:
    """TypeSafe Jev re-ranking for ``mode: jev``. The API key is read from ``api_key_env``."""

    model: str = "jev-latest"
    api_key_env: str = "TYPESAFE_API_KEY"
    base_url: str = "https://api.typesafe.ai"
    timeout_seconds: float = 1.5
    cooldown_seconds: float = 60.0
    shortlist: int = 32
    threshold: float = 0.5
    max_description_chars: int = 300


@dataclass
class ToolSlimmerConfig:
    enabled: bool = True
    mode: str = "keyword"
    top_k: int = 8
    always_include: list[str] = field(default_factory=lambda: ["terminal", "read_file", "write_file", "patch", "search_files"])
    never_defer: list[str] = field(default_factory=lambda: ["terminal", "read_file"])
    disabled_tools: list[str] = field(default_factory=list)
    disabled_toolsets: list[str] = field(default_factory=list)
    include_mcp_tools: bool = True
    include_native_tools: bool = True
    log_decisions: bool = True
    fail_open: bool = True
    dry_run: bool = False
    min_total_tools: int = 0
    min_estimated_reduction_percent: float = 5.0
    min_score: float = 0.25
    aliases: dict[str, list[str]] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    two_pass: TwoPassConfig = field(default_factory=TwoPassConfig)
    jev: JevConfig = field(default_factory=JevConfig)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "ToolSlimmerConfig":
        raw = dict(data or {})
        if "always_exclude" in raw and "disabled_tools" not in raw:
            raw["disabled_tools"] = raw["always_exclude"]
        profiles_raw = raw.pop("profiles", {}) or {}
        anthropic_raw = raw.pop("anthropic", {}) or {}
        two_pass_raw = raw.pop("two_pass", {}) or {}
        jev_raw = raw.pop("jev", {}) or {}
        if not isinstance(anthropic_raw, dict):
            anthropic_raw = {}
        if not isinstance(two_pass_raw, dict):
            two_pass_raw = {}
        if not isinstance(jev_raw, dict):
            jev_raw = {}
        raw = _normalize_mapping(raw, cls.__dataclass_fields__, _LIST_FIELDS, _BOOL_FIELDS)
        raw["profiles"] = _normalize_profiles(profiles_raw)
        anthropic_raw = _normalize_mapping(
            anthropic_raw,
            AnthropicConfig.__dataclass_fields__,
            _ANTHROPIC_LIST_FIELDS,
            _ANTHROPIC_BOOL_FIELDS,
            allow_none_booleans=True,
        )
        two_pass_raw = _normalize_mapping(
            two_pass_raw,
            TwoPassConfig.__dataclass_fields__,
            set(),
            _TWO_PASS_BOOL_FIELDS,
        )
        cfg = cls(**{key: value for key, value in raw.items() if key in cls.__dataclass_fields__ and key not in _NESTED_SECTIONS})
        cfg.anthropic = AnthropicConfig(**{key: value for key, value in anthropic_raw.items() if key in AnthropicConfig.__dataclass_fields__})
        cfg.two_pass = TwoPassConfig(**{key: value for key, value in two_pass_raw.items() if key in TwoPassConfig.__dataclass_fields__})
        cfg.jev = JevConfig(**{key: value for key, value in jev_raw.items() if key in JevConfig.__dataclass_fields__})
        cfg.validate()
        return cfg

    def for_context(self, *, platform: str | None = None, profile: str | None = None) -> "ToolSlimmerConfig":
        """Return this config with default and platform profile overlays applied."""
        names = ["default"]
        resolved = _profile_name(profile or platform)
        if resolved and resolved != "default":
            names.append(resolved)
        overlays = [self.profiles[name] for name in names if name in self.profiles]
        if not overlays:
            return self

        raw = asdict(self)
        raw["anthropic"] = asdict(self.anthropic)
        raw["two_pass"] = asdict(self.two_pass)
        raw["jev"] = asdict(self.jev)
        raw["profiles"] = self.profiles
        for overlay in overlays:
            _merge_profile_overlay(raw, overlay)
        return ToolSlimmerConfig.from_mapping(raw)

    def with_mode(self, mode: str) -> "ToolSlimmerConfig":
        """Return a validated copy of this config using ``mode``."""
        cfg = replace(self, mode=mode)
        cfg.validate()
        return cfg

    @property
    def always_exclude(self) -> list[str]:
        """User-facing alias for disabled_tools."""
        return self.disabled_tools

    def validate(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"Invalid tool_slimmer.mode {self.mode!r}; expected one of {sorted(VALID_MODES)}")
        if not isinstance(self.top_k, int) or isinstance(self.top_k, bool) or not math.isfinite(self.top_k):
            raise ValueError("tool_slimmer.top_k must be a finite integer")
        if self.top_k < 0:
            raise ValueError("tool_slimmer.top_k must be >= 0")
        if not isinstance(self.min_total_tools, int) or isinstance(self.min_total_tools, bool) or not math.isfinite(self.min_total_tools):
            raise ValueError("tool_slimmer.min_total_tools must be a finite integer")
        if self.min_total_tools < 0:
            raise ValueError("tool_slimmer.min_total_tools must be >= 0")
        if not isinstance(self.min_estimated_reduction_percent, (int, float)) or isinstance(self.min_estimated_reduction_percent, bool) or not math.isfinite(self.min_estimated_reduction_percent):
            raise ValueError("tool_slimmer.min_estimated_reduction_percent must be finite")
        if self.min_estimated_reduction_percent < 0:
            raise ValueError("tool_slimmer.min_estimated_reduction_percent must be >= 0")
        if not isinstance(self.min_score, (int, float)) or isinstance(self.min_score, bool) or not math.isfinite(self.min_score):
            raise ValueError("tool_slimmer.min_score must be finite")
        if self.min_score < 0:
            raise ValueError("tool_slimmer.min_score must be >= 0")
        if not isinstance(self.two_pass.max_catalog_tools, int) or isinstance(self.two_pass.max_catalog_tools, bool) or not math.isfinite(self.two_pass.max_catalog_tools):
            raise ValueError("tool_slimmer.two_pass.max_catalog_tools must be a finite integer")
        if self.two_pass.max_catalog_tools < 1:
            raise ValueError("tool_slimmer.two_pass.max_catalog_tools must be >= 1")
        if not isinstance(self.two_pass.hydrate_limit, int) or isinstance(self.two_pass.hydrate_limit, bool) or not math.isfinite(self.two_pass.hydrate_limit):
            raise ValueError("tool_slimmer.two_pass.hydrate_limit must be a finite integer")
        if self.two_pass.hydrate_limit < 1:
            raise ValueError("tool_slimmer.two_pass.hydrate_limit must be >= 1")
        for name in ("model", "api_key_env", "base_url"):
            if not isinstance(getattr(self.jev, name), str) or not getattr(self.jev, name).strip():
                raise ValueError(f"tool_slimmer.jev.{name} must be a non-empty string")
        if not self.jev.base_url.startswith("https://"):
            raise ValueError("tool_slimmer.jev.base_url must use https")
        for name in ("timeout_seconds", "cooldown_seconds", "threshold"):
            value = getattr(self.jev, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"tool_slimmer.jev.{name} must be a finite number >= 0")
        if self.jev.threshold > 1:
            raise ValueError("tool_slimmer.jev.threshold must be <= 1")
        for name in ("shortlist", "max_description_chars"):
            value = getattr(self.jev, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"tool_slimmer.jev.{name} must be an integer >= 1")


def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    raise ValueError(f"tool_slimmer.{field_name} must be a string or list")


def _normalize_aliases(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("tool_slimmer.aliases must be a mapping")
    aliases: dict[str, list[str]] = {}
    for key, values in value.items():
        aliases[str(key)] = _normalize_string_list(values, f"aliases.{key}")
    return aliases


def _normalize_profiles(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("tool_slimmer.profiles must be a mapping")
    profiles: dict[str, dict[str, Any]] = {}
    for name, profile_raw in value.items():
        if profile_raw is None:
            continue
        if not isinstance(profile_raw, dict):
            raise ValueError(f"tool_slimmer.profiles.{name} must be a mapping")
        profile = dict(profile_raw)
        if "always_exclude" in profile and "disabled_tools" not in profile:
            profile["disabled_tools"] = profile["always_exclude"]
        anthropic_raw = profile.pop("anthropic", None)
        normalized = _normalize_mapping(profile, ToolSlimmerConfig.__dataclass_fields__, _LIST_FIELDS, _BOOL_FIELDS)
        if isinstance(anthropic_raw, dict):
            normalized["anthropic"] = _normalize_mapping(
                anthropic_raw,
                AnthropicConfig.__dataclass_fields__,
                _ANTHROPIC_LIST_FIELDS,
                _ANTHROPIC_BOOL_FIELDS,
                allow_none_booleans=True,
            )
        jev_raw = profile.pop("jev", None)
        if isinstance(jev_raw, dict):
            normalized["jev"] = _normalize_mapping(jev_raw, JevConfig.__dataclass_fields__, set(), set())
        two_pass_raw = profile.pop("two_pass", None)
        if isinstance(two_pass_raw, dict):
            normalized["two_pass"] = _normalize_mapping(
                two_pass_raw,
                TwoPassConfig.__dataclass_fields__,
                set(),
                _TWO_PASS_BOOL_FIELDS,
            )
        profiles[_profile_name(str(name)) or str(name)] = normalized
    return profiles


def _profile_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("-", "_")
    return _PROFILE_ALIASES.get(normalized, normalized)


def _merge_profile_overlay(raw: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if key == "anthropic" and isinstance(value, dict):
            anthropic = raw.get("anthropic")
            if not isinstance(anthropic, dict):
                anthropic = {}
            raw["anthropic"] = {**anthropic, **value}
        elif key in {"two_pass", "jev"} and isinstance(value, dict):
            section = raw.get(key)
            if not isinstance(section, dict):
                section = {}
            raw[key] = {**section, **value}
        elif key == "aliases" and isinstance(value, dict):
            aliases = raw.get("aliases")
            if not isinstance(aliases, dict):
                aliases = {}
            raw["aliases"] = {**aliases, **value}
        elif key in {"disabled_tools", "disabled_toolsets"} and isinstance(value, list):
            current = raw.get(key)
            if not isinstance(current, list):
                current = []
            raw[key] = _dedupe_strings([*current, *value])
        elif key != "profiles":
            raw[key] = value


def _normalize_mapping(
    raw: dict[str, Any],
    allowed_fields: Collection[str],
    list_fields: set[str],
    bool_fields: set[str],
    *,
    allow_none_booleans: bool = False,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed_fields:
            continue
        if key in list_fields:
            normalized[key] = _normalize_string_list(value, key)
        elif key == "aliases":
            normalized[key] = _normalize_aliases(value)
        elif key in bool_fields:
            if value is None and allow_none_booleans:
                normalized[key] = None
            elif isinstance(value, bool):
                normalized[key] = value
            else:
                raise ValueError(f"tool_slimmer.{key} must be a boolean")
        else:
            normalized[key] = value
    return normalized


def _dedupe_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text not in seen:
            out.append(text)
            seen.add(text)
    return out


def hermes_home() -> Path:
    """Hermes home directory, resolved the same way Hermes itself does when importable."""
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home()).expanduser()
    except Exception:
        pass
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def config_path() -> Path:
    return Path(os.environ.get("HERMES_CONFIG", hermes_home() / "config.yaml")).expanduser()


def load_config(path: str | Path | None = None, *, strict: bool = False) -> ToolSlimmerConfig:
    target = Path(path).expanduser() if path else config_path()
    if not target.is_file():
        return ToolSlimmerConfig()
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        if strict:
            raise ValueError(f"Could not parse tool_slimmer config: {target}") from exc
        return ToolSlimmerConfig()
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"tool_slimmer config must be a mapping: {target}")
        return ToolSlimmerConfig()
    section = data.get("tool_slimmer", data if "mode" in data else {})
    if not isinstance(section, dict):
        if strict:
            raise ValueError(f"tool_slimmer section must be a mapping: {target}")
        return ToolSlimmerConfig()
    return ToolSlimmerConfig.from_mapping(section)
