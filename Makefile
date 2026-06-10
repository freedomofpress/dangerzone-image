.PHONY: lint ruff ty fix

lint: ruff ty

ruff:
	uv run ruff check

ty:
	uv run ty check

fix:
	uv run ruff check --fix

Dockerfile: Dockerfile.env Dockerfile.in ## Regenerate the Dockerfile from its template
	uv run jinja2 Dockerfile.in Dockerfile.env > Dockerfile
