import json
import urllib.error

import pytest

from hermes_tool_slimmer import jev
from hermes_tool_slimmer.config import JevConfig, ToolSlimmerConfig
from hermes_tool_slimmer.selector import ToolSelector
from hermes_tool_slimmer.tools import tool_slimmer_select

SCHEMAS = [
    {"name": "terminal", "toolset": "native", "description": "Run shell commands"},
    {"name": "tool_slimmer_request_full_tools", "toolset": "tool-slimmer", "description": "Request full tools"},
    {"name": "web_search", "toolset": "web", "description": "Search the web for information"},
    {"name": "web_extract", "toolset": "web", "description": "Extract content from web page URLs"},
    {"name": "cronjob_manage", "toolset": "cron", "description": "Manage scheduled cron jobs"},
    {"name": "x_search", "toolset": "x", "description": "Search X posts"},
    {"name": "image_generate", "toolset": "image", "description": "Generate images from text prompts"},
]


@pytest.fixture(autouse=True)
def _jev_env(monkeypatch):
    jev.reset_state()
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    yield
    jev.reset_state()


def _fake_post(probabilities, calls):
    def post(url, payload, api_key, timeout):
        calls.append({"url": url, "payload": payload, "api_key": api_key, "timeout": timeout})
        answers = {}
        for key, question in payload["questions"].items():
            name = question["instructions"]["tool"]["name"]
            answers[key] = {"type": "noul", "noul": probabilities.get(name, 0.0)}
        return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 1234, "output_tokens": 7}}

    return post


def _jev_selector(**overrides):
    return ToolSelector(ToolSlimmerConfig.from_mapping({"mode": "jev", "always_include": ["terminal"], **overrides}))


def test_jev_keeps_tools_above_threshold_ranked_by_probability(monkeypatch):
    calls = []
    monkeypatch.setattr(jev, "_post", _fake_post({"web_extract": 0.7, "web_search": 0.95, "x_search": 0.3}, calls))
    result = _jev_selector().select("search the web for hermes release notes", SCHEMAS)

    assert result.mode == "jev"
    # Always-included first, then kept tools in catalog order (stable for prompt caching).
    assert result.selected_names == ["terminal", "tool_slimmer_request_full_tools", "web_search", "web_extract"]
    assert result.always_included == ["terminal", "tool_slimmer_request_full_tools"]
    assert result.score_details["web_search"]["jev"] == 0.95
    assert result.metadata["jev_input_tokens"] == 1234
    assert result.metadata["jev_cache_hit"] is False
    # Always-included and non-task tools are never sent as candidates.
    sent = [q["instructions"]["tool"]["name"] for q in calls[0]["payload"]["questions"].values()]
    assert "terminal" not in sent and "tool_slimmer_request_full_tools" not in sent
    assert calls[0]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert calls[0]["api_key"] == "test-key"


def test_jev_respects_top_k_and_shortlist(monkeypatch):
    calls = []
    monkeypatch.setattr(jev, "_post", _fake_post({name: 0.9 for name in ("web_search", "web_extract", "x_search", "image_generate")}, calls))
    result = _jev_selector(top_k=2, jev={"shortlist": 3}).select("search the web and extract pages", SCHEMAS)

    assert len(calls[0]["payload"]["questions"]) == 3
    assert len([n for n in result.selected_names if n not in result.always_included]) == 2


def test_jev_selects_nothing_extra_when_no_tool_is_needed(monkeypatch):
    monkeypatch.setattr(jev, "_post", _fake_post({}, []))
    result = _jev_selector().select("explain how tcp handshakes work", SCHEMAS)

    assert result.selected_names == ["terminal", "tool_slimmer_request_full_tools"]
    assert result.reason == "jev_no_tool_above_threshold"


def test_jev_caches_identical_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(jev, "_post", _fake_post({"web_search": 0.9}, calls))
    selector = _jev_selector()
    selector.select("search the web for news", SCHEMAS)
    second = selector.select("search the web for news", SCHEMAS)

    assert len(calls) == 1
    assert second.metadata["jev_cache_hit"] is True
    assert "web_search" in second.selected_names


def test_jev_skips_low_information_turns(monkeypatch):
    calls = []
    monkeypatch.setattr(jev, "_post", _fake_post({"web_search": 0.9}, calls))
    result = _jev_selector().select("thanks", SCHEMAS)

    assert calls == []
    assert result.mode == "jev"
    assert result.reason == "low_information_query"


def test_jev_missing_key_falls_back_to_keyword_without_network(monkeypatch):
    calls = []
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(jev, "_post", _fake_post({}, calls))
    result = _jev_selector().select("search the web for news", SCHEMAS)
    keyword = ToolSelector(ToolSlimmerConfig.from_mapping({"mode": "keyword", "always_include": ["terminal"]})).select("search the web for news", SCHEMAS)

    assert calls == []
    assert result.metadata["jev_fallback"] == "missing_api_key"
    assert result.selected_names == keyword.selected_names


