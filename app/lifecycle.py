"""Claim lifecycle after submission: automatic checks, employee review and decisions.

Separation of concerns
- Extraction (OCR/LLM or prepared demo data) happens per document in ``services``.
- This module only runs *deterministic* checks over what was extracted, the member's
  seeded policy rules and the invoice history. It never predicts approval, never infers
  medical necessity and never makes a final decision: only an employee can decide.

Tracking stages (exactly four):
    1 submitted        تم إرسال الطلب
    2 auto_check       قيد التحقق من المستندات   (+ action_required = يحتاج استكمال)
    3 employee_review  قيد مراجعة الموظف
    4 decided          تمت الموافقة / تم الرفض   (decision = approved | rejected)

SLA clock rule (documented, persisted): the employee-review estimate starts when a claim
enters ``employee_review``. If the employee asks for more documents the clock pauses; when
the member resubmits and the claim re-enters review, the clock restarts from that moment
for the category's full estimate.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import services
from .db import SessionLocal
from .models import (CheckJob, CheckResult, Claim, Document, InvoiceRecord, Member, Policy, PolicyRule,
                     SlaTarget, StatusEvent, utcnow)
from .normalize import norm_text, providers_compatible, western_digits

log = logging.getLogger("claims.lifecycle")

STAGES = ("submitted", "auto_check", "employee_review", "decided")
TRACK_CATEGORIES = ("optical", "dental", "inpatient", "outpatient")
TECHNICAL_REASONS = {"document_unavailable", "document_unreadable", "documents_still_reading", "check_error"}
SEED_NAMESPACE = uuid.UUID("6f1f7c1e-5d0a-4a7e-9a55-2f1b7f3c9b10")

run_inline = False  # tests run jobs synchronously
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="checks")


# ====================================================================== money & identifiers


def to_halalas(d: Optional[Decimal]) -> Optional[int]:
    if d is None:
        return None
    return int((Decimal(d) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_halalas(h: Optional[int]) -> Optional[Decimal]:
    return None if h is None else (Decimal(h) / 100).quantize(Decimal("0.01"))


def fmt_sar(h: Optional[int]) -> str:
    """'2,400.00' — callers add the currency code. None renders as an em dash."""
    if h is None:
        return "—"
    return f"{from_halalas(h):,.2f}"


_INV_STRIP = re.compile(r"[\s\-_/\\.#:,،]+")


def norm_invoice_number(raw: Optional[str]) -> Optional[str]:
    """Normalise digits, whitespace, punctuation and case. Leading zeroes are kept."""
    if raw is None:
        return None
    s = western_digits(str(raw)).strip().upper()
    s = _INV_STRIP.sub("", s)
    return s or None


def norm_identity(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = _INV_STRIP.sub("", western_digits(str(raw)).strip().upper())
    return s or None


def seed_uuid(business_key: str) -> str:
    return str(uuid.uuid5(SEED_NAMESPACE, business_key))


def new_claim_number(now: datetime) -> str:
    return f"CLM-{now.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


# ====================================================================== events


def add_event(db: Session, claim: Claim, stage: str, event_type: str, actor_role: str,
              actor_id: Optional[str] = None, reason: Optional[str] = None, data: Optional[dict] = None,
              idem: Optional[str] = None, at: Optional[datetime] = None) -> Optional[StatusEvent]:
    """Append one history event. With an idempotency key, a repeat is silently ignored."""
    if idem and db.scalar(select(StatusEvent.id).where(StatusEvent.idempotency_key == idem)):
        return None
    ev = StatusEvent(claim_id=claim.id, stage=stage, event_type=event_type, actor_role=actor_role,
                     actor_id=actor_id, reason=reason, idempotency_key=idem, occurred_at=at or utcnow(),
                     data_json=json.dumps(data, ensure_ascii=False, default=str) if data else None)
    db.add(ev)
    return ev


def events_of(db: Session, claim_id: str) -> List[StatusEvent]:
    # rowid = insertion order, the tie-breaker for events recorded in the same instant
    return list(db.scalars(select(StatusEvent).where(StatusEvent.claim_id == claim_id)
                           .order_by(StatusEvent.occurred_at, text("status_events.rowid"))))


# ====================================================================== check outcomes


@dataclass
class Check:
    check_type: str
    result: str  # pass | fail | manual_review
    reason_code: str
    message_ar: str
    message_en: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    rule_version: Optional[str] = None
    related_claim_id: Optional[str] = None
    evidence_ref: Optional[str] = None


def _invoice_inputs(claim: Claim, view) -> Dict[str, Any]:
    """The primary invoice as extracted (or as corrected by the member)."""
    invs = [i for i in view.invoices if not i.duplicate_of]
    inv = invs[0] if invs else None
    out: Dict[str, Any] = {"invoice": inv, "count": len(invs)}
    if inv is None:
        return out
    f = inv.fields
    out.update(
        number=f["invoice_number"].value,
        total=f["total"].typed,
        currency=f["currency"].typed or (f["currency"].value or None),
        service_date=f["service_date"].typed or f["invoice_date"].typed,
        provider_name=f["provider_name"].value or view.claim_fields["provider_name"].value,
        doc_id=inv.doc_id,
    )
    doc = next((d for d in claim.active_documents if d.id == inv.doc_id), None)
    out["sha256"] = doc.sha256 if doc else None
    return out


def _seed_invoice(db: Session, claim: Claim) -> Optional[InvoiceRecord]:
    return db.scalar(select(InvoiceRecord).where(InvoiceRecord.claim_id == claim.id,
                                                 InvoiceRecord.source == "seed"))


def evaluate(db: Session, claim: Claim, today: Optional[date] = None) -> List[Check]:
    """Run every deterministic check. Pure with respect to the database (reads only)."""
    view = services.build_view(claim, today)
    member = db.get(Member, claim.member_id) if claim.member_id else None
    inv = _invoice_inputs(claim, view)
    seed = _seed_invoice(db, claim)
    checks: List[Check] = []

    # 1. documents readable --------------------------------------------------------
    docs = claim.active_documents
    if any(d.status in ("queued", "processing") for d in docs):
        checks.append(Check("document_readability", "fail", "documents_still_reading",
                            "ما زالت قراءة بعض المستندات جارية. أعد المحاولة بعد قليل.",
                            "Some documents are still being read. Try again shortly."))
    bad = [d for d in docs if d.status in ("failed", "unavailable")]
    if bad:
        checks.append(Check("document_readability", "fail", "document_unavailable",
                            "تعذر قراءة أحد المستندات. أعد المحاولة أو استبدل الملف.",
                            "A document couldn't be read. Retry or replace the file.",
                            {"documents": [d.original_filename for d in bad]}))
    blurry = [d for d in docs if d.status == "unreadable"]
    if blurry:
        checks.append(Check("document_readability", "fail", "document_unreadable",
                            "أحد المستندات غير واضح. أعد تصويره أو ارفع نسخة أوضح.",
                            "A document is unreadable. Retake it or upload a clearer copy.",
                            {"documents": [d.original_filename for d in blurry]}))

    # 2. invoice completeness --------------------------------------------------------
    if inv["invoice"] is None:
        checks.append(Check("invoice_completeness", "fail", "invoice_missing",
                            "لم نجد فاتورة بين المستندات. أرفق الفاتورة.",
                            "No invoice was found. Attach the invoice."))
    elif inv["total"] is None:
        checks.append(Check("invoice_completeness", "fail", "invoice_total_missing",
                            "إجمالي الفاتورة غير ظاهر. أرفق فاتورة واضحة وكاملة.",
                            "The invoice total isn't visible. Attach a clear, complete invoice.",
                            {"invoice_number": inv.get("number"), "document_id": inv.get("doc_id")},
                            evidence_ref=seed.id if seed else None))
    elif not inv.get("number"):
        checks.append(Check("invoice_completeness", "fail", "invoice_number_missing",
                            "رقم الفاتورة غير ظاهر. أرفق فاتورة واضحة وكاملة.",
                            "The invoice number isn't visible. Attach a clear, complete invoice."))
    else:
        checks.append(Check("invoice_completeness", "pass", "invoice_complete", "الفاتورة مكتملة.",
                            "Invoice is complete.", {"invoice_number": inv["number"],
                                                     "total": str(inv["total"]), "currency": inv.get("currency")},
                            evidence_ref=seed.id if seed else None))

    # 3. member identity: read from the documents, never inferred from the account ------
    ident_field = view.claim_fields["patient_identifier"]
    doc_ident = ident_field.value
    if not doc_ident:
        checks.append(Check("identity_match", "fail", "identity_missing",
                            "رقم العضوية أو الهوية غير ظاهر في المستند.",
                            "The membership or ID number isn't shown on the documents."))
    elif member is None:
        checks.append(Check("identity_match", "manual_review", "member_record_unavailable",
                            "لا يتوفر سجل عضو للمقارنة.", "No member record to compare against.",
                            {"document_identity": doc_ident}))
    elif norm_identity(doc_ident) == norm_identity(member.member_identity):
        checks.append(Check("identity_match", "pass", "identity_match", "رقم العضوية مطابق.",
                            "Membership number matches.", {"document_identity": doc_ident}))
    else:
        checks.append(Check("identity_match", "manual_review", "identity_mismatch",
                            "رقم الهوية في المستند لا يطابق بيانات العضو ويحتاج مراجعة الموظف.",
                            "The identifier on the documents doesn't match the member record.",
                            {"document_identity": doc_ident, "member_identity": member.member_identity}))

    # 4. policy lookup for the service date ---------------------------------------------
    category = claim.category or view.category
    claimed = to_halalas(inv.get("total")) if inv.get("total") is not None else claim.claimed_halalas
    rule, policy, policy_check = _policy_check(db, member, category, inv.get("service_date"),
                                               claimed, (inv.get("currency") or claim.currency or "SAR"),
                                               seed.service_code if seed else None)

    # 5. payment evidence -------------------------------------------------------------
    pay_required = rule.payment_proof_required if rule is not None else True
    if inv["invoice"] is not None and pay_required:
        iv = inv["invoice"]
        payments = [p for p in iv.payments if not p.duplicate_of]
        if not payments and not iv.embedded_payment:
            checks.append(Check("payment_evidence", "fail", "payment_proof_missing",
                                "إثبات الدفع غير مرفق. أرفق إثبات الدفع لهذه الزيارة.",
                                "Proof of payment is missing. Attach the proof of payment for this visit.",
                                rule_version=rule.version if rule else None))
        elif iv.payment_status == "amount_mismatch":
            checks.append(Check("payment_evidence", "manual_review", "payment_amount_mismatch",
                                "المبلغ المدفوع لا يطابق إجمالي الفاتورة ويحتاج مراجعة الموظف.",
                                "The amount paid differs from the invoice total.",
                                {"paid": str(iv.paid_total), "total": str(inv.get("total"))}))
        else:
            checks.append(Check("payment_evidence", "pass", "payment_attached", "إثبات الدفع مرفق.",
                                "Proof of payment attached.", {"paid": str(iv.paid_total) if iv.paid_total else None}))

    checks.append(policy_check)

    # 6. duplicate invoice ---------------------------------------------------------------
    if inv["invoice"] is not None:
        checks.append(_duplicate_check(db, claim, inv))
    return checks


def _policy_check(db: Session, member: Optional[Member], category: Optional[str], service_date: Optional[date],
                  claimed: Optional[int], currency: str, service_code: Optional[str]
                  ) -> Tuple[Optional[PolicyRule], Optional[Policy], Check]:
    def manual(code: str, ar: str, en: str, **ev) -> Tuple[None, Optional[Policy], Check]:
        return None, None, Check("policy_check", "manual_review", code, ar, en, ev)

    if member is None:
        return manual("policy_unavailable", "لا تتوفر بيانات وثيقة للمقارنة. يحتاج مراجعة الموظف.",
                      "No policy data is available. Manual review required.")
    if service_date is None:
        return manual("service_date_unknown", "تاريخ الخدمة غير معروف، فلا يمكن تحديد الوثيقة السارية.",
                      "Service date unknown, so the applicable policy can't be determined.")
    policy = db.scalar(select(Policy).where(Policy.member_id == member.member_id, Policy.valid_from <= service_date,
                                            Policy.valid_to >= service_date))
    if policy is None:
        return manual("no_active_policy", "لا توجد وثيقة سارية في تاريخ الخدمة. يحتاج مراجعة الموظف.",
                      "No policy active on the service date. Manual review required.",
                      service_date=service_date.isoformat())
    if not category or category not in TRACK_CATEGORIES:
        return None, policy, Check("policy_check", "manual_review", "category_unknown",
                                   "نوع الخدمة غير محدد في قواعد الوثيقة. يحتاج مراجعة الموظف.",
                                   "The service category has no policy rule. Manual review required.",
                                   {"category": category}, policy.version)
    rule = db.scalar(select(PolicyRule).where(PolicyRule.policy_id == policy.policy_id,
                                              PolicyRule.category == category))
    if rule is None:
        return None, policy, Check("policy_check", "manual_review", "rule_unavailable",
                                   "لا توجد قاعدة لهذا القسم في الوثيقة. يحتاج مراجعة الموظف.",
                                   "No rule for this category. Manual review required.",
                                   {"category": category}, policy.version)
    share = Decimal(rule.member_share_rate)
    evidence = {"rule_id": rule.rule_id, "policy_id": policy.policy_id, "category": category,
                "limit_sar": fmt_sar(rule.limit_halalas), "member_share_rate": str(share),
                "claimed_sar": fmt_sar(claimed), "service_code": service_code,
                "policy_period": f"{policy.valid_from.isoformat()} – {policy.valid_to.isoformat()}"}
    if claimed is not None and currency == "SAR":
        # A policy calculation for the employee, not an approved amount.
        evidence["indicative_member_share_sar"] = fmt_sar(to_halalas(from_halalas(claimed) * share))
    excluded = [c.strip() for c in (rule.excluded_service_codes or "").split(",") if c.strip()]
    if currency != "SAR":
        return rule, policy, Check("policy_check", "manual_review", "currency_not_comparable",
                                   "عملة الفاتورة تختلف عن عملة حد الوثيقة. يحتاج مراجعة الموظف.",
                                   "Invoice currency differs from the policy limit currency.", evidence, rule.version)
    if service_code and service_code in excluded:
        return rule, policy, Check("policy_check", "manual_review", "service_excluded",
                                   "الخدمة مدرجة ضمن الاستثناءات التجريبية للوثيقة. يحتاج قرار الموظف.",
                                   "The service code is excluded by the test policy. Employee decision required.",
                                   evidence, rule.version)
    if excluded and not service_code:
        return rule, policy, Check("policy_check", "manual_review", "service_code_unavailable",
                                   "رمز الخدمة غير متوفر للتحقق من الاستثناءات. يحتاج مراجعة الموظف.",
                                   "Service code unavailable to check exclusions. Manual review required.",
                                   evidence, rule.version)
    if claimed is not None and claimed > rule.limit_halalas:
        return rule, policy, Check("policy_check", "manual_review", "policy_limit_review",
                                   "المبلغ يتجاوز الحد التجريبي ويحتاج مراجعة الموظف.",
                                   "The amount exceeds the test policy limit and needs employee review.",
                                   evidence, rule.version)
    return rule, policy, Check("policy_check", "pass", "policy_ok", "مطابق لقواعد الوثيقة التجريبية.",
                               "Within the test policy rule.", evidence, rule.version)


def _claim_state(c: Claim) -> str:
    if c.payout_status == "paid" and c.paid_at is not None:
        return "paid"
    if c.stage == "decided":
        return "approved_unpaid" if c.decision == "approved" else "rejected"
    return "pending"


def _duplicate_check(db: Session, claim: Claim, inv: Dict[str, Any]) -> Check:
    number = norm_invoice_number(inv.get("number"))
    total = to_halalas(inv.get("total")) if inv.get("total") is not None else None
    sdate = inv.get("service_date")
    sha = inv.get("sha256")
    before = claim.submitted_at or utcnow()
    rows = db.execute(
        select(InvoiceRecord, Claim).join(Claim, Claim.id == InvoiceRecord.claim_id)
        .where(InvoiceRecord.claim_id != claim.id, Claim.stage.is_not(None), Claim.submitted_at < before)
    ).all()
    strong: List[Tuple[InvoiceRecord, Claim]] = []
    other_member: List[Tuple[InvoiceRecord, Claim]] = []
    weak: List[Tuple[InvoiceRecord, Claim]] = []
    similar: List[str] = []
    for rec, other in rows:
        same_number = number is not None and rec.invoice_number_norm == number
        same_file = sha is not None and rec.file_sha256 == sha
        if not (same_number or same_file):
            if (claim.member_id and rec.member_id == claim.member_id and sdate and rec.service_date == sdate
                    and total is not None and rec.total_halalas == total):
                similar.append(other.claim_number or other.id)  # same date and amount only: not evidence
            continue
        if claim.member_id is None or rec.member_id != claim.member_id:
            other_member.append((rec, other))
            continue
        provider_ok = (rec.provider_id is None or rec.provider_name is None or not inv.get("provider_name")
                       or providers_compatible(rec.provider_name, inv.get("provider_name")) is not False)
        agrees = (sdate is not None and rec.service_date == sdate) or (total is not None and rec.total_halalas == total)
        (strong if (same_file or (provider_ok and agrees)) else weak).append((rec, other))

    def ref(rec: InvoiceRecord, other: Claim) -> Dict[str, Any]:
        return {"claim_number": other.claim_number, "state": _claim_state(other), "invoice_number": rec.invoice_number,
                "service_date": rec.service_date.isoformat() if rec.service_date else None,
                "total_sar": fmt_sar(rec.total_halalas), "same_file": bool(sha and rec.file_sha256 == sha)}

    if strong:
        order = {"paid": 0, "pending": 1, "approved_unpaid": 2, "rejected": 3}
        rec, other = sorted(strong, key=lambda ro: order[_claim_state(ro[1])])[0]
        state = _claim_state(other)
        ev = {"match": ref(rec, other), "all_matches": [ref(r, o) for r, o in strong]}
        if state == "paid":
            return Check("duplicate_check", "manual_review", "paid_duplicate",
                         "هذه الفاتورة مرتبطة بمطالبة تم تعويضها مسبقًا",
                         "This invoice is linked to a claim that was already compensated.", ev,
                         related_claim_id=other.id, evidence_ref=rec.id)
        if state == "pending":
            return Check("duplicate_check", "manual_review", "pending_duplicate",
                         "توجد مطالبة قيد المراجعة لهذه الفاتورة",
                         "Another claim for this invoice is still under review.", ev,
                         related_claim_id=other.id, evidence_ref=rec.id)
        return Check("duplicate_check", "manual_review", "prior_claim_not_paid",
                     "توجد مطالبة سابقة لهذه الفاتورة لم يُصرف لها تعويض. يحتاج مراجعة الموظف.",
                     "A previous claim for this invoice exists but was not paid.", ev,
                     related_claim_id=other.id, evidence_ref=rec.id)
    if other_member:
        # Never expose another member's claim to this member; the employee sees the reference.
        rec, other = other_member[0]
        return Check("duplicate_check", "manual_review", "cross_member_match",
                     "يحتاج رقم الفاتورة تحققًا إضافيًا من الموظف.",
                     "The invoice number matches another member's claim.",
                     {"match": ref(rec, other), "member_visible": False}, related_claim_id=other.id,
                     evidence_ref=rec.id)
    if weak:
        rec, other = weak[0]
        return Check("duplicate_check", "manual_review", "possible_duplicate",
                     "رقم الفاتورة مطابق لمطالبة سابقة مع اختلاف في التفاصيل. يحتاج مراجعة الموظف.",
                     "Same invoice number as an earlier claim, but other details differ.",
                     {"match": ref(rec, other)}, related_claim_id=other.id, evidence_ref=rec.id)
    ev = {"similar_date_and_amount": similar} if similar else {}
    return Check("duplicate_check", "pass", "no_duplicate", "لا توجد فاتورة مطابقة سابقة.",
                 "No matching earlier invoice." + (" Same date and amount alone is not a duplicate." if similar else ""),
                 ev)


# ====================================================================== invoice history


def refresh_invoice_records(db: Session, claim: Claim, view=None) -> None:
    """Keep this claim's extracted invoice rows in step with its current documents."""
    view = view or services.build_view(claim)
    seed = _seed_invoice(db, claim)
    for rec in db.scalars(select(InvoiceRecord).where(InvoiceRecord.claim_id == claim.id,
                                                      InvoiceRecord.source == "extracted")):
        db.delete(rec)
    for iv in view.invoices:
        if iv.duplicate_of:
            continue
        f = iv.fields
        doc = next((d for d in claim.active_documents if d.id == iv.doc_id), None)
        number = f["invoice_number"].value
        if seed is not None and norm_invoice_number(number) == seed.invoice_number_norm:
            if doc is not None and seed.document_id == doc.id:
                continue  # the seeded row already describes this exact document
        db.add(InvoiceRecord(
            id=f"x-{claim.id[:8]}-{iv.doc_id[:8]}", claim_id=claim.id, document_id=iv.doc_id,
            member_id=claim.member_id, provider_id=seed.provider_id if seed else None,
            provider_name=f["provider_name"].value or view.claim_fields["provider_name"].value,
            invoice_number=number, invoice_number_norm=norm_invoice_number(number),
            service_date=f["service_date"].typed or f["invoice_date"].typed,
            total_halalas=to_halalas(f["total"].typed) if f["total"].typed is not None else None,
            currency=f["currency"].typed or None,
            extracted_member_identity=view.claim_fields["patient_identifier"].value,
            service_code=seed.service_code if seed else None,
            file_sha256=doc.sha256 if doc else None, source="extracted"))


