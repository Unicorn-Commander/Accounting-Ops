FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
# Install with [tax-agents] extras baked in — the 1120 Preparer + 1099 + SC
# specialists' runners need anthropic/openai/mcp client libs at runtime when
# `docker exec` invokes them inside this container.
RUN pip install --no-cache-dir ".[tax-agents]"
COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
