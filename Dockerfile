FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# WeasyPrint runtime deps (Pango/Cairo/GDK-Pixbuf) for tax-specialist PDF export.
# Note: Debian Bookworm uses libgdk-pixbuf-2.0-0 (with dash), not the older
# libgdk-pixbuf2.0-0 name.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libcairo2 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
# Install with [tax-agents] extras baked in — the 1120 Preparer + 1099 + SC
# specialists' runners need anthropic/openai/mcp client libs at runtime when
# `docker exec` invokes them inside this container.
RUN pip install --no-cache-dir ".[tax-agents]"
COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
