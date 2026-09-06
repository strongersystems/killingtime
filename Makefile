.PHONY: install check sync serve test lint

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

check:
	kt check

sync:
	kt sync

serve:
	kt serve

test:
	pytest -q

lint:
	ruff check .
