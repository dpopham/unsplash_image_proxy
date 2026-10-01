"""
ASGI entry-point (re-exports the FastAPI app).

    uvicorn asgi:app --host 127.0.0.1 --port 1962

Run a single worker: the background refresher, cache bookkeeping and the
in-memory rate limiter are per process.
"""
from server.unsplash import app  # noqa: F401
