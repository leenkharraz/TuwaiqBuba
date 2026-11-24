"""View models for the member claim history, tracking page and the employee dashboard."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import lifecycle
from .config import settings
from .models import CheckJob, Claim, SlaTarget, utcnow

try:
    from zoneinfo import ZoneInfo

    RIYADH = ZoneInfo("Asia/Riyadh")
except Exception:  # pragma: no cover - zoneinfo data missing
    RIYADH = timezone(timedelta(hours=3), "AST")

CATEGORY_AR = {"optical": "البصريات", "dental": "الأسنان", "inpatient": "التنويم", "outpatient": "العيادات الخارجية",
               "physiotherapy": "العلاج الطبيعي", "medication": "الأدوية"}
CATEGORY_EN = {"optical": "Optical", "dental": "Dental", "inpatient": "Inpatient", "outpatient": "Outpatient",
               "physiotherapy": "Physiotherapy", "medication": "Medication"}
STAGE_KEYS = {"submitted": "track.stage_submitted", "auto_check": "track.stage_review",
              "employee_review": "track.stage_processing", "decided": "track.stage_final"}


def local(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(RIYADH)


def fmt_local(dt: Optional[datetime], style: str = "datetime") -> str:
    """Stored UTC → shown in Asia/Riyadh."""
    d = local(dt)
    if d is None:
        return "—"
    if style == "date":
        return d.strftime("%d/%m/%Y")
    return d.strftime("%d/%m/%Y %H:%M")


def status_label(code: str, lang: str = "ar") -> str:
    ar, en, _tone = lifecycle.STATUS_LABELS[code]
    return ar if lang == "ar" else en


def status_tone(code: str) -> str:
    return lifecycle.STATUS_LABELS[code][2]


# ====================================================================== member


def member_claims(db: Session, member_id: Optional[str], sid: str) -> Dict[str, List[Claim]]:
    """Submitted claims (newest first) and drafts for this member (or this session if no member)."""
    legacy = (Claim.session_id == sid) & Claim.member_id.is_(None)  # claims made before member sign-in existed
    owner = or_(Claim.member_id == member_id, legacy) if member_id else legacy
    # Records explicitly marked as imported test data or sample data are never listed or counted.
    rows = list(db.scalars(select(Claim).where(owner, Claim.origin != "seed", Claim.is_sample.is_(False))))
    submitted = sorted([c for c in rows if c.stage is not None], key=lambda c: c.submitted_at or c.created_at,
                       reverse=True)
    drafts = sorted([c for c in rows if c.stage is None and c.status == "draft" and c.active_documents],
                    key=lambda c: c.updated_at, reverse=True)
    return {"submitted": submitted, "drafts": drafts}


def claim_row(db: Session, c: Claim) -> Dict[str, Any]:
    checks = lifecycle.latest_checks(db, c)
    code = lifecycle.status_code(c, checks)
    return {"id": c.id, "number": c.claim_number or c.reference or c.id[:8], "category": c.category,
            "category_ar": CATEGORY_AR.get(c.category or "", "—"), "submitted": fmt_local(c.submitted_at, "date"),
            "status": code, "status_ar": status_label(code, "ar"), "tone": status_tone(code),
            "paid": c.payout_status == "paid" and c.paid_at is not None}


def _job(db: Session, c: Claim) -> Optional[CheckJob]:
    return db.scalar(select(CheckJob).where(CheckJob.claim_id == c.id, CheckJob.round == c.check_round))


def track_context(db: Session, c: Claim) -> Dict[str, Any]:
    now = utcnow()
    checks = lifecycle.latest_checks(db, c)
    code = lifecycle.status_code(c, checks)
    stages = lifecycle.tracker(db, c)
    for s in stages:
        s["label_key"] = STAGE_KEYS[s["stage"]]
        s["time"] = fmt_local(s["at"]) if s["at"] else None
    if c.stage == "decided":
        stages[-1]["label_key"] = "track.approved" if c.decision == "approved" else "track.rejected"
    sla = db.get(SlaTarget, c.category) if c.category else None
    if sla is not None and sla.is_synthetic:
        sla = None  # fictional estimates from test data are never shown to customers
    estimate: Dict[str, Any] = {"kind": None}
    job = _job(db, c)
    if c.stage in ("submitted", "auto_check") and not c.action_required:
        started = (job.started_at or job.created_at) if job else c.submitted_at
        target = sla.initial_check_target_minutes if sla else settings.initial_check_target_minutes
        late = started is not None and now > started + timedelta(minutes=target)
        estimate = {"kind": "initial", "late": late and c.stage == "auto_check",
                    "show": settings.show_initial_check_estimate, "minutes": target}
    elif c.stage == "auto_check" and c.action_required:
        estimate = {"kind": "paused"}
    elif c.stage == "employee_review":
        if c.review_due_at and sla is not None:
            estimate = {"kind": "review", "due": fmt_local(c.review_due_at), "overdue": now > c.review_due_at,
                        "hours": sla.employee_estimate_hours}
        else:
            estimate = {"kind": "review_unknown"}
    issues = [r for r in checks if r.result == "fail"]
    dup = next((r for r in checks if r.check_type == "duplicate_check"
                and r.reason_code in ("paid_duplicate", "pending_duplicate")), None)
    related = None
    if dup and dup.related_claim_id:
        other = db.get(Claim, dup.related_claim_id)
        if other is not None and other.member_id and other.member_id == c.member_id:
            related = {"id": other.id, "number": other.claim_number}
        else:
            dup = None  # never reveal another member's claim
    member_review_requested = any(e.event_type == "member_requested_review" for e in lifecycle.events_of(db, c.id))
    return {
        "claim": c, "status": code, "status_ar": status_label(code), "tone": status_tone(code),
        "category_ar": CATEGORY_AR.get(c.category or "", "—"),
        "amount": lifecycle.fmt_sar(c.claimed_halalas), "submitted": fmt_local(c.submitted_at),
        "updated": fmt_local(c.last_update_at or c.updated_at), "stages": stages, "estimate": estimate,
        "issues": issues, "technical": lifecycle.is_technical(checks), "dup": dup, "related": related,
        "review_requested": member_review_requested,
        "approved": lifecycle.fmt_sar(c.approved_halalas), "paid": lifecycle.fmt_sar(c.paid_halalas),
        "paid_at": fmt_local(c.paid_at) if c.paid_at else None,
        "docs": c.active_documents, "version": c.version,
        "processing": c.stage in ("submitted", "auto_check") and not c.action_required and c.check_round > 0,
    }
