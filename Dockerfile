FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    FLASK_APP=main:app \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Install the dependency manager once, then cache the project dependencies
# separately from the application source.
RUN pip install --no-cache-dir "uv>=0.6"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/persistent_data

EXPOSE 5010

ENTRYPOINT ["/app/entrypoint.sh"]
