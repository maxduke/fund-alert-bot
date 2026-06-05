FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade \
        pip==25.2 \
        setuptools==75.1.0 \
        wheel==0.44.0 \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQLITE_PATH=/app/data/fund_alert_bot.sqlite3 \
    TZ=Asia/Shanghai \
    AFTER_CLOSE_CHECK_TIME=17:10

WORKDIR /app

RUN groupadd -r appuser \
    && useradd -r -s /bin/false -g appuser appuser

COPY --from=builder /wheels /wheels

RUN python -m pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data

VOLUME ["/app/data"]

USER appuser

CMD ["python", "-m", "fund_alert_bot.main"]
