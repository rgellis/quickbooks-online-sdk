#!/usr/bin/env bash
# Lint, type check and test. Runs inside the dev container.
# From the host, use ./scripts/dev check -- which is the supported entry point.
set -e

echo "Running ruff formatter..."
uv run ruff format .

echo -e "\nRunning ruff linter..."
uv run ruff check . --fix

echo -e "\nRunning pyright type checker..."
uv run pyright

echo -e "\nRunning tests with coverage..."
uv run pytest --cov --cov-report=term-missing

echo -e "\nAll checks passed!"
