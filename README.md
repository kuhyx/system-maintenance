# system-maintenance

Periodic Arch system maintenance, extracted from the `testsAndMisc` monorepo
with its history.

```
bin/          periodic-system-maintenance.sh, auto-system-update.sh,
              hosts-file-monitor.sh, shutdown-timer-monitor.sh,
              browser-preexec-wrapper.sh, the usage_report Python modules,
              and home_tidy.py + its _home_tidy_* modules
bin/lib/      catchup_timer, distro_detect, nvidia_pmon, packages,
              system_services
systemd/      the units and timers
logrotate/    log rotation for the maintenance log
```

## home-tidy: keep `~` readable

`~` is allowed to hold exactly the buckets listed in `home-tidy.toml`
(`src vendor services data media games sdk archive inbox Downloads
Documents`); everything else is swept to `~/inbox` by an hourly user timer
and a zero-fork login nag prints the last report. Nothing is ever deleted.

```bash
bin/home_tidy.py check                 # lint; exit 1 on a failure
bin/home_tidy.py sweep [--dry-run]     # stray root entries -> ~/inbox (journaled)
bin/home_tidy.py undo [last|all|<ts>]  # replay sweeps backwards (never the migration)
bin/home_tidy.py migrate --plan        # the one-shot reorganisation, read-only
bin/home_tidy.py migrate --apply --yes # resumable, journaled, verifies itself
bin/install_home_tidy.sh               # timer + ~/.zshrc nag (--remove undoes)
```

The migration re-points every reference the audit could find (systemd
units, `/usr/local/bin`, i3, `~/.claude`, `~/.claude.json`, venv shebangs,
editable installs, git hooks, compose files, code that spells
`Path.home() / "name"`), commits the rewritten tracked files in each repo
through its own pre-commit gate, leaves 14-day bridge symlinks at the old
paths and drops them at once if a verify pass with the bridges moved aside
comes back clean. State lives in `~/.local/state/home-tidy/`.

## Install

```bash
sudo bash bin/install_usage_monitoring.sh
```

Installs `atop`, `nvtop`, `netdata` and `xclip`, then wires the timers. It
detects the distro family itself and assumes nothing about where this
checkout lives.

## Tests

```bash
python -m pytest tests -q          # usage_report + home_tidy modules
for t in tests/*.sh; do bash "$t"; done
```

`tests/conftest.py` puts `bin/` on `sys.path`, because those are standalone
scripts rather than an installed package.

## Note on the extraction

`test_shutdown_timer_monitor.sh` did **not** come across: it exercises the
dispatcher against `setup_midnight_shutdown.sh`, which belongs to the
digital-wellbeing subsystem, so it stays with that half of the pair.
