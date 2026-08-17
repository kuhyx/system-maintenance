#!/bin/bash
# Installs the hourly usage-report catch-up user timer.
# Sourced by install_usage_monitoring.sh; inherits the caller's strict mode.
#
# REPO_DIR is computed ONCE by the entry script and read here as a global. It
# is interpolated into the generated catch-up script at write time, so
# recomputing it from this file's own depth would silently point the generated
# script at the wrong repo while still exiting 0.

setup_catchup_timer() {
	unit_dir="$HOME/.config/systemd/user"
	mkdir -p "$unit_dir" "$HOME/.local/bin" "$HOME/.local/share/usage-reports"

	# Unquoted delimiter: $HOME and $REPO_DIR interpolate at write time, while
	# \$REPO and \$OUT_DIR stay literal. The escaping is load-bearing.
	cat >"$HOME/.local/bin/usage-report-catchup.sh" <<SCRIPT
#!/bin/bash
set -euo pipefail

REPO="$REPO_DIR"
RUN_SCRIPT="\$REPO/run.sh"
OUT_DIR="\$HOME/.local/share/usage-reports"
ATOP_DIR="/var/log/atop"

mkdir -p "\$OUT_DIR"

if [[ ! -x "\$RUN_SCRIPT" ]]; then
  echo "usage-report-catchup: missing executable \$RUN_SCRIPT" >&2
  exit 1
fi

shopt -s nullglob
TODAY="\$(date +%Y%m%d)"
for atop_file in "\$ATOP_DIR"/atop_*; do
  date_part="\${atop_file##*_}"
  if [[ ! "\$date_part" =~ ^[0-9]{8}\$ ]]; then
    continue
  fi

  out_file="\$OUT_DIR/usage-report-\${date_part}.md"
  tmp_file="\$out_file.tmp"

  if [[ "\$date_part" == "\$TODAY" || ! -s "\$out_file" ]]; then
    if "\$RUN_SCRIPT" --date "\$date_part" > "\$tmp_file"; then
      mv -f "\$tmp_file" "\$out_file"
    else
      rm -f "\$tmp_file"
    fi
  fi
done
SCRIPT
	chmod +x "$HOME/.local/bin/usage-report-catchup.sh"

	cat >"$unit_dir/usage-report-catchup.service" <<'UNIT'
[Unit]
Description=Generate usage reports for available atop days
After=default.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/usage-report-catchup.sh
UNIT

	cat >"$unit_dir/usage-report-catchup.timer" <<'UNIT'
[Unit]
Description=Run usage report catch-up hourly
Requires=usage-report-catchup.service

[Timer]
OnBootSec=2min
OnCalendar=hourly
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

	systemctl --user daemon-reload
	systemctl --user enable --now usage-report-catchup.timer || log "warn: usage-report-catchup timer failed"
	log "usage reports will be generated hourly in $HOME/.local/share/usage-reports/"
}
