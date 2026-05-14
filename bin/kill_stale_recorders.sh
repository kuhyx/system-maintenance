#!/bin/bash
# Kill stray recorders and .NET profilers that were left running unintentionally.
#
# Targets based on the resource-usage report: ffmpeg x11grab and dotnet
# trace/monitor/ust processes can easily burn GiB of RAM and many CPU-hours
# after whatever session started them has ended.
#
# Usage:
#   kill_stale_recorders.sh            # interactive: lists matches, prompts
#   kill_stale_recorders.sh --force    # non-interactive: kill all matches
#   kill_stale_recorders.sh --dry-run  # only list, don't kill

set -euo pipefail

PATTERNS=(
  'ffmpeg.*x11grab'
  'ffmpeg.*-f[[:space:]]+x11grab'
  'dotnet-trace'
  'dotnet-monitor'
  'dotnet-ust'
)

mode='prompt'
case ${1:-} in
  --force) mode='force' ;;
  --dry-run) mode='dry' ;;
  -h | --help)
    sed -n '2,12p' "$0"
    exit 0
    ;;
esac

mapfile -t matches < <(
  for pat in "${PATTERNS[@]}"; do
    pgrep -af "$pat" 2> /dev/null || true
  done | sort -u
)

if ((${#matches[@]} == 0)); then
  echo 'No stale recorder/profiler processes found.'
  exit 0
fi

echo 'Stale processes:'
printf '  %s\n' "${matches[@]}"

if [[ $mode == dry ]]; then
  exit 0
fi

if [[ $mode == prompt ]]; then
  read -r -p 'Kill these? [y/N] ' answer
  [[ $answer == [yY] ]] || {
    echo 'Aborted.'
    exit 0
  }
fi

killed=0
for line in "${matches[@]}"; do
  pid=${line%% *}
  [[ -n $pid ]] || continue
  if kill "$pid" 2> /dev/null; then
    killed=$((killed + 1))
  fi
done
echo "Sent SIGTERM to $killed process(es)."
