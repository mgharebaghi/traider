FROM node:22-bookworm-slim AS gmx-deps
WORKDIR /sdk
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data/gmx

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libstdc++6 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY *.py ./
COPY *.mjs *.md ./
COPY --from=gmx-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=gmx-deps /sdk/node_modules ./node_modules

RUN mkdir -p /app/data

VOLUME ["/app/data"]

# بررسی سلامت کانتینر: فایل heartbeat هر ۳۰ ثانیه توسط main.py به‌روزرسانی می‌شود
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "main.py"]
