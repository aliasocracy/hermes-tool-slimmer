from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Iterable

from .bm25 import BM25
from .config import ToolSlimmerConfig
from .corpus import build_corpus, tool_name
from .jev import JevUnavailable, score_tools
from .native import NATIVE_TOOL_SEARCH_BRIDGE_NAMES
from .policy import eligible_schemas
from .tokenizer import tokenize
from .types import Schema, SelectionResult, ToolDocument
from .two_pass import HYDRATE_TOOL_NAME

LOG = logging.getLogger(__name__)

SAFETY_TOOL_NAMES = ("tool_slimmer_request_full_tools",)
NON_TASK_TOOL_NAMES = (
    "tool_slimmer_request_full_tools",
    HYDRATE_TOOL_NAME,
    "tool_slimmer_select",
    "tool_slimmer_status",
    *NATIVE_TOOL_SEARCH_BRIDGE_NAMES,
)
SKILL_COMPANION_TOOL_NAMES = ("skill_view", "skills_list")
# The integration appends tools named in the current turn's tool loop to the query.
TOOL_MENTIONS_MARKER = "\n\nRecent missing/needed tool mentions: "

BUILTIN_ALIASES = {
    "browse": ["browser", "navigate", "url", "web", "website", "page"],
    "browsing": ["browser", "navigate", "url", "web", "website", "page"],
    "site": ["website", "web", "url", "page"],
    "webpage": ["website", "web", "url", "page"],
    "website": ["web", "url", "page"],
}

LOW_INFORMATION_TOKENS = {
    "hello",
    "hi",
    "hey",
    "yo",
    "sup",
    "test",
    "testing",
    "ping",
    "ok",
    "okay",
    "thanks",
    "thank",
    "you",
    "yes",
    "no",
    "yep",
    "nope",
    "cool",
}


@dataclass
class _Ranking:
    """Keyword ranking shared by keyword/hybrid selection and the Jev shortlist."""

    eligible: list[Schema]
    by_name: dict[str, Schema]
    ranked_names: list[str]
    scores: dict[str, float]
    score_details: dict[str, dict[str, float]]
    query_tokens: list[str]
    always_selected: list[Schema]
    always_present: list[str]


