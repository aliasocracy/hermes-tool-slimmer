# Changelog

## 0.8.0 - 2026-09-30

Experimental Jev ranking mode and Hermes plugin catalog readiness release.

### Added

- Experimental `mode: jev`: the keyword shortlist is re-ranked by TypeSafe's Jev decision model with one yes/no probability per candidate tool, answered in a single request. Tools at or above `jev.threshold` are kept, results are cached for the rest of the turn, and any Jev failure falls back to keyword selection with a cooldown. The API key is read from `TYPESAFE_API_KEY`.
- `doctor` reports whether the Jev API key is available, and `privacy` lists what `jev` mode sends to TypeSafe.
- The model-callable `tool_slimmer_select` tool cannot switch to `jev` unless it is already the configured mode, so the agent cannot start external calls on its own.

### Fixed

- `plugin.yaml` now declares the `llm_request` middleware that v0.7.0 registers on Hermes v0.19+, so `hermes plugins validate` (the Hermes plugin catalog admission check) passes. A test now compares what `register()` records against the manifest.

### Changed

- README reorganized around a quick start, how it works, modes, config, dashboard, and a docs index, with a Hermes compatibility table for the v0.19+ middleware path.
- Repository links in the README, docs, `pyproject.toml`, and the self-heal unit point at `aliasocracy/hermes-tool-slimmer`.

## 0.7.0 - 2026-09-30

Hermes v0.19+ selector compatibility and cleanup release.

### Added

- Active slimming through Hermes' built-in `llm_request` middleware (Hermes v0.19+), so current Hermes releases no longer need the local `select_tool_schemas` core patch and Hermes updates or container rebuilds no longer silently turn Tool Slimmer into diagnostics-only mode (#5). The adapter handles OpenAI chat, Anthropic messages, and Codex responses payloads, keeps provider server tools and forced `tool_choice` targets, and keeps the tool prompt-cache breakpoint on the last kept tool.
- `doctor` and `status` now report the active selector surface (`select_tool_schemas_hook` or `llm_request_middleware`).

### Fixed

- A core whose `VALID_HOOKS` advertises `select_tool_schemas` but whose turn loop never calls it (a partially applied core patch) is now detected, and the middleware is used instead of registering a hook that never fires.
- The installer no longer aborts on Hermes layouts it cannot patch, skips core patching when the middleware is available, handles multi-entry `VALID_HOOKS` lines, and writes `hermes_cli/plugins.py` only after the turn-loop patch succeeds.
- Advisor apply and tool-preference changes refuse to rewrite an unparseable Hermes `config.yaml` instead of replacing it with only the Tool Slimmer section.
- Dashboard rollback only restores files from the Tool Slimmer backup directory, and same-second advisor edits no longer overwrite each other's backups.
- Dashboard index rebuild now prefers the larger of the dashboard process catalog and the last live gateway snapshot, as documented.
- Two-pass hydration keeps Anthropic-shaped `input_schema` tools intact.
- Hermes home resolves like Hermes itself (including `%LOCALAPPDATA%\hermes` on Windows), and config/index files are read and written as UTF-8. Index snapshots are written atomically and only rewritten when the catalog or request context changes.
- The root `dashboard/` bundle had drifted from the installed `dashboard-plugin/` copy; both are identical again and a test now enforces it.
- Diagnostics live snapshots now report `has_session_id` correctly.
- The two-pass hydration cache is bounded to recent sessions.

## 0.6.5 - 2026-06-05

Post-Hermes-update compatibility and packaging guardrail release.

### Added

- Added dashboard asset, package asset, and schema-contract regression checks inspired by review of the `rzyns/hermes-tool-slimmer` fork. These checks guard the shipped dashboard/plugin layout without adopting the fork's experimental progressive or semantic ranking code.

### Fixed

- `hermes tool-slimmer select` now falls back to the persisted tool index, then live snapshots, when `--schemas` is not supplied.
- Wheel builds now include the dashboard and dashboard-plugin files required by Hermes dashboard/plugin installs.

## 0.6.4 - 2026-05-30

Installer environment override repair.

### Fixed

- Installer, updater, self-heal, and troubleshooting scripts now treat an environment-provided `HERMES_BIN` as an explicit trusted binary, matching the documented `HERMES_BIN=... bash ...` install flow and avoiding fallback to `command -v hermes`.

