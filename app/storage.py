"""Private file storage.

Uploaded documents are kept in DATA_DIR (outside the static directory) under
random names. They are only served through session-scoped routes.
"""
from __future__ import annotations

import hashlib
import io
import re
import secrets
import unicodedata
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageOps

from .config import settings

Image.MAX_IMAGE_PIXELS = 60_000_000  # guard against decompression bombs

MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
_NAME_RE = re.compile(r"^[a-f0-9]{32}(_p\d{1,3})?\.(jpg|png|pdf)$")


class UploadRejected(Exception):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def ensure_dirs() -> None:
    for d in (settings.upload_dir, settings.thumb_dir, settings.render_dir):
        d.mkdir(parents=True, exist_ok=True)


def safe_display_name(name: Optional[str]) -> str:
    name = Path(name or "document").name
    name = "".join(ch for ch in unicodedata.normalize("NFC", name) if unicodedata.category(ch)[0] != "C")
    name = name.strip() or "document"
    return name[:120]


def sniff_mime(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    return None


def validate_upload(filename: str, data: bytes) -> Tuple[str, Optional[int]]:
    """Return (mime_type, page_count) or raise UploadRejected."""
    if not data:
        raise UploadRejected("empty_file")
    if len(data) > settings.max_upload_bytes:
        raise UploadRejected("too_large", mb=_fmt_mb(settings.max_upload_mb))
    ext = Path(filename or "").suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise UploadRejected("unsupported_type")
    mime = sniff_mime(data)
    if mime is None:
        raise UploadRejected("unsupported_type")
    if mime.startswith("image/"):
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception:
            raise UploadRejected("corrupt_file")
        if w < 64 or h < 64:
            raise UploadRejected("image_too_small")
        return mime, 1
    # PDF
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise UploadRejected("encrypted_pdf")
        pages = len(reader.pages)
    except UploadRejected:
        raise
    except Exception:
        raise UploadRejected("corrupt_file")
    if pages == 0:
        raise UploadRejected("corrupt_file")
    if pages > settings.max_pdf_pages:
        raise UploadRejected("too_many_pages", pages=settings.max_pdf_pages)
    return mime, pages


def _fmt_mb(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path(directory: Path, name: str) -> Path:
    if not _NAME_RE.match(name or ""):
        raise ValueError("invalid storage name")
    return directory / name


def save_file(data: bytes, mime: str) -> str:
    ensure_dirs()
    name = secrets.token_hex(16) + MIME_EXT[mime]
    _path(settings.upload_dir, name).write_bytes(data)
    return name


def read_file(name: str) -> bytes:
    return _path(settings.upload_dir, name).read_bytes()


def file_path(name: str) -> Path:
    return _path(settings.upload_dir, name)


def thumb_path(name: str) -> Path:
    return _path(settings.thumb_dir, name)


def delete_files(*names: Optional[str]) -> None:
    for n in names:
        if not n:
            continue
        for d in (settings.upload_dir, settings.thumb_dir):
            try:
                _path(d, n).unlink(missing_ok=True)
            except ValueError:
                pass
        stem = n.rsplit(".", 1)[0]
        for p in settings.render_dir.glob(f"{stem}_p*.png"):
            p.unlink(missing_ok=True)


def _render_pdf_page(data: bytes, page_index: int, target_width: int) -> Image.Image:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[page_index]
        width = page.get_width() or 600
        scale = max(0.5, min(4.0, target_width / width))
        img = page.render(scale=scale).to_pil()
        page.close()
        return img.convert("RGB")
    finally:
        pdf.close()


def make_thumbnail(storage_name: str, mime: str) -> Optional[str]:
    """Create a small JPEG preview. Returns its storage name, or None if not possible."""
    ensure_dirs()
    try:
        data = read_file(storage_name)
        if mime == "application/pdf":
            img = _render_pdf_page(data, 0, 480)
        else:
            with Image.open(io.BytesIO(data)) as im:
                img = ImageOps.exif_transpose(im).convert("RGB")
        img.thumbnail((480, 640))
        name = storage_name.rsplit(".", 1)[0] + ".jpg"
        img.save(_path(settings.thumb_dir, name), "JPEG", quality=82)
        return name
    except Exception:
        return None


def render_page(storage_name: str, page: int) -> Optional[Path]:
    """Render one PDF page to PNG (cached) for the in-app source viewer."""
    ensure_dirs()
    stem = storage_name.rsplit(".", 1)[0]
    out = _path(settings.render_dir, f"{stem}_p{page}.png")
    if out.exists():
        return out
    try:
        img = _render_pdf_page(read_file(storage_name), page - 1, 1400)
        img.save(out, "PNG", optimize=True)
        return out
    except Exception:
        return None


def prepare_for_model(data: bytes, mime: str) -> Tuple[bytes, str]:
    """Apply EXIF rotation to photos so the model sees them upright. PDFs pass through."""
    if mime != "image/jpeg":
        return data, mime
    try:
        with Image.open(io.BytesIO(data)) as im:
            orientation = im.getexif().get(0x0112, 1)
            if orientation in (None, 1):
                return data, mime
            fixed = ImageOps.exif_transpose(im).convert("RGB")
        buf = io.BytesIO()
        fixed.save(buf, "JPEG", quality=92)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, mime
