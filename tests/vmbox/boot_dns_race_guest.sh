#!/bin/bash
# Guest half of tests/vmbox_boot_dns_race.sh. Runs as root inside a vmbox guest.
#
# setup <red|green> <unit-dir>: install the probe units from <unit-dir>, delay
#   the network by 8 s at boot (a slow DHCP/link), and set wait-online the way
#   the old (red: disabled, as the Arch perf fixers left it) or current
#   (green: enabled) host has it.
# assert: wait for boot to settle, print `systemctl --failed`, exit 1 if any
#   probe unit is in it.
set -euo pipefail

readonly PROBES=(reflector periodic-system-startup nftables-drift periodic-system-maintenance)
readonly WAIT_ONLINE=systemd-networkd-wait-online.service

setup() {
	local mode="$1" src="$2" name
	for name in "${PROBES[@]}"; do
		install -m 0644 "$src/probe-$name.service" "/etc/systemd/system/probe-$name.service"
	done
	install -d /etc/systemd/system/systemd-networkd.service.d
	printf '[Service]\nExecStartPre=/bin/sleep 8\n' >/etc/systemd/system/systemd-networkd.service.d/slow.conf
	systemctl daemon-reload
	for name in "${PROBES[@]}"; do systemctl enable -q "probe-$name.service"; done
	case "$mode" in
	red) systemctl disable -q "$WAIT_ONLINE" ;;
	green) systemctl enable -q "$WAIT_ONLINE" ;;
	*) echo "mode must be red or green" >&2; exit 2 ;;
	esac
	echo "setup $mode: wait-online $(systemctl is-enabled "$WAIT_ONLINE")"
}

assert_none_failed() {
	local failed name bad=0
	timeout 45 systemctl is-system-running --wait >/dev/null || true
	failed=$(systemctl --failed --no-legend --plain)
	printf -- '--- systemctl --failed\n%s\n' "$failed"
	for name in "${PROBES[@]}"; do
		if grep -q "probe-$name.service" <<<"$failed"; then
			echo "FAILED: $name"
			bad=1
		fi
	done
	((bad == 0)) && echo "PASS: no network-race unit in --failed"
	return "$bad"
}

case "${1:-}" in
setup) setup "$2" "$3" ;;
assert) assert_none_failed ;;
*) echo "usage: $0 setup <red|green> <dir> | assert" >&2; exit 2 ;;
esac