# ====================================================================== jobs


def enqueue_checks(db: Session, claim: Claim) -> CheckJob:
    """Create the job for the claim's current round (idempotent per round)."""
    job = db.scalar(select(CheckJob).where(CheckJob.claim_id == claim.id, CheckJob.round == claim.check_round))
    if job is None:
        job = CheckJob(claim_id=claim.id, round=claim.check_round)
        db.add(job)
        db.flush()
    return job


def schedule(job_ids: Sequence[str]) -> None:
    for jid in job_ids:
        if run_inline:
            run_job(jid)
        else:
            _executor.submit(run_job, jid)


def run_job(job_id: str, today: Optional[date] = None) -> None:
    """Run one check job. Re-running a finished job does nothing; a crashed one resumes cleanly."""
    with SessionLocal() as db:
        job = db.get(CheckJob, job_id)
        if job is None or job.status == "done":
            return
        claim = db.get(Claim, job.claim_id)
        if claim is None or claim.stage != "auto_check" or claim.check_round != job.round:
            job.status, job.finished_at = "done", utcnow()  # superseded by a newer round
            db.commit()
            return
        job.status, job.attempts, job.started_at = "running", job.attempts + 1, job.started_at or utcnow()
        db.commit()
        try:
            checks = evaluate(db, claim, today)
            _apply_outcome(db, claim, job, checks)
            job.status, job.finished_at, job.error = "done", utcnow(), None
            db.commit()
        except Exception as exc:  # pragma: no cover - defensive; leaves a recoverable state
            db.rollback()
            log.warning("Check job %s failed (%s)", job_id, type(exc).__name__)
            job = db.get(CheckJob, job_id)
            claim = db.get(Claim, job.claim_id)
            job.status, job.error, job.finished_at = "failed", type(exc).__name__[:80], utcnow()
            claim.action_required = True
            db.add(CheckResult(claim_id=claim.id, run_id=job.id, check_type="system", result="fail",
                               reason_code="check_error", message_ar="تعذر إكمال التحقق الآلي. أعد المحاولة.",
                               message_en="The automatic check couldn't finish. Please retry."))
            add_event(db, claim, "auto_check", "check_failed", "system", "checks", idem=f"{job.id}:failed")
            db.commit()


