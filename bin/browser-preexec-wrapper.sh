#!/bin/bash
# Generic pre-exec wrapper for browsers: ensures /etc/hosts is (re)installed
# before launching the actual browser binary.

set -euo pipefail

HOSTS_INSTALL_SCRIPT="__HOSTS_INSTALL_SCRIPT__"

prog_name="$(basename "$0")"
real_bin="/usr/bin/${prog_name}"

# If run directly (not via a browser symlink) or if the target binary doesn't exist,
# allow passing the real browser command as the first argument for testing:
if [[ ! -x $real_bin || $prog_name == "browser-preexec-wrapper.sh" ]]; then
  if [[ $# -ge 1 ]]; then
    real_bin="$1"
    shift
  else
    echo "Error: could not resolve real browser binary for '$prog_name'." >&2
    echo "Usage (testing): $0 <real-browser-command> [args...]" >&2
    echo "Typical install: symlink this script as /usr/local/bin/<browser> so it wraps /usr/bin/<browser>." >&2
    exit 127
  fi
fi

# Best-effort: install hosts file quietly; don't block browser startup
if command -v sudo > /dev/null 2>&1; then
  sudo -n "$HOSTS_INSTALL_SCRIPT" > /dev/null 2>&1 || true
else
  "$HOSTS_INSTALL_SCRIPT" > /dev/null 2>&1 || true
fi

exec "$real_bin" "$@"
