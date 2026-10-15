FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Keep the image resolver identical to the lock-checking CI job.
COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /uvx /bin/

# WeasyPrint runtime deps (Pango/Cairo/GDK-Pixbuf) for tax-specialist PDF export.
# Note: Debian Bookworm uses libgdk-pixbuf-2.0-0 (with dash), not the older
# libgdk-pixbuf2.0-0 name.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libcairo2 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Resolve and install dependencies before copying the application so changes to
# source files do not invalidate the dependency layer. The second sync installs
# the project itself after its source is available.
RUN uv sync --frozen --no-dev --extra tax-agents --no-install-project
COPY . .
RUN uv sync --frozen --no-dev --extra tax-agents --no-editable
# pip inside the venv: the integration harness (scripts/integration_env.py) and ops recipes run
# `python -m pip install -e .[dev]` in the container; uv-made venvs carry no pip by default.
RUN uv pip install --python /app/.venv/bin/python --no-cache pip

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
