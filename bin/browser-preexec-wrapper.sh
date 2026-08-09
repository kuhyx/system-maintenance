#!/bin/bash
# Generic pre-exec wrapper for browsers: ensures /etc/hosts is (re)installed
# before launching the actual browser binary, and — for Chromium-family
# browsers — captures a crash log for every launch.
#
# Why the crash logging exists (diagnosed 2026-08-09):
#   Thorium was "closing and dying randomly" with NO evidence anywhere:
#   Crashpad was never enabled (no crash_reporting consent key, empty crash DB),
#   and stderr from a dmenu/.desktop launch goes to no terminal and no journal.
#   The only Thorium lines ever seen in the journal came from a systemd-launched
#   instance. So the browser could die repeatedly and leave nothing behind.
#
#   Each launch now gets its own log holding: a VRAM/RAM snapshot before start,
#   the browser's own stderr (--enable-logging=stderr --v=1), and — on exit —
#   the exit code, decoded signal, and a SECOND VRAM snapshot. That last pair is
#   the discriminator between the two live hypotheses: a GPU/VRAM death shows
#   NVRM/NV_ERR_NO_MEMORY or near-full VRAM at exit, whereas a stale-binary
#   death shows SIGSEGV/SIGILL with the GPU idle.

set -euo pipefail

HOSTS_INSTALL_SCRIPT="__HOSTS_INSTALL_SCRIPT__"

# Where per-launch logs go, and how many to keep.
readonly LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/browser-crash-logs"
readonly LOG_KEEP=20

# Per-log byte cap. Measured 2026-08-09: Thorium writes ~2 MB in the first 45s
# (65% of it VERBOSE1 net/TLS chatter) and ~0 while idle, so cost scales with
# navigation, not wall-clock. Capping COUNT alone is therefore not enough — a
# heavy browsing day could fill the disk, and a full disk causes exactly the
# kind of random death this logging exists to diagnose.
#
# Enforced by the watchdog below, which keeps the NEWEST bytes: the lines
# immediately before death are the diagnostic ones, so trimming must drop the
# head, never the tail.
readonly LOG_MAX_BYTES=$((32 * 1024 * 1024))

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
if command -v sudo >/dev/null 2>&1; then
	sudo -n "$HOSTS_INSTALL_SCRIPT" >/dev/null 2>&1 || true
else
	"$HOSTS_INSTALL_SCRIPT" >/dev/null 2>&1 || true
fi

# Only Chromium-family browsers understand --enable-logging/--v; everything else
# (firefox, librewolf) is exec'd unchanged so this wrapper stays generic.
is_chromium_family() {
	case "$prog_name" in
	thorium-browser | google-chrome | google-chrome-stable | chromium | brave | brave-browser | vivaldi-stable)
		return 0
		;;
	*) return 1 ;;
	esac
}

# GPU + RAM state. Written before launch and again at exit so the two can be
# compared; nvidia-smi may be absent (or the GPU wedged), so never fail here.
emit_resource_snapshot() {
	local label="$1"
	echo "--- ${label} $(date '+%Y-%m-%d %H:%M:%S') ---"
	if command -v nvidia-smi >/dev/null 2>&1; then
		echo -n "VRAM used/total: "
		nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>&1 || echo "nvidia-smi failed"
	else
		echo "VRAM: nvidia-smi unavailable"
	fi
	echo -n "RAM: "
	free -m | awk '/^Mem:/ {printf "%s MiB used / %s MiB total\n", $3, $2}'
	echo "kernel: $(uname -r)"
	# Match the dotted version anywhere on the line rather than by field index:
	# the surrounding wording differs between open/proprietary driver builds.
	# Worth logging because a module/userspace version skew (nvidia-utils upgraded
	# without a reboot) is a known cause of GL crashes in Chromium.
	if [[ -r /proc/driver/nvidia/version ]]; then
		echo -n "nvidia module: "
		grep -oE '[0-9]+\.[0-9]+\.[0-9]+' /proc/driver/nvidia/version 2>/dev/null | head -1 || echo "unreadable"
	fi
}