def _apply_outcome(db: Session, claim: Claim, job: CheckJob, checks: List[Check]) -> None:
    now = utcnow()
    for old in db.scalars(select(CheckResult).where(CheckResult.run_id == job.id)):
        db.delete(old)  # a resumed job replaces its own partial results, never older rounds
    db.flush()
    for c in checks:
        db.add(CheckResult(claim_id=claim.id, run_id=job.id, check_type=c.check_type, result=c.result,
                           reason_code=c.reason_code, message_ar=c.message_ar, message_en=c.message_en,
                           evidence_json=json.dumps(c.evidence, ensure_ascii=False, default=str) if c.evidence else None,
                           evidence_ref=c.evidence_ref, related_claim_id=c.related_claim_id,
                           rule_version=c.rule_version, checked_at=now))
    refresh_invoice_records(db, claim)
    fails = [c for c in checks if c.result == "fail"]
    flags = [c.reason_code for c in checks if c.result == "manual_review"]
    claim.last_update_at = now
    claim.version += 1
    if fails:
        claim.action_required = True
        add_event(db, claim, "auto_check", "needs_completion", "system", "checks",
                  reason=fails[0].message_ar, data={"reasons": [c.reason_code for c in fails]},
                  idem=f"{job.id}:outcome")
        return
    claim.action_required = False
    claim.stage = "employee_review"
    start_review_clock(db, claim, now)
    add_event(db, claim, "employee_review", "queued_for_review", "system", "checks",
              data={"flags": flags} if flags else None, idem=f"{job.id}:outcome")


