#!/bin/bash
# Regression tests for nvidia-pmon logger installer template efficiency.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
INSTALLER="$REPO_DIR/scripts/system-maintenance/bin/install_usage_monitoring.sh"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

logger_template=$(
  awk '
    /cat > "\$HOME\/.local\/bin\/nvidia-pmon-logger\.sh" << '\''SCRIPT'\''/ {capture=1; next}
    capture && /^SCRIPT$/ {capture=0; exit}
    capture {print}
  ' "$INSTALLER"
)

[[ -n $logger_template ]] || fail 'could not extract nvidia-pmon-logger template from installer'

printf 'Checking pmon logger template avoids read -t busy-loop pattern...\n'
! grep -q 'read -r -t' <<< "$logger_template" \
  || fail 'logger template must not use read -t as sleep surrogate'

printf 'Checking pmon logger template uses sleep-based waiting...\n'
grep -q 'sleep 60' <<< "$logger_template" \
  || fail 'logger template must sleep between day rollover checks'

printf 'Checking pmon logger template uses fork-free date builtin...\n'
grep -q "printf '%(%Y%m%d)T' -1" <<< "$logger_template" \
  || fail 'logger template must use bash printf time builtin for current day'

printf 'Checking pmon logger template avoids external date command...\n'
! grep -q 'date +%Y%m%d' <<< "$logger_template" \
  || fail 'logger template must not call external date command in hot path'

printf 'Usage monitoring installer efficiency tests passed.\n'
