FROM python:3.11-slim

WORKDIR /app

# Install build tools and app dependencies as a separate cached layer
COPY pyproject.toml README.md ./
# Create minimal package stubs so hatchling can resolve the editable install
RUN mkdir -p agents shared && \
    touch agents/__init__.py shared/__init__.py && \
    pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir -e .

# Copy full source (overwrites the stubs above)
COPY agents/ agents/
COPY shared/ shared/
COPY demo/ demo/
COPY scripts/ scripts/

# Runtime directories
RUN mkdir -p current_tasks state

ENV DEMO_MODE=true
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port", "8000"]
