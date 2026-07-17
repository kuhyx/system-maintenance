#!/bin/bash
# Periodic system maintenance script
# Installs pacman/makepkg wrappers and updates hosts file
# This file is installed by setup_periodic_system.sh
#
# FAILURES ARE LOUD, ON PURPOSE.
# This script used to swallow every failure: a missing repair script was one
# line appended to a log nobody reads, execute_with_log returned 0, and the
# systemd unit therefore reported SUCCESS. That is exactly how a stale path
# (from the 2026-05-15 repo reorg) silently disabled the pacman-wrapper and
# hosts self-repair for five months without a single visible symptom.
#
# Any failure now surfaces four ways:
#   1. journal at ERROR priority   -> journalctl -p err -t periodic-system-maintenance
#   2. the systemd unit FAILS      -> systemctl --failed / red timer (exit 1 below)
#   3. a critical desktop notification that does not auto-dismiss
#   4. a breadcrumb file           -> /var/lib/periodic-system-maintenance/LAST_RUN_FAILED

set -e

LOG_FILE="/var/log/periodic-system-maintenance.log"
STATE_DIR="/var/lib/periodic-system-maintenance"
FAILURE_FLAG="$STATE_DIR/LAST_RUN_FAILED"

# Path placeholders replaced at install time
PACMAN_WRAPPER_INSTALL="__PACMAN_WRAPPER_INSTALL__"
MAKEPKG_WRAPPER_INSTALL="__MAKEPKG_WRAPPER_INSTALL__"
HOSTS_INSTALL_SCRIPT="__HOSTS_INSTALL_SCRIPT__"

declare -a FAILURES=()

# Function to log with timestamp
log_message() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Record a failure loudly: journal at ERROR priority + collect for the summary.
fail_loudly() {
  local msg="$1"
  log_message "FAILURE: $msg"
  echo "✗ $msg" >&2
  logger -t periodic-system-maintenance -p user.err "FAILURE: $msg"
  FAILURES+=("$msg")
}

# Desktop notification for the graphical user (this runs as root under systemd,
# so we have to reach the user's session bus explicitly).
notify_user() {
  local title="$1" body="$2"
  command -v notify-send >/dev/null 2>&1 || return 0
  local target_user target_uid
  target_user="$(loginctl list-sessions --no-legend 2>/dev/null | awk 'NF {print $3; exit}')"
  [[ -n $target_user ]] || return 0
  target_uid="$(id -u "$target_user" 2>/dev/null)" || return 0
  # -t 0 => never auto-dismiss; the user must acknowledge it.
  sudo -u "$target_user" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$target_uid/bus" \
    notify-send -u critical -t 0 "$title" "$body" 2>/dev/null || true
}

# Function to execute with logging
execute_with_log() {
  local script_path="$1"
  local script_name="$2"
  local rc=0

  log_message "Starting $script_name"
  echo "Executing $script_name..." >&2

  if [[ ! -f $script_path ]]; then
    fail_loudly "$script_name: script NOT FOUND at $script_path"
    return 0
  fi

  # `|| rc=$?` keeps set -e from aborting so every step still runs and we can
  # report ALL failures, not just the first.
  bash "$script_path" >> "$LOG_FILE" 2>&1 || rc=$?
  if ((rc == 0)); then
    log_message "$script_name completed successfully"
    echo "✓ $script_name completed successfully" >&2
  else
    fail_loudly "$script_name: FAILED (exit $rc) — see $LOG_FILE"
  fi
}

# Start maintenance
log_message "=== Periodic System Maintenance Started ==="

# Install pacman/makepkg wrappers
execute_with_log "$PACMAN_WRAPPER_INSTALL" "Pacman Wrapper Installation"
execute_with_log "$MAKEPKG_WRAPPER_INSTALL" "Makepkg Wrapper Installation"

# Update hosts file
execute_with_log "$HOSTS_INSTALL_SCRIPT" "Hosts File Update"

if ((${#FAILURES[@]} > 0)); then
  mkdir -p "$STATE_DIR"
  printf '%s\n' "${FAILURES[@]}" > "$FAILURE_FLAG"
  log_message "=== Periodic System Maintenance FAILED (${#FAILURES[@]} failure(s)) ==="
  echo "✗ Periodic system maintenance FAILED with ${#FAILURES[@]} failure(s):" >&2
  printf '  - %s\n' "${FAILURES[@]}" >&2
  notify_user "⚠ System maintenance FAILED" \
    "$(printf '%s\n' "${FAILURES[@]}")

journalctl -p err -t periodic-system-maintenance"
  # Non-zero => systemd marks the unit failed, so `systemctl --failed` and the
  # timer's status show it instead of a green "success".
  exit 1
fi

rm -f "$FAILURE_FLAG" 2>/dev/null || true
log_message "=== Periodic System Maintenance Completed ==="
echo "Periodic system maintenance completed. Check $LOG_FILE for details." >&2
