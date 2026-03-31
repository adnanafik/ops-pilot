# ── base: shared deps ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app

COPY pyproject.toml README.md ./
RUN mkdir -p agents shared providers && \
    touch agents/__init__.py shared/__init__.py providers/__init__.py && \
    pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir -e ".[dev]"

COPY agents/ agents/
COPY shared/ shared/
COPY providers/ providers/
COPY demo/ demo/
COPY scripts/ scripts/

RUN mkdir -p current_tasks state

ENV PYTHONUNBUFFERED=1

# ── test: runs pytest inside the container ────────────────────────────────────
FROM base AS test

COPY tests/ tests/

CMD ["pytest", "--tb=short", "-q"]

# ── app: production demo / watcher ────────────────────────────────────────────
FROM base AS app

ENV DEMO_MODE=true

EXPOSE 8000

CMD ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port", "8000"]
