FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright chromium optional — only needed for browser login inside container
# RUN playwright install --with-deps chromium

COPY app ./app
COPY static ./static
COPY run-prod.sh .
RUN chmod +x run-prod.sh && mkdir -p data downloads

ENV DOLA_ENV=production \
    DOLA_DEMO_MODE=false \
    DOLA_HOST=0.0.0.0 \
    DOLA_PORT=8787

EXPOSE 8787
VOLUME ["/app/data", "/app/downloads"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/api/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
