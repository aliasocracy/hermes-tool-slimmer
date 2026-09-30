# Privacy And Logging

Hermes Tool Slimmer does not write raw user prompts to its decision log.

Every mode except `jev` runs entirely locally. With `mode: jev`, each user turn's request text and the names and descriptions of up to `jev.shortlist` candidate tools are sent to TypeSafe (`jev.base_url`, default `https://api.typesafe.ai`) to rank them. Conversation history, tool results, and memory are not sent. See TypeSafe's [data handling](https://docs.typesafe.ai/models#data-handling) terms before enabling it.

Decision events are stored at `$HERMES_HOME/tool-slimmer/decisions.jsonl` when `tool_slimmer.log_decisions: true`. Each event has three top-level fields:

- `timestamp`
- `metrics`
- `context`

`context` may include provider, model, platform, session ID, dry-run state, and schema count.

`metrics` includes selector mode, tool counts, schema byte/token estimates, selected tool names, always-included tool names, skip/fail-open reasons, selector timing, score details, top candidates, and expanded query tokens.

Dashboard headline totals exclude probe/test events without a `session_id`. Full audit data remains available through the dashboard API and local decision log.

Run this for the exact field inventory in the installed version:

```bash
hermes tool-slimmer privacy
```
