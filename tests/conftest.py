import io
import os
import tempfile

# Isolated settings: temporary data dir, no .env, no live key.
_TMP = tempfile.mkdtemp(prefix="claims-test-")
os.environ["PROTOTYPE_SKIP_DOTENV"] = "1"
os.environ["DATA_DIR"] = _TMP
os.environ["GEMINI_API_KEY"] = ""
os.environ["GEMINI_MODEL"] = "test-model"

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app import services  # noqa: E402
from app.db import Base, engine  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    services.extractor_factory = None
    services.run_inline = True
    yield
    services.extractor_factory = None


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def other_client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def png_bytes(color=(240, 240, 240), size=(300, 400)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()
