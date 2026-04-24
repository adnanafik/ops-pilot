# Contributing to ops-pilot

Thanks for considering a contribution.

## Getting started
1. Fork and clone.
2. `pip install -e ".[dev]"` (Python 3.11+).
3. `docker compose run --rm test` to run the suite.

## Finding something to work on
- Check [good first issues](https://github.com/adnanafik/ops-pilot/labels/good%20first%20issue) if you're new.
- For larger changes, open an issue first to align on scope.

## Code style
- Type hints everywhere, no `Any`.
- `ruff check .` must pass.
- pytest coverage ≥ 80% on agent logic.

## Pull requests
- One logical change per PR.
- Add or update tests.
- Short imperative commit subjects.