class ToolSelector:
    def __init__(self, config: ToolSlimmerConfig | None = None) -> None:
        self.config = config or ToolSlimmerConfig()
        self.config.validate()

    def select(self, user_message: str, schemas: list[Schema], **kwargs: object) -> SelectionResult:
        mode = kwargs.get("mode")
        if isinstance(mode, str) and mode != self.config.mode:
            override = asdict(self.config)
            override["mode"] = mode
            return ToolSelector(ToolSlimmerConfig.from_mapping(override)).select(user_message, schemas)
        if not self.config.enabled or self.config.mode == "eager":
            return SelectionResult(self.config.mode, schemas, [tool_name(s) for s in schemas], {}, len(schemas), [])
        try:
            if self.config.mode == "jev":
                return self._select_jev(user_message, schemas)
            return self._select_keyword(user_message, schemas)
        except Exception as exc:
            if self.config.fail_open:
                return SelectionResult(self.config.mode, schemas, [tool_name(s) for s in schemas], {}, len(schemas), [], fail_open=True, reason=str(exc))
            raise

    def _eligible(self, schemas: Iterable[Schema]) -> list[Schema]:
        return eligible_schemas(schemas, self.config)

    def _rank(self, user_message: str, schemas: list[Schema]) -> _Ranking:
        eligible = self._eligible(schemas)
        schemas_by_name: dict[str, list[Schema]] = defaultdict(list)
        for schema in eligible:
            schemas_by_name[tool_name(schema)].append(schema)
        duplicate_names = sorted(name for name, matches in schemas_by_name.items() if len(matches) > 1)
        if duplicate_names:
            LOG.warning("duplicate tool schema names encountered; first schema wins: %s", ", ".join(duplicate_names))
        by_name = {name: matches[0] for name, matches in schemas_by_name.items()}
        unique_eligible = list(by_name.values())

        non_task_names = set(NON_TASK_TOOL_NAMES)
        rankable_schemas = [schema for schema in unique_eligible if tool_name(schema) not in non_task_names]
        docs = build_corpus(rankable_schemas)
        base_query_tokens = tokenize(user_message)
        query_tokens, alias_terms = expand_query_tokens(base_query_tokens, self.config.aliases)
        bm25 = BM25([doc.tokens for doc in docs])
        raw_scores = bm25.scores(query_tokens)
        score_details: dict[str, dict[str, float]] = {}
        scores: dict[str, float] = {}
        for doc, raw_score in zip(docs, raw_scores, strict=True):
            parts = self._score_parts(query_tokens, alias_terms, doc, hybrid=self.config.mode == "hybrid")
            parts["bm25"] = raw_score
            total = round(sum(parts.values()), 6)
            parts["total"] = total
            score_details[doc.name] = parts
            scores[doc.name] = total

        always_selected: list[Schema] = []
        always_present: list[str] = []
        for name in _always_include_names(self.config.always_include):
            if name in by_name and name not in always_present:
                always_selected.append(by_name[name])
                always_present.append(name)
        ranked_names = [doc.name for doc in sorted(docs, key=lambda doc: (scores.get(doc.name, 0.0), doc.name), reverse=True)]
        return _Ranking(eligible, by_name, ranked_names, scores, score_details, query_tokens, always_selected, always_present)

    def _select_keyword(self, user_message: str, schemas: list[Schema], ranking: _Ranking | None = None) -> SelectionResult:
        ranking = ranking or self._rank(user_message, schemas)
        eligible = ranking.eligible
        by_name = ranking.by_name
        scores = ranking.scores
        score_details = ranking.score_details
        query_tokens = ranking.query_tokens
        always_present = list(ranking.always_present)
        selected = list(ranking.always_selected)
        selected_names = set(always_present)

        if _is_low_information_query(query_tokens):
            return SelectionResult(
                self.config.mode,
                selected,
                [tool_name(s) for s in selected],
                scores,
                len(schemas),
                always_present,
                reason="low_information_query",
                score_details=score_details,
                expanded_query_tokens=query_tokens,
            )

        has_relevant_match = bool(query_tokens) and any(score > 0 for score in scores.values())
        if not has_relevant_match:
            if selected:
                return SelectionResult(self.config.mode, selected, [tool_name(s) for s in selected], scores, len(schemas), always_present, reason="no_relevant_match", score_details=score_details, expanded_query_tokens=query_tokens)
            if eligible and self.config.fail_open and self.config.top_k > 0:
                return SelectionResult(self.config.mode, eligible, [tool_name(s) for s in eligible], scores, len(schemas), always_present, fail_open=True, reason="no_relevant_match", score_details=score_details, expanded_query_tokens=query_tokens)
            return SelectionResult(self.config.mode, selected, [], scores, len(schemas), always_present, reason="no_relevant_match", score_details=score_details, expanded_query_tokens=query_tokens)

        remaining_slots = self.config.top_k
        for name in ranking.ranked_names:
            if remaining_slots <= 0:
                break
            if name in selected_names:
                continue
            score = scores.get(name, 0.0)
            if score < self.config.min_score:
                continue
            selected.append(by_name[name])
            selected_names.add(name)
            remaining_slots -= 1

        if _needs_skill_companions(query_tokens, selected_names):
            for name in SKILL_COMPANION_TOOL_NAMES:
                if name in by_name and name not in selected_names:
                    selected.append(by_name[name])
                    selected_names.add(name)

        if not selected and eligible and self.config.top_k > 0:
            return SelectionResult(self.config.mode, selected, [], scores, len(schemas), always_present, reason="below_min_score", score_details=score_details, expanded_query_tokens=query_tokens)
        return SelectionResult(self.config.mode, selected, [tool_name(s) for s in selected], scores, len(schemas), always_present, score_details=score_details, expanded_query_tokens=query_tokens)

    def _select_jev(self, user_message: str, schemas: list[Schema]) -> SelectionResult:
        """Keyword shortlist re-ranked by Jev; any Jev failure keeps the keyword result.

        Jev sees only the user's request, with candidates in a stable order, so
        every model call in one turn's tool loop sends the same payload and hits
        the cache. Tools already used in the turn stay selected directly.
        """
        keyword = self._select_keyword(user_message, schemas)
        request_text, _, mention_text = user_message.partition(TOOL_MENTIONS_MARKER)
        ranking = self._rank(request_text, schemas)
        if keyword.reason == "low_information_query" or not ranking.query_tokens:
            return replace(keyword, mode="jev")
        jev_cfg = self.config.jev
        always = set(ranking.always_present)
        shortlist = [name for name in ranking.ranked_names if name not in always][: jev_cfg.shortlist]
        candidates = sorted(shortlist)
        try:
            probabilities, metadata = score_tools(request_text, [ranking.by_name[name] for name in candidates], jev_cfg)
        except JevUnavailable as exc:
            LOG.info("tool-slimmer jev ranking unavailable (%s); using keyword selection", exc)
            return replace(keyword, mode="jev", metadata={**keyword.metadata, "jev_fallback": str(exc)})

        selected = list(ranking.always_selected)
        selected_names = set(always)
        mentioned = [name for name in dict.fromkeys(mention_text.split()) if name in ranking.by_name and name not in selected_names]
        for name in mentioned:
            selected.append(ranking.by_name[name])
            selected_names.add(name)
        if mentioned:
            metadata["jev_kept_turn_tools"] = mentioned
        passing = sorted(
            (name for name in candidates if probabilities.get(name, 0.0) >= jev_cfg.threshold),
            key=lambda name: (probabilities[name], ranking.scores.get(name, 0.0)),
            reverse=True,
        )
        for name in [name for name in passing if name not in selected_names][: self.config.top_k]:
            selected.append(ranking.by_name[name])
            selected_names.add(name)
        if _needs_skill_companions(ranking.query_tokens, selected_names):
            for name in SKILL_COMPANION_TOOL_NAMES:
                if name in ranking.by_name and name not in selected_names:
                    selected.append(ranking.by_name[name])
                    selected_names.add(name)
        # Catalog order keeps the tool block byte-identical across the turn's tool
        # loop, so provider and local prefix caches keep hitting.
        catalog_order = {name: index for index, name in enumerate(ranking.by_name)}
        selected = selected[: len(always)] + sorted(selected[len(always) :], key=lambda schema: catalog_order[tool_name(schema)])
        score_details = {
            name: {"jev": round(probabilities[name], 4), "keyword_total": ranking.scores.get(name, 0.0)}
            for name in candidates
            if name in probabilities
        }
        return SelectionResult(
            "jev",
            selected,
            [tool_name(schema) for schema in selected],
            {name: round(probability, 4) for name, probability in probabilities.items()},
            len(schemas),
            list(ranking.always_present),
            reason=None if passing or mentioned else "jev_no_tool_above_threshold",
            score_details=score_details,
            expanded_query_tokens=ranking.query_tokens,
            metadata=metadata,
        )

    @staticmethod
    def _score_parts(query_tokens: list[str], alias_terms: set[str], doc: ToolDocument, *, hybrid: bool = False) -> dict[str, float]:
        query = set(query_tokens)
        parts = {"name_boost": 0.0, "toolset_boost": 0.0, "parameter_boost": 0.0, "alias_boost": 0.0, "hybrid_boost": 0.0, "context_penalty": 0.0}
        normalized_name = doc.name.lower()
        query_text = " ".join(query_tokens)
        if len(normalized_name) >= 2 and (normalized_name in query_text or normalized_name.replace("_", " ") in query_text):
            parts["name_boost"] += 10.0
        if doc.toolset and set(tokenize(doc.toolset)) & query:
            parts["toolset_boost"] += 2.5
        parts["parameter_boost"] += 1.25 * len(doc.parameter_tokens & query)
        alias_matches = alias_terms & (set(doc.tokens) | doc.parameter_tokens | set(tokenize(doc.toolset or "")))
        parts["alias_boost"] += 0.75 * len(alias_matches)
        if hybrid:
            doc_terms = {token for token in doc.tokens if len(token) >= 4}
            for token in query:
                if len(token) < 4 or token in doc_terms:
                    continue
                if any(SequenceMatcher(None, token, candidate).ratio() >= 0.84 for candidate in doc_terms):
                    parts["hybrid_boost"] += 0.5
        has_schedule_context = bool({"cron", "schedule", "scheduled", "recurring", "every"} & query)
        has_feishu_context = bool({"feishu", "lark", "drive", "doc", "docs"} & query)
        has_browser_context = bool({"browser", "browse", "browsing", "navigate", "website", "webpage", "url", "page"} & query)
        if doc.name == "cronjob" and {"python", "script", "run", "execute"} & query and not has_schedule_context:
            parts["context_penalty"] -= 12.0
        if doc.name == "cronjob" and has_browser_context and not has_schedule_context:
            parts["context_penalty"] -= 8.0
        if doc.name == "memory" and has_browser_context:
            parts["context_penalty"] -= 6.0
        if doc.name == "skill_manage" and {"edit", "patch", "write", "file", "repo", "repository"} & query and not {"skill", "skills"} & query:
            parts["context_penalty"] -= 8.0
        if doc.name.startswith("feishu_") and not has_feishu_context and {"comment", "comments", "edit", "file", "patch", "write", "script", "python", "code", "repo", "repository", "github", "pr"} & query:
            parts["context_penalty"] -= 10.0
        return parts


