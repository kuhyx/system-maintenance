#!/bin/bash
# Hosts file monitor script
# Watches /etc/hosts for changes and restores it if needed
# This file is installed by setup_periodic_system.sh

set -euo pipefail

LOG_FILE="/var/log/hosts-file-monitor.log"
HOSTS_FILE="/etc/hosts"
HOSTS_INSTALL_SCRIPT="__HOSTS_INSTALL_SCRIPT__"
readonly MIN_HOSTS_LINES=1000
# Event drain: after an event, swallow already-queued lines until the pipe has
# been quiet for DRAIN_QUIET_S (or DRAIN_MAX_S passed), then run ONE check.
readonly DRAIN_QUIET_S=0.5
readonly DRAIN_MAX_S=10

wait_seconds() {
  local timeout_s=$1
  local start_ts end_ts elapsed_s remaining_s

  printf -v start_ts '%(%s)T' -1
  IFS= read -r -t "$timeout_s" || true
  printf -v end_ts '%(%s)T' -1

  elapsed_s=$((end_ts - start_ts))
  if (( elapsed_s < timeout_s )); then
    remaining_s=$((timeout_s - elapsed_s))
    sleep "$remaining_s"
  fi
}

# Log with timestamp (hosts-file-monitor specific)
log_message() {
  local _ts
  local msg
  printf -v _ts '%(%Y-%m-%d %H:%M:%S)T' -1
  printf -v msg '%s [hosts-monitor] %s' "$_ts" "$1"
  printf '%s\n' "$msg" >&2
  printf '%s\n' "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# Function to check if hosts file needs restoration.
# Uses grep (a few large reads) instead of a bash line loop: the file has ~170k
# lines, and every bash read() of /etc/hosts is an IN_ACCESS event for each
# watcher of /etc (tailscaled). FAIL CLOSED: any grep error (exit >= 2) or
# non-match means restore, never "intact".
needs_restoration() {
  if [[ ! -f $HOSTS_FILE ]]; then
    return 0 # File missing, needs restoration
  fi

  # '' matches every line, incl. a final unterminated one (same as the old loop).
  local line_count=0 grep_rc=0
  line_count=$(grep -c '' -- "$HOSTS_FILE") || grep_rc=$?
  if (( grep_rc > 1 )); then
    return 0 # grep error: fail closed
  fi
  if [[ ! $line_count =~ ^[0-9]+$ ]] || (( line_count < MIN_HOSTS_LINES )); then
    return 0 # File too small (or unreadable count), likely tampered with
  fi

  # -q stops at the first hit; rc 1 (absent) and rc >= 2 (error) both restore.
  grep -qF -- 'Custom blocking entries' "$HOSTS_FILE" || return 0
  grep -qF -- 'StevenBlack' "$HOSTS_FILE" || return 0

  return 1 # File seems intact
}

# Function to restore hosts file
restore_hosts_file() {
  log_message "Hosts file modification detected - initiating restoration"

  if [[ -f $HOSTS_INSTALL_SCRIPT ]]; then
    log_message "Running hosts installation script: $HOSTS_INSTALL_SCRIPT"

    if bash "$HOSTS_INSTALL_SCRIPT" >> "$LOG_FILE" 2>&1; then
      log_message "Hosts file restoration completed successfully"
    else
      log_message "Hosts file restoration failed with exit code $?"
    fi
  else
    log_message "ERROR: Hosts install script not found at $HOSTS_INSTALL_SCRIPT"
  fi
}

# Swallow every already-queued event line so a burst costs ONE check. Bounded
# by DRAIN_MAX_S so a constant trickle cannot starve the check. Events that
# arrive later stay in the pipe and trigger the next check: nothing is dropped.
drain_queued_events() {
  local deadline=$((SECONDS + DRAIN_MAX_S))
  while IFS= read -r -t "$DRAIN_QUIET_S" _; do
    if (( SECONDS >= deadline )); then
      break
    fi
  done
}

# Function to monitor with inotifywait
monitor_with_inotify() {
  log_message "Starting hosts file monitoring with inotify"

  # Monitor the hosts file and its directory for various events
  inotifywait -m -e delete,move,modify,attrib,create --format '%w%f %e %T' --timefmt '%Y-%m-%d %H:%M:%S' "$HOSTS_FILE" /etc/ 2> /dev/null |
    while read -r file event time; do
      # Check if the event is related to our hosts file
      if [[ $file == "$HOSTS_FILE" ]] || [[ $file == "/etc/hosts" ]]; then
        drain_queued_events

        log_message "Event detected: $event on $file at $time"

        # Check if restoration is needed
        if needs_restoration; then
          restore_hosts_file
        else
          log_message "Hosts file check passed - no restoration needed"
        fi
      fi
    done
}

# Function to monitor with polling (fallback)
monitor_with_polling() {
  log_message "Starting hosts file monitoring with polling (fallback method)"

  while true; do
    if needs_restoration; then
      restore_hosts_file
    fi

    # Check every 30 seconds
    wait_seconds 30
  done
}

start_monitoring() {
  log_message "=== Hosts File Monitor Started ==="

  if command -v inotifywait > /dev/null 2>&1; then
    log_message "Using inotify for file monitoring"
    monitor_with_inotify
  else
    log_message "inotify-tools not available, using polling method"
    log_message "Consider installing inotify-tools for better performance: pacman -S inotify-tools"
    monitor_with_polling
  fi
}

# Main execution
if [[ ${HOSTS_FILE_MONITOR_SKIP_MAIN:-0} -ne 1 ]]; then
  start_monitoring
fi