## 0.6.3 - 2026-05-30

Security hardening release.

### Fixed

- Enforces disabled tool, disabled toolset, MCP/native origin, and malformed-schema policy consistently across keyword, two-pass, Anthropic Tool Search, full-tool fallback, and guardrail skip paths.
- Keeps global disabled-tool policy when applying platform profiles or Guided Setup recommendations.
- Restricts model-callable `tool_slimmer_select` so it no longer reads live/indexed catalogs unless explicitly opted in and no longer accepts `mode: eager`.
- Stops logging prompt-derived expanded query tokens; decision logs now store only the expanded-query token count.
- Redacts live snapshot summaries returned to the dashboard and refuses stale live-schema snapshots by default.
- Avoids fixed `/tmp/hermes-tool-slimmer` installer commands in docs, prevents installer raw-decision output, and hardens self-heal systemd unit generation.

## 0.6.1 - 2026-05-30

Install and support diagnostics repair release.

### Added

- `hermes tool-slimmer diagnostics` emits a sanitized GitHub-issue support report without raw prompts, environment secrets, or session IDs.
- Dashboard API exposes the same sanitized diagnostics at `/diagnostics`.

### Fixed

- Installer-based dashboard/user-plugin installs now include a bundled `src/hermes_tool_slimmer` fallback so the dashboard can import the matching plugin package even when Hermes dashboard runs under a different Python launcher.
- Normal install docs and doctor messages now point users back to the installer compatibility patcher instead of asking them to manually apply the upstream Hermes core patch artifact.

## 0.6.0 - 2026-05-29

Experimental two-pass schema hydration release.

### Added

- Experimental `mode: two_pass` with compact deterministic tool catalogs, batched schema hydration through `tool_slimmer_hydrate_tools`, session-scoped hydrated-tool caching, decision-log metrics, CLI/doctor/status visibility, and dashboard diagnostics.

## 0.5.3 - 2026-05-29

Dashboard git-install repair release.

### Fixed

- Dashboard git installs now include the root dashboard bundle assets expected by Hermes' `/dashboard-plugins/tool-slimmer/dist/...` static routes.
- Git-installed dashboard/API loading can import the repo-local `src/hermes_tool_slimmer` package before the repair installer has installed the Python package into the Hermes venv.

## 0.5.2 - 2026-05-28

Hermes update repair release.

### Added

- `scripts/update-hermes-and-repair-tool-slimmer.sh` to run `hermes update --yes`, preserve Hermes' normal backup behavior by default, rerun Tool Slimmer repair, and restart services after Hermes updates.
- `scripts/self-heal-tool-slimmer.sh` with an optional user systemd unit for guarded boot/login repair when Tool Slimmer is enabled but the Hermes selector hook is missing.

### Tested

- Verified Hermes Agent update from v0.14.0 to v0.15.0 with default backup behavior and noninteractive `--yes` prompt handling.
- Verified post-update Tool Slimmer repair on Hermes v0.15.0 and all-pass doctor after gateway/dashboard restart.
- Verified the self-heal systemd unit installs, starts, exits cleanly in healthy no-op mode, and leaves gateway/dashboard active.

## 0.5.1 - 2026-05-28

Live snapshot clarity release.

### Added

- Per-platform live schema snapshots for TUI, Slack, Telegram, and API server turns.
- Dashboard and CLI status context explaining which live request snapshot populated the persisted index.
- Dashboard snapshot chips so users can see why Hermes TUI and Tool Slimmer counts may differ by entry point.

### Tested

- Verified TUI, Slack, Telegram, and API server turns against live Hermes with full-tool fallback available.
- Smoke-tested a clean Hermes install on a disposable exe.dev VM, including installer patching, doctor, status, and eval.

## 0.5.0 - 2026-05-27

Guided setup and profile tuning release.

### Added

- Platform profiles for Telegram, Slack, CLI/TUI, cron, and webhook entry points.
- Advisor recommendations with plain-English setup checklist, recommended YAML, safe apply, config backups, and rollback support.
- Dashboard Guided Setup card with one-click recommended config apply and backup visibility.
- Low-information query handling so greetings, pings, thanks, and numeric nudges do not fill `top_k` with unrelated task tools.
- Beginner-friendly setup docs and an agent-install prompt for users who do not want to run shell commands manually.

