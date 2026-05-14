# syntax=docker/dockerfile:1.7
# Multi-stage build for bragi-admin. Same shape as delivery.Dockerfile;
# CMD is the only deliberate difference.

FROM python:3.12-slim AS build

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN pip install --no-cache-dir poetry

WORKDIR /app
COPY pyproject.toml poetry.lock* ./
RUN poetry export -f requirements.txt --without-hashes --only main > requirements.txt


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY --from=build /app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY pyproject.toml README.md ./

RUN pip install --no-deps -e .

EXPOSE 8001
CMD ["bragi-admin", "--host", "0.0.0.0", "--port", "8001"]
