# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir \
        --constraint constraints.txt \
        hatchling==1.32.0 \
        setuptools==84.0.0 \
        wheel==0.48.0 \
    && python -m pip wheel \
        --no-build-isolation \
        --no-cache-dir \
        --constraint constraints.txt \
        --wheel-dir /wheels .

FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQLITE_PATH=/app/data/fund_alert_bot.sqlite3 \
    TZ=Asia/Shanghai \
    AFTER_CLOSE_CHECK_TIME=17:10

WORKDIR /app

RUN groupadd -r appuser \
    && useradd -r -s /bin/false -g appuser appuser

# BuildKit mounts the builder wheels without copying them into a final layer.
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels,ro \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl \
    && python -m pip uninstall --yes pip setuptools wheel \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

VOLUME ["/app/data"]

# The scheduler writes SQLITE_PATH + ".heartbeat" at least once per minute.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,time; p=os.environ.get('SQLITE_PATH','/app/data/fund_alert_bot.sqlite3')+'.heartbeat'; raise SystemExit(0 if os.path.isfile(p) and time.time()-os.path.getmtime(p) <= 180 else 1)"

USER appuser

CMD ["python", "-m", "fund_alert_bot.main"]
