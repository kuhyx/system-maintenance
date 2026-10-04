## Commands

- run: `bin/home_tidy.py sweep --dry-run`
- test: `python -m pytest tests -q`
- test-changed: `scripts/test_changed.sh`
- lint: `pre-commit run --all-files`
- coverage: `python3 -m pytest -q --cov --cov-branch --cov-report=xml`
- coverage-gaps: `coverage-gaps coverage.xml`
