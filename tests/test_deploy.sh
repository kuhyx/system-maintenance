#!/bin/bash
# Regression tests for scripts/deploy.sh: fast-forward of the live checkout,
# home-tidy unit refresh only on drift, and the root-file drift gate. Runs
# against a scratch origin + clone, a fake sysroot and a systemctl stub, so
# it never touches the real user/system systemd or kuhy's checkout.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
DEPLOY="$REPO_DIR/scripts/deploy.sh"

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	exit 1
}

TMP_DIR=$(mktemp -d)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

export HOME="$TMP_DIR/home"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_STATE_HOME="$HOME/.local/state"
export SYSROOT="$TMP_DIR/sysroot"
export SRC_ROOT="$TMP_DIR/src"
export LIVE_REPO="$TMP_DIR/live"
export SYSTEMCTL_LOG="$TMP_DIR/systemctl.log"
STUB_BIN="$TMP_DIR/stub"
ORIGIN="$TMP_DIR/origin.git"
mkdir -p "$HOME" "$STUB_BIN"

# systemctl stub: records every call; `show` answers a fake next-run time.
cat >"$STUB_BIN/systemctl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >>"$SYSTEMCTL_LOG"
[[ " $* " == *" show "* ]] && echo "n/a"
exit "${SYSTEMCTL_RC:-0}"
EOF
chmod +x "$STUB_BIN/systemctl"
export PATH="$STUB_BIN:$PATH"

git clone -q --bare --no-local "$REPO_DIR" "$ORIGIN"
git clone -q "$ORIGIN" "$LIVE_REPO"
git -C "$LIVE_REPO" config user.email t@t
git -C "$LIVE_REPO" config user.name t

# Lay down an exact install: what install_home_tidy.sh and
# setup_periodic_system.sh would have written for this checkout.
install_everything() {
	local unit_dir="$XDG_CONFIG_HOME/systemd/user"
	mkdir -p "$unit_dir" "$SYSROOT/usr/local/bin" "$SYSROOT/etc/systemd/system"
	sed "s|__REPO__|$LIVE_REPO|g" "$LIVE_REPO/systemd/home-tidy.service" >"$unit_dir/home-tidy.service"
	cp "$LIVE_REPO/systemd/home-tidy.timer" "$unit_dir/home-tidy.timer"
	sed -e "s|__PACMAN_WRAPPER_INSTALL__|$SRC_ROOT/digital-wellbeing/pacman/install_pacman_wrapper.sh|g" \
		-e "s|__MAKEPKG_WRAPPER_INSTALL__|$SRC_ROOT/digital-wellbeing/pacman/install_makepkg_wrapper.sh|g" \
		-e "s|__HOSTS_INSTALL_SCRIPT__|$SRC_ROOT/hosts-blocker/install.sh|g" \
		"$LIVE_REPO/bin/periodic-system-maintenance.sh" >"$SYSROOT/usr/local/bin/periodic-system-maintenance.sh"
	sed "s|__HOSTS_INSTALL_SCRIPT__|$SRC_ROOT/hosts-blocker/install.sh|g" \
		"$LIVE_REPO/bin/hosts-file-monitor.sh" >"$SYSROOT/usr/local/bin/hosts-file-monitor.sh"
	sed "s|__ACTUAL_USER__|$(id -un)|g" \
		"$LIVE_REPO/bin/auto-system-update.sh" >"$SYSROOT/usr/local/bin/auto-system-update.sh"
	local u
	for u in periodic-system-maintenance.service periodic-system-maintenance.timer \
		periodic-system-startup.service hosts-file-monitor.service \
		auto-system-update.service auto-system-update.timer; do
		cp "$LIVE_REPO/systemd/$u" "$SYSROOT/etc/systemd/system/$u"
	done
}

run_deploy() {
	: >"$SYSTEMCTL_LOG"
	set +e
	OUT=$(bash "$DEPLOY" "$@" 2>&1)
	RC=$?
	set -e
}

# 1. Everything current: exit 0, the home-tidy installer is not rerun.
install_everything
run_deploy
[[ $RC -eq 0 ]] || fail "clean deploy exited $RC: $OUT"
grep -q 'enable --now home-tidy.timer' "$SYSTEMCTL_LOG" && fail "installer ran with no drift"

# 2. Live checkout behind origin: deploy fast-forwards it.
work="$TMP_DIR/work"
git clone -q "$ORIGIN" "$work"
git -C "$work" -c user.email=t@t -c user.name=t commit -q --allow-empty -m bump
git -C "$work" push -q origin HEAD
run_deploy
[[ $RC -eq 0 ]] || fail "deploy after bump exited $RC: $OUT"
[[ $(git -C "$LIVE_REPO" rev-parse HEAD) == $(git -C "$work" rev-parse HEAD) ]] || fail "live checkout not fast-forwarded"

# 3. --check never pulls.
git -C "$work" -c user.email=t@t -c user.name=t commit -q --allow-empty -m bump2
git -C "$work" push -q origin HEAD
before=$(git -C "$LIVE_REPO" rev-parse HEAD)
run_deploy --check
[[ $(git -C "$LIVE_REPO" rev-parse HEAD) == "$before" ]] || fail "--check pulled"
[[ $RC -ne 0 ]] || fail "--check passed while live checkout is behind origin"
run_deploy
[[ $RC -eq 0 ]] || fail "deploy after bump2 exited $RC: $OUT"

# 4. Home-tidy unit drift: the installer reruns and repairs it.
rm "$XDG_CONFIG_HOME/systemd/user/home-tidy.service"
run_deploy
[[ $RC -eq 0 ]] || fail "drift deploy exited $RC: $OUT"
grep -q 'enable --now home-tidy.timer' "$SYSTEMCTL_LOG" || fail "installer not rerun on unit drift"
grep -q "$LIVE_REPO/bin/home_tidy.py" "$XDG_CONFIG_HOME/systemd/user/home-tidy.service" || fail "unit not repaired"

# 5. Root file drift: non-zero, names the root installer.
echo '# stale' >>"$SYSROOT/usr/local/bin/hosts-file-monitor.sh"
run_deploy
[[ $RC -ne 0 ]] || fail "root drift passed the health check"
[[ $OUT == *hosts-file-monitor.sh* && $OUT == *setup_periodic_system.sh* ]] || fail "root drift message unclear: $OUT"
install_everything

# 6. A dead timer fails the health check.
export SYSTEMCTL_RC=3
run_deploy
unset SYSTEMCTL_RC
[[ $RC -ne 0 ]] || fail "inactive units passed the health check"

echo "test_deploy: ok"
