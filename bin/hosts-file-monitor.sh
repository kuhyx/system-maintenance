#!/bin/bash
# Hosts file monitor script
# Watches /etc/hosts for changes and restores it if needed
# This file is installed by setup_periodic_system.sh

set -euo pipefail

LOG_FILE="/var/log/hosts-file-monitor.log"
HOSTS_FILE="/etc/hosts"
HOSTS_INSTALL_SCRIPT="__HOSTS_INSTALL_SCRIPT__"
readonly MIN_HOSTS_LINES=1000
readonly EVENT_COOLDOWN_S=5

current_epoch() {
  local out_var="${1:-}"
  if [[ -n $out_var ]]; then
    printf -v "$out_var" '%(%s)T' -1
  else
    printf '%(%s)T\n' -1
  fi
}

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

# Function to check if hosts file needs restoration
needs_restoration() {
  # Check if file exists
  if [[ ! -f $HOSTS_FILE ]]; then
    return 0 # File missing, needs restoration
  fi

  # Check if file is empty or too small (less than MIN_HOSTS_LINES indicates tampering)
  local line_count=0
  local has_custom_entries=0
  local has_stevenblack_entries=0
  local line=""

  while IFS= read -r line || [[ -n $line ]]; do
    line_count=$((line_count + 1))

    if [[ $has_custom_entries -eq 0 && $line == *"Custom blocking entries"* ]]; then
      has_custom_entries=1
    fi

    if [[ $has_stevenblack_entries -eq 0 && $line == *"StevenBlack"* ]]; then
      has_stevenblack_entries=1
    fi

    if (( line_count >= MIN_HOSTS_LINES && has_custom_entries == 1 && has_stevenblack_entries == 1 )); then
      return 1 # File seems intact
    fi
  done < "$HOSTS_FILE"

  if [[ $line_count -lt $MIN_HOSTS_LINES ]]; then
    return 0 # File too small, likely tampered with
  fi

  # Check if our custom entries are missing
  if [[ $has_custom_entries -eq 0 ]]; then
    return 0 # Our custom entries missing, needs restoration
  fi

  # Check if StevenBlack entries are missing
  if [[ $has_stevenblack_entries -eq 0 ]]; then
    return 0 # StevenBlack entries missing, needs restoration
  fi

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

# Function to monitor with inotifywait
monitor_with_inotify() {
  log_message "Starting hosts file monitoring with inotify"
  local last_check_ts=0

  # Monitor the hosts file and its directory for various events
  inotifywait -m -e delete,move,modify,attrib,create --format '%w%f %e %T' --timefmt '%Y-%m-%d %H:%M:%S' "$HOSTS_FILE" /etc/ 2> /dev/null |
    while read -r file event time; do
      # Check if the event is related to our hosts file
      if [[ $file == "$HOSTS_FILE" ]] || [[ $file == "/etc/hosts" ]]; then
        local now_ts
        current_epoch now_ts
        if (( now_ts - last_check_ts < EVENT_COOLDOWN_S )); then
          continue
        fi
        last_check_ts=$now_ts

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
