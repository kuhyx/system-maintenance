#!/bin/bash
# Enables the system-level units the monitoring stack needs.
# Sourced by install_usage_monitoring.sh; inherits the caller's strict mode.

enable_unit() {
	local unit=$1
	if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
		log "enabling $unit"
		sudo systemctl enable --now "$unit" || log "warn: failed to enable $unit"
	else
		log "skip $unit (not present on this system)"
	fi
}

enable_system_services() {
	enable_unit atop.service
	# atop-rotate exists on Arch; Debian/Ubuntu rotate via cron instead.
	enable_unit atop-rotate.timer
	enable_unit netdata.service
}
