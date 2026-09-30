# Troubleshooting

## Create a support report

When opening a GitHub issue, include this sanitized report:

```bash
hermes tool-slimmer diagnostics
```

It includes config shape, doctor checks, index counts, live snapshot summaries, and recent decision counters. It does not include raw prompts, environment secret values, or session IDs.

## Reinstall keeps installing an old version

The installer installs the version in the local checkout you run it from. If the output says something like:

```text
Built hermes-tool-slimmer @ file:///tmp/hermes-tool-slimmer
~ hermes-tool-slimmer==0.4.7
```

then the installer worked, but `/tmp/hermes-tool-slimmer` is an old checkout. Update or replace the checkout before reinstalling:

```bash
cd "$HOME"
if [ -d "$HOME/hermes-tool-slimmer/.git" ]; then
  cd "$HOME/hermes-tool-slimmer"
  git pull --ff-only
else
  git clone https://github.com/alias8818/hermes-tool-slimmer.git "$HOME/hermes-tool-slimmer"
  cd "$HOME/hermes-tool-slimmer"
fi

HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes" bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
$HOME/.hermes/hermes-agent/venv/bin/hermes tool-slimmer doctor
```

If Tool Slimmer was installed from the Hermes dashboard and shows `Source: git`, use the dashboard **Git pull** / **Update** action instead. If it shows `Source: user`, use the terminal path above.

## Installer script is blocked

If Hermes or an agent says the Tool Slimmer repo downloaded correctly but the installer script was blocked, this is usually an execution approval issue. Run the same command from a normal terminal on the Hermes machine:

```bash
bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
```

If the repo is somewhere else, replace `$HOME/hermes-tool-slimmer` with that path. Avoid running installer scripts from a predictable shared `/tmp` checkout. The installer performs the remaining normal steps: package install, dashboard plugin copy, plugin enablement, selector-hook patch check, service restart, and `hermes tool-slimmer doctor`.

Using `bash ...` avoids needing the script executable bit. If the environment still blocks it, approve that exact command in the approval prompt.

If `hermes tool-slimmer doctor` says `invalid choice: 'tool-slimmer'`, check whether you are using the source-checkout launcher instead of the venv launcher. Prefer:

```bash
$HOME/.hermes/hermes-agent/venv/bin/hermes tool-slimmer doctor
```

For reinstall/repair, force that same launcher:

```bash
HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes" bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
```

If Hermes Agent is handling the install, tell it:

```text
The repo is downloaded. Continue by running:
HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes" bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
If this command is blocked, request approval for that exact command.
After it runs, verify with:
$HOME/.hermes/hermes-agent/venv/bin/hermes tool-slimmer doctor
```

## Hermes update removed active slimming

On Hermes v0.19 and newer, Tool Slimmer slims requests through Hermes' built-in `llm_request` middleware, so Hermes updates no longer remove active slimming. Upgrade Tool Slimmer to v0.7.0+ if an older release reports `Hermes selector hook is unavailable; tool-slimmer will run diagnostics only`.

On older Hermes releases, updates can replace the files that Tool Slimmer patches for active schema selection. If `hermes tool-slimmer doctor` warns that no selector surface is available after updating Hermes, run:

```bash
bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"
```

Do not manually apply `docs/hermes-core-selector-hook.patch` for a normal install. That file is an upstream patch artifact for Hermes core development; it must be applied from a matching Hermes checkout and may not match released source layouts. The installer contains the compatibility patcher used for released Hermes versions.

For future Hermes updates, use the update-and-repair helper from the Tool Slimmer repo:

```bash
scripts/update-hermes-and-repair-tool-slimmer.sh
```

It runs `hermes update --yes` so Hermes does not wait for a keypress at the stash-restore prompt, keeps the default pre-update backup unless you pass `--no-backup`, reruns the Tool Slimmer installer, restarts services, and prints the doctor report.

To make this repair automatic after reboot/login, enable the guarded self-heal unit:

```bash
scripts/self-heal-tool-slimmer.sh --install-systemd
```

It does not update Hermes or Tool Slimmer. It only reruns the local repair installer when `doctor` confirms Tool Slimmer is enabled and the selector hook is missing. Remove it with:

```bash
scripts/self-heal-tool-slimmer.sh --uninstall-systemd
```

## Dashboard savings look too high

Dashboard savings are estimated schema-token savings, not guaranteed billable-token savings. Tool Slimmer computes them from serialized tool-schema JSON bytes divided by 4 before and after selection. Actual provider input-token and billing deltas can differ because tokenizers, prompt caching, system prompts, conversation history, and provider-specific tool serialization are outside this estimate.

The dashboard headline excludes probe/test events that do not have a Hermes `session_id`. Use the dashboard API's `all_summary` field when you need a full audit of every logged decision.

