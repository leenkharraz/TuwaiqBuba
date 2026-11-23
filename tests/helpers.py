"""Builders for engine tests: synthetic DocIn objects with extraction data."""
from datetime import date
from typing import Dict, List, Optional

from app.engine import ClaimIn, ClaimView, CorrectionIn, DocIn, build_claim_view
from app.rules import get_rules
from app.schemas import DocumentExtraction

TODAY = date(2026, 11, 23)
_pos = [0]


def ev(value, excerpt=None):
    return {"value": value, "excerpt": excerpt or value, "page": 1} if value is not None else {}


def _doc(doc_id: str, data: dict, status: str = "extracted", **kw) -> DocIn:
    _pos[0] += 1
    base = {"readability": "readable"}
    base.update(data)
    return DocIn(id=doc_id, position=kw.pop("position", _pos[0]), filename=f"{doc_id}.png", status=status,
                 extraction=DocumentExtraction.model_validate(base), **kw)


def invoice(doc_id="inv1", number="INV-1", total="575.00", currency="SAR", date_="2026-11-16",
            provider="Al Waha Medical Complex", patient="Reem Khalid Almutairi", pid="0042187",
            category="outpatient", lines=None, subtotal=None, tax=None, country="Saudi Arabia",
            paid=None, **kw) -> DocIn:
    if lines is None:
        lines = [{"description": "Consultation", "category": "consultation", "amount": total}]
    return _doc(doc_id, {
        "document_type": "invoice",
        "invoice_number": ev(number), "invoice_date": ev(date_), "service_date": ev(date_),
        "provider_name": ev(provider), "provider_country": ev(country),
        "patient_name": ev(patient), "patient_identifier": ev(pid),
        "total": ev(total), "subtotal": ev(subtotal), "tax": ev(tax), "currency": ev(currency),
        "paid_amount": ev(paid), "service_category_hint": category, "line_items": lines,
    }, **kw)


def receipt(doc_id="rcp1", amount="575.00", ref="INV-1", currency="SAR", txn="RRN-1",
            provider="Al Waha Medical Complex", **kw) -> DocIn:
    return _doc(doc_id, {
        "document_type": "payment_receipt",
        "paid_amount": ev(amount), "currency": ev(currency), "payment_date": ev("2026-11-16"),
        "transaction_reference": ev(txn), "referenced_invoice_number": ev(ref), "provider_name": ev(provider),
    }, **kw)


def medical(doc_id="rx1", doc_type="prescription", patient="Reem Khalid Almutairi", pid="0042187",
            diagnosis="Acute pharyngitis (J02.9)", date_="2026-11-16", meds=True,
            provider="Al Waha Medical Complex", **kw) -> DocIn:
    return _doc(doc_id, {
        "document_type": doc_type, "patient_name": ev(patient), "patient_identifier": ev(pid),
        "diagnosis": ev(diagnosis), "service_date": ev(date_), "medication_prescribed": meds,
        "provider_name": ev(provider), "service_category_hint": "outpatient",
    }, **kw)


def build(docs: List[DocIn], corrections: Optional[Dict[str, str]] = None, acks=None,
          kinds: Optional[Dict[str, str]] = None, context: Optional[Dict[str, list]] = None) -> ClaimView:
    for i, d in enumerate(docs, start=1):  # list order == upload order
        d.position = i
    corr = {}
    for i, (k, v) in enumerate((corrections or {}).items(), start=1):
        corr[k] = CorrectionIn(id=i, field_key=k, value=v, kind=(kinds or {}).get(k, "correction"),
                               context_values=(context or {}).get(k, []))
    return build_claim_view(ClaimIn(id="c1", docs=docs, corrections=corr, acknowledged=set(acks or []),
                                    rules=get_rules(), today=TODAY))


def codes(view: ClaimView, blocking_only=False) -> List[str]:
    return [i.code for i in view.issues if (i.blocking or not blocking_only)]
