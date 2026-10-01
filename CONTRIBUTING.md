# Contributing to Hermes Tool Slimmer

Thanks for helping improve Hermes Tool Slimmer. This project is intentionally small, deterministic, and fail-open because it influences which tool schemas Hermes sends to model providers.

## Development setup

```bash
git clone https://github.com/aliasocracy/hermes-tool-slimmer.git
cd hermes-tool-slimmer
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

## Validation before a PR

Run the same checks used for release:

```bash
ruff check .
mypy src tests
python -m compileall -q src tests
pytest -q
python -m build
```

If you update `docs/hermes-core-selector-hook.patch`, also validate it against a clean Hermes checkout:

```bash
git clone https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent-core
cd /tmp/hermes-agent-core
git apply --check /path/to/hermes-tool-slimmer/docs/hermes-core-selector-hook.patch
git apply /path/to/hermes-tool-slimmer/docs/hermes-core-selector-hook.patch
python -m py_compile run_agent.py hermes_cli/plugins.py tests/hermes_cli/test_tool_schema_selector_hook.py
PYTHONPATH=$PWD pytest -q -o addopts='' tests/hermes_cli/test_tool_schema_selector_hook.py
git diff --check
```

## Design rules

- Do not monkeypatch Hermes provider internals for the release path.
- Keep selector behavior deterministic unless an optional mode clearly documents otherwise.
- Preserve fail-open behavior: selector errors must not remove user tools.
- Do not resurrect tools that Hermes already disabled.
- Add regression tests for ranking, provider gating, and config behavior changes.
