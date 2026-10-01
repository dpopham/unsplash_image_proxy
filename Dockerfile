FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CACHE=/app/cache

RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY asgi.py ./
COPY server ./server
RUN mkdir -p /app/cache && chown app:app /app/cache

USER app
VOLUME /app/cache
EXPOSE 1962

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1962/healthz', timeout=3)" || exit 1

# Single worker on purpose (see README). Set FORWARDED_ALLOW_IPS to the address
# your reverse proxy connects from so the real client IP is used for rate limiting.
CMD ["uvicorn", "server.unsplash:app", "--host", "0.0.0.0", "--port", "1962", \
     "--workers", "1", "--timeout-keep-alive", "10", "--no-server-header", "--proxy-headers"]
