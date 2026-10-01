#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_NAME="tool-slimmer"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN_FROM_ENV="${HERMES_BIN:-}"
HERMES_BIN_EXPLICIT=0
RESTART_SERVICES=1
INSTALL_SYSTEMD=0
UNINSTALL_SYSTEMD=0
FORCE_REPAIR=0

default_hermes_bin() {
  local venv_bin="$HERMES_HOME/hermes-agent/venv/bin/hermes"
  if [[ -x "$venv_bin" ]]; then
    printf '%s\n' "$venv_bin"
    return
  fi
  command -v hermes || true
}

if [[ -n "$HERMES_BIN_FROM_ENV" ]]; then
  HERMES_BIN_EXPLICIT=1
fi
HERMES_BIN="${HERMES_BIN_FROM_ENV:-$(default_hermes_bin)}"

usage() {
  cat <<'USAGE'
Self-heal Hermes Tool Slimmer after a reboot or Hermes update.

Usage:
  scripts/self-heal-tool-slimmer.sh [options]

Options:
  --install-systemd   Install and enable the user systemd self-heal unit.
  --uninstall-systemd Remove the user systemd self-heal unit.
  --force-repair      Run the repair installer even if doctor currently passes.
  --no-restart        Do not restart active Hermes services after a repair.
  --hermes-bin PATH   Hermes executable to use. Defaults to ~/.hermes/hermes-agent/venv/bin/hermes when present.
  --hermes-home PATH  Hermes home directory. Defaults to ~/.hermes.
  -h, --help          Show this help.

Guardrails:
  - Does not run `hermes update`, git pull, or any network update.
  - Does not repair unless Tool Slimmer is enabled and the selector hook is missing.
  - Restarts Hermes services only after a repair and only when those services are active.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-systemd)
      INSTALL_SYSTEMD=1
      shift
      ;;
    --uninstall-systemd)
      UNINSTALL_SYSTEMD=1
      shift
      ;;
    --force-repair)
      FORCE_REPAIR=1
      shift
      ;;
    --no-restart)
      RESTART_SERVICES=0
      shift
      ;;
    --hermes-bin)
      HERMES_BIN="${2:-}"
      HERMES_BIN_EXPLICIT=1
      shift 2
      ;;
    --hermes-home)
      HERMES_HOME="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$HERMES_BIN_EXPLICIT" != "1" ]]; then
  HERMES_BIN="$(default_hermes_bin)"
fi

step() {
  printf '\n==> %s\n' "$1"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

reject_systemd_value() {
  local name="$1"
  local value="$2"
  case "$value" in
    *$'\n'*|*$'\r'*|*'"'*)
      fail "$name contains characters that are unsafe for a generated systemd unit"
      ;;
  esac
}

unit_path() {
  printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/hermes-tool-slimmer-self-heal.service"
}

install_systemd_unit() {
  command -v systemctl >/dev/null 2>&1 || fail "systemctl is not available"
  [[ -n "$HERMES_BIN" ]] || fail "Hermes executable not found. Pass --hermes-bin PATH."
  [[ -x "$HERMES_BIN" ]] || fail "Hermes executable is not executable: $HERMES_BIN"
  HERMES_BIN="$(readlink -f "$HERMES_BIN")"
  ROOT_DIR="$(readlink -f "$ROOT_DIR")"
  reject_systemd_value "ROOT_DIR" "$ROOT_DIR"
  reject_systemd_value "HERMES_BIN" "$HERMES_BIN"
  reject_systemd_value "HERMES_HOME" "$HERMES_HOME"
  mkdir -p "$(dirname "$(unit_path)")"
  cat >"$(unit_path)" <<EOF
[Unit]
Description=Hermes Tool Slimmer self-heal
Documentation=https://github.com/aliasocracy/hermes-tool-slimmer
After=default.target
Before=hermes-gateway.service hermes-dashboard.service

[Service]
Type=oneshot
Environment="HERMES_HOME=$HERMES_HOME"
ExecStart=/usr/bin/env bash "$ROOT_DIR/scripts/self-heal-tool-slimmer.sh" --hermes-bin "$HERMES_BIN" --hermes-home "$HERMES_HOME"

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable hermes-tool-slimmer-self-heal.service
  echo "Installed: $(unit_path)"
  echo "Run now: systemctl --user start hermes-tool-slimmer-self-heal.service"
}

