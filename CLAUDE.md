## Commands

- run: `bin/home_tidy.py sweep --dry-run`
- test: `python -m pytest tests -q`
- test-changed: `scripts/test_changed.sh`
- lint: `pre-commit run --all-files`
- coverage: `python3 -m pytest -q --cov --cov-branch --cov-report=xml`
- coverage-gaps: `coverage-gaps coverage.xml`
- deploy: `git -C ~/src/system-maintenance pull --ff-only && bash ~/src/system-maintenance/scripts/deploy.sh` (home-tidy runs from that checkout: re-renders its user units only on drift; root scripts/units come from linux_configuration's `setup_periodic_system.sh`, which deploy only verifies; exits non-zero unless the checkout is at upstream, every installed file matches and the timers/monitor are active; `--check` = health only)
