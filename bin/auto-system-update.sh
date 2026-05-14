#!/bin/bash
# Automatic system update script for Arch Linux
# Runs pacman -Syuu and yay -Sua non-interactively
# This file is installed by setup_periodic_system.sh

set -euo pipefail

readonly LOG_FILE="/var/log/auto-system-update.log"
readonly LOCK_FILE="/var/lock/auto-system-update.lock"
readonly ACTUAL_USER="__ACTUAL_USER__"

log_msg() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE" >&2
}

cleanup() {
  rm -f "$LOCK_FILE"
}

trap cleanup EXIT

# Prevent concurrent runs
if ! (set -o noclobber && echo $$ > "$LOCK_FILE") 2>/dev/null; then
  log_msg "Another update is already running (lock: $LOCK_FILE). Exiting."
  exit 0
fi

log_msg "=== Automatic System Update Started ==="

# --- Official repository update (pacman) ---
log_msg "Running pacman -Syuu --noconfirm ..."
if /usr/bin/pacman -Syuu --noconfirm >> "$LOG_FILE" 2>&1; then
  log_msg "pacman update completed successfully"
else
  log_msg "pacman update failed (exit $?)"
fi

# --- AUR update (yay) ---
# yay must not run as root; run as the actual user
if command -v /usr/bin/yay > /dev/null 2>&1; then
  log_msg "Running yay -Sua --noconfirm as $ACTUAL_USER ..."
  if sudo -u "$ACTUAL_USER" /usr/bin/yay -Sua --noconfirm 2>&1 | tee -a "$LOG_FILE" > /dev/null; then
    log_msg "yay AUR update completed successfully"
  else
    log_msg "yay AUR update failed (exit $?)"
  fi
else
  log_msg "yay not found, skipping AUR updates"
fi

log_msg "=== Automatic System Update Completed ==="
