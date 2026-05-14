#!/bin/bash
# Periodic system maintenance script
# Installs pacman wrapper and updates hosts file
# This file is installed by setup_periodic_system.sh

set -e

LOG_FILE="/var/log/periodic-system-maintenance.log"

# Path placeholders replaced at install time
PACMAN_WRAPPER_INSTALL="__PACMAN_WRAPPER_INSTALL__"
HOSTS_INSTALL_SCRIPT="__HOSTS_INSTALL_SCRIPT__"

# Function to log with timestamp
log_message() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Function to execute with logging
execute_with_log() {
  local script_path="$1"
  local script_name="$2"

  log_message "Starting $script_name"
  echo "Executing $script_name..." >&2

  if [[ -f $script_path ]]; then
    if bash "$script_path" >> "$LOG_FILE" 2>&1; then
      log_message "$script_name completed successfully"
      echo "✓ $script_name completed successfully" >&2
    else
      local ec=$?
      log_message "$script_name failed with exit code $ec"
      echo "✗ $script_name failed (exit $ec)" >&2
    fi
  else
    log_message "$script_name not found at $script_path"
    echo "✗ $script_name not found at $script_path" >&2
  fi
}

# Start maintenance
log_message "=== Periodic System Maintenance Started ==="

# Install pacman wrapper
execute_with_log "$PACMAN_WRAPPER_INSTALL" "Pacman Wrapper Installation"

# Update hosts file
execute_with_log "$HOSTS_INSTALL_SCRIPT" "Hosts File Update"

log_message "=== Periodic System Maintenance Completed ==="
echo "Periodic system maintenance completed. Check $LOG_FILE for details." >&2
