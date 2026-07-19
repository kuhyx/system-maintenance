#!/bin/bash
# Regression tests for nvidia-pmon logger installer template efficiency.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
INSTALLER="$REPO_DIR/scripts/periodic_background/system-maintenance/bin/install_usage_monitoring.sh"

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

# The template used to poll with `sleep 60` until the day changed. It now
# sleeps exactly once, until the next midnight, and blocks on `wait` for the
# pmon child — strictly fewer wakeups. Assert that property rather than the
# old literal, so an accidental return to polling still fails the test.
printf 'Checking pmon logger template sleeps until rollover rather than polling...\n'
grep -q 'sleep "\$(seconds_until_next_day)"' <<< "$logger_template" \
  || fail 'logger template must sleep until the next day boundary'

grep -q 'seconds_until_next_day()' <<< "$logger_template" \
  || fail 'logger template must define seconds_until_next_day'

printf 'Checking pmon logger template blocks on wait instead of spinning...\n'
grep -qE '^\s*wait "\$pmon_pid"' <<< "$logger_template" \
  || fail 'logger template must block on wait for the pmon child'

printf 'Checking pmon logger template uses fork-free date builtin...\n'
grep -q "printf '%(%Y%m%d)T' -1" <<< "$logger_template" \
  || fail 'logger template must use bash printf time builtin for current day'

printf 'Checking pmon logger template avoids external date command...\n'
! grep -q 'date +%Y%m%d' <<< "$logger_template" \
  || fail 'logger template must not call external date command in hot path'

printf 'Usage monitoring installer efficiency tests passed.\n'
