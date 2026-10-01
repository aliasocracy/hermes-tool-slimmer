# Hermes Dashboard Plugin

Hermes Tool Slimmer includes a dashboard plugin for visibility into selector activity, estimated schema-byte savings, and approximate schema-token savings.

## One-command install

From a terminal on the Hermes machine:

```bash
cd "$HOME"
git clone https://github.com/aliasocracy/hermes-tool-slimmer.git
cd hermes-tool-slimmer
```

Then run:

```bash
scripts/install-hermes-tool-slimmer.sh
```

That script:

- installs this package into the Hermes Python environment
- copies the dashboard/user plugin into `$HERMES_HOME/plugins/tool-slimmer`
- enables `tool-slimmer` with `hermes plugins enable tool-slimmer`
- uses Hermes' `llm_request` middleware on Hermes v0.19+, and only patches Hermes core with the `select_tool_schemas` hook on older releases
- restarts `hermes-dashboard.service` and `hermes-gateway.service` when they exist
- runs a final health report

If a restricted agent approval layer blocks direct script execution after downloading the repo, approve or run the explicit shell invocation instead:

```bash
bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
```

That is the same installer. The block is environmental, not a dashboard plugin failure. Avoid running installer scripts from a predictable shared `/tmp` checkout.

If there are multiple `hermes` binaries, force the venv launcher:

```bash
HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes" bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
```

This keeps the plugin install and Hermes CLI command in the same Python environment.

After install, verify:

```bash
$HOME/.hermes/hermes-agent/venv/bin/hermes tool-slimmer doctor
```

## Updating

If Tool Slimmer shows as `Source: git` on the Hermes dashboard **Plugins** page, use the dashboard **Git pull** / **Update** action, then restart the gateway so active schema slimming uses the updated code.

If Tool Slimmer shows as `Source: user`, update the terminal checkout before reinstalling:

```bash
cd "$HOME"
if [ -d "$HOME/hermes-tool-slimmer/.git" ]; then
  cd "$HOME/hermes-tool-slimmer"
  git pull --ff-only
else
  git clone https://github.com/aliasocracy/hermes-tool-slimmer.git "$HOME/hermes-tool-slimmer"
  cd "$HOME/hermes-tool-slimmer"
fi

HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes" bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
```

Do not rerun an old checkout from `/tmp/hermes-tool-slimmer`; the installer will reinstall whatever version is in that folder.

If you only want a health report:

```bash
scripts/troubleshoot-hermes-tool-slimmer.sh
```

## Manual install

Use this only when you cannot run the installer:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -e .
rm -rf ~/.hermes/plugins/tool-slimmer
cp -R dashboard-plugin/tool-slimmer ~/.hermes/plugins/tool-slimmer
hermes plugins enable tool-slimmer
systemctl --user restart hermes-dashboard.service hermes-gateway.service
hermes tool-slimmer doctor
```

Hermes mounts plugin API routers during dashboard startup; a plugin rescan can discover assets, but it does not mount a newly added `plugin_api.py`.

The dashboard reads from `$HERMES_HOME/tool-slimmer/decisions.jsonl`. Decision logging is enabled when `tool_slimmer.log_decisions: true`, which is the default. Logged records contain selector metrics, provider/model/platform/session metadata, and selected tool names; they do not store user prompts.

The dashboard includes a privacy card backed by the same field inventory as `hermes tool-slimmer privacy`. It also exposes score details for recent v0.3.0+ decisions in the Decision Inspector, including alias and hybrid boosts, and can generate the bundled example eval report from the Release Evidence card.

## Guided Setup

The **Guided Setup** card calls the same advisor engine as:

```bash
hermes tool-slimmer advisor
```

It checks whether the config loads, whether the index exists, whether the full-tool fallback is visible, and whether platform profiles are configured. **Apply Recommended Config** writes only the `tool_slimmer` section and ensures `tool-slimmer` is listed in `plugins.enabled`. A timestamped backup is created first under:

```text
~/.hermes/tool-slimmer/backups/
```

Recommended profiles are intentionally conservative:

- Telegram gets a narrow tool budget with memory and fallback kept available.
- Slack gets a moderate budget with memory and file-search helpers.
- CLI keeps the normal file/edit/search tool set.
- Cron gets a larger budget for scheduled execution tasks.
- Webhook keeps the fallback hot because webhook prompts are often short.

If the recommendation does not fit a machine, restore the backup:

```bash
hermes tool-slimmer advisor --rollback ~/.hermes/tool-slimmer/backups/config-YYYYmmdd-HHMMSS.yaml
```

Recent decision rows also expose quick actions for selected tools:

- **Always include** adds that tool to the current row's platform profile when Hermes recorded one.
- **Never pick here** adds that tool to the current row's platform profile `always_exclude` list when Hermes recorded one.

Both actions create a config backup before writing.

## Tool Index

The dashboard has a **Tool Index** card that shows the persisted index path, rebuild state, indexed tool count, checksum, last-updated time, and a preview of indexed tool names. Use **Rebuild From Hermes Tools** after adding or removing Hermes plugins or MCP toolsets. When recent Tool Slimmer decisions exist, the rebuild uses the last live request schema snapshot so agent-injected context and memory tools are included; otherwise it falls back to Hermes' base tool definitions.

The persisted index is an operator aid. Live Hermes requests still rank the request-local tool schemas in memory, so the selector always respects the exact tools Hermes made available for that turn.

## Metrics Caveat

The dashboard reports estimated schema-token savings, not guaranteed billable-token savings. The estimate is `serialized tool-schema JSON bytes / 4` before and after Tool Slimmer selection. Actual provider input tokens can differ because model tokenizers, message formatting, prompt caching, system prompts, conversation history, and provider-specific tool serialization are outside this estimate.

Headline dashboard totals count real Hermes session events by default. Events without a `session_id` are treated as probes/tests and excluded from headline totals; the backend still returns `all_summary` for full audit visibility.

The dashboard also reports average selector overhead in milliseconds, the number of low-value selections skipped by the guardrails, and Anthropic deferred-tool counts when `anthropic_tool_search` is active. Skips are expected for small cron catalogs or any request where estimated reduction is below `tool_slimmer.min_estimated_reduction_percent`.

## Troubleshooting

Run:

```bash
scripts/troubleshoot-hermes-tool-slimmer.sh
```

The last line is written for operators:

- `Ready` means the package, plugin, dashboard manifest, and core hook are in place.
- `Usable with warnings` usually means no schema file was supplied to validate `always_include`; this is acceptable after install.
- `Needs attention` lists failing checks that must be fixed.
