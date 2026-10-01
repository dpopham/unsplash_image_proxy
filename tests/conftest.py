import importlib
import io
import sys
import uuid

import pytest
from PIL import Image


def make_jpeg(size=(320, 180), color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def load_app(tmp_path, monkeypatch):
    """Import a fresh copy of server.unsplash with the given environment."""

    def _load(**env):
        values = {
            "ACCESS_KEY": "test-key",
            "CACHE": str(tmp_path / "cache"),
            "REFRESH_INTERVAL": "0",
            "RATE_LIMIT": "1000/minute",
            "JSON_RATE_LIMIT": "1000/minute",
            "BASE_URL": "https://wall.example.com",
        }
        values.update(env)
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        sys.modules.pop("server.unsplash", None)
        return importlib.import_module("server.unsplash")

    yield _load
    sys.modules.pop("server.unsplash", None)


@pytest.fixture
def seed():
    """Write n valid cached images into the module's cache; return their names."""

    def _seed(mod, n=1, size=(320, 180)):
        names = []
        for _ in range(n):
            name = f"{uuid.uuid4()}.jpg"
            (mod.CACHE / name).write_bytes(make_jpeg(size))
            names.append(name)
        return names

    return _seed
