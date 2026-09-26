FROM python:3.13-slim AS builder

# This tag must match [tool.uv] required-version in pyproject.toml.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
RUN mkdir -p /app/.cache && chown app:app /app/.cache

USER app

EXPOSE 8000

# --proxy-headers takes the client address from X-Forwarded-For, but only from the proxies listed in the
# FORWARDED_ALLOW_IPS environment variable (uvicorn's default: 127.0.0.1). Uvicorn's access log is disabled at startup
# (app.main.configure_logging), which logs one line per request itself.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
