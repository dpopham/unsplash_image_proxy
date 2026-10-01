import asyncio
import base64
import hashlib
import io
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from .conftest import make_jpeg


def client(mod):
    return TestClient(mod.app)


def offline(mod, monkeypatch, status=500):
    """Make any upstream call fail fast without touching the network."""
    monkeypatch.setattr(
        mod, "_HTTP_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(status))
    )


# --------------------------------------------------------------------- routes

def test_html_page(load_app, seed):
    mod = load_app()
    seed(mod)
    r = client(mod).get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "data:image/jpeg;base64," in r.text
    assert '<meta http-equiv="refresh" content="30">' in r.text


def test_page_refresh_can_be_disabled(load_app, seed):
    mod = load_app(PAGE_REFRESH_SECONDS="0")
    seed(mod)
    assert "http-equiv" not in client(mod).get("/").text


def test_raw_image_and_etag(load_app, seed):
    mod = load_app()
    seed(mod)
    c = client(mod)
    r = c.get("/raw/wallpaper.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(r.content)).format == "JPEG"
    etag = r.headers["etag"]
    r2 = c.get("/raw/wallpaper.jpg", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""


def test_resize_downscales(load_app, seed):
    mod = load_app()
    seed(mod, size=(3000, 2000))
    r = client(mod).get("/raw/resize/x.jpg")
    img = Image.open(io.BytesIO(r.content))
    assert r.status_code == 200
    assert img.width <= 1080 and img.height <= 608


def test_head_is_cheap_and_needs_no_cache(load_app):
    mod = load_app()  # empty cache, no upstream configured
    r = client(mod).head("/")
    assert r.status_code == 200 and r.content == b""
    assert client(mod).head("/raw/a.jpg").headers["content-type"] == "image/jpeg"


def test_json_summary(load_app):
    mod = load_app()
    r = client(mod).get("/json/anything")
    body = r.json()
    assert re.fullmatch(r"https://wall\.example\.com/raw/[0-9a-f-]{36}\.jpg", body["urls"]["raw"])
    assert body["description"] == "Walt Disney World"


def test_healthz_and_misc_routes(load_app):
    mod = load_app()
    c = client(mod)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/favicon.ico").status_code == 204
    assert "Disallow: /" in c.get("/robots.txt").text


def test_api_docs_not_exposed(load_app, seed):
    mod = load_app()
    seed(mod)
    c = client(mod)
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = c.get(path)
        assert "swagger" not in r.text.lower() and '"openapi"' not in r.text


# ------------------------------------------------------------------- security

def test_security_headers_and_csp_hash(load_app, seed):
    mod = load_app()
    seed(mod)
    r = client(mod).get("/")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert "max-age=" in h["strict-transport-security"]  # BASE_URL is https
    style = re.search(r"<style>(.*?)</style>", r.text, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in h["content-security-policy"]
    assert "script-src" not in h["content-security-policy"]  # falls back to 'none'
    assert "<script" not in r.text


def test_no_hsts_over_plain_http(load_app):
    mod = load_app(BASE_URL="http://localhost:1962")
    assert "strict-transport-security" not in client(mod).get("/healthz").headers


def test_rate_limit_returns_429_but_not_for_healthz(load_app, seed):
    mod = load_app(RATE_LIMIT="3/minute")
    seed(mod)
    c = client(mod)
    assert [c.get("/raw/x.jpg").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert c.get("/healthz").status_code == 200


def test_stray_files_and_symlinks_are_never_served(load_app, monkeypatch, tmp_path):
    mod = load_app()
    offline(mod, monkeypatch)
    (mod.CACHE / ".gitkeep").write_text("<script>alert(1)</script>")
    (mod.CACHE / "notes.txt").write_text("hi")
    (mod.CACHE / "not-a-uuid.jpg").write_bytes(make_jpeg())
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(make_jpeg())
    (mod.CACHE / "11111111-1111-1111-1111-111111111111.jpg").symlink_to(outside)
    assert mod._list_cache() == []
    r = client(mod).get("/raw/x.jpg")
    assert r.status_code == 503 and r.headers["retry-after"] == "30"


@pytest.mark.parametrize(
    "url",
    [
        "http://images.unsplash.com/a.jpg",
        "https://evil.example.com/a.jpg",
        "https://images.unsplash.com@evil.example.com/a.jpg",
        "https://images.unsplash.com.evil.example.com/a.jpg",
        "https://images.unsplash.com:8443/a.jpg",
        "https://user:pw@images.unsplash.com/a.jpg",
        "https://images.unsplash.com:abc/a.jpg",
        "file:///etc/passwd",
    ],
)
def test_ssrf_guard_rejects(load_app, url):
    assert load_app()._validate_image_url(url) is False


def test_ssrf_guard_accepts_unsplash_cdn(load_app):
    mod = load_app()
    assert mod._validate_image_url("https://images.unsplash.com/photo-1?ixid=a")
    assert mod._validate_api_url("https://api.unsplash.com/photos/1/download?ixid=a")
    assert not mod._validate_api_url("https://images.unsplash.com/photos/1/download")


def test_config_validation(load_app):
    with pytest.raises(RuntimeError, match="ACCESS_KEY"):
        load_app(ACCESS_KEY="")
    with pytest.raises(RuntimeError, match="MAX_FILES"):
        load_app(MAX_FILES="lots")
    with pytest.raises(RuntimeError, match="ORIENTATION"):
        load_app(MAX_FILES="500", UNSPLASH_ORIENTATION="diagonal")


# ---------------------------------------------------------------------- cache

def test_store_is_atomic_and_evicts(load_app):
    mod = load_app(MAX_FILES="3")
    for _ in range(5):
        mod._store_image(make_jpeg())
        time.sleep(0.01)
    assert len(mod._list_cache()) == 3
    assert not list(mod.CACHE.glob(".tmp-*"))


def test_cleanup_removes_leftover_temp_files(load_app):
    mod = load_app()
    (mod.CACHE / ".tmp-abc").write_bytes(b"partial")
    mod._cleanup_tmp_files()
    assert not list(mod.CACHE.glob(".tmp-*"))


def test_concurrent_requests_cause_one_upstream_fetch(load_app, monkeypatch):
    mod = load_app()
    calls = []

    async def fake_fetch():
        calls.append(1)
        await asyncio.sleep(0.05)
        mod._store_image(make_jpeg())
        return True

    monkeypatch.setattr(mod, "_fetch_new_image", fake_fetch)

    async def run():
        return await asyncio.gather(*[mod._ensure_image_available() for _ in range(10)])

    assert all(asyncio.run(run()))
    assert len(calls) == 1


def test_failed_fetch_triggers_cooldown(load_app, monkeypatch):
    mod = load_app()
    calls = []

    async def failing_fetch():
        calls.append(1)
        mod._last_failure = time.monotonic()
        return False

    monkeypatch.setattr(mod, "_fetch_new_image", failing_fetch)

    async def run():
        return [await mod._ensure_image_available() for _ in range(5)]

    assert asyncio.run(run()) == [False] * 5
    assert len(calls) == 1  # the other four were short-circuited by the cooldown


def test_anonymous_requests_never_hit_upstream_when_cache_has_images(load_app, seed, monkeypatch):
    mod = load_app()
    seed(mod, n=5)

    def boom(request):  # any upstream call is a failure
        raise AssertionError("upstream called")

    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", httpx.MockTransport(boom))
    c = client(mod)
    assert all(c.get("/raw/x.jpg").status_code == 200 for _ in range(50))


# ------------------------------------------------------------------- fetching

def _upstream(photo_raw="https://images.unsplash.com/photo-1?ixid=abc", image=None,
              content_type="image/jpeg", log=None):
    image = image if image is not None else make_jpeg((2400, 1350))

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        if request.url.host == "api.unsplash.com" and request.url.path == "/photos/random":
            assert request.headers["authorization"] == "Client-ID test-key"
            assert "test-key" not in str(request.url)
            return httpx.Response(200, json={
                "urls": {"raw": photo_raw},
                "links": {"download_location": "https://api.unsplash.com/photos/1/download?ixid=abc"},
            })
        if request.url.host == "api.unsplash.com":
            return httpx.Response(200, json={"url": "x"})
        if request.url.host == "images.unsplash.com":
            assert request.url.params["w"] == "1920" and request.url.params["fm"] == "jpg"
            return httpx.Response(200, content=image, headers={"content-type": content_type})
        raise AssertionError(f"unexpected host {request.url.host}")

    return httpx.MockTransport(handler)


def test_fetch_success_sanitizes_and_tracks_download(load_app, monkeypatch):
    mod = load_app()
    log = []
    # JPEG with junk appended after the EOI marker, as a polyglot would have.
    dirty = make_jpeg((2400, 1350)) + b"<?php system($_GET['c']); ?>"
    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", _upstream(image=dirty, log=log))
    assert asyncio.run(mod._fetch_new_image()) is True
    (stored,) = mod._list_cache()
    assert b"<?php" not in stored.read_bytes()
    assert Image.open(stored).format == "JPEG"
    assert any("/photos/1/download" in u for u in log)


def test_fetch_rejects_non_jpeg(load_app, monkeypatch):
    mod = load_app()
    buf = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buf, "PNG")
    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", _upstream(image=buf.getvalue()))
    assert asyncio.run(mod._fetch_new_image()) is False
    assert mod._list_cache() == []


def test_fetch_rejects_garbage(load_app, monkeypatch):
    mod = load_app()
    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", _upstream(image=b"not an image"))
    assert asyncio.run(mod._fetch_new_image()) is False


def test_fetch_rejects_truncated_jpeg(load_app, monkeypatch):
    mod = load_app()
    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", _upstream(image=make_jpeg((800, 600))[:400]))
    assert asyncio.run(mod._fetch_new_image()) is False


def test_fetch_rejects_oversized_download(load_app, monkeypatch):
    mod = load_app()
    monkeypatch.setattr(mod, "MAX_IMAGE_BYTES", 1000)
    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", _upstream(image=make_jpeg((2400, 1350))))
    assert asyncio.run(mod._fetch_new_image()) is False


def test_fetch_rejects_too_many_pixels(load_app, monkeypatch):
    mod = load_app()
    monkeypatch.setattr(mod, "MAX_IMAGE_PIXELS", 1000)
    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", _upstream())
    assert asyncio.run(mod._fetch_new_image()) is False


def test_fetch_rejects_image_url_on_other_host(load_app, monkeypatch):
    mod = load_app()
    log = []
    monkeypatch.setattr(
        mod, "_HTTP_TRANSPORT", _upstream(photo_raw="https://evil.example.com/x.jpg", log=log)
    )
    assert asyncio.run(mod._fetch_new_image()) is False
    assert not any("evil.example.com" in u for u in log)  # never contacted


def test_fetch_does_not_follow_redirects(load_app, monkeypatch):
    mod = load_app()

    def handler(request):
        if request.url.host == "api.unsplash.com":
            return httpx.Response(200, json={"urls": {"raw": "https://images.unsplash.com/p"}})
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    monkeypatch.setattr(mod, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    assert asyncio.run(mod._fetch_new_image()) is False
    assert mod._list_cache() == []


def test_failure_does_not_leak_access_key(load_app, monkeypatch, caplog):
    mod = load_app()
    offline(mod, monkeypatch, status=401)
    with caplog.at_level("WARNING"):
        asyncio.run(mod._fetch_new_image())
    assert "test-key" not in caplog.text
