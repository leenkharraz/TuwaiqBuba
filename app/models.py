"""Database models.

Files themselves live in the private data directory; these rows only hold
metadata, extraction results, member corrections and claim snapshots.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | submitted
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    reference: Mapped[Optional[str]] = mapped_column(String(40), unique=True, nullable=True)
    split_from_claim_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    submit_token: Mapped[str] = mapped_column(String(64), default=lambda: secrets.token_urlsafe(24))
    member_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    snapshot_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    documents: Mapped[List["Document"]] = relationship(
        back_populates="claim", order_by="Document.position", cascade="all, delete-orphan"
    )
    corrections: Mapped[List["Correction"]] = relationship(
        back_populates="claim", order_by="Correction.id", cascade="all, delete-orphan"
    )
    acknowledgements: Mapped[List["Acknowledgement"]] = relationship(
        back_populates="claim", order_by="Acknowledgement.id", cascade="all, delete-orphan"
    )

    @property
    def active_documents(self) -> List["Document"]:
        return [d for d in self.documents if d.status not in ("removed", "replaced")]


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    original_filename: Mapped[str] = mapped_column(String(160))
    storage_name: Mapped[Optional[str]] = mapped_column(String(80), unique=True, nullable=True)
    thumb_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="upload")  # upload | camera | sample_fixture
    fixture_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    # queued | processing | extracted | unreadable | failed | unavailable | removed | replaced
    status: Mapped[str] = mapped_column(String(20), default="queued")
    status_detail: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    extraction_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extraction_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # gemini | sample_fixture
    extraction_model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Member decisions about this document
    type_override: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    grouping_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # same_visit
    linked_invoice_doc_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # or "none"

    replaces_doc_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    replaced_by_doc_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    claim: Mapped[Claim] = relationship(back_populates="documents")


class Correction(Base):
    """A member's correction or confirmation of one field.

    The original extracted value stays untouched in ``Document.extraction_json``
    and is also copied here, so the correction history is self-contained.
    """

    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    field_key: Mapped[str] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="correction")  # correction | confirmation
    original_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    corrected_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    original_sources_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    claim: Mapped[Claim] = relationship(back_populates="corrections")


class Acknowledgement(Base):
    """Member statement that a non-critical issue is understood (e.g. "I don't have lab results")."""

    __tablename__ = "acknowledgements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    issue_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    claim: Mapped[Claim] = relationship(back_populates="acknowledgements")
