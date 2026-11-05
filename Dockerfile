FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
WORKDIR /srv
RUN pip install --no-cache-dir uv==0.12.10
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev
ENV PATH="/srv/.venv/bin:$PATH"
COPY alembic.ini ./
COPY migrations ./migrations
COPY data ./data
COPY scripts ./scripts
RUN useradd --create-home service

FROM base AS test
ENV COVERAGE_FILE=/tmp/.coverage RUFF_CACHE_DIR=/tmp/ruff-cache PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"
RUN uv sync --frozen
COPY tests ./tests
COPY examples ./examples
COPY mock-provider ./mock-provider
COPY starter-manifest.json ./starter-manifest.json
USER service
CMD ["pytest", "-q", "--cov=app", "--cov-report=term-missing"]

FROM base AS runtime
USER service
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]

