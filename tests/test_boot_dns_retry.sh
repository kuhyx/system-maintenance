#!/bin/bash
# Regression test: boot-time network units retry instead of staying failed.
#
# network-online.target is reached before name resolution works on this PC
# (systemd-resolved is masked), so reflector and periodic-system-startup both
# failed at boot with "Temporary failure in name resolution" and sat in
# `systemctl --failed` until someone looked (seen 2026-11-18/20).
set -euo pipefail

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

for f in systemd/periodic-system-startup.service systemd/reflector.service.d/retry-on-dns.conf; do
	[[ -f "$REPO_DIR/$f" ]] || fail "$f missing"
	grep -qx 'Restart=on-failure' "$REPO_DIR/$f" || fail "$f must set Restart=on-failure"
	grep -qE '^RestartSec=[1-9]' "$REPO_DIR/$f" || fail "$f must wait before retrying"
	grep -qE '^StartLimitBurst=[1-9]' "$REPO_DIR/$f" || fail "$f must bound its retries"
	echo "PASS: $f retries on failure"
done
if command -v systemd-analyze >/dev/null 2>&1; then
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	sed 's#^ExecStart=.*#ExecStart=/bin/true#' "$REPO_DIR/systemd/periodic-system-startup.service" \
		>"$tmp/periodic-system-startup.service"
	systemd-analyze verify "$tmp/periodic-system-startup.service" 2>&1 | grep -iE 'restart|unknown' &&
		fail "systemd rejects the retry settings"
	echo "PASS: systemd-analyze accepts the startup unit"
fi
