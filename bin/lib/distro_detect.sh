#!/bin/bash
# Maps /etc/os-release onto the FAMILY value the package layer switches on.
# Sourced by install_usage_monitoring.sh; inherits the caller's strict mode.

# Sets the global FAMILY. Uses log()/die() from the entry script.
detect_family() {
	. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"

	FAMILY=""
	for id in ${ID:-} ${ID_LIKE:-}; do
		case "$id" in
		arch | manjaro | endeavouros)
			FAMILY="arch"
			break
			;;
		debian | ubuntu | linuxmint | pop)
			FAMILY="debian"
			break
			;;
		elementary)
			FAMILY="debian"
			break
			;;
		fedora | rhel | centos)
			FAMILY="fedora"
			break
			;;
		opensuse* | suse | sles)
			FAMILY="suse"
			break
			;;
		esac
	done
	[[ -n $FAMILY ]] || die "unsupported distro: ID=${ID:-?} ID_LIKE=${ID_LIKE:-?}"
	log "detected distro family: $FAMILY (${PRETTY_NAME:-unknown})"
}
