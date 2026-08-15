FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY elections ./elections
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir '.[dev]'

EXPOSE 8000

CMD ["uvicorn", "elections.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
