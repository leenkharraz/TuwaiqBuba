"""Structured claim output (provisional integration schema).

This is the prototype's own schema for a future claims-system integration. It
is NOT Bupa's official internal claims schema.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from .engine import ClaimView, Field, InvoiceView, Issue
from .i18n import t
from .models import Claim
from .normalize import money
from .present import field_label, issue_text
from .rules import get_rules

SCHEMA_VERSION = "0.1.0-prototype"
SCHEMA_NOTE = ("Provisional integration schema produced by a Bupa Discovery Program prototype. "
               "It is not Bupa Arabia's official claims schema.")
DISCLAIMER = ("Saved locally in a prototype. Not sent to, reviewed by or approved by Bupa Arabia. "
              "'ready_for_submission' only means the prototype's document and consistency checks passed; "
              "it does not establish coverage, authenticity, medical necessity or reimbursement.")


def new_reference(now: datetime) -> str:
    return f"DP-{now.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def field_json(fld: Field) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "value": fld.value,
        "status": fld.status,
        "sources": [s.as_json() for s in fld.sources],
    }
    if fld.original_value is not None and fld.member_set:
        out["original_extracted_value"] = fld.original_value
    if fld.note:
        out["review_note"] = fld.note
    if len(fld.candidates) > 1:
        out["candidates"] = [{"value": c.value, "sources": [s.as_json() for s in c.sources]} for c in fld.candidates]
    return out


def _issue_json(i: Issue) -> Dict[str, Any]:
    return {
        "key": i.key,
        "code": i.code,
        "severity": i.severity,
        "blocking": i.blocking,
        "acknowledged_by_member": i.acknowledged,
        "message_ar": issue_text(i, "ar"),
        "message_en": issue_text(i, "en"),
        "document_id": i.doc_id,
        "field_key": i.field_key,
        "actions": [a.kind for a in i.actions],
    }


def _invoice_json(inv: InvoiceView) -> Dict[str, Any]:
    f = inv.fields
    return {
        "invoice_id": f"inv-{inv.doc_id[:8]}",
        "document_id": inv.doc_id,
        "invoice_number": field_json(f["invoice_number"]),
        "invoice_date": field_json(f["invoice_date"]),
        "service_date": field_json(f["service_date"]),
        "provider_name": field_json(f["provider_name"]),
        "currency": field_json(f["currency"]),
        "subtotal": field_json(f["subtotal"]),
        "tax": field_json(f["tax"]),
        "total": field_json(f["total"]),
        "paid_amount_on_invoice": field_json(f["paid_amount"]) if f["paid_amount"].value else None,
        "line_items": [
            {
                "description": li.description,
                "category": li.category,
                "tooth_number": li.tooth_number,
                "quantity": li.quantity,
                "amount": money(li.amount) if li.amount is not None else None,
                "amount_as_printed": li.amount_raw,
                "source": li.source.as_json(),
            }
            for li in inv.lines
        ],
        "payment_status": inv.payment_status,
        "paid_total": money(inv.paid_total) if inv.paid_total is not None else None,
        "linked_payment_ids": [f"pay-{p.doc_id[:8]}" for p in inv.payments if not p.duplicate_of],
        "duplicate_of_document_id": inv.duplicate_of,
        "visit_grouping": {"group": inv.group, "member_confirmed": inv.member_confirmed_group},
    }


def build_export(claim: Claim, view: ClaimView, reference: Optional[str],
                 submitted_at: Optional[datetime]) -> Dict[str, Any]:
    rules = get_rules()
    cf = view.claim_fields
    docs = {d.id: d for d in claim.documents}
    primary = set(view.primary_doc_ids)

    payments = []
    for p in view.payments:
        if p.group != "primary":
            continue
        payments.append({
            "payment_id": f"pay-{p.doc_id[:8]}",
            "document_id": p.doc_id,
            "paid_amount": field_json(p.fields["paid_amount"]),
            "currency": field_json(p.fields["currency"]),
            "payment_date": field_json(p.fields["payment_date"]),
            "payment_method": field_json(p.fields["payment_method"]),
            "transaction_reference": field_json(p.fields["transaction_reference"]),
            "referenced_invoice_number": field_json(p.fields["referenced_invoice_number"]),
            "linked_invoice_id": f"inv-{p.linked_invoice_id[:8]}" if p.linked_invoice_id else None,
            "link_basis": p.link_basis,
            "duplicate_of_document_id": p.duplicate_of,
            "counts_toward_claimed_total": False,
        })

    services = []
    for inv in view.invoices:
        for n, li in enumerate(inv.lines, start=1):
            services.append({
                "service_id": f"svc-{inv.doc_id[:8]}-{n}",
                "invoice_id": f"inv-{inv.doc_id[:8]}",
                "description": li.description,
                "category": li.category,
                "tooth_number": li.tooth_number,
                "amount": money(li.amount) if li.amount is not None else None,
                "currency": inv.fields["currency"].value,
            })

    attachments = []
    for d in claim.active_documents:
        dv = view.docs.get(d.id)
        attachments.append({
            "document_id": d.id,
            "filename": d.original_filename,
            "mime_type": d.mime_type,
            "size_bytes": d.size_bytes,
            "sha256": d.sha256,
            "pages": d.page_count,
            "document_type": dv.doc_type if dv else None,
            "document_type_source": "member" if d.type_override else ("extracted" if dv and dv.doc_type else None),
            "capture_source": d.source,
            "extraction_status": d.status,
            "extraction_mode": d.extraction_mode,
            "extraction_model": d.extraction_model,
            "included_in_this_claim": d.id in primary,
            "storage": "private-local",
        })

    source_refs: List[Dict[str, Any]] = []

    def collect(path: str, fld: Field) -> None:
        for s in fld.sources:
            source_refs.append({"field": path, **s.as_json()})

    for name, fld in cf.items():
        collect(f"claim.{name}", fld)
    for i, inv in enumerate(view.invoices):
        for name, fld in inv.fields.items():
            collect(f"invoices[{i}].{name}", fld)
    for i, p in enumerate(payments):
        pv = next(x for x in view.payments if x.doc_id == p["document_id"])
        for name, fld in pv.fields.items():
            collect(f"payments[{i}].{name}", fld)

    corrections = []
    for c in claim.corrections:
        if c.claim_id != claim.id:
            continue
        corrections.append({
            "field_key": c.field_key,
            "kind": c.kind,
            "original_extracted_value": json.loads(c.original_value) if c.original_value else None,
            "member_value": json.loads(c.corrected_value) if c.corrected_value else None,
            "original_sources": json.loads(c.original_sources_json) if c.original_sources_json else [],
            "active": c.active,
            "superseded_reason": c.superseded_reason,
            "recorded_at": _iso(c.created_at),
        })

    status = "saved_locally" if reference else ("ready_for_submission" if view.ready else "draft_incomplete")
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_note": SCHEMA_NOTE,
        "disclaimer": DISCLAIMER,
        "claim": {
            "claim_id": claim.id,
            "claim_reference": reference,
            "status": status,
            "ready_for_submission": view.ready,
            "data_mode": "sample_fixtures" if claim.is_sample else "live_extraction",
            "split_from_claim_id": claim.split_from_claim_id,
            "created_at": _iso(claim.created_at),
            "submitted_at": _iso(submitted_at),
            "member_reviewed": bool(reference),
        },
        "patient": {
            "name": field_json(cf["patient_name"]),
            "identifier": field_json(cf["patient_identifier"]),
        },
        "provider": {"name": field_json(cf["provider_name"])},
        "service": {
            "category": field_json(cf["service_category"]),
            "claim_location": field_json(cf["claim_location"]),
            "service_date": field_json(cf["service_date"]),
            "diagnosis_as_written": field_json(cf["diagnosis"]),
        },
        "invoices": [_invoice_json(inv) for inv in view.invoices],
        "payments": payments,
        "services": services,
        "totals": {
            "claimed_by_currency": {k: money(v) for k, v in view.totals.items()},
            "paid_by_currency": {k: money(v) for k, v in view.paid_totals.items()},
            "note": "Totals are per currency and are never summed across currencies. Payments do not add to the claimed total.",
        },
        "attachments": attachments,
        "source_references": source_refs,
        "corrections": corrections,
        "validation": {
            "ready_for_submission": view.ready,
            "readiness_meaning": "Prototype submission checks passed. Not an approval or coverage decision.",
            "rule_set": {"id": rules.rule_set_id, "version": rules.version, "status": rules.status,
                         "note": rules.source_note["en"]},
            "document_checklist": [
                {"requirement_id": r.id, "label_en": r.label.get("en"), "label_ar": r.label.get("ar"),
                 "condition": r.when, "status": r.status, "satisfied_by_document_ids": r.satisfied_by,
                 "trigger": r.trigger}
                for r in view.requirements
            ],
            "unresolved_blocking": [_issue_json(i) for i in view.issues if i.blocking],
            "acknowledged": [_issue_json(i) for i in view.issues if i.acknowledged],
            "warnings": [_issue_json(i) for i in view.issues if not i.blocking and not i.acknowledged
                         and i.severity == "warning"],
            "info": [_issue_json(i) for i in view.issues if i.severity == "info"],
        },
        "grouping": {
            "separate_or_unconfirmed_groups": [
                {"kind": p.kind, "document_ids": p.doc_ids, "reasons": p.reasons} for p in view.proposals
            ],
            "member_decisions": [
                {"document_id": d.id, "decision": d.grouping_decision}
                for d in claim.active_documents if d.grouping_decision
            ],
        },
    }


def resolved_items(claim: Claim, view: ClaimView) -> List[Dict[str, Any]]:
    """Things the member resolved, for the summary screen (both languages rendered in template)."""
    items: List[Dict[str, Any]] = []
    names = {d.id: d.original_filename for d in claim.active_documents}
    for c in claim.corrections:
        if c.active:
            items.append({"kind": c.kind, "field": c.field_key.rsplit(":", 1)[-1]})
    for i in view.issues:
        if i.acknowledged:
            items.append({"kind": "ack", "issue": i})
    for d in claim.active_documents:
        if d.grouping_decision == "same_visit":
            items.append({"kind": "grouping", "filename": names[d.id]})
        if d.linked_invoice_doc_id and d.linked_invoice_doc_id != "none":
            items.append({"kind": "link", "filename": names[d.id]})
        if d.type_override:
            items.append({"kind": "type", "filename": names[d.id]})
    return items
