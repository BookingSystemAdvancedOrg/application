"""user-backdrop: an image whose header announces too many pixels is refused
as not_an_image before any decode (a small PNG can expand to hundreds of MB)."""

import importlib.util
import io
from pathlib import Path

import pytest
from PIL import Image

APP = Path(__file__).parents[1] / "functions" / "user-backdrop" / "app.py"


@pytest.fixture
def app(monkeypatch):
    for key, value in {"AWS_DEFAULT_REGION": "eu-north-1", "ENVIRONMENT": "dev",
                       "USER_BACKDROPS_BUCKET_NAME": "b", "USER_BACKDROPS_TABLE_NAME": "t"}.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("user_backdrop_dims", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png(size):
    out = io.BytesIO()
    Image.new("1", size).save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("fn", ["make_thumbnail", "make_avatar"])
def test_too_many_pixels_is_refused_before_decode(app, fn, monkeypatch):
    data = _png((7000, 6000))  # 42 MP: under Pillow's bomb limit, over the decode cap
    assert len(data) < 1024 * 1024
    decoded = []
    monkeypatch.setattr(app.ImageOps, "exif_transpose", lambda img: decoded.append(img) or img)
    with pytest.raises(ValueError, match="not_an_image"):
        getattr(app, fn)(data)
    assert decoded == []


@pytest.mark.parametrize("fn", ["make_thumbnail", "make_avatar"])
def test_too_wide_is_refused(app, fn):
    with pytest.raises(ValueError, match="not_an_image"):
        getattr(app, fn)(_png((20001, 10)))


def test_normal_image_still_works(app):
    width, height, thumb = app.make_thumbnail(_png((1200, 800)))
    assert (width, height) == (1200, 800) and thumb
    assert app.make_avatar(_png((600, 600)))
