ifeq ($(shell test -x .venv/bin/python && echo yes),yes)
PYTHON ?= .venv/bin/python
else
PYTHON ?= /opt/miniconda3/bin/python3
endif
PYTHONPATH ?= src

.PHONY: venv doctor lint typecheck test agent-check db-create migrate export-contracts

venv:
	python3 -m venv .venv
	.venv/bin/pip install -e ../../packages/envelope_kernel -e '.[test,dev]'

doctor:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m asset_intake.cli.doctor

lint:
	$(PYTHON) -m ruff check src tests

typecheck:
	$(PYTHON) -m mypy

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'

# Aggregate deterministic gate for AI-agent edits: lint + types + no-DB tests +
# diff hygiene in one command. Live-DB tests run separately with the intake DSNs set.
agent-check:
	$(MAKE) lint
	$(MAKE) typecheck
	env -u ASSET_INTAKE_DATABASE_URL -u ASSET_INTAKE_MIGRATION_DATABASE_URL $(MAKE) test
	git diff --check

db-create:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m asset_intake.cli.db create

migrate:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m alembic upgrade head

export-contracts:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m asset_intake.cli.export_contracts
