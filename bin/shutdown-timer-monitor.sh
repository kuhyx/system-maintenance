#!/bin/bash
# Shutdown timer monitor script
# Watches the day-specific-shutdown timer and re-enables it if disabled
# This file is installed by setup_midnight_shutdown.sh

set -euo pipefail

LOG_FILE="/var/log/shutdown-timer-monitor.log"
TIMER_NAME="day-specific-shutdown.timer"
SERVICE_NAME="day-specific-shutdown.service"
CHECK_INTERVAL=30

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

# Log with timestamp (shutdown-timer-monitor specific)
log_message() {
	local _ts
	local msg
	printf -v _ts '%(%Y-%m-%d %H:%M:%S)T' -1
	printf -v msg '%s [shutdown-monitor] %s' "$_ts" "$1"
	printf '%s\n' "$msg" >&2
	printf '%s\n' "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# Function to check if timer needs to be re-enabled
timer_needs_restoration() {
	# Check if timer is enabled
	if ! systemctl is-enabled "$TIMER_NAME" &>/dev/null; then
		log_message "Timer $TIMER_NAME is not enabled"
		return 0
	fi

	# Check if timer is active
	if ! systemctl is-active "$TIMER_NAME" &>/dev/null; then
		log_message "Timer $TIMER_NAME is not active"
		return 0
	fi

	# Check if timer unit file exists
	if [[ ! -f "/etc/systemd/system/$TIMER_NAME" ]]; then
		log_message "Timer unit file missing: /etc/systemd/system/$TIMER_NAME"
		return 0
	fi

	# Check if service unit file exists
	if [[ ! -f "/etc/systemd/system/$SERVICE_NAME" ]]; then
		log_message "Service unit file missing: /etc/systemd/system/$SERVICE_NAME"
		return 0
	fi

	# Check if check script exists
	if [[ ! -f "/usr/local/bin/day-specific-shutdown-check.sh" ]]; then
		log_message "Check script missing: /usr/local/bin/day-specific-shutdown-check.sh"
		return 0
	fi

	return 1 # Timer is properly configured
}

# Function to restore timer
restore_timer() {
	log_message "Shutdown timer tampering detected - initiating restoration"

	# Reload systemd daemon in case unit files were modified
	systemctl daemon-reload

	# Re-enable timer if disabled
	if ! systemctl is-enabled "$TIMER_NAME" &>/dev/null; then
		log_message "Re-enabling $TIMER_NAME"
		systemctl enable "$TIMER_NAME" 2>/dev/null || true
	fi

	# Re-start timer if not active
	if ! systemctl is-active "$TIMER_NAME" &>/dev/null; then
		log_message "Re-starting $TIMER_NAME"
		systemctl start "$TIMER_NAME" 2>/dev/null || true
	fi

	# Verify restoration
	if systemctl is-active "$TIMER_NAME" &>/dev/null; then
		log_message "Timer restoration completed successfully"
	else
		log_message "WARNING: Timer restoration may have failed"
	fi
}

# Function to monitor timer with systemd events
monitor_with_dbus() {
	log_message "Starting shutdown timer monitoring with D-Bus events"
	local last_check_ts=0

	# Use busctl to monitor systemd unit changes
	# Fall back to polling if this fails.
	if command -v busctl &>/dev/null; then
		# Monitor for unit state changes
		busctl monitor --system org.freedesktop.systemd1 2>/dev/null |
			while read -r line; do
				# Check if the line mentions our timer
				if [[ $line == *"$TIMER_NAME"* || $line == *"$SERVICE_NAME"* ]]; then
					local now_ts
					current_epoch now_ts
					if (( now_ts - last_check_ts < CHECK_INTERVAL )); then
						continue
					fi
					last_check_ts=$now_ts
					log_message "Systemd event detected for shutdown timer"
					if timer_needs_restoration; then
						restore_timer
					fi
				fi
			done
	else
		log_message "busctl not available, falling back to polling"
		monitor_with_polling
	fi
}

# Function to monitor with polling (primary method for reliability)
monitor_with_polling() {
	log_message "Starting shutdown timer monitoring with polling (interval: ${CHECK_INTERVAL}s)"

	while true; do
		if timer_needs_restoration; then
			restore_timer
		fi
		wait_seconds "$CHECK_INTERVAL"
	done
}

start_monitoring() {
	log_message "=== Shutdown Timer Monitor Started ==="
	log_message "Monitoring timer: $TIMER_NAME"
	log_message "Monitoring service: $SERVICE_NAME"

	# Initial check
	if timer_needs_restoration; then
		log_message "Initial check: Timer needs restoration"
		restore_timer
	else
		log_message "Initial check: Timer is properly configured"
	fi

	# Prefer D-Bus monitoring, with polling as the fallback path.
	if command -v busctl &>/dev/null; then
		monitor_with_dbus
	else
		log_message "busctl not available, falling back to polling"
		monitor_with_polling
	fi
}

# Main execution
if [[ ${SHUTDOWN_TIMER_MONITOR_SKIP_MAIN:-0} -ne 1 ]]; then
	start_monitoring
fi
