#!/bin/bash
# Installs the optional per-process NVIDIA GPU logger (user service).
# Sourced by install_usage_monitoring.sh; inherits the caller's strict mode.
#
# The nvidia-pmon-logger.sh heredoc below is asserted against by
# tests/test_usage_monitoring_installer_efficiency.sh, which greps THIS file's
# source text for the `cat > ... << 'SCRIPT'` anchor line. That match is
# whitespace-insensitive, so shfmt's `cat >"..." <<'SCRIPT'` form is fine, but
# the heredoc BODY is compared property-by-property -- keep it verbatim.

setup_nvidia_pmon() {
	if command -v nvidia-smi >/dev/null 2>&1; then
		log "setting up nvidia-pmon user service"
		mkdir -p "$HOME/.local/share/gpu-log"
		mkdir -p "$HOME/.local/bin"
		unit_dir="$HOME/.config/systemd/user"
		mkdir -p "$unit_dir"

		# Install the day-rolling wrapper script.
		cat >"$HOME/.local/bin/nvidia-pmon-logger.sh" <<'SCRIPT'
#!/bin/bash
set -euo pipefail

LOG_DIR="$HOME/.local/share/gpu-log"
ERR_LOG="$LOG_DIR/pmon-errors.log"
mkdir -p "$LOG_DIR"

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-pmon-logger: nvidia-smi not found" >&2
  exit 1
fi

current_day() {
  printf '%(%Y%m%d)T' -1
}

seconds_until_next_day() {
  local hour minute second
  printf -v hour '%(%H)T' -1
  printf -v minute '%(%M)T' -1
  printf -v second '%(%S)T' -1
  printf '%s\n' $(((23 - 10#$hour) * 3600 + (59 - 10#$minute) * 60 + (60 - 10#$second)))
}

while true; do
  day="$(current_day)"
  out_file="$LOG_DIR/pmon-${day}.log"
  rollover_pid=''

  nvidia-smi pmon -d 10 -o DT >> "$out_file" 2>> "$ERR_LOG" &
  pmon_pid=$!

  (
    sleep "$(seconds_until_next_day)"
    kill "$pmon_pid" >/dev/null 2>&1 || true
  ) &
  rollover_pid=$!

  wait "$pmon_pid" || true

  if [[ -n $rollover_pid ]]; then
    kill "$rollover_pid" >/dev/null 2>&1 || true
    wait "$rollover_pid" 2>/dev/null || true
  fi

done
SCRIPT
		chmod +x "$HOME/.local/bin/nvidia-pmon-logger.sh"

		cat >"$unit_dir/nvidia-pmon.service" <<'UNIT'
[Unit]
Description=Per-day NVIDIA pmon logger
After=default.target

[Service]
Type=simple
ExecStart=%h/.local/bin/nvidia-pmon-logger.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
		systemctl --user daemon-reload
		systemctl --user enable --now nvidia-pmon.service || log "warn: nvidia-pmon user service failed"
	else
		log "no nvidia-smi found; skipping GPU per-process logger"
	fi
}