# Keep only the newest $keep logs. Uses -printf/sort -z so filenames containing
# spaces or newlines can't split a record. Called with LOG_KEEP-1 before a new
# log is created, so the directory settles at exactly LOG_KEEP afterwards.
rotate_logs() {
	local keep="$1"
	local old
	while IFS= read -r -d '' old; do
		rm -f -- "$old"
	done < <(
		find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -printf '%T@\t%p\0' 2>/dev/null |
			sort -zrn |
			tail -zn "+$((keep + 1))" |
			cut -zf2-
	)
}

if ! is_chromium_family; then
	exec "$real_bin" "$@"
fi

mkdir -p "$LOG_DIR"
rotate_logs "$((LOG_KEEP - 1))"
log_file="${LOG_DIR}/${prog_name}-$(date +%Y%m%d-%H%M%S)-$$.log"

{
	echo "=== launch $(date '+%Y-%m-%d %H:%M:%S') ==="
	echo "binary: $real_bin"
	echo "args: $*"
	emit_resource_snapshot "PRE-LAUNCH"
	echo "=== browser stderr follows ==="
} >"$log_file"

# Written from a trap rather than inline: if the wrapper itself is killed (the
# session ends, the process group is torn down, the OOM killer picks it), an
# inline block after `wait` would never run and the log would end mid-stream
# with no verdict — indistinguishable from a browser still running.
record_exit() {
	local code="$1"
	# Fire once; EXIT still runs after a signal trap has already reported.
	[[ -n ${exit_recorded:-} ]] && return 0
	exit_recorded=1
	{
		echo "=== exit ==="
		echo "exit code: $code"
		if ((code > 128)); then
			local signal_num=$((code - 128))
			echo "died on signal: ${signal_num} ($(kill -l "$signal_num" 2>/dev/null || echo unknown))"
		fi
		emit_resource_snapshot "POST-EXIT"
	} >>"$log_file"
}

# Run in the background rather than exec'ing, so this shell survives to record
# how the browser died. `wait` reports 128+N for a signal death, which is the
# single most useful fact the old setup threw away.
#
# Deliberately a direct redirect, NOT a pipe into a size-limiting filter: in a
# pipeline `wait` reports the LAST element's status, which would report the
# filter's exit and discard the browser's signal — the one fact this whole
# wrapper exists to capture. Size is bounded by the watchdog below instead.
"$real_bin" --enable-logging=stderr --v=1 "$@" >>"$log_file" 2>&1 &
browser_pid=$!

# Watchdog: if a browsing session makes the log exceed the cap, keep the most
# recent half and continue appending. Checks once a minute — the measured burst
# rate (~2 MB/45s of navigation, ~0 idle) can't overshoot 32 MB meaningfully in
# that window, and a 1/min poll costs nothing next to the browser itself.
(
	while kill -0 "$browser_pid" 2>/dev/null; do
		sleep 60
		size="$(stat -c %s "$log_file" 2>/dev/null || echo 0)"
		if ((size > LOG_MAX_BYTES)); then
			tail -c "$((LOG_MAX_BYTES / 2))" "$log_file" >"${log_file}.trim" 2>/dev/null &&
				mv "${log_file}.trim" "$log_file" &&
				echo "=== log trimmed to newest $((LOG_MAX_BYTES / 2)) bytes at $(date '+%H:%M:%S') ===" >>"$log_file"
		fi
	done
) &
watchdog_pid=$!

# A signal aimed at the wrapper is recorded as the wrapper's own death, and the
# browser is taken down with it so no orphan outlives the log describing it.
# Written inline (not via a helper) so shellcheck can see the calls; each then
# re-raises with the default handler so the exit status stays truthful.
trap 'record_exit 129; kill "$browser_pid" "$watchdog_pid" 2>/dev/null || true; trap - HUP; kill -s HUP $$' HUP
trap 'record_exit 130; kill "$browser_pid" "$watchdog_pid" 2>/dev/null || true; trap - INT; kill -s INT $$' INT
trap 'record_exit 143; kill "$browser_pid" "$watchdog_pid" 2>/dev/null || true; trap - TERM; kill -s TERM $$' TERM

exit_code=0
wait "$browser_pid" || exit_code=$?

kill "$watchdog_pid" 2>/dev/null || true
record_exit "$exit_code"
exit "$exit_code"
