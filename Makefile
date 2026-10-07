PYTHON ?= python3
export PYTHONDONTWRITEBYTECODE = 1

.PHONY: build check release release-anki test

check:
	$(PYTHON) scripts/check.py

test:
	$(PYTHON) -m unittest discover -s tests -v

build:
	$(PYTHON) build.py

release: check test build

# Run with Anki's Python/PYTHONPATH; unlike CI, this must not skip Qt coverage.
release-anki:
	$(PYTHON) scripts/check.py --require-anki
	$(MAKE) release
