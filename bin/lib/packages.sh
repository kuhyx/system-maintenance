#!/bin/bash
# Per-family package name tables and the installer front-end.
# Sourced by install_usage_monitoring.sh; inherits the caller's strict mode.

# Format: "<generic>=<package>"; empty package = skip on this distro.
# These stay at top level on purpose: `declare -A` inside a function is local,
# which would make every pkg_name lookup return empty.
declare -A PKG_ARCH=(
	[atop]=atop [nvtop]=nvtop [netdata]=netdata
	[wl_clipboard]=wl-clipboard [xclip]=xclip
)
declare -A PKG_DEBIAN=(
	[atop]=atop [nvtop]=nvtop [netdata]=netdata
	[wl_clipboard]=wl-clipboard [xclip]=xclip
)
declare -A PKG_FEDORA=(
	[atop]=atop [nvtop]=nvtop [netdata]=netdata
	[wl_clipboard]=wl-clipboard [xclip]=xclip
)
declare -A PKG_SUSE=(
	[atop]=atop [nvtop]=nvtop [netdata]=netdata
	[wl_clipboard]=wl-clipboard [xclip]=xclip
)

pkg_name() {
	local key=$1
	case "$FAMILY" in
	arch) printf '%s' "${PKG_ARCH[$key]-}" ;;
	debian) printf '%s' "${PKG_DEBIAN[$key]-}" ;;
	fedora) printf '%s' "${PKG_FEDORA[$key]-}" ;;
	suse) printf '%s' "${PKG_SUSE[$key]-}" ;;
	esac
}

install_packages() {
	local -a pkgs=("$@")
	[[ ${#pkgs[@]} -eq 0 ]] && return 0
	log "installing: ${pkgs[*]}"
	case "$FAMILY" in
	arch) sudo pacman -S --needed --noconfirm "${pkgs[@]}" ;;
	debian)
		sudo apt-get update -qq
		sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
		;;
	fedora) sudo dnf install -y "${pkgs[@]}" ;;
	suse) sudo zypper --non-interactive install "${pkgs[@]}" ;;
	esac
}

# Pick a clipboard tool matching the session type.
clipboard_pkg() {
	if [[ ${XDG_SESSION_TYPE:-} == "wayland" ]]; then
		pkg_name wl_clipboard
	else
		pkg_name xclip
	fi
}

# Resolves the global `pkgs` array (and `clip`) from FAMILY, then installs.
# The trailing `|| true` guards the bare conditional from ending the function
# with a false return value under `set -e`.
resolve_and_install_packages() {
	want_keys=(atop nvtop netdata)
	pkgs=()
	for key in "${want_keys[@]}"; do
		p=$(pkg_name "$key")
		[[ -n $p ]] && pkgs+=("$p")
	done
	clip=$(clipboard_pkg)
	[[ -n $clip ]] && pkgs+=("$clip") || true

	install_packages "${pkgs[@]}"
}
