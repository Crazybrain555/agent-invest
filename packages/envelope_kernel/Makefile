ifeq ($(shell test -x .venv/bin/python && echo yes),yes)
PYTHON ?= .venv/bin/python
else
PYTHON ?= /opt/miniconda3/bin/python3
endif
PYTHONPATH ?= src

.PHONY: lint typecheck test export-contracts agent-check

lint:
	$(PYTHON) -m ruff check src tests

typecheck:
	$(PYTHON) -m mypy

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'

# Regenerate contracts/data_asset.v1.json from the model. The committed file
# must match the export byte-for-byte (guarded by tests/contract).
export-contracts:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m envelope_kernel.export_contracts

# Aggregate deterministic gate for AI-agent edits: lint + types + tests +
# diff hygiene in one command. No DB anywhere in this package.
agent-check:
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test
	git diff --check
