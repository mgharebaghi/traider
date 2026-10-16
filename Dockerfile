FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY *.py ./

RUN mkdir -p /app/data

VOLUME ["/app/data"]

# بررسی سلامت کانتینر: فایل heartbeat هر ۳۰ ثانیه توسط main.py به‌روزرسانی می‌شود
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import os, time; p='/app/data/heartbeat'; exit(0 if os.path.exists(p) and time.time()-float(open(p).read().strip())<180 else 1)"

CMD ["python", "main.py"]
