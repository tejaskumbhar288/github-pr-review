FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so the layer caches across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ruff powers the static-analysis pre-pass; it is small and worth having.
RUN pip install --no-cache-dir ruff

COPY app ./app
COPY evals ./evals

# Reviews are read-mostly; there is no reason to run as root.
RUN useradd --create-home --uid 10001 reviewer && chown -R reviewer /srv
USER reviewer

EXPOSE 8000

# Override with `command:` for the worker.
CMD ["python", "-m", "uvicorn", "app.server.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