def test_jev_error_starts_cooldown(monkeypatch):
    attempts = []

    def failing_post(url, payload, api_key, timeout):
        attempts.append(url)
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(jev, "_post", failing_post)
    selector = _jev_selector()
    first = selector.select("search the web for news", SCHEMAS)
    second = selector.select("generate an image of a cat", SCHEMAS)

    assert first.metadata["jev_fallback"] == "connection_error"
    assert second.metadata["jev_fallback"] == "cooldown"
    assert len(attempts) == 1
    assert "web_search" in first.selected_names


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (urllib.error.HTTPError("https://api.typesafe.ai", 429, "Too Many Requests", {}, None), "http_429"),
        (TimeoutError("timed out"), "timeout"),
        (KeyError("answers"), "malformed_response"),
    ],
)
def test_jev_error_labels(monkeypatch, error, label):
    def failing_post(url, payload, api_key, timeout):
        raise error

    monkeypatch.setattr(jev, "_post", failing_post)
    result = _jev_selector().select("search the web for news", SCHEMAS)
    assert result.metadata["jev_fallback"] == label


def test_jev_payload_uses_structured_noul_questions():
    long_description = "word " * 200
    payload = jev.build_payload(
        "check the weather",
        [{"type": "function", "function": {"name": "web_search", "description": long_description}}],
        JevConfig(max_description_chars=40),
    )

    question = payload["questions"]["t0"]
    assert payload["model"] == "jev-latest"
    assert payload["state"] == {"user_request": "check the weather"}
    assert question["type"] == "noul"
    assert question["instructions"]["tool"]["name"] == "web_search"
    assert len(question["instructions"]["tool"]["description"]) == 40
    assert set(question["criteria"]) == {"true", "false"}
    json.dumps(payload)


def test_jev_config_parses_and_validates():
    cfg = ToolSlimmerConfig.from_mapping({"mode": "jev", "jev": {"threshold": 0.7, "shortlist": 12, "unknown": 1}})
    assert cfg.jev.threshold == 0.7
    assert cfg.jev.shortlist == 12
    assert cfg.jev.model == "jev-latest"

    for bad in ({"threshold": 1.5}, {"shortlist": 0}, {"base_url": "http://api.typesafe.ai"}, {"api_key_env": ""}):
        with pytest.raises(ValueError):
            ToolSlimmerConfig.from_mapping({"mode": "jev", "jev": bad})


def test_jev_config_profile_overlay():
    cfg = ToolSlimmerConfig.from_mapping({"mode": "jev", "profiles": {"telegram": {"jev": {"threshold": 0.8}}}})
    assert cfg.for_context(platform="telegram").jev.threshold == 0.8
    assert cfg.for_context(platform="cli").jev.threshold == 0.5


def test_model_callable_selector_cannot_opt_into_jev(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jev, "_post", _fake_post({"web_search": 0.9}, calls))
    config = tmp_path / "config.yaml"
    config.write_text("tool_slimmer:\n  mode: keyword\n", encoding="utf-8")
    result = json.loads(tool_slimmer_select({"query": "search the web", "mode": "jev", "schemas": SCHEMAS, "config_path": str(config)}))

    assert result["ok"] is False
    assert result["error"] == "mode_not_allowed"
    assert calls == []


def test_jev_reuses_cached_answer_across_the_tool_loop_and_keeps_used_tools(monkeypatch):
    from hermes_tool_slimmer.selector import TOOL_MENTIONS_MARKER

    calls = []
    monkeypatch.setattr(jev, "_post", _fake_post({"web_search": 0.9}, calls))
    selector = _jev_selector()
    first = selector.select("search the web for news", SCHEMAS)
    later = selector.select("search the web for news" + TOOL_MENTIONS_MARKER + "web_search cronjob_manage", SCHEMAS)

    assert len(calls) == 1
    assert later.metadata["jev_cache_hit"] is True
    assert calls[0]["payload"]["state"] == {"user_request": "search the web for news"}
    assert "cronjob_manage" not in first.selected_names
    assert later.metadata["jev_kept_turn_tools"] == ["web_search", "cronjob_manage"]
    assert later.selected_names.count("web_search") == 1
    assert "cronjob_manage" in later.selected_names


def test_jev_selection_order_is_stable_across_the_tool_loop(monkeypatch):
    from hermes_tool_slimmer.selector import TOOL_MENTIONS_MARKER

    monkeypatch.setattr(jev, "_post", _fake_post({"web_search": 0.6, "web_extract": 0.9}, []))
    selector = _jev_selector()
    first = selector.select("search the web and extract the page", SCHEMAS)
    later = selector.select("search the web and extract the page" + TOOL_MENTIONS_MARKER + "web_extract", SCHEMAS)

    assert first.selected_names == later.selected_names
    assert first.selected_names[-2:] == ["web_search", "web_extract"]