def recover_jobs() -> List[str]:
    """After a restart, resume jobs that were queued or interrupted mid-run."""
    with SessionLocal() as db:
        jobs = list(db.scalars(select(CheckJob).where(CheckJob.status.in_(("queued", "running")))))
        for j in jobs:
            j.status = "queued"
        db.commit()
        return [j.id for j in jobs]


# ====================================================================== SLA


def sla_for(db: Session, category: Optional[str]) -> Optional[SlaTarget]:
    return db.get(SlaTarget, category) if category else None


def start_review_clock(db: Session, claim: Claim, now: datetime) -> None:
    claim.review_started_at = now
    claim.review_paused_at = None
    sla = sla_for(db, claim.category)
    claim.review_due_at = now + timedelta(hours=sla.employee_estimate_hours) if sla else None


# ====================================================================== member transitions


def submit(db: Session, claim: Claim, view, member_id: Optional[str], actor_id: Optional[str]) -> CheckJob:
    """Called inside the submission transaction, after the draft→submitted update succeeded."""
    now = claim.submitted_at or utcnow()
    totals = view.totals
    cur = next(iter(totals), None) if totals else None
    claim.member_id = claim.member_id or member_id
    claim.category = claim.category or (view.category if view.category in TRACK_CATEGORIES else view.category)
    claim.currency = cur
    claim.claimed_halalas = to_halalas(totals[cur]) if cur and len(totals) == 1 else None
    if claim.member_id and not claim.policy_id:
        pol = db.scalar(select(Policy).where(Policy.member_id == claim.member_id).order_by(Policy.valid_to.desc()))
        claim.policy_id = pol.policy_id if pol else None
    claim.stage = "auto_check"
    claim.action_required = False
    claim.check_round = 1
    claim.last_update_at = now
    add_event(db, claim, "submitted", "submitted", "member", actor_id, idem=f"{claim.id}:submitted", at=now)
    add_event(db, claim, "auto_check", "checks_started", "system", "checks", idem=f"{claim.id}:r1:started", at=now)
    refresh_invoice_records(db, claim, view)
    return enqueue_checks(db, claim)