### Changed

- `always_exclude` is accepted as a user-facing alias for `disabled_tools`.
- Dashboard status now exposes disabled tools, disabled toolsets, aliases, and profiles for easier troubleshooting.

## 0.4.7 - 2026-05-20

Missing skill-tool fallback release.

### Fixed

- Full-tool fallback now survives the first user retry after `tool_slimmer_request_full_tools`, covering the common chat flow where the model asks the user to send another message before retrying.
- Ambiguous retry messages now use recent assistant/tool mentions of known tool names, so a follow-up like `12` can still expose a recently requested tool such as `skill_view`.
- Skill companion tools are kept together: selecting `skill_manage` or skill-context requests also keeps `skill_view` and `skills_list` available when present.

## 0.4.6 - 2026-05-19

Dashboard git-install compatibility release.

### Changed

- The repository root now includes the Hermes runtime plugin entry point and dashboard files, matching Hermes dashboard git-install expectations.
- Git checkouts installed at `$HERMES_HOME/plugins/tool-slimmer` now stay clean after repair, so the dashboard can show `Source: git` and keep using `git pull`.
- Mypy configuration now uses explicit package bases so the root Hermes plugin entry point can coexist with the `src/` Python package layout.

## 0.4.5 - 2026-05-19

Dashboard installer compatibility release.

### Changed

- The installer now supports being run from an in-place dashboard git checkout at `$HERMES_HOME/plugins/tool-slimmer` by overlaying the runtime plugin files without deleting the checkout, preserving future `git pull` updates from the plugin page.
- Plugin and dashboard manifest versions now track the package release version.

## 0.4.4 - 2026-05-19

Dashboard index reliability release.

### Fixed

- Dashboard "Rebuild From Hermes Tools" now chooses the largest available runtime catalog between Hermes tool definitions and the last live request snapshot.
- Dashboard rebuild now preserves an existing larger index instead of replacing it with a smaller standalone catalog, preventing full gateway catalogs from shrinking after cron/subagent snapshots or incomplete standalone `model_tools` discovery.

## 0.4.3 - 2026-05-19

Installer reliability release.

### Changed

- Prefer the Hermes virtualenv launcher at `$HOME/.hermes/hermes-agent/venv/bin/hermes` when install and troubleshooting scripts need a Hermes executable.
- Document the venv launcher path for Hermes Agent-assisted installs and repairs.
- Run the troubleshooting script through `bash` from the installer so executable-bit restrictions do not block the final health report.

## 0.2.0 - 2026-05-15

Dashboard and operations release.

### Added

- Hermes dashboard plugin with status, health checks, recent selection decisions, selected-tool visibility, and estimated schema-token savings.
- Dashboard backend API routes for Tool Slimmer status, session-filtered summaries, full audit summaries, and raw recent events.
- Durable JSONL decision logging under `$HERMES_HOME/tool-slimmer/decisions.jsonl`.
- One-command local installer/repair script and deterministic troubleshooting report script.
- GitHub Actions test workflow plus README badges and professional README hero image.

### Changed

- Dashboard headline totals now exclude probe/test events without a Hermes `session_id`; full audit totals remain available as `all_summary`.
- README and docs now clearly label savings as estimated schema-token savings, not guaranteed billable-token savings.

### Tested

- Added tests for decision logging, session-filtered summary accounting, dashboard API routes, and existing selector/provider behavior.

## 0.1.0 - 2026-05-03

Initial public release.

### Added

- Hermes plugin entry point `tool-slimmer`.
- Deterministic tokenizer, corpus builder, local BM25 ranker, and selector.
- Config loader for `tool_slimmer` settings in Hermes config files.
- CLI commands for status, doctor, index, select, benchmark, and config recommendations.
- Slash command and JSON tool handlers.
- Metrics for schema byte/token reduction estimates.
- Anthropic Tool Search helpers with explicit provider capability gating.
- JSON index store with checksum-based rebuilds.
- Upstreamable Hermes core selector-hook patch artifact.
- Documentation, examples, and unit tests.
