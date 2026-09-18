# Development and CI image. There is no Python toolchain requirement on the
# host: everything (uv, Python 3.12, deps) lives in here.
FROM python:3.12-slim

# libatomic1 is required by pyright's bundled node binary on slim images.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends libatomic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependency layer, cached independently of source changes.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra dev --no-install-project || true

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --extra dev

CMD ["bash"]
