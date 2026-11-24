"""Synthetic demo documents for the workbook scenarios (English only, all fictional).

The same row values are used to *draw* each document and to build its prepared
extraction, so what the file shows and what the app records always agree. These
results are labelled as demo data in the app and are never presented as real OCR.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw, ImageFont

from .config import settings
from .schemas import DocumentExtraction

FIXTURE_SCHEME = "fixture://"
DEMO_NOTICE = "FICTIONAL SAMPLE - synthetic test document. Not a real invoice, receipt or medical record."
CATEGORY_LINES = {
    "outpatient": "Clinic consultation (fictional)",
    "dental": "Dental filling (fictional)",
    "optical": "Prescription glasses (fictional)",
    "inpatient": "Hospital stay, 2 nights (fictional)",
}
WB_DOC_TYPES = {"invoice": "invoice", "payment_receipt": "payment_receipt", "medical_report": "medical_report"}

_FONTS = ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
          "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]
_BOLD = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "C:/Windows/Fonts/arialbd.ttf"] + _FONTS


def fixture_root() -> Path:
    return settings.data_dir / "demo_fixtures"


def uri_to_path(uri: str) -> Path:
    if not uri.startswith(FIXTURE_SCHEME):
        raise ValueError(f"not a fixture URI: {uri}")
    rel = uri[len(FIXTURE_SCHEME):]
    root = fixture_root().resolve()
    path = (root / rel).resolve()
    if root not in path.parents:
        raise ValueError(f"fixture URI escapes the fixture directory: {uri}")
    return path


@dataclass
class DocSpec:
    """Everything needed to draw one document and describe its contents."""

    doc_type: str  # invoice | payment_receipt | medical_report
    claim_id: str
    category: str
    member_name: str
    member_identity: Optional[str]  # None = not printed on the document
    provider_name: str
    invoice_number: str
    service_date: date
    total: Optional[Decimal]  # None = total not printed
    service_code: str
    paid_amount: Optional[Decimal] = None
    receipt_number: Optional[str] = None


def _font(cands, size):
    for c in cands:
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def _money(d: Optional[Decimal]) -> str:
    return f"{d:,.2f}" if d is not None else ""


def render_pdf(spec: DocSpec) -> bytes:
    W, H = 1240, 1754  # A4 at 150 dpi
    img = Image.new("RGB", (W, H), "white")
    g = ImageDraw.Draw(img)
    f_h, f_b, f_r, f_s = _font(_BOLD, 44), _font(_BOLD, 30), _font(_FONTS, 30), _font(_FONTS, 22)
    blue, ink, muted = (0, 92, 160), (20, 32, 52), (90, 104, 124)
    g.rectangle([0, 0, W, 18], fill=blue)
    g.text((80, 70), spec.provider_name, font=f_h, fill=blue)
    g.text((80, 130), "Riyadh (fictional address)  |  Provider ID PROV-DEMO-01", font=f_s, fill=muted)
    title = {"invoice": "TAX INVOICE", "payment_receipt": "PAYMENT RECEIPT", "medical_report": "MEDICAL REPORT"}[spec.doc_type]
    g.text((80, 220), title, font=f_h, fill=ink)
    g.line([80, 290, W - 80, 290], fill=(205, 216, 230), width=2)

    rows = []
    if spec.doc_type == "invoice":
        rows = [("Invoice No.", spec.invoice_number), ("Invoice date", spec.service_date.isoformat()),
                ("Service date", spec.service_date.isoformat()), ("Patient", spec.member_name)]
        if spec.member_identity:
            rows.append(("Member ID", spec.member_identity))
        rows += [("Service code", spec.service_code), ("Currency", "SAR")]
    elif spec.doc_type == "payment_receipt":
        rows = [("Receipt No.", spec.receipt_number or "-"), ("Payment date", spec.service_date.isoformat()),
                ("For invoice No.", spec.invoice_number), ("Paid by", spec.member_name),
                ("Payment method", "Card (fictional)"), ("Amount paid (SAR)", _money(spec.paid_amount))]
    else:
        rows = [("Patient", spec.member_name)]
        if spec.member_identity:
            rows.append(("Member ID", spec.member_identity))
        rows += [("Visit date", spec.service_date.isoformat()), ("Department", spec.category.title()),
                 ("Diagnosis", "Fictional diagnosis for testing only")]
    y = 330
    for label, value in rows:
        g.text((80, y), label, font=f_r, fill=muted)
        g.text((520, y), value, font=f_b, fill=ink)
        y += 62

    if spec.doc_type == "invoice":
        y += 30
        g.rectangle([80, y, W - 80, y + 56], fill=(232, 241, 250))
        g.text((100, y + 12), "Description", font=f_b, fill=ink)
        g.text((W - 340, y + 12), "Amount (SAR)", font=f_b, fill=ink)
        y += 80
        g.text((100, y), CATEGORY_LINES.get(spec.category, "Medical service (fictional)"), font=f_r, fill=ink)
        g.text((W - 340, y), _money(spec.total) or "", font=f_r, fill=ink)
        y += 90
        g.line([80, y, W - 80, y], fill=(205, 216, 230), width=2)
        y += 24
        g.text((100, y), "TOTAL (SAR)", font=f_b, fill=ink)
        if spec.total is not None:
            g.text((W - 340, y), _money(spec.total), font=f_b, fill=ink)
        else:
            g.rectangle([W - 360, y - 6, W - 100, y + 44], fill=(60, 60, 60))  # total obscured on purpose

    g.text((80, H - 120), DEMO_NOTICE, font=f_s, fill=(170, 40, 40))
    g.text((80, H - 84), f"Claim reference in test data: {spec.claim_id}", font=f_s, fill=muted)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    return buf.getvalue()


def _ev(value: Optional[str], excerpt: Optional[str] = None) -> Dict[str, Any]:
    if value is None:
        return {}
    return {"value": value, "excerpt": excerpt or value, "page": 1}


def extraction(spec: DocSpec) -> DocumentExtraction:
    """Prepared extraction matching exactly what ``render_pdf`` draws."""
    d = spec.service_date.isoformat()
    base: Dict[str, Any] = {"document_type": spec.doc_type, "readability": "readable", "language": "en",
                            "provider_name": _ev(spec.provider_name), "service_category_hint": spec.category,
                            "provider_country": _ev("Saudi Arabia")}
    if spec.doc_type == "invoice":
        total = f"{spec.total:.2f}" if spec.total is not None else None
        base.update({
            "invoice_number": _ev(spec.invoice_number, f"Invoice No. {spec.invoice_number}"),
            "invoice_date": _ev(d, f"Invoice date {d}"), "service_date": _ev(d, f"Service date {d}"),
            "patient_name": _ev(spec.member_name, f"Patient {spec.member_name}"),
            "patient_identifier": _ev(spec.member_identity, f"Member ID {spec.member_identity}") if spec.member_identity else {},
            "currency": _ev("SAR"), "total": _ev(total, f"TOTAL (SAR) {total}") if total else {},
            "line_items": [{"description": CATEGORY_LINES.get(spec.category, "Medical service"),
                            "category": "consultation", "amount": total}],
        })
    elif spec.doc_type == "payment_receipt":
        paid = f"{spec.paid_amount:.2f}" if spec.paid_amount is not None else None
        base.update({
            "paid_amount": _ev(paid, f"Amount paid (SAR) {paid}") if paid else {}, "currency": _ev("SAR"),
            "payment_date": _ev(d), "payment_method": _ev("Card (fictional)"),
            "transaction_reference": _ev(spec.receipt_number),
            "referenced_invoice_number": _ev(spec.invoice_number, f"For invoice No. {spec.invoice_number}"),
        })
    else:
        base.update({
            "patient_name": _ev(spec.member_name),
            "patient_identifier": _ev(spec.member_identity, f"Member ID {spec.member_identity}") if spec.member_identity else {},
            "service_date": _ev(d, f"Visit date {d}"),
            "diagnosis": _ev("Fictional diagnosis for testing only"),
        })
    return DocumentExtraction.model_validate(base)


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    return Decimal(str(v)).quantize(Decimal("0.01"))


def spec_from_rows(doc: Dict[str, Any], invoice: Dict[str, Any], member: Dict[str, Any],
                   claim: Dict[str, Any], doc_type: Optional[str] = None) -> DocSpec:
    """Rebuild a document spec from workbook rows (used by the generator and the importer)."""
    total = _dec(invoice.get("total_sar"))
    sdate = invoice["service_date"]
    sdate = sdate.date() if hasattr(sdate, "hour") else sdate
    return DocSpec(
        doc_type=doc_type or doc["document_type"], claim_id=claim["claim_id"], category=claim["category"],
        member_name=member["display_name"], member_identity=invoice.get("extracted_member_identity") or None,
        provider_name=invoice["provider_name"], invoice_number=invoice["invoice_number"], service_date=sdate,
        total=total, service_code=invoice["service_code"],
        paid_amount=total if total is not None else _dec(claim.get("claimed_amount_sar")),
        receipt_number=f"RCPT-{invoice['invoice_number'].split('-', 1)[-1]}",
    )


# Corrected copies a member would upload to complete T01-T03 (demo panel only).
CORRECTIONS = {
    "invoice_total_missing": ("invoice", "invoice_corrected.pdf", {"total": True}),
    "identity_missing": ("invoice", "invoice_with_member_id.pdf", {"identity": True}),
    "payment_proof_missing": ("payment_receipt", "payment_receipt.pdf", {}),
}


def corrected_spec(scenario: str, base: DocSpec, member_identity: str, claimed: Optional[Decimal]) -> Optional[DocSpec]:
    item = CORRECTIONS.get(scenario)
    if item is None:
        return None
    doc_type, _name, opts = item
    spec = DocSpec(**{**base.__dict__, "doc_type": doc_type})
    if opts.get("total"):
        spec.total = claimed
        spec.paid_amount = claimed
    if opts.get("identity"):
        spec.member_identity = member_identity
    return spec
