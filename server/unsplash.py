"""
Random Unsplash wallpaper server.

Images are fetched from the Unsplash API by a background task (or on demand
when the cache is empty), sanitised, cached on disk and then served from the
cache.  Anonymous HTTP requests never trigger upstream API calls once the cache
holds at least one image, so they cannot be used to burn the Unsplash quota.

Routes
    GET|HEAD /healthz          liveness probe (not rate limited)
    GET|HEAD /json/<any>       Unsplash-style JSON summary
    GET|HEAD /<any>            full-screen HTML page with a random image
    GET|HEAD /<any>/raw/<any>  the random image itself (image/jpeg)
    ... with a "resize" path segment the image is downscaled to 1080x608 max.

Run a SINGLE worker process: each worker has its own refresh task, cache
bookkeeping and in-memory rate limiter.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import random
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from PIL import Image
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from None
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ACCESS_KEY = (os.getenv("ACCESS_KEY") or "").strip()
if not ACCESS_KEY:
    raise RuntimeError("ACCESS_KEY environment variable is not set.")

_SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SERVER_DIR.parent
_TEMPLATE_DIR = _SERVER_DIR / "templates"

DEBUG = _env_bool("DEBUG", False)
BASE_URL = (os.getenv("BASE_URL") or "http://localhost:1962").rstrip("/")
CACHE = Path(os.getenv("CACHE") or _PROJECT_DIR / "cache").resolve()
MAX_FILES = _env_int("MAX_FILES", 500, minimum=1)

UNSPLASH_QUERY = os.getenv("UNSPLASH_QUERY") or "disney"
UNSPLASH_ORIENTATION = os.getenv("UNSPLASH_ORIENTATION") or "landscape"
if UNSPLASH_ORIENTATION not in {"landscape", "portrait", "squarish"}:
    raise RuntimeError("UNSPLASH_ORIENTATION must be landscape, portrait or squarish.")
DESCRIPTION = os.getenv("DESCRIPTION") or "Walt Disney World"
IMAGE_WIDTH = _env_int("IMAGE_WIDTH", 1920, minimum=320)

# Seconds between page reloads in the HTML view (0 disables auto-reload).
PAGE_REFRESH_SECONDS = _env_int("PAGE_REFRESH_SECONDS", 30)
# Seconds between background downloads of a fresh image (0 disables the task;
# the cache is then only filled on demand while it is empty).
REFRESH_INTERVAL = _env_int("REFRESH_INTERVAL", 300)
# After a failed upstream fetch, don't try again on demand for this long.
FETCH_COOLDOWN = 30

RATE_LIMIT = os.getenv("RATE_LIMIT") or "10/minute"
JSON_RATE_LIMIT = os.getenv("JSON_RATE_LIMIT") or "60/minute"
TRACK_DOWNLOADS = _env_bool("TRACK_DOWNLOADS", True)

CACHE.mkdir(parents=True, exist_ok=True)

# Hard cap on bytes accepted from a single download (after decompression).
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Reject anything bigger than this many pixels (Pillow raises at 2x this value).
MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
RESIZE_SIZE = (1080, 608)

# Only these hosts are ever contacted for image bytes / API calls.
_ALLOWED_IMAGE_HOSTS = frozenset({"images.unsplash.com", "plus.unsplash.com"})
_API_HOST = "api.unsplash.com"

_CACHE_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jpg$"
)

# Tests may set this to an httpx transport (e.g. httpx.MockTransport).
_HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None

_fetch_lock = asyncio.Lock()
_last_failure = float("-inf")


# ---------------------------------------------------------------------------
# Security headers / CSP
# ---------------------------------------------------------------------------

def _build_csp() -> str:
    directives = [
        "default-src 'none'",
        "img-src data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
    source = (_TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
    match = re.search(r"<style>(.*?)</style>", source, re.DOTALL)
    if match:
        digest = base64.b64encode(
            hashlib.sha256(match.group(1).encode("utf-8")).digest()
        ).decode("ascii")
        directives.append(f"style-src 'sha256-{digest}'")
    return "; ".join(directives)


_CSP = _build_csp()


# ---------------------------------------------------------------------------
# Cache helpers (synchronous; call via asyncio.to_thread from async code)
# ---------------------------------------------------------------------------

def _list_cache() -> list[Path]:
    """Cached images only: UUID-named regular files ending in .jpg."""
    try:
        with os.scandir(CACHE) as it:
            return [
                Path(e.path)
                for e in it
                if _CACHE_NAME.match(e.name) and e.is_file(follow_symlinks=False)
            ]
    except OSError as exc:
        logger.error("Cannot read cache directory: %s", exc)
        return []


def _cache_count() -> int:
    return len(_list_cache())


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _evict_cache_if_needed() -> None:
    files = sorted(_list_cache(), key=_mtime)
    for path in files[: max(0, len(files) - MAX_FILES)]:
        path.unlink(missing_ok=True)


def _cleanup_tmp_files() -> None:
    for tmp in CACHE.glob(".tmp-*"):
        tmp.unlink(missing_ok=True)


def _store_image(data: bytes) -> str:
    """Atomically write a sanitised JPEG into the cache and evict old files."""
    name = f"{uuid.uuid4()}.jpg"
    tmp = CACHE / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, CACHE / name)  # readers never see a partial file
    finally:
        tmp.unlink(missing_ok=True)
    _evict_cache_if_needed()
    logger.info("Cached new image %s", name)
    return name


def _sanitize_jpeg(data: bytes) -> bytes:
    """
    Fully decode and re-encode the download.

    Rejects non-JPEG data, truncated files and oversized images, and strips
    metadata and any trailing bytes (polyglot files) from what gets cached.
    """
    with Image.open(io.BytesIO(data)) as img:
        if img.format != "JPEG":
            raise ValueError(f"unexpected image format {img.format!r}")
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise ValueError("image dimensions too large")
        img.load()
        rgb = img.convert("RGB")
    out = io.BytesIO()
    rgb.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def _resize_jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        img.draft("RGB", RESIZE_SIZE)
        rgb = img.convert("RGB")
    rgb.thumbnail(RESIZE_SIZE)
    out = io.BytesIO()
    rgb.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def _read_random_image(resize: bool) -> tuple[bytes, str, str] | None:
    """Return (jpeg_bytes, filename, etag) for a random cached image."""
    candidates = _list_cache()
    for path in random.sample(candidates, k=min(3, len(candidates))):
        try:
            data = path.read_bytes()  # may vanish if evicted concurrently
            tag = path.stem
            if resize:
                data = _resize_jpeg(data)
                tag += f"-{RESIZE_SIZE[0]}"
        except Exception as exc:
            logger.warning("Skipping unreadable cache file %s: %s", path.name, exc)
            continue
        return data, path.name, f'"{tag}"'
    return None


def _check_not_modified(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match", "").strip()
    if header == "*":
        return True
    candidates = {e.strip().removeprefix("W/") for e in header.split(",")}
    return etag in candidates


# ---------------------------------------------------------------------------
# Upstream (Unsplash) access
# ---------------------------------------------------------------------------

class _FetchError(Exception):
    pass


def _validate_image_url(url: str) -> bool:
    """SSRF guard: https, allow-listed host, default port, no credentials."""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _ALLOWED_IMAGE_HOSTS
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _validate_api_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == _API_HOST
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _sized_image_url(raw_url: str) -> str:
    """Ask the image CDN for a right-sized JPEG instead of the multi-MB original."""
    parsed = urlparse(raw_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"w": str(IMAGE_WIDTH), "fit": "max", "q": "85", "fm": "jpg"})
    return urlunparse(parsed._replace(query=urlencode(query)))


def _http_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,  # never follow redirects off the allow-list
        transport=_HTTP_TRANSPORT,
    )


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise _FetchError("declared image size exceeds MAX_IMAGE_BYTES")
        buf = bytearray()
        async for chunk in resp.aiter_bytes(chunk_size=65536):
            buf.extend(chunk)
            if len(buf) > MAX_IMAGE_BYTES:
                raise _FetchError("image exceeded MAX_IMAGE_BYTES")
    return bytes(buf)


async def _fetch_new_image() -> bool:
    """
    Fetch one random image from Unsplash into the cache.  Never raises.
    Callers should hold _fetch_lock.
    """
    global _last_failure
    headers = {
        # Header, not query string: keeps the key out of URLs and logs.
        "Authorization": f"Client-ID {ACCESS_KEY}",
        "Accept-Version": "v1",
    }
    params = {"query": UNSPLASH_QUERY, "orientation": UNSPLASH_ORIENTATION}
    try:
        async with _http_client(10) as client:
            api_resp = await client.get(
                f"https://{_API_HOST}/photos/random", params=params, headers=headers
            )
            api_resp.raise_for_status()
            photo = api_resp.json()
            image_url = _sized_image_url(photo["urls"]["raw"])
            download_location = (photo.get("links") or {}).get("download_location")

        if not _validate_image_url(image_url):
            raise _FetchError(f"disallowed image URL host: {urlparse(image_url).hostname!r}")

        async with _http_client(30) as client:
            raw = await _download_image(client, image_url)

        clean = await asyncio.to_thread(_sanitize_jpeg, raw)
        await asyncio.to_thread(_store_image, clean)

        # Unsplash API guidelines ask apps to report each photo use.  Best effort.
        if TRACK_DOWNLOADS and download_location and _validate_api_url(download_location):
            try:
                async with _http_client(10) as client:
                    await client.get(download_location, headers=headers)
            except Exception as exc:
                logger.warning("Unsplash download tracking failed: %s", type(exc).__name__)
        return True
    except Exception as exc:
        _last_failure = time.monotonic()
        logger.warning("Failed to fetch from Unsplash: %s: %s", type(exc).__name__, exc)
        return False


async def _ensure_image_available() -> bool:
    """True if the cache holds at least one image (fetching one if it is empty)."""
    if await asyncio.to_thread(_cache_count) > 0:
        return True
    async with _fetch_lock:  # one upstream call for any number of waiters
        if await asyncio.to_thread(_cache_count) > 0:
            return True
        if time.monotonic() - _last_failure < FETCH_COOLDOWN:
            return False
        return await _fetch_new_image()


async def _refresh_once() -> None:
    async with _fetch_lock:
        await _fetch_new_image()


async def _refresher() -> None:
    """Background task: add one fresh image every REFRESH_INTERVAL seconds."""
    if await asyncio.to_thread(_cache_count) == 0:
        await _refresh_once()
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        await _refresh_once()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_: FastAPI):
    _cleanup_tmp_files()
    task = asyncio.create_task(_refresher()) if REFRESH_INTERVAL > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


# Interactive docs / OpenAPI schema are disabled: nothing to gain by publishing
# the route table of a public service.
app = FastAPI(
    debug=DEBUG,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    if BASE_URL.startswith("https://"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def _unavailable() -> Response:
    return Response(
        "No image available yet, try again shortly.",
        status_code=503,
        headers={"Retry-After": "30", "Content-Type": "text/plain; charset=utf-8"},
    )


def _make_summary() -> dict:
    return {
        "urls": {"raw": f"{BASE_URL}/raw/{uuid.uuid4()}.jpg"},
        "description": DESCRIPTION,
    }


# Routes are matched in registration order: specific ones first, catch-all last.

@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> Response:
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.api_route("/json/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit(JSON_RATE_LIMIT)
async def json_summary(request: Request, path: str = ""):
    if request.method == "HEAD":
        return Response(headers={"Content-Type": "application/json"})
    return JSONResponse(_make_summary())


@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.shared_limit(RATE_LIMIT, scope="images")
async def random_image(request: Request, path: str = ""):
    segments = path.split("/")
    raw = "raw" in segments
    resize = "resize" in segments

    if request.method == "HEAD":
        # The image is random, so per-image headers from a HEAD would not match
        # the following GET.  Answer cheaply without touching the cache.
        return Response(
            status_code=200,
            headers={
                "Content-Type": "image/jpeg" if raw else "text/html; charset=utf-8",
                "Cache-Control": "no-store",
            },
        )

    if not await _ensure_image_available():
        return _unavailable()
    picked = await asyncio.to_thread(_read_random_image, resize)
    if picked is None:
        return _unavailable()
    data, name, etag = picked

    headers = {
        "Content-Disposition": f'inline; filename="{name}"',
        "Cache-Control": "no-cache",
        "ETag": etag,
    }
    if _check_not_modified(request, etag):
        return Response(status_code=304, headers=headers)

    if raw:
        return Response(content=data, media_type="image/jpeg", headers=headers)

    body = templates.TemplateResponse(
        request,
        "index.html",
        {
            "img_data": base64.b64encode(data).decode("ascii"),
            "refresh_seconds": PAGE_REFRESH_SECONDS,
        },
    ).body
    return Response(content=body, media_type="text/html", headers=headers)