def expand_query_tokens(tokens: list[str], configured_aliases: dict[str, list[str]] | None = None) -> tuple[list[str], set[str]]:
    aliases = {key: list(values) for key, values in BUILTIN_ALIASES.items()}
    for key, values in (configured_aliases or {}).items():
        aliases.setdefault(str(key).lower(), [])
        aliases[str(key).lower()].extend(str(value).lower() for value in values)
    expanded: list[str] = []
    seen: set[str] = set()
    alias_terms: set[str] = set()
    for token in tokens:
        token_aliases = aliases.get(token, [])
        alias_terms.update(token_aliases)
        for value in [token, *token_aliases]:
            if value not in seen:
                expanded.append(value)
                seen.add(value)
    return expanded, alias_terms


def _always_include_names(configured: Iterable[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in [*configured, *SAFETY_TOOL_NAMES]:
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _needs_skill_companions(query_tokens: list[str], selected_names: set[str]) -> bool:
    if {"skill_manage", "skill_view", "skills_list"} & selected_names:
        return True
    return bool({"skill", "skills", "skill_view", "skills_list"} & set(query_tokens))


def _is_low_information_query(query_tokens: list[str]) -> bool:
    if not query_tokens:
        return False
    meaningful = [token for token in query_tokens if not token.isdigit()]
    if not meaningful:
        return True
    if len(meaningful) > 4:
        return False
    return all(token in LOW_INFORMATION_TOKENS for token in meaningful)


def select_schemas(user_message: str, schemas: list[Schema], config: ToolSlimmerConfig | None = None, **kwargs: object) -> list[Schema]:
    return ToolSelector(config).select(user_message, schemas, **kwargs).selected
