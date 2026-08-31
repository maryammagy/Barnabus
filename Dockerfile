# syntax=docker/dockerfile:1.7

FROM python:3.12.9-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    SOURCE_DATE_EPOCH=1754006400 \
    BARNABUS_SEED=20250301 \
    BARNABUS_DUCKDB_THREADS=8 \
    BARNABUS_DUCKDB_MEMORY_LIMIT=12GB \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1

WORKDIR /app

RUN mkdir -p /work /output /results

FROM base AS runtime-dependencies
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --no-compile --require-hashes -r requirements.lock

FROM base AS test
COPY requirements-test.lock ./
RUN python -m pip install --no-cache-dir --no-compile --require-hashes -r requirements-test.lock
COPY src/ ./src/
COPY config/ ./config/
COPY analyst_reproduction/ ./analyst_reproduction/
COPY tests/ ./tests/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["pytest"]
CMD ["-q"]

FROM runtime-dependencies AS runtime
COPY src/ ./src/
COPY config/ ./config/
COPY analyst_reproduction/ ./analyst_reproduction/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "barnabus.pipeline"]
CMD ["run"]

FROM runtime-dependencies AS service-runtime
RUN groupadd --system --gid 10001 barnabus \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin barnabus \
    && mkdir -p /state /analytics-source \
    && chown -R 10001:10001 /state
COPY src/ ./src/
COPY config/ ./config/
ENV PYTHONPATH=/app/src
USER 10001:10001
CMD ["python", "-m", "barnabus.monitoring_service", "serve"]
