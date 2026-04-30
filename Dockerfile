# syntax=docker/dockerfile:1.6
# Multi-stage build. Builder installs the package + deps into a venv;
# runtime copies the venv + source. Non-root user. Single CMD that picks
# the worker via HERMES_WORKER env var so one image runs any worker.

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build
# Install deps first (cacheable layer); then the package.
COPY pyproject.toml ./
COPY engine/__init__.py engine/__init__.py
COPY funds/__init__.py funds/__init__.py
RUN pip install --upgrade pip wheel \
 && pip install .

# Now copy the rest of the source and reinstall to pick up the modules.
COPY engine/ engine/
COPY funds/ funds/
COPY scripts/ scripts/
COPY onchain/ onchain/
COPY config/ config/
COPY data/ data/
COPY tests/ tests/
RUN pip install --no-deps -e .


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HERMES_LOG_FORMAT=json \
    HERMES_LOG_STREAM=stdout

# Non-root user. Container writes only to /data (mounted) and stdout.
RUN useradd --create-home --uid 10001 --shell /bin/bash hermes \
 && mkdir -p /home/hermes/.hermes/brain/status /home/hermes/.hermes/brain/state \
 && chown -R hermes:hermes /home/hermes

COPY --from=builder --chown=hermes:hermes /opt/venv /opt/venv
COPY --from=builder --chown=hermes:hermes /build /app

WORKDIR /app
USER hermes

# HERMES_WORKER picks the worker module — same image runs any of the 17.
# Override at run time:
#   docker run --rm -e HERMES_WORKER=aave_usdc hermes-kite
# Or supply a different command for ops scripts:
#   docker run --rm hermes-kite python -m scripts.reconcile --skip-onchain
ENV HERMES_WORKER=aave_usdc

CMD ["sh", "-c", "exec python -m funds.${HERMES_WORKER}_worker"]
