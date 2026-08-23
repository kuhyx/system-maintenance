# system-maintenance

Periodic Arch system maintenance, extracted from the `testsAndMisc` monorepo
with its history.

```
bin/          periodic-system-maintenance.sh, auto-system-update.sh,
              hosts-file-monitor.sh, shutdown-timer-monitor.sh,
              browser-preexec-wrapper.sh, and the usage_report Python modules
bin/lib/      catchup_timer, distro_detect, nvidia_pmon, packages,
              system_services
systemd/      the units and timers
logrotate/    log rotation for the maintenance log
```

## Install

```bash
sudo bash bin/install_usage_monitoring.sh
```

Installs `atop`, `nvtop`, `netdata` and `xclip`, then wires the timers. It
detects the distro family itself and assumes nothing about where this
checkout lives.

## Tests

```bash
python -m pytest tests -q          # 136 tests over the usage_report modules
for t in tests/*.sh; do bash "$t"; done
```

`tests/conftest.py` puts `bin/` on `sys.path`, because those are standalone
scripts rather than an installed package.

## Note on the extraction

`test_shutdown_timer_monitor.sh` did **not** come across: it exercises the
dispatcher against `setup_midnight_shutdown.sh`, which belongs to the
digital-wellbeing subsystem, so it stays with that half of the pair.
