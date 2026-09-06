FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    FLASK_APP=main:app \
    GUNICORN_CMD_ARGS="--bind=0.0.0.0:5010 --workers=2 --threads=4 --worker-tmp-dir=/dev/shm --timeout=60 --graceful-timeout=30 --access-logfile=- --error-logfile=- --capture-output" \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Install the dependency manager once, then cache the project dependencies
# separately from the application source.
RUN pip install --no-cache-dir "uv>=0.6"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

RUN addgroup --system --gid 1000 app \
    && adduser --system --uid 1000 --ingroup app --home /app app \
    && mkdir -p /app/persistent_data \
    && chown -R app:app /app

COPY --chown=app:app src ./src

EXPOSE 5010

USER app

CMD ["gunicorn", "main:app"]