def can_complete(claim: Claim) -> bool:
    """The member may change documents only while a submitted claim waits for them."""
    return claim.stage == "auto_check" and bool(claim.action_required)


def resubmit(db: Session, claim: Claim, expected_version: int, actor_id: Optional[str]) -> Optional[CheckJob]:
    """Member finished completing documents: same claim, new check round."""
    if not can_complete(claim):
        return None
    res = db.execute(update(Claim).where(Claim.id == claim.id, Claim.version == expected_version,
                                         Claim.stage == "auto_check", Claim.action_required.is_(True))
                     .values(check_round=Claim.check_round + 1, version=Claim.version + 1,
                             last_update_at=utcnow()))
    if res.rowcount != 1:
        return None
    db.refresh(claim)
    add_event(db, claim, "auto_check", "resubmitted", "member", actor_id,
              idem=f"{claim.id}:r{claim.check_round}:resubmitted")
    return enqueue_checks(db, claim)


def retry_checks(db: Session, claim: Claim) -> Optional[CheckJob]:
    """Re-run a stalled or failed round (same round; the job itself is idempotent)."""
    if claim.stage != "auto_check":
        return None
    job = db.scalar(select(CheckJob).where(CheckJob.claim_id == claim.id, CheckJob.round == claim.check_round))
    if job is None:
        return enqueue_checks(db, claim)
    if job.status in ("failed", "queued", "running"):
        job.status = "queued"
        claim.action_required = False
        return job
    return None


