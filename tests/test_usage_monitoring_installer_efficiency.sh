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

printf 'Checking pmon logger template uses a sleep-until-midnight helper...\n'
grep -q 'seconds_until_next_day()' <<< "$logger_template" \
  || fail 'logger template must define seconds_until_next_day for rollover timing'

printf 'Checking pmon logger template avoids minute polling loop...\n'
! grep -q 'sleep 60' <<< "$logger_template" \
  || fail 'logger template must not poll every minute for day rollover'

printf 'Checking pmon logger template avoids repeated kill -0 probes...\n'
! grep -q 'while kill -0' <<< "$logger_template" \
  || fail 'logger template must not spin on kill -0 for day rollover detection'

printf 'Checking pmon logger template starts a rollover sleeper...\n'
grep -q 'sleep "\$(seconds_until_next_day)"' <<< "$logger_template" \
  || fail 'logger template must sleep until midnight before rotating pmon'

printf 'Checking pmon logger template uses fork-free date builtin...\n'
grep -q "printf '%(%Y%m%d)T' -1" <<< "$logger_template" \
  || fail 'logger template must use bash printf time builtin for current day'

printf 'Checking pmon logger template avoids external date command...\n'
! grep -q 'date +%Y%m%d' <<< "$logger_template" \
  || fail 'logger template must not call external date command in hot path'

printf 'Usage monitoring installer efficiency tests passed.\n'