uninstall_systemd_unit() {
  command -v systemctl >/dev/null 2>&1 || fail "systemctl is not available"
  systemctl --user disable --now hermes-tool-slimmer-self-heal.service >/dev/null 2>&1 || true
  rm -f "$(unit_path)"
  systemctl --user daemon-reload
  echo "Removed: $(unit_path)"
}

if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
  install_systemd_unit
  exit 0
fi

if [[ "$UNINSTALL_SYSTEMD" == "1" ]]; then
  uninstall_systemd_unit
  exit 0
fi

[[ -n "$HERMES_BIN" ]] || fail "Hermes executable not found. Install Hermes or pass --hermes-bin PATH."
[[ -x "$HERMES_BIN" ]] || fail "Hermes executable is not executable: $HERMES_BIN"

HERMES_BIN="$(readlink -f "$HERMES_BIN")"
HERMES_VENV_DIR="$(dirname "$HERMES_BIN")"
HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_VENV_DIR/python}"
[[ -x "$HERMES_PYTHON" ]] || HERMES_PYTHON="${HERMES_VENV_DIR}/python3"
[[ -x "$HERMES_PYTHON" ]] || fail "Could not find Hermes Python next to $HERMES_BIN"

export HERMES_HOME

step "Checking Tool Slimmer health"
echo "Hermes: $HERMES_BIN"
echo "Hermes home: $HERMES_HOME"

DOCTOR_JSON="$("$HERMES_BIN" tool-slimmer doctor 2>/dev/null || true)"
ACTION="$(DOCTOR_JSON="$DOCTOR_JSON" "$HERMES_PYTHON" - <<'PY'
import json
import os
import sys

raw = os.environ.get("DOCTOR_JSON", "").strip()
try:
    data = json.loads(raw)
except Exception:
    print("skip:doctor_unavailable")
    raise SystemExit(0)

checks = data.get("checks") if isinstance(data, dict) else {}
if not isinstance(checks, dict):
    print("skip:doctor_malformed")
    raise SystemExit(0)

plugin_status = checks.get("plugin_enabled", {}).get("status")
hook_status = checks.get("core_selector_hook", {}).get("status")

if plugin_status != "pass":
    print("skip:plugin_not_enabled")
elif hook_status == "pass":
    print("ok")
elif hook_status in {"warn", "fail", "unknown", None}:
    print("repair")
else:
    print(f"skip:unexpected_hook_status:{hook_status}")
PY
)"

if [[ "$FORCE_REPAIR" == "1" ]]; then
  ACTION="repair"
fi

case "$ACTION" in
  ok)
    echo "Tool Slimmer is healthy; no repair needed."
    exit 0
    ;;
  repair)
    echo "Tool Slimmer selector hook needs repair."
    ;;
  skip:*)
    echo "Skipping repair (${ACTION#skip:})."
    exit 0
    ;;
  *)
    echo "Skipping repair (unexpected action: $ACTION)."
    exit 0
    ;;
esac

step "Repairing Tool Slimmer"
bash "$ROOT_DIR/scripts/install-hermes-tool-slimmer.sh" \
  --hermes-bin "$HERMES_BIN" \
  --hermes-home "$HERMES_HOME" \
  --no-restart

if [[ "$RESTART_SERVICES" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  step "Restarting active Hermes services"
  for service in hermes-gateway.service hermes-dashboard.service; do
    if systemctl --user is-active --quiet "$service"; then
      systemctl --user restart "$service" || true
      echo "Restarted: $service"
    else
      echo "Not active: $service"
    fi
  done
fi

step "Self-heal complete"
"$HERMES_BIN" tool-slimmer doctor
