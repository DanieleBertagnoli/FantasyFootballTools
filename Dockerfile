FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=main:app

WORKDIR /app

COPY pyproject.toml README.md ./

RUN pip install --no-cache-dir "flask>=3.1.3"

COPY src ./src

WORKDIR /app/src

EXPOSE 5010

CMD ["flask", "run", "--host=0.0.0.0", "--port=5010"]