## No reduction occurs

Run `hermes tool-slimmer doctor`. If the core selector hook is unavailable, Hermes Tool Slimmer can benchmark and log dry-run decisions but cannot replace schemas sent to providers.

If `doctor`, `status`, or recent decisions mention `native_hermes_tool_search_active`, Hermes has already replaced deferrable MCP/plugin schemas with its native `tool_search` / `tool_describe` / `tool_call` bridge. Tool Slimmer intentionally skips active slimming for that request so it does not remove the bridge Hermes needs to reach deferred tools. This is expected on newer Hermes builds when Hermes' own tool schema threshold is crossed.

On Hermes Agent v0.14.0, use Tool Slimmer v0.4.0 or newer and rerun `scripts/install-hermes-tool-slimmer.sh` so the installer applies the modular core patch. Older Tool Slimmer releases targeted the previous `run_agent.py` request path and will not actively slim v0.14.0 provider requests.

Also check `tool_slimmer.min_total_tools` and `tool_slimmer.min_estimated_reduction_percent`. By default, Tool Slimmer ranks even small catalogs (`min_total_tools: 0`) so subagents and restricted toolsets still benefit, then skips ranked selections under 5% estimated schema reduction. Raise `min_total_tools` only for cron/small-toolset paths where the overhead is not worth the tiny savings.

In `anthropic_tool_search` mode, reduction metrics and the reduction guardrail use the hot set of immediately loaded tools. The dashboard still records Anthropic payload and deferred-tool counts so you can confirm defer loading is active.

If the standalone `tool_slimmer_select` tool reports `no_schemas_available`, rebuild the index from the dashboard or run it inside a Hermes process where live tool definitions are importable. The tool reports `schema_source` as `provided`, `live`, or `index` when selection succeeds.

## Tool index looks stale

Open the Tool Slimmer dashboard page and click **Rebuild From Hermes Tools**. The card shows the index path, count, checksum, and last-updated time. The live selector still ranks the current request's tool schemas in memory, so a stale persisted index affects visibility and troubleshooting, not request-time safety.

## A required tool is missing

Ask the model to call `tool_slimmer_request_full_tools`, or add the tool to `tool_slimmer.always_include` if it should stay loaded on every request. The fallback tool marks the conversation so the next model request receives the full Hermes schema list and can retry the original action. The selector never resurrects tools that Hermes already disabled.

In keyword mode, the selector mostly matches text present in tool names, toolsets, descriptions, and parameter schemas. It includes a small built-in synonym map for common browser/navigation wording, but domain-specific synonyms should still be added to tool descriptions or handled by a semantic selector mode when one is available.

`hybrid` mode adds a deterministic fuzzy-token boost on top of keyword ranking. It is intended for close spelling/wording misses, not broad semantic reasoning.

`always_include` is intentionally outside the `top_k` budget. For example, five always-included tools plus `top_k: 8` can return up to thirteen selected tools.

`top_k: 0` is a valid explicit setting for selecting no ranked tools. It does not trigger fail-open by itself.

If simple text-only messages like `hello`, `ping`, or `thanks` still show high baseline tokens, separate tool-schema overhead from Hermes' system prompt, skills, platform context, and conversation history. Tool Slimmer can reduce the schema portion, but it cannot remove non-tool prompt content. Low-information messages keep only `always_include` plus `tool_slimmer_request_full_tools`.

If a tool is noisy in your deployment, add it to `tool_slimmer.always_exclude` (alias for `disabled_tools`). Example:

```yaml
tool_slimmer:
  top_k: 4
  always_include: [memory]
  always_exclude: [terminal, cronjob]
```

Use this only when that entry point should not receive those tools through Tool Slimmer ranking. The full-tool fallback remains available when Hermes has registered it.

## Experimental two-pass mode

Use `mode: two_pass` only when a deployment has very large tool catalogs or providers with tight TPM limits. It sends a compact catalog first, then relies on `tool_slimmer_hydrate_tools` to request full schemas for multiple tools in one batch. The next request exposes those full schemas and can cache them for the session.

If two-pass does not expose the expected tool, check recent dashboard events for `two_pass_requested_tools`, `two_pass_hydrated_tools`, and `two_pass_phase`. If `tool_slimmer_hydrate_tools` is missing from Hermes' registered tools, two-pass falls back to keyword mode when `two_pass.fallback_to_keyword: true`.

Example:

```yaml
tool_slimmer:
  mode: two_pass
  always_include: [memory]
  two_pass:
    hydrate_limit: 8
    cache_hydrated_tools: true
    fallback_to_keyword: true
```

## Selector errors

Keep `fail_open: true` for normal use. Errors then preserve the original full schema list.
