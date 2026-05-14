#!/bin/bash
# Install and enable the resource-monitoring stack used by usage_report.py:
#   atop   -- daily CPU/RAM/disk history (systemd service + rotation)
#   nvtop  -- live GPU top (optional, NVIDIA/AMD/Intel)
#   netdata -- live dashboard on http://localhost:19999 (optional)
#   a clipboard tool (wl-clipboard or xclip) so usage_report.py can paste
#
# Plus an `nvidia-pmon` user service that logs per-process GPU samples to
# ~/.local/share/gpu-log/pmon-YYYYMMDD.log (only if nvidia-smi is present).
#
# Works on Arch, Debian/Ubuntu (and derivatives), Fedora/RHEL, openSUSE.
# Re-run safely; everything is idempotent.

set -euo pipefail

log() { printf '[install-usage] %s\n' "$*" >&2; }
die() {
  printf '[install-usage] ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] && die "run as your normal user; sudo is invoked where needed"
command -v sudo > /dev/null 2>&1 || die "sudo is required"

# --- Distro detection -------------------------------------------------------
. /etc/os-release 2> /dev/null || die "cannot read /etc/os-release"

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

# --- Package names per family ----------------------------------------------
# Format: "<generic>=<package>"; empty package = skip on this distro.
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

# --- Choose a clipboard tool matching the session --------------------------
clipboard_pkg() {
  if [[ ${XDG_SESSION_TYPE:-} == "wayland" ]]; then
    pkg_name wl_clipboard
  else
    pkg_name xclip
  fi
}

# --- Resolve final package set ---------------------------------------------
want_keys=(atop nvtop netdata)
pkgs=()
for key in "${want_keys[@]}"; do
  p=$(pkg_name "$key")
  [[ -n $p ]] && pkgs+=("$p")
done
clip=$(clipboard_pkg)
[[ -n $clip ]] && pkgs+=("$clip")

install_packages "${pkgs[@]}"

# --- Enable system services -------------------------------------------------
enable_unit() {
  local unit=$1
  if systemctl list-unit-files "$unit" > /dev/null 2>&1; then
    log "enabling $unit"
    sudo systemctl enable --now "$unit" || log "warn: failed to enable $unit"
  else
    log "skip $unit (not present on this system)"
  fi
}

enable_unit atop.service
# atop-rotate exists on Arch; Debian/Ubuntu rotate via cron instead.
enable_unit atop-rotate.timer
enable_unit netdata.service

# --- NVIDIA per-process GPU logger (optional) -------------------------------
if command -v nvidia-smi > /dev/null 2>&1; then
  log "setting up nvidia-pmon user service"
  mkdir -p "$HOME/.local/share/gpu-log"
  mkdir -p "$HOME/.local/bin"
  unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"

  # Install the day-rolling wrapper script.
  cat > "$HOME/.local/bin/nvidia-pmon-logger.sh" << 'SCRIPT'
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

  cat > "$unit_dir/nvidia-pmon.service" << 'UNIT'
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

# --- Daily usage-report catch-up timer -------------------------------------
REPO_DIR="$(dirname "$(readlink -f "$0")")/../../../../.."
REPO_DIR="$(readlink -f "$REPO_DIR")"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/.local/bin" "$HOME/.local/share/usage-reports"

cat > "$HOME/.local/bin/usage-report-catchup.sh" << SCRIPT
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

cat > "$unit_dir/usage-report-catchup.service" << 'UNIT'
[Unit]
Description=Generate usage reports for available atop days
After=default.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/usage-report-catchup.sh
UNIT

cat > "$unit_dir/usage-report-catchup.timer" << 'UNIT'
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

log "done. Wait for the first atop sample (default 10 min), then run:"
log "  python $(dirname "$(readlink -f "$0")")/usage_report.py"
