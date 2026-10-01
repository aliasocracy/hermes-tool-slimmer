# Guided Setup

This page is for people who want Tool Slimmer installed and tuned with the fewest manual steps.

## Easiest Path

1. Open the Hermes dashboard.
2. Go to **Plugins**.
3. Install from this repo:

   ```text
   aliasocracy/hermes-tool-slimmer
   ```

4. Restart Hermes when the dashboard asks.
5. Open **Tool Slimmer** in the dashboard.
6. Click **Rebuild From Hermes Tools**.
7. Click **Apply Recommended Config** in **Guided Setup**.
8. Restart the gateway once more so active requests use the new config.

The dashboard makes a backup before writing config. The backup path is shown after apply.

## If Hermes Agent Is Installing It

Paste this prompt into Hermes Agent:

```text
Install Hermes Tool Slimmer from https://github.com/aliasocracy/hermes-tool-slimmer.

Use the Hermes virtualenv launcher, not a system Python launcher:
HERMES_BIN="$HOME/.hermes/hermes-agent/venv/bin/hermes" bash "$HOME/hermes-tool-slimmer/scripts/install-hermes-tool-slimmer.sh"

If the repo is not downloaded yet, clone it into $HOME/hermes-tool-slimmer first. If that directory already exists and is a git checkout, run git pull --ff-only there before installing. Do not use an old /tmp/hermes-tool-slimmer checkout.
If the environment blocks direct script execution, request approval for that exact bash command.

After install:
1. Run $HOME/.hermes/hermes-agent/venv/bin/hermes tool-slimmer doctor
2. Open the dashboard Tool Slimmer page
3. Rebuild the tool index
4. Apply the recommended config from Guided Setup
5. Restart the Hermes gateway only after the install and config apply finish

Report the doctor result, the dashboard index tool count, and any backup path created.
```

## What The Recommended Config Does

The advisor keeps the normal CLI profile conservative and creates narrower profiles for chatty entry points:

- **telegram**: fewer ranked tools, keeps memory and the full-tool fallback, excludes noisy terminal/cron tools when present.
- **slack**: moderate budget, keeps memory and file-search helpers hot.
- **cli**: normal `top_k: 8` with core file tools.
- **cron**: larger budget because scheduled tasks often need execution and file context.
- **webhook**: conservative budget with fallback available.

Profiles are only applied when Hermes passes that platform name to the selector. If Hermes does not send platform metadata, the base config is used.

## Undo

If a recommended config makes your setup worse:

1. Open Tool Slimmer in the dashboard.
2. Find the backup path shown after **Apply Recommended Config**.
3. Restore it from a terminal:

   ```bash
   hermes tool-slimmer advisor --rollback /path/to/config-backup.yaml
   ```

4. Restart the Hermes gateway.

If you do not have the backup path, backups are stored under:

```text
~/.hermes/tool-slimmer/backups/
```

## When To Rebuild Or Reapply

Use **Rebuild From Hermes Tools** after:

- installing or removing a Hermes plugin
- adding or removing MCP servers
- updating Hermes
- seeing the dashboard tool count differ from the TUI by more than one tool

Use **Apply Recommended Config** again after a rebuild if the tool catalog changed meaningfully.

## Good First Test

After setup, send a few simple requests through the Hermes entry point you use most:

```text
hello
search this repo for plugin registration
edit this file
run a short python script
browse to a website
```

Then open the dashboard. You should see recent decisions, the full-tool fallback available, and low-information messages such as `hello` selecting only always-on tools.
