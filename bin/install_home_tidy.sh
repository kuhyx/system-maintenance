#!/bin/bash

# ============================================================================
# Install the home-tidy user timer and the zsh login nag.
#
# Renders systemd/home-tidy.{service,timer} with this checkout's real path
# (nothing assumes where the repo lives), enables the hourly timer, records
# the install time so `sweep` stays in dry-run for its grace window, and
# appends a zero-fork nag block to ~/.zshrc that prints the last report.
#
# Usage:  bin/install_home_tidy.sh            # install / refresh
#         bin/install_home_tidy.sh --remove   # disable timer, drop the nag
# ============================================================================

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO
readonly UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
readonly STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/home-tidy"
readonly ZSHRC="$HOME/.zshrc"
readonly NAG_BEGIN="# >>> home-tidy nag >>>"
readonly NAG_END="# <<< home-tidy nag <<<"

install_units() {
    mkdir -p "$UNIT_DIR" "$STATE_DIR"
    sed "s|__REPO__|$REPO|g" "$REPO/systemd/home-tidy.service" > "$UNIT_DIR/home-tidy.service"
    cp "$REPO/systemd/home-tidy.timer" "$UNIT_DIR/home-tidy.timer"
    systemctl --user daemon-reload
    systemctl --user enable --now home-tidy.timer
    [[ -f "$STATE_DIR/installed-at" ]] || date +%s > "$STATE_DIR/installed-at"
    echo "home-tidy.timer enabled (next: $(systemctl --user show home-tidy.timer -p NextElapseUSecRealtime --value))"
}

install_nag() {
    local block
    # Zero forks: a builtin test and one cat, only when there is a report.
    block="$NAG_BEGIN
[[ -s \"\${XDG_STATE_HOME:-\$HOME/.local/state}/home-tidy/report.txt\" ]] && cat \"\${XDG_STATE_HOME:-\$HOME/.local/state}/home-tidy/report.txt\"
$NAG_END"
    touch "$ZSHRC"
    if grep -qF "$NAG_BEGIN" "$ZSHRC"; then
        python3 "$REPO/bin/_replace_block.py" "$ZSHRC" "$NAG_BEGIN" "$NAG_END" "$block"
    else
        printf '\n%s\n' "$block" >> "$ZSHRC"
    fi
    echo "login nag installed in $ZSHRC"
}

remove() {
    systemctl --user disable --now home-tidy.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/home-tidy.service" "$UNIT_DIR/home-tidy.timer"
    systemctl --user daemon-reload
    if [[ -f "$ZSHRC" ]] && grep -qF "$NAG_BEGIN" "$ZSHRC"; then
        python3 "$REPO/bin/_replace_block.py" "$ZSHRC" "$NAG_BEGIN" "$NAG_END" ""
    fi
    echo "home-tidy timer and nag removed"
}

main() {
    if [[ "${1:-}" == "--remove" ]]; then
        remove
        return
    fi
    install_units
    install_nag
}

main "$@"