def start_seeded_checks(db: Session, claim: Claim, actor_id: str) -> Optional[CheckJob]:
    """Demo helper for imported claims still at stage 1 (e.g. T08): begin automatic checks."""
    if claim.stage != "submitted":
        return None
    claim.stage = "auto_check"
    claim.check_round = max(1, claim.check_round)
    claim.version += 1
    claim.last_update_at = utcnow()
    add_event(db, claim, "auto_check", "checks_started", "system", actor_id,
              idem=f"{claim.id}:r{claim.check_round}:started")
    return enqueue_checks(db, claim)


# ====================================================================== employee actions


class DecisionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _guarded(db: Session, claim: Claim, version: int, allowed: Sequence[str], **values) -> None:
    res = db.execute(update(Claim).where(Claim.id == claim.id, Claim.version == version, Claim.stage.in_(allowed))
                     .values(version=Claim.version + 1, last_update_at=utcnow(), **values))
    if res.rowcount != 1:
        raise DecisionError("version_conflict")
    db.refresh(claim)


def request_documents(db: Session, claim: Claim, version: int, employee_id: str, message: str,
                      doc_types: Sequence[str]) -> None:
    message = (message or "").strip()
    if not message:
        raise DecisionError("message_required")
    now = utcnow()
    _guarded(db, claim, version, ("employee_review", "auto_check"),
             stage="auto_check", action_required=True, review_paused_at=now, review_due_at=None)
    db.add(CheckResult(claim_id=claim.id, run_id=f"employee:{claim.version}", check_type="employee_request",
                       result="fail", reason_code="employee_requested_documents", message_ar=message,
                       message_en=message, evidence_json=json.dumps({"doc_types": list(doc_types)}),
                       checked_at=now))
    add_event(db, claim, "auto_check", "documents_requested", "employee", employee_id, reason=message,
              data={"doc_types": list(doc_types)})


