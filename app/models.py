"""Database models.

Files themselves live in the private data directory; these rows only hold
metadata, extraction results, member corrections and claim snapshots.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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

    # ---- lifecycle (migration 2). A draft has stage=None; submission sets stage="submitted".
    claim_number: Mapped[Optional[str]] = mapped_column(String(40), unique=True, nullable=True)
    member_id: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    policy_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    claimed_halalas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    stage: Mapped[Optional[str]] = mapped_column(String(20), index=True, nullable=True)
    action_required: Mapped[bool] = mapped_column(Boolean, default=False)
    decision: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # approved | rejected
    approved_halalas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    payout_status: Mapped[str] = mapped_column(String(10), default="unpaid")  # unpaid | paid
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paid_halalas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    check_round: Mapped[int] = mapped_column(Integer, default=0)
    review_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_update_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    origin: Mapped[str] = mapped_column(String(10), default="app")  # app | seed
    scenario: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

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


# ====================================================================== lifecycle & reference data
# Reference rows (members, policies, rules, SLA) come from the demo workbook importer.
# They are synthetic test data, not Bupa policy terms.


class Member(Base):
    __tablename__ = "members"

    member_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name_ar: Mapped[str] = mapped_column(String(120))
    member_identity: Mapped[str] = mapped_column(String(60))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)


class Policy(Base):
    __tablename__ = "policies"

    policy_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.member_id"), index=True)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    version: Mapped[str] = mapped_column(String(40))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    rule_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("policies.policy_id"), index=True)
    category: Mapped[str] = mapped_column(String(20))
    limit_halalas: Mapped[int] = mapped_column(Integer)
    member_share_rate: Mapped[str] = mapped_column(String(12))  # Decimal as text
    invoice_required: Mapped[bool] = mapped_column(Boolean, default=True)
    payment_proof_required: Mapped[bool] = mapped_column(Boolean, default=True)
    excluded_service_codes: Mapped[str] = mapped_column(String(400), default="")
    version: Mapped[str] = mapped_column(String(40))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)


class SlaTarget(Base):
    __tablename__ = "sla_targets"

    category: Mapped[str] = mapped_column(String(20), primary_key=True)
    category_ar: Mapped[str] = mapped_column(String(60))
    initial_check_target_minutes: Mapped[int] = mapped_column(Integer)
    employee_estimate_hours: Mapped[int] = mapped_column(Integer)
    clock_basis: Mapped[str] = mapped_column(String(20))
    estimate_label_ar: Mapped[str] = mapped_column(String(60))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)


class InvoiceRecord(Base):
    """Normalised invoice history used for duplicate checks (seeded rows and extracted invoices)."""

    __tablename__ = "invoice_records"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    document_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    member_id: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    provider_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    provider_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    invoice_number_norm: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    service_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    total_halalas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    extracted_member_identity: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    service_code: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    file_sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="extracted")  # seed | extracted
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CheckResult(Base):
    __tablename__ = "check_results"

    id: Mapped[str] = mapped_column(String(60), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(60), index=True, nullable=True)
    check_type: Mapped[str] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(16))  # pass | fail | manual_review
    reason_code: Mapped[str] = mapped_column(String(60))
    message_ar: Mapped[str] = mapped_column(Text)
    message_en: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    related_claim_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    rule_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    origin: Mapped[str] = mapped_column(String(10), default="app")


class StatusEvent(Base):
    """Append-only claim history. Rows are never updated or deleted by the application."""

    __tablename__ = "status_events"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_status_event_idem"),)

    id: Mapped[str] = mapped_column(String(60), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20))  # submitted | auto_check | employee_review | decided
    event_type: Mapped[str] = mapped_column(String(40))
    actor_role: Mapped[str] = mapped_column(String(12))  # member | system | employee
    actor_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    data_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    origin: Mapped[str] = mapped_column(String(10), default="app")


class CheckJob(Base):
    """One automatic-check run per claim and round. Safe to resume after a restart."""

    __tablename__ = "check_jobs"
    __table_args__ = (UniqueConstraint("claim_id", "round", name="uq_check_job_round"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | running | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SessionLink(Base):
    """Demo sign-in: which synthetic member (or employee role) a browser session acts as.

    This is a local demonstration switch, not production authentication.
    """

    __tablename__ = "session_links"

    sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    member_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    role: Mapped[str] = mapped_column(String(12), default="member")  # member | employee
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
