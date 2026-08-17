#!/bin/bash
# Install and enable the resource-monitoring stack used by usage_report.py:
#   atop   -- daily CPU/RAM/disk history (systemd service + rotation)
#   nvtop  -- live GPU top (optional, NVIDIA/AMD/Intel)
#   netdata -- live dashboard on http://localhost:19999 (optional)
#   a clipboard tool (wl-clipboard or xclip) so usage_report.py can paste
#
# Plus an `nvidia-pmon` user service that logs per-process GPU samples to
# ~/.local/share/gpu-log/pmon-YYYYMMDD.log (only if nvidia-smi is present).
#
# Works on Arch, Debian/Ubuntu (and derivatives), Fedora/RHEL, openSUSE.
# Re-run safely; everything is idempotent.
#
# The steps live in lib/; this file owns the shared helpers, the globals that
# cross between steps (FAMILY, pkgs, clip, unit_dir, REPO_DIR) and the order.

set -euo pipefail

log() { printf '[install-usage] %s\n' "$*" >&2; }
die() {
	printf '[install-usage] ERROR: %s\n' "$*" >&2
	exit 1
}

# readlink -f so a symlinked entry point still finds lib/.
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=lib/distro_detect.sh
. "$SCRIPT_DIR/lib/distro_detect.sh"
# shellcheck source=lib/packages.sh
. "$SCRIPT_DIR/lib/packages.sh"
# shellcheck source=lib/system_services.sh
. "$SCRIPT_DIR/lib/system_services.sh"
# shellcheck source=lib/nvidia_pmon.sh
. "$SCRIPT_DIR/lib/nvidia_pmon.sh"
# shellcheck source=lib/catchup_timer.sh
. "$SCRIPT_DIR/lib/catchup_timer.sh"

[[ $EUID -eq 0 ]] && die "run as your normal user; sudo is invoked where needed"
command -v sudo >/dev/null 2>&1 || die "sudo is required"

detect_family
resolve_and_install_packages
enable_system_services
setup_nvidia_pmon

# Resolved here, once, from the entry script's own location: lib/ sits one
# level deeper, so a lib recomputing this would point at the wrong repo.
REPO_DIR="$SCRIPT_DIR/../../../../.."
REPO_DIR="$(readlink -f "$REPO_DIR")"
setup_catchup_timer

log "done. Wait for the first atop sample (default 10 min), then run:"
log "  python $SCRIPT_DIR/usage_report.py"
