"""Application settings, read from environment variables and a local .env file.

Secrets (GEMINI_API_KEY) are only ever read here, on the server. They are never
rendered into templates, sent to the browser, or written to logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / ".env"

if os.environ.get("PROTOTYPE_SKIP_DOTENV") != "1":
    try:
        from dotenv import dotenv_values

        # A non-empty real environment variable takes precedence over .env;
        # an empty or unset one does not hide the value from .env.
        for _k, _v in dotenv_values(ENV_FILE).items():
            if _v is not None and not os.environ.get(_k, "").strip():
                os.environ[_k] = _v
    except ImportError:  # pragma: no cover - python-dotenv is a hard dependency
        pass

_PLACEHOLDER_MARKERS = ("your-", "your_", "placeholder", "changeme", "<", "xxx")


def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return (not v) or any(m in v for m in _PLACEHOLDER_MARKERS)


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    gemini_timeout_s: int
    gemini_fallback_models: tuple
    max_upload_mb: float
    max_files_per_claim: int
    max_pdf_pages: int
    data_dir: Path
    secure_cookies: bool
    extraction_workers: int

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def live_extraction_configured(self) -> bool:
        return not _is_placeholder(self.gemini_api_key) and bool(self.gemini_model.strip())

    @property
    def missing_live_config(self) -> list:
        missing = []
        if _is_placeholder(self.gemini_api_key):
            missing.append("GEMINI_API_KEY")
        if not self.gemini_model.strip():
            missing.append("GEMINI_MODEL")
        return missing

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def render_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'claims.db'}"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
    if not data_dir.is_absolute():
        data_dir = (BASE_DIR / data_dir).resolve()
    return Settings(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "").strip(),
        gemini_timeout_s=_int("GEMINI_TIMEOUT_SECONDS", 90),
        gemini_fallback_models=tuple(m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()),
        max_upload_mb=_float("MAX_UPLOAD_MB", 5),
        max_files_per_claim=_int("MAX_FILES_PER_CLAIM", 12),
        max_pdf_pages=_int("MAX_PDF_PAGES", 10),
        data_dir=data_dir,
        secure_cookies=_bool("SECURE_COOKIES", False),
        extraction_workers=max(1, _int("EXTRACTION_WORKERS", 3)),
    )


settings = load_settings()
