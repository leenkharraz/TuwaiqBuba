"""Document processing and claim assembly (database <-> engine)."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import storage
from .config import settings
from .db import SessionLocal
from .engine import ClaimIn, ClaimView, CorrectionIn, DocIn, build_claim_view
from .extraction.gemini import ExtractionError, GeminiExtractor
from .extraction.samples import Fixture
from .models import Acknowledgement, Claim, Correction, Document, utcnow
from .rules import get_rules
from .schemas import DocumentExtraction

log = logging.getLogger("claims.services")

_executor = ThreadPoolExecutor(max_workers=settings.extraction_workers, thread_name_prefix="extract")

# Tests can swap in a fake extractor factory.
extractor_factory: Optional[Callable[[], Optional[GeminiExtractor]]] = None
run_inline = False  # tests: process synchronously


def live_extractor() -> Optional[GeminiExtractor]:
    if extractor_factory is not None:
        return extractor_factory()
    if not settings.live_extraction_configured:
        return None
    return GeminiExtractor(settings.gemini_api_key, settings.gemini_model, settings.gemini_timeout_s,
                           fallback_models=list(settings.gemini_fallback_models))


def next_position(db: Session, claim_id: str) -> int:
    cur = db.scalar(select(func.max(Document.position)).where(Document.claim_id == claim_id))
    return (cur or 0) + 1


def add_uploaded_document(db: Session, claim: Claim, filename: str, data: bytes, source: str,
                          replaces: Optional[Document] = None) -> Document:
    """Validate, store and queue one uploaded file. Raises storage.UploadRejected."""
    mime, pages = storage.validate_upload(filename, data)
    digest = storage.sha256(data)
    active = [d for d in claim.active_documents if not (replaces and d.id == replaces.id)]
    if any(d.sha256 == digest for d in active):
        raise storage.UploadRejected("duplicate_file")
    if len(active) >= settings.max_files_per_claim:
        raise storage.UploadRejected("too_many_files", n=settings.max_files_per_claim)
    name = storage.save_file(data, mime)
    doc = Document(
        claim_id=claim.id,
        position=replaces.position if replaces else next_position(db, claim.id),
        original_filename=storage.safe_display_name(filename),
        storage_name=name,
        mime_type=mime,
        size_bytes=len(data),
        sha256=digest,
        page_count=pages,
        source=source,
        status="queued",
        replaces_doc_id=replaces.id if replaces else None,
    )
    doc.thumb_name = storage.make_thumbnail(name, mime)
    if live_extractor() is None:
        # Honest state: we do not fake extraction when no live extractor is configured.
        doc.status = "unavailable"
        doc.status_detail = "not_configured"
    db.add(doc)
    db.flush()
    if replaces is not None:
        retire_document(db, claim, replaces, "replaced", replaced_by=doc.id)
    return doc


def add_fixture_document(db: Session, claim: Claim, fixture: Fixture) -> Document:
    data = fixture.path.read_bytes()
    digest = storage.sha256(data)
    if any(d.sha256 == digest for d in claim.active_documents):
        raise storage.UploadRejected("duplicate_file")
    mime, pages = storage.validate_upload(fixture.file, data)
    name = storage.save_file(data, mime)
    extraction = fixture.extraction()
    doc = Document(
        claim_id=claim.id,
        position=next_position(db, claim.id),
        original_filename=fixture.file,
        storage_name=name,
        mime_type=mime,
        size_bytes=len(data),
        sha256=digest,
        page_count=pages,
        source="sample_fixture",
        fixture_id=fixture.id,
        status="unreadable" if extraction.readability == "unreadable" else "extracted",
        extraction_json=extraction.model_dump_json(),
        extraction_mode="sample_fixture",
        extracted_at=utcnow(),
    )
    doc.thumb_name = storage.make_thumbnail(name, mime)
    db.add(doc)
    db.flush()
    return doc


def retire_document(db: Session, claim: Claim, doc: Document, status: str,
                    replaced_by: Optional[str] = None) -> None:
    """Remove/replace a document: its values stop contributing and its corrections are superseded."""
    doc.status = status
    doc.removed_at = utcnow()
    doc.replaced_by_doc_id = replaced_by
    doc.extraction_json = doc.extraction_json  # keep for the audit trail of the draft
    storage.delete_files(doc.storage_name, doc.thumb_name)
    doc.storage_name = None
    doc.thumb_name = None
    prefix = f"doc:{doc.id}:"
    for c in claim.corrections:
        if c.active and c.field_key.startswith(prefix):
            c.active = False
            c.superseded_reason = "document_replaced" if status == "replaced" else "document_removed"
    for other in claim.active_documents:
        if other.linked_invoice_doc_id == doc.id:
            other.linked_invoice_doc_id = None


def schedule_extraction(doc_ids: List[str]) -> None:
    for doc_id in doc_ids:
        if run_inline:
            process_document(doc_id)
        else:
            _executor.submit(process_document, doc_id)


def process_document(doc_id: str) -> None:
    """Run live extraction for one queued document (background thread)."""
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        if doc is None or doc.status != "queued":
            return
        extractor = live_extractor()
        if extractor is None:
            doc.status, doc.status_detail = "unavailable", "not_configured"
            db.commit()
            return
        doc.status = "processing"
        db.commit()
        storage_name, mime = doc.storage_name, doc.mime_type

    try:
        data = storage.read_file(storage_name) if storage_name else None
        if data is None:
            raise ExtractionError("file_missing")
        result = extractor.extract(data, mime)
        error = None
    except ExtractionError as exc:
        result, error = None, exc.code
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Unexpected extraction error (%s)", type(exc).__name__)
        result, error = None, "unknown_error"

    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        # If the member removed or replaced the document meanwhile, discard the result.
        if doc is None or doc.status != "processing":
            return
        if result is None:
            doc.status, doc.status_detail = "failed", error
        else:
            doc.extraction_json = result.model_dump_json()
            doc.extraction_mode = "gemini"
            doc.extraction_model = getattr(extractor, "used_model", None) or getattr(extractor, "model", None)
            doc.extracted_at = utcnow()
            doc.status = "unreadable" if result.readability == "unreadable" else "extracted"
            doc.status_detail = None
        db.commit()
    log.info("Document %s processed: %s", doc_id, "ok" if result else error)


def recover_interrupted() -> None:
    """Documents left mid-extraction by a server restart become retryable failures."""
    with SessionLocal() as db:
        for doc in db.scalars(select(Document).where(Document.status.in_(("queued", "processing")))):
            doc.status, doc.status_detail = "failed", "interrupted"
        db.commit()


# ------------------------------------------------------------------ engine glue


def extraction_of(doc: Document) -> Optional[DocumentExtraction]:
    if not doc.extraction_json:
        return None
    try:
        return DocumentExtraction.model_validate_json(doc.extraction_json)
    except Exception:
        return None


def claim_input(claim: Claim, today: Optional[date] = None) -> ClaimIn:
    docs = [
        DocIn(
            id=d.id, position=d.position, filename=d.original_filename, status=d.status,
            extraction=extraction_of(d) if d.status in ("extracted", "unreadable") else None,
            type_override=d.type_override, grouping_decision=d.grouping_decision,
            linked_invoice_doc_id=d.linked_invoice_doc_id, source=d.source, mime_type=d.mime_type,
            page_count=d.page_count, extraction_mode=d.extraction_mode, status_detail=d.status_detail,
        )
        for d in claim.active_documents
    ]
    corrections: Dict[str, CorrectionIn] = {}
    for c in claim.corrections:
        if not c.active:
            continue
        corrections[c.field_key] = CorrectionIn(
            id=c.id, field_key=c.field_key, value=json.loads(c.corrected_value) if c.corrected_value else None,
            original_value=json.loads(c.original_value) if c.original_value else None, kind=c.kind,
            context_values=json.loads(c.context_json) if c.context_json else [],
        )
    return ClaimIn(
        id=claim.id, docs=docs, corrections=corrections,
        acknowledged={a.issue_key for a in claim.acknowledgements},
        rules=get_rules(), today=today or date.today(),
    )


def build_view(claim: Claim, today: Optional[date] = None) -> ClaimView:
    return build_claim_view(claim_input(claim, today))


def state_version(claim: Claim) -> str:
    parts = [claim.status, str(len(claim.corrections)), str(len(claim.acknowledgements))]
    for d in claim.documents:
        parts.append(f"{d.id[:8]}:{d.status}:{d.type_override}:{d.grouping_decision}:{d.linked_invoice_doc_id}")
    return str(abs(hash("|".join(parts))))
