# Unsplash Random Wallpaper Server

A small, hardened [FastAPI](https://fastapi.tiangolo.com/) service that serves a random full-screen wallpaper from [Unsplash](https://unsplash.com/developers). By default it is themed around **Disney / Walt Disney World** photography; the search query is configurable.

Use it as a browser-kiosk or dashboard background, a smart-TV / Raspberry Pi screensaver, a digital photo frame source, or for any client that wants "a nice random picture at a URL" or Unsplash-style JSON.

A background task downloads images from the Unsplash API, sanitises them and keeps them in a local disk cache. Web requests are served **from the cache only**, so anonymous visitors can never use up your Unsplash API quota.

## Features

- **Full-screen wallpaper page**: the image fills the viewport and the page reloads itself every 30 s (configurable). The page contains no JavaScript.
- **Raw image endpoint**: any path containing a `/raw/` segment returns the JPEG, with `ETag` / `304 Not Modified` support.
- **Optional downscaling**: add a `/resize/` path segment to get a copy no larger than 1080x608 (e.g. `/raw/resize/x.jpg`).
- **Unsplash-style JSON** at `/json/...`, returning `{"urls": {"raw": ...}, "description": ...}` with a unique `/raw/<uuid>.jpg` URL per call.
- **Right-sized downloads**: images are requested from the CDN at `IMAGE_WIDTH` (default 1920 px) rather than as multi-MB originals.
- **Health endpoint** at `/healthz` for load balancers and containers.

## Security highlights

- **No upstream calls on the request path** once the cache has an image; fetching happens on a fixed schedule (`REFRESH_INTERVAL`), serialised by a lock, with a cool-down after failures.
- **SSRF guard**: only `https` URLs on Unsplash's CDN hosts (default port, no credentials) are fetched; redirects are never followed.
- **Untrusted-image handling**: size cap (20 MB), pixel cap, JPEG-only, full decode, then **re-encode**, which strips metadata and any appended payload. Truncated or non-JPEG files are rejected.
- **Cache hygiene**: UUID filenames, atomic writes (`os.replace`), only regular `*.jpg` UUID files are ever listed or served (no stray files, no symlinks), temp files cleaned on start-up.
- **API key** is sent in the `Authorization` header only, never in a URL or log line.
- **Response headers**: strict `Content-Security-Policy` (`default-src 'none'`, hash-pinned stylesheet, `img-src data:`, no scripts), `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and HSTS when `BASE_URL` is `https`.
- **No `/docs`, `/redoc` or `/openapi.json`**; the route table isn't published.
- **Per-IP rate limiting** (`slowapi`) with proper `429` responses.
- **Sandboxed systemd unit**, non-root Docker image, pinned dependencies, CI with `pip-audit`, Dependabot.

## Project layout

```
.
├── asgi.py                  # ASGI entry point (re-exports the app)
├── server/
│   ├── unsplash.py          # application
│   └── templates/index.html # full-screen page (static CSS, no JS)
├── cache/                   # image cache (contents git-ignored)
├── tests/                   # pytest suite
├── deploy/nginx.conf.example
├── unsplash.service         # hardened systemd unit
├── Dockerfile
├── requirements.txt         # pinned runtime deps
├── requirements-dev.txt
├── .env.example
└── .github/                 # CI workflow + Dependabot
```

## Requirements

- Python **3.10+**
- An Unsplash developer account and an **Access Key** ([create an application](https://unsplash.com/oauth/applications))

## Quick start

```bash
git clone https://github.com/<you>/<repo>.git && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # set ACCESS_KEY
chmod 600 .env

uvicorn server.unsplash:app --host 127.0.0.1 --port 1962 --workers 1
```

Open <http://127.0.0.1:1962/>. On first start the cache is empty, so the first request (or the start-up task) downloads an image; until then you may briefly see `503 Retry-After: 30`.

> **Run exactly one worker.** The refresh task, cache bookkeeping and in-memory rate limiter are per process.

## Configuration

Environment variables (a `.env` file is loaded automatically). See [`.env.example`](.env.example).

| Variable | Default | Description |
|---|---|---|
| `ACCESS_KEY` | *required* | Unsplash API Access Key. The app refuses to start without it. |
| `BASE_URL` | `http://localhost:1962` | Public URL, used for the `raw` link in `/json/`. An `https://` value also enables HSTS. |
| `CACHE` | `<project>/cache` | Cache directory (must be writable by the service user). |
| `MAX_FILES` | `500` | Cached images kept; the oldest are evicted. |
| `UNSPLASH_QUERY` | `disney` | Search term sent to Unsplash. |
| `UNSPLASH_ORIENTATION` | `landscape` | `landscape`, `portrait` or `squarish`. |
| `DESCRIPTION` | `Walt Disney World` | Text returned by `/json/`. |
| `IMAGE_WIDTH` | `1920` | Width requested from the image CDN (px). |
| `PAGE_REFRESH_SECONDS` | `30` | Reload interval of the HTML page; `0` disables. |
| `REFRESH_INTERVAL` | `300` | Seconds between background downloads; `0` disables the task (cache then only fills on demand while empty). Keep this within your Unsplash quota (demo apps are limited per hour). |
| `RATE_LIMIT` | `10/minute` | Per-IP limit shared by the HTML page and raw images. |
| `JSON_RATE_LIMIT` | `60/minute` | Per-IP limit for `/json/`. |
| `TRACK_DOWNLOADS` | `true` | Report each photo use to Unsplash, as their API guidelines request. |
| `DEBUG` | `false` | FastAPI debug mode. **Never enable in production.** |

## Endpoints

| Method | Path | Response |
|---|---|---|
| `GET` `HEAD` | `/healthz` | `{"status":"ok"}` (not rate limited) |
| `GET` `HEAD` | `/json/<anything>` | JSON summary with a unique `/raw/<uuid>.jpg` URL |
| `GET` | `/<anything>` | HTML page with a random image |
| `GET` | `/<anything>/raw/<anything>` | Random image, `image/jpeg`, `ETag` + `304` support |
| `GET` | `.../resize/...` | Same, downscaled to at most 1080x608 |
| `HEAD` | `/<anything>` | `200` with content type only. The image is random, so a HEAD can't describe the next GET. |

```bash
curl -o wallpaper.jpg http://127.0.0.1:1962/raw/wallpaper.jpg
curl http://127.0.0.1:1962/json/random
```

## Deployment

### systemd

```bash
sudo useradd --system --home /var/www/unsplash --shell /usr/sbin/nologin unsplash
sudo mkdir -p /var/www/unsplash && sudo cp -r . /var/www/unsplash
cd /var/www/unsplash
sudo python3 -m venv vpython && sudo vpython/bin/pip install -r requirements.txt
sudo chown -R root:unsplash /var/www/unsplash
sudo chown unsplash:unsplash cache && sudo chmod 640 .env

sudo cp unsplash.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now unsplash
journalctl -u unsplash -f
```

The unit runs as the unprivileged `unsplash` user with a restrictive sandbox (`ProtectSystem=strict`, only `cache/` writable, empty capability set, syscall filter, ...). The `cache/` directory **must exist** because of `ReadWritePaths=`.

### Reverse proxy

Bind uvicorn to loopback and put nginx (or Caddy/Traefik) in front for TLS. A complete example with request limiting and optional access control is in [`deploy/nginx.conf.example`](deploy/nginx.conf.example).

The per-IP rate limiter needs the real client address: the proxy must send `X-Forwarded-For`, and uvicorn is told to trust it **only from** `--forwarded-allow-ips` (default `127.0.0.1`). Don't expose the app port directly to the internet.

### Docker

```bash
docker build -t unsplash-wallpaper .
docker run -d --name wallpaper \
  --env-file .env \
  -p 127.0.0.1:1962:1962 \
  -v wallpaper-cache:/app/cache \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  unsplash-wallpaper
```

The image runs as a non-root user and includes a health check. If a proxy on the host fronts the container, set `-e FORWARDED_ALLOW_IPS=<docker bridge gateway IP>` so client IPs are resolved correctly.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The tests use mocked HTTP transports; they never contact Unsplash.

## How it works

1. At start-up the app removes leftover temp files and, if `REFRESH_INTERVAL > 0`, starts a task that downloads one new image immediately if the cache is empty and then every `REFRESH_INTERVAL` seconds.
2. Each download: Unsplash API call -> CDN URL validated against the allow-list -> streamed with a hard byte cap -> decoded, size-checked and re-encoded -> written atomically as `<uuid>.jpg` -> old files evicted beyond `MAX_FILES`.
3. A web request picks a random cached file and returns it as raw JPEG, or base64-embedded in the HTML page.
4. If the cache is empty, requests wait on a single shared fetch; after a failure further on-demand attempts pause for 30 s.

## Limitations

- **Unsplash API terms.** Caching and re-serving images does not follow Unsplash's "hotlink the photos" guideline, and photographer attribution is not displayed. The app does report downloads (`TRACK_DOWNLOADS`), but please read the current [API Guidelines](https://help.unsplash.com/en/articles/2511245-unsplash-api-guidelines) and make sure your use is compliant, especially for a public deployment.
- Single process only; the rate limiter is in memory.
- `/resize/` re-encodes on each request (cheap at these sizes, but not free).
- No built-in authentication. Use proxy-level `allow`/`deny` or basic auth if the service must not be public.
- To allow embedding in an `<iframe>`, change `frame-ancestors` in the CSP and `X-Frame-Options` in `server/unsplash.py`.

## Upgrading from the original version

- The default cache moved from `server/cache/` to `./cache/`. Set `CACHE=server/cache` (absolute path) to keep the old location, or move your files.
- `LISTEN` and `PORT` were never used and are gone; bind address/port come from the uvicorn command line. The `BASE_URL` default is now port 1962.
- `/resize/` now really resizes. Images are fetched at `IMAGE_WIDTH` instead of full size.
- Fresh images arrive via the scheduled background task, not 10 % of random requests.
- `/docs`, `/redoc` and `/openapi.json` are disabled.
- Error responses are `503` (no image yet) / `429` (rate limit) instead of `500`.
- The service now runs as user `unsplash` instead of `www-data` (see systemd section).

## License

Add a `LICENSE` file (for example MIT) and reference it here.

Photos are provided by [Unsplash](https://unsplash.com) and remain the property of their photographers.
