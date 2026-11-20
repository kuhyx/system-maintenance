#!/bin/bash
# vmbox check: on a boot where the network comes up late, none of the units
# that need it (reflector, periodic-system-startup, nftables-drift,
# periodic-system-maintenance) ends in `systemctl --failed`.
#
# The guest cannot run the real units (no NetworkManager, reflector, repo
# config), so each is a probe unit that copies the real unit's ordering and
# retry lines from the source of truth and swaps ExecStart for the condition
# the real one failed on at the 2026-11-20 08:22 boot: a LAN source address
# (nftables-drift: "Could not detect the LAN interface/IP") and working DNS
# (reflector, the hosts download). Steps are separate so each call stays
# under 60 s:
#   vmbox_boot_dns_race.sh push <guest> <red|green>
#   vmbox_boot_dns_race.sh reboot <guest>
#   vmbox_boot_dns_race.sh assert <guest>     # exit 1 = a probe failed
set -euo pipefail

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
readonly REPO_DIR
readonly TAM_DIR="${TAM_DIR:-$HOME/src/testsAndMisc}"
readonly REFLECTOR_UNIT=/usr/lib/systemd/system/reflector.service
readonly PROBE_EXEC="ExecStart=/bin/sh -c 'ip -4 route get 1.1.1.1 | grep -q src && getent hosts archlinux.org'"
readonly GUEST_DIR=/var/lib/dnsrace # not /tmp: the reboot wipes it

# Keep the lines that decide when a unit starts and whether it retries.
ordering_lines() { grep -hE '^(After|Wants|Requires|StartLimit[A-Za-z]+|Restart|RestartSec)=' "$@" || true; }

write_probe() {
	local out="$1"
	shift
	{
		echo '[Unit]'
		ordering_lines "$@" | grep -vE '^(Restart|RestartSec)='
		printf '[Service]\nType=oneshot\n%s\n' "$PROBE_EXEC"
		ordering_lines "$@" | grep -E '^(Restart|RestartSec)=' || true
		printf '[Install]\nWantedBy=multi-user.target\n'
	} >"$out"
}

render() {
	local dir="$1" tam_units
	tam_units=$(mktemp -d)
	# shellcheck source=/dev/null
	source "$TAM_DIR/linux_configuration/features/lib/wg_verify_units.sh"
	render_verify_units "$tam_units" /bin/true
	write_probe "$dir/probe-reflector.service" "$REFLECTOR_UNIT" "$REPO_DIR/systemd/reflector.service.d/retry-on-dns.conf"
	write_probe "$dir/probe-periodic-system-startup.service" "$REPO_DIR/systemd/periodic-system-startup.service"
	write_probe "$dir/probe-periodic-system-maintenance.service" "$REPO_DIR/systemd/periodic-system-maintenance.service"
	write_probe "$dir/probe-nftables-drift.service" "$tam_units/nftables-drift.service"
	rm -rf "$tam_units"
}

push() {
	local guest="$1" mode="$2" tmp f
	tmp=$(mktemp -d)
	render "$tmp"
	cp "$REPO_DIR/tests/vmbox/boot_dns_race_guest.sh" "$tmp/"
	vm run "$guest" "sudo install -d -o \$(id -u) $GUEST_DIR"
	for f in "$tmp"/*; do vm scp "$guest" "$f" "$GUEST_DIR/"; done
	rm -rf "$tmp"
	vm run "$guest" "sudo bash $GUEST_DIR/boot_dns_race_guest.sh setup $mode $GUEST_DIR"
}

# vm run exits 0 whatever the guest command returned, so read the verdict line.
assert_guest() {
	local out
	out=$(vm run "$1" "sudo bash $GUEST_DIR/boot_dns_race_guest.sh assert" 2>&1)
	printf '%s\n' "$out"
	grep -q '^PASS: no network-race unit' <<<"$out"
}

case "${1:-}" in
push) push "$2" "$3" ;;
reboot) vm run "$2" 'sudo systemctl reboot' || true ;;
assert) assert_guest "$2" ;;
*) echo "usage: $0 push <guest> <red|green> | reboot <guest> | assert <guest>" >&2; exit 2 ;;
esac