def approve(db: Session, claim: Claim, version: int, employee_id: str, amount: Optional[Decimal],
            note: str = "") -> None:
    note = (note or "").strip()
    if amount is None or amount < 0:
        raise DecisionError("amount_required")
    halalas = to_halalas(amount)
    if claim.claimed_halalas is not None and halalas > claim.claimed_halalas:
        raise DecisionError("amount_above_claimed")
    if halalas == 0 and not note:
        raise DecisionError("zero_needs_reason")
    now = utcnow()
    _guarded(db, claim, version, ("employee_review",), stage="decided", decision="approved",
             approved_halalas=halalas, decision_reason=note or None, decided_at=now, action_required=False)
    add_event(db, claim, "decided", "approved", "employee", employee_id, reason=note or None,
              data={"approved_sar": fmt_sar(halalas)})


def reject(db: Session, claim: Claim, version: int, employee_id: str, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise DecisionError("reason_required")
    now = utcnow()
    _guarded(db, claim, version, ("employee_review",), stage="decided", decision="rejected",
             approved_halalas=None, decision_reason=reason, decided_at=now, action_required=False)
    add_event(db, claim, "decided", "rejected", "employee", employee_id, reason=reason)


def mark_paid(db: Session, claim: Claim, version: int, employee_id: str) -> None:
    """Record a (demo) payment. Only an approved claim with an approved amount can be paid."""
    if claim.decision != "approved" or claim.approved_halalas is None:
        raise DecisionError("not_approved")
    if claim.payout_status == "paid":
        raise DecisionError("already_paid")
    now = utcnow()
    _guarded(db, claim, version, ("decided",), payout_status="paid", paid_at=now,
             paid_halalas=claim.approved_halalas)
    add_event(db, claim, "decided", "payout_recorded", "employee", employee_id,
              data={"paid_sar": fmt_sar(claim.paid_halalas)})


# ====================================================================== presentation helpers


def latest_checks(db: Session, claim: Claim) -> List[CheckResult]:
    """Results of the most recent automatic run (or the seeded checks) plus open employee requests."""
    rows = list(db.scalars(select(CheckResult).where(CheckResult.claim_id == claim.id)
                           .order_by(CheckResult.checked_at.desc(), CheckResult.id)))
    if not rows:
        return []
    auto = [r for r in rows if r.check_type != "employee_request"]
    latest_run = auto[0].run_id if auto else None
    out = [r for r in auto if r.run_id == latest_run]
    if claim.stage == "auto_check" and claim.action_required:
        req = [r for r in rows if r.check_type == "employee_request"]
        if req and (not auto or req[0].checked_at >= auto[0].checked_at):
            out = [req[0]] + out
    order = {"fail": 0, "manual_review": 1, "pass": 2}
    return sorted(out, key=lambda r: order.get(r.result, 3))


def is_technical(checks: Sequence[CheckResult]) -> bool:
    fails = [c for c in checks if c.result == "fail"]
    return bool(fails) and all(c.reason_code in TECHNICAL_REASONS for c in fails)


STATUS_LABELS = {
    "draft": ("مسودة", "Draft", "neutral"),
    "submitted": ("تم الإرسال", "Submitted", "progress"),
    "auto_check": ("قيد المراجعة", "Under Review", "progress"),
    "needs_completion": ("يحتاج مستندات", "Needs documents", "warn"),
    "check_failed": ("تعذر التحقق", "Couldn't complete checks", "warn"),
    "employee_review": ("قيد المعالجة", "Under Processing", "progress"),
    "approved": ("تمت الموافقة", "Approved", "ok"),
    "rejected": ("تم الرفض", "Rejected", "bad"),
}


def status_code(claim: Claim, checks: Optional[Sequence[CheckResult]] = None) -> str:
    if claim.stage is None:
        return "draft"
    if claim.stage == "decided":
        return "approved" if claim.decision == "approved" else "rejected"
    if claim.stage == "auto_check" and claim.action_required:
        return "check_failed" if (checks is not None and is_technical(checks)) else "needs_completion"
    return claim.stage


def tracker(db: Session, claim: Claim) -> List[Dict[str, Any]]:
    """Four stages with the real timestamp of each stage the claim has actually reached."""
    events = events_of(db, claim.id)
    current = STAGES.index(claim.stage) if claim.stage in STAGES else -1
    out = []
    for idx, stage in enumerate(STAGES):
        reached = [e for e in events if e.stage == stage]
        state = "done" if idx < current else ("current" if idx == current else "upcoming")
        if stage == "decided" and claim.stage == "decided":
            state = "done"
        if idx == current and stage == "auto_check" and claim.action_required:
            state = "paused"
        at = reached[-1].occurred_at if reached and state != "upcoming" else None
        if stage == "auto_check" and reached and state != "upcoming":
            at = [e for e in reached][-1].occurred_at
        out.append({"stage": stage, "state": state, "at": at})
    return out


# Arabic member-facing text per reason code (seeded checks carry English text from the workbook).
REASON_AR = {
    "invoice_total_missing": "إجمالي الفاتورة غير ظاهر. أرفق فاتورة واضحة وكاملة.",
    "invoice_number_missing": "رقم الفاتورة غير ظاهر. أرفق فاتورة واضحة وكاملة.",
    "invoice_missing": "لم نجد فاتورة بين المستندات. أرفق الفاتورة.",
    "identity_missing": "رقم العضوية أو الهوية غير ظاهر في المستند.",
    "identity_mismatch": "رقم الهوية في المستند لا يطابق بيانات العضو ويحتاج مراجعة الموظف.",
    "payment_proof_missing": "إثبات الدفع غير مرفق. أرفق إثبات الدفع لهذه الزيارة.",
    "payment_amount_mismatch": "المبلغ المدفوع لا يطابق إجمالي الفاتورة ويحتاج مراجعة الموظف.",
    "paid_duplicate": "هذه الفاتورة مرتبطة بمطالبة تم تعويضها مسبقًا",
    "pending_duplicate": "توجد مطالبة قيد المراجعة لهذه الفاتورة",
    "prior_claim_not_paid": "توجد مطالبة سابقة لهذه الفاتورة لم يُصرف لها تعويض. يحتاج مراجعة الموظف.",
    "policy_limit_review": "المبلغ يتجاوز الحد التجريبي ويحتاج مراجعة الموظف.",
    "service_excluded": "الخدمة مدرجة ضمن الاستثناءات التجريبية للوثيقة. يحتاج قرار الموظف.",
    "documents_consistent": "اكتملت المستندات وتنتظر قرار الموظف.",
    "document_unavailable": "تعذر قراءة أحد المستندات. أعد المحاولة أو استبدل الملف.",
    "document_unreadable": "أحد المستندات غير واضح. أعد تصويره أو ارفع نسخة أوضح.",
}
