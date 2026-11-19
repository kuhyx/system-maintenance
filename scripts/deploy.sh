#!/bin/bash

# ============================================================================
# Deploy system-maintenance on this machine, then prove it is live.
#
# What runs where:
#   - home-tidy runs straight from the live checkout ($LIVE_REPO) through a
#     user timer, so deploying it = fast-forward the checkout and re-render
#     the user units when they drifted (bin/install_home_tidy.sh).
#   - The root half (/usr/local/bin scripts, /etc/systemd/system units) is
#     installed by linux_configuration's setup_periodic_system.sh, which also
#     wires pacman wrappers and runs a full maintenance pass. That is too
#     heavy and too root to run from here, so this script only checks the
#     root files still equal what the installer would render now, and fails
#     naming the installer when they do not.
#
# Usage:  scripts/deploy.sh           # pull --ff-only, refresh, health check
#         scripts/deploy.sh --check   # health check only (fetch, no pull/install)
#
# Exit 0 only when the checkout is at its upstream, every installed file
# matches and every timer/monitor is enabled and active.
# ============================================================================

set -euo pipefail

readonly LIVE_REPO="${LIVE_REPO:-$HOME/src/system-maintenance}"
readonly SRC_ROOT="${SRC_ROOT:-$HOME/src}"
readonly SYSROOT="${SYSROOT:-}"
readonly UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
readonly ROOT_INSTALLER="$SRC_ROOT/testsAndMisc/linux_configuration/periodic_background/setup_periodic_system.sh"
# Units setup_periodic_system.sh copies verbatim (install -m 0644, no sed).
readonly ROOT_UNITS=(
	periodic-system-maintenance.service periodic-system-maintenance.timer
	periodic-system-startup.service hosts-file-monitor.service
	auto-system-update.service auto-system-update.timer
)
readonly ROOT_ACTIVE=(
	periodic-system-maintenance.timer auto-system-update.timer
	hosts-file-monitor.service
)

problems=()

problem() { problems+=("$1"); }

# Fast-forward only: git refuses on a conflicting local edit, never clobbers.
update_checkout() {
	git -C "$LIVE_REPO" pull --ff-only -q
}

render_home_tidy_service() {
	sed "s|__REPO__|$LIVE_REPO|g" "$LIVE_REPO/systemd/home-tidy.service"
}

home_tidy_units_current() {
	cmp -s <(render_home_tidy_service) "$UNIT_DIR/home-tidy.service" &&
		cmp -s "$LIVE_REPO/systemd/home-tidy.timer" "$UNIT_DIR/home-tidy.timer"
}

# Rerun the installer only on drift, so a no-op deploy never touches systemd.
refresh_home_tidy() {
	home_tidy_units_current && return 0
	echo "home-tidy units drifted; reinstalling"
	bash "$LIVE_REPO/bin/install_home_tidy.sh"
}

# Same substitutions setup_periodic_system.sh / periodic_browser.sh apply.
render_root_script() {
	sed -e "s|__PACMAN_WRAPPER_INSTALL__|$SRC_ROOT/digital-wellbeing/pacman/install_pacman_wrapper.sh|g" \
		-e "s|__MAKEPKG_WRAPPER_INSTALL__|$SRC_ROOT/digital-wellbeing/pacman/install_makepkg_wrapper.sh|g" \
		-e "s|__HOSTS_INSTALL_SCRIPT__|$SRC_ROOT/hosts-blocker/install.sh|g" \
		-e "s|__ACTUAL_USER__|$(id -un)|g" \
		"$LIVE_REPO/bin/$1"
}

check_checkout() {
	local head upstream
	# Fetch (refs only) so --check sees an upstream that moved since the pull.
	git -C "$LIVE_REPO" fetch -q
	head="$(git -C "$LIVE_REPO" rev-parse HEAD)"
	upstream="$(git -C "$LIVE_REPO" rev-parse '@{u}')"
	[[ $head == "$upstream" ]] || problem "$LIVE_REPO is at ${head:0:7}, upstream at ${upstream:0:7}"
}

check_root_files() {
	local s u
	for s in periodic-system-maintenance.sh hosts-file-monitor.sh auto-system-update.sh; do
		cmp -s <(render_root_script "$s") "$SYSROOT/usr/local/bin/$s" ||
			problem "/usr/local/bin/$s differs from the repo; run: sudo bash $ROOT_INSTALLER"
	done
	for u in "${ROOT_UNITS[@]}"; do
		cmp -s "$LIVE_REPO/systemd/$u" "$SYSROOT/etc/systemd/system/$u" ||
			problem "/etc/systemd/system/$u differs from the repo; run: sudo bash $ROOT_INSTALLER"
	done
}

check_units() {
	local u
	home_tidy_units_current || problem "home-tidy user units differ from the repo"
	systemctl --user is-active --quiet home-tidy.timer || problem "home-tidy.timer not active"
	for u in "${ROOT_ACTIVE[@]}"; do
		systemctl is-enabled --quiet "$u" || problem "$u not enabled"
		systemctl is-active --quiet "$u" || problem "$u not active"
	done
}

main() {
	if [[ ${1:-} != --check ]]; then
		update_checkout
		refresh_home_tidy
	fi
	check_checkout
	check_root_files
	check_units
	if ((${#problems[@]})); then
		printf 'deploy health check FAILED:\n' >&2
		printf '  - %s\n' "${problems[@]}" >&2
		exit 1
	fi
	echo "system-maintenance live at $(git -C "$LIVE_REPO" rev-parse --short HEAD); all units current and active"
}

main "$@"
