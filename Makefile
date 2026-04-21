.PHONY: lint ruff ty fix

lint: ruff ty

ruff:
	uv run ruff check

ty:
	uv run ty check

fix:
	uv run ruff check --fix
