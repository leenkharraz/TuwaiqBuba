"""Claim engine.

Turns the current set of documents (plus member corrections and decisions) into
one claim view: combined fields with their sources, invoice entries, linked
payments, visit grouping, document checklist status and validation issues.

It is a pure function of its inputs. Nothing is cached between calls, so a
removed or replaced document can never leave stale values behind, and member
corrections (stored separately) are re-applied on every build.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .normalize import (
    amounts_equal,
    country_to_location,
    money,
    names_compatible,
    norm_currency,
    norm_ident,
    norm_text,
    parse_amount,
    parse_iso_date,
    providers_compatible,
)
from .rules import Requirement, RuleSet
from .schemas import DocumentExtraction

MEDICAL_TYPES = ("medical_report", "prescription", "lab_result", "imaging_result")
CATEGORIES = ("outpatient", "dental", "inpatient", "physiotherapy", "optical", "medication")
LOCATIONS = ("domestic", "international")

DOC_FIELD_KINDS: Dict[str, str] = {
    "patient_name": "text",
    "patient_identifier": "ident",
    "provider_name": "text",
    "provider_country": "text",
    "invoice_number": "ident",
    "invoice_date": "date",
    "service_date": "date",
    "admission_date": "date",
    "discharge_date": "date",
    "service_description": "text",
    "subtotal": "amount",
    "tax": "amount",
    "total": "amount",
    "currency": "currency",
    "paid_amount": "amount",
    "payment_date": "date",
    "payment_method": "text",
    "transaction_reference": "ident",
    "referenced_invoice_number": "ident",
    "diagnosis": "text",
}

INVOICE_FIELDS = ("invoice_number", "invoice_date", "service_date", "provider_name", "currency",
                  "subtotal", "tax", "total", "paid_amount")
PAYMENT_FIELDS = ("paid_amount", "currency", "payment_date", "payment_method",
                  "transaction_reference", "referenced_invoice_number", "provider_name")
MEDICAL_FIELDS = ("patient_name", "patient_identifier", "provider_name", "service_date", "diagnosis")
REQUIRED_INVOICE_FIELDS = ("invoice_number", "invoice_date", "total", "currency")


# ====================================================================== inputs


@dataclass
class DocIn:
    id: str
    position: int
    filename: str
    status: str
    extraction: Optional[DocumentExtraction] = None
    type_override: Optional[str] = None
    grouping_decision: Optional[str] = None
    linked_invoice_doc_id: Optional[str] = None
    source: str = "upload"
    mime_type: str = "image/jpeg"
    page_count: Optional[int] = None
    extraction_mode: Optional[str] = None
    status_detail: Optional[str] = None


@dataclass
class CorrectionIn:
    id: int
    field_key: str
    value: Optional[str]
    original_value: Optional[str] = None
    kind: str = "correction"
    context_values: List[str] = field(default_factory=list)


@dataclass
class ClaimIn:
    id: str
    docs: List[DocIn]
    corrections: Dict[str, CorrectionIn]
    acknowledged: Set[str]
    rules: RuleSet
    today: date


# ===================================================================== outputs


@dataclass
class Source:
    doc_id: str
    doc_type: Optional[str]
    filename: str
    page: Optional[int]
    excerpt: Optional[str]

    def as_json(self) -> dict:
        return {"document_id": self.doc_id, "document_type": self.doc_type,
                "page": self.page, "excerpt": self.excerpt}


@dataclass
class Candidate:
    value: str
    sources: List[Source]


@dataclass
class Field:
    key: str
    kind: str
    value: Optional[str] = None
    typed: Any = None
    status: str = "missing"  # extracted | needs_review | missing | corrected | confirmed
    sources: List[Source] = field(default_factory=list)
    original_value: Optional[str] = None
    candidates: List[Candidate] = field(default_factory=list)
    note: Optional[str] = None
    extraction_note: Optional[str] = None
    correction_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status in ("extracted", "corrected", "confirmed") and self.value is not None

    @property
    def member_set(self) -> bool:
        return self.status in ("corrected", "confirmed")


@dataclass
class Action:
    kind: str
    target: Optional[str] = None
    doc_type: Optional[str] = None
    value: Optional[str] = None


@dataclass
class Issue:
    key: str
    code: str
    severity: str  # blocking | warning | info
    params: Dict[str, Any] = field(default_factory=dict)
    actions: List[Action] = field(default_factory=list)
    acknowledgeable: bool = False
    acknowledged: bool = False
    doc_id: Optional[str] = None
    field_key: Optional[str] = None

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking" and not self.acknowledged


@dataclass
class LineView:
    description: Optional[str]
    category: Optional[str]
    tooth_number: Optional[str]
    quantity: Optional[str]
    amount: Optional[Decimal]
    amount_raw: Optional[str]
    source: Source


@dataclass
class PaymentView:
    doc_id: str
    fields: Dict[str, Field]
    linked_invoice_id: Optional[str] = None
    link_basis: Optional[str] = None
    duplicate_of: Optional[str] = None
    group: Optional[str] = None


@dataclass
class InvoiceView:
    doc_id: str
    label: str
    fields: Dict[str, Field]
    lines: List[LineView]
    category_hint: Optional[str]
    payments: List[PaymentView] = field(default_factory=list)
    embedded_payment: bool = False
    paid_total: Optional[Decimal] = None
    payment_status: str = "no_proof"
    duplicate_of: Optional[str] = None
    group: str = "primary"
    group_reasons: List[str] = field(default_factory=list)
    member_confirmed_group: bool = False


@dataclass
class MedicalView:
    doc_id: str
    doc_type: str
    fields: Dict[str, Field]
    group: str = "primary"
    patient_mismatch: bool = False


@dataclass
class DocView:
    doc_id: str
    doc_type: Optional[str]
    type_overridden: bool
    status: str
    role: Optional[str] = None  # invoice | payment | medical | other
    group: Optional[str] = None  # primary | separate | pending | excluded
    group_reasons: List[str] = field(default_factory=list)
    fields: List[Field] = field(default_factory=list)
    lines: List[LineView] = field(default_factory=list)
    duplicate_of: Optional[str] = None
    linked_invoice_label: Optional[str] = None
    link_basis: Optional[str] = None
    readability: Optional[str] = None
    readability_note: Optional[str] = None


@dataclass
class RequirementView:
    id: str
    label: Dict[str, str]
    when: str
    status: str  # satisfied | missing | not_triggered | optional | reminder | acknowledged
    satisfied_by: List[str] = field(default_factory=list)
    trigger: Optional[str] = None


@dataclass
class GroupProposal:
    key: str
    kind: str  # separate | pending
    doc_ids: List[str]
    invoice_ids: List[str]
    reasons: List[str]
    category: Optional[str]
    visit_date: Optional[date]
    provider: Optional[str]


@dataclass
class ClaimView:
    claim_fields: Dict[str, Field]
    invoices: List[InvoiceView]
    all_invoices: Dict[str, InvoiceView]
    payments: List[PaymentView]
    medical: List[MedicalView]
    docs: Dict[str, DocView]
    proposals: List[GroupProposal]
    issues: List[Issue]
    requirements: List[RequirementView]
    totals: "OrderedDict[str, Decimal]"
    paid_totals: "OrderedDict[str, Decimal]"
    category: Optional[str]
    location: Optional[str]
    processing: bool
    has_documents: bool
    primary_doc_ids: List[str]

    @property
    def blocking_issues(self) -> List[Issue]:
        return [i for i in self.issues if i.blocking]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if not i.blocking and i.severity in ("warning", "blocking")]

    @property
    def infos(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == "info"]

    @property
    def ready(self) -> bool:
        return (
            self.has_documents
            and not self.processing
            and bool(self.invoices)
            and not self.blocking_issues
        )


# ================================================================== helpers


def parse_kind(kind: str, raw: Optional[str]) -> Tuple[Optional[str], Any, Optional[str]]:
    """Return (display value, typed value, problem code)."""
    if raw is None or str(raw).strip() == "":
        return None, None, None
    raw = str(raw).strip()
    if kind == "amount":
        d = parse_amount(raw)
        return (money(d), d, None) if d is not None else (raw, None, "amount_unparsed")
    if kind == "date":
        d = parse_iso_date(raw)
        return (d.isoformat(), d, None) if d else (raw, None, "date_unparsed")
    if kind == "currency":
        code, problem = norm_currency(raw)
        return (code, code, None) if code else (raw, None, problem or "currency_unrecognized")
    if kind == "ident":
        return raw, norm_ident(raw), None
    if kind == "category":
        return (raw, raw, None) if raw in CATEGORIES else (raw, None, "invalid_choice")
    if kind == "location":
        return (raw, raw, None) if raw in LOCATIONS else (raw, None, "invalid_choice")
    return raw, raw, None


def apply_correction(fld: Field, c: CorrectionIn) -> None:
    if fld.value is not None and fld.original_value is None:
        fld.original_value = fld.value
    display, typed, problem = parse_kind(fld.kind, c.value)
    fld.value = display
    fld.typed = None if problem else typed
    fld.status = "confirmed" if c.kind == "confirmation" else "corrected"
    fld.note = problem
    fld.correction_id = c.id


class DocCtx:
    """A document with member corrections applied to its extracted fields."""

    def __init__(self, doc: DocIn, corrections: Dict[str, CorrectionIn]):
        self.doc = doc
        self.x = doc.extraction
        self.type = doc.type_override or (self.x.document_type if self.x else None)
        self._corr = corrections
        self._cache: Dict[str, Field] = {}

    @property
    def id(self) -> str:
        return self.doc.id

    def source(self, page: Optional[int], excerpt: Optional[str]) -> Source:
        return Source(self.doc.id, self.type, self.doc.filename, page, excerpt)

    def f(self, name: str) -> Field:
        if name in self._cache:
            return self._cache[name]
        kind = DOC_FIELD_KINDS[name]
        key = f"doc:{self.doc.id}:{name}"
        fld = Field(key=key, kind=kind)
        ev = getattr(self.x, name, None) if self.x else None
        if ev is not None and ev.value is not None:
            display, typed, problem = parse_kind(kind, ev.value)
            fld.value = display
            fld.sources = [self.source(ev.page, ev.excerpt)]
            fld.extraction_note = ev.note
            if problem:
                fld.status, fld.note, fld.typed = "needs_review", problem, None
            elif ev.ambiguous:
                fld.status, fld.note, fld.typed = "needs_review", "ambiguous", None
            else:
                fld.status, fld.typed = "extracted", typed
        c = self._corr.get(key)
        if c is not None:
            apply_correction(fld, c)
        self._cache[name] = fld
        return fld

    def typed(self, name: str) -> Any:
        fld = self.f(name)
        return fld.typed if fld.ok else None

    def value(self, name: str) -> Optional[str]:
        fld = self.f(name)
        return fld.value if fld.ok else None

    @property
    def category_hint(self) -> Optional[str]:
        h = self.x.service_category_hint if self.x else None
        return h if h in CATEGORIES else None

    def lines(self) -> List[LineView]:
        out = []
        for li in (self.x.line_items if self.x else []):
            out.append(LineView(
                description=li.description,
                category=li.category,
                tooth_number=li.tooth_number,
                quantity=li.quantity,
                amount=parse_amount(li.amount),
                amount_raw=li.amount,
                source=self.source(li.page, li.excerpt or li.description),
            ))
        return out

    @property
    def label(self) -> str:
        return self.value("invoice_number") or self.doc.filename


def _visit(ctx: DocCtx) -> dict:
    return {
        "date": ctx.typed("service_date") or ctx.typed("invoice_date"),
        "admission": ctx.typed("admission_date"),
        "discharge": ctx.typed("discharge_date"),
        "category": ctx.category_hint,
        "patient_name": ctx.value("patient_name"),
        "patient_id": ctx.typed("patient_identifier"),
        "provider": ctx.value("provider_name"),
    }


def compare_visit(a: dict, b: dict, claim_category: Optional[str] = None) -> Tuple[str, List[str]]:
    """Decide whether invoice b belongs to the same visit/admission as anchor a."""
    sep: List[str] = []
    amb: List[str] = []
    cat_a = claim_category or a["category"]
    if cat_a and b["category"] and cat_a != b["category"]:
        sep.append("different_service_type")
    pid_a, pid_b = a["patient_id"], b["patient_id"]
    same_provider = providers_compatible(a["provider"], b["provider"]) is True
    # File numbers are only comparable within the same provider.
    if pid_a and pid_b and pid_a != pid_b and same_provider:
        sep.append("different_patient")
    elif not (pid_a and pid_b and pid_a == pid_b):
        if names_compatible(a["patient_name"], b["patient_name"]) is False:
            sep.append("different_patient")
    if a["date"] and b["date"]:
        inpatient = "inpatient" in (cat_a, b["category"])
        if inpatient:
            lo = a["admission"] or a["date"]
            hi = a["discharge"] or a["date"]
            if not (lo <= b["date"] <= hi):
                sep.append("different_admission")
        elif a["date"] != b["date"]:
            sep.append("different_visit_date")
    else:
        amb.append("visit_date_missing")
    if providers_compatible(a["provider"], b["provider"]) is False:
        (sep if sep else amb).append("different_provider")
    if sep:
        return "separate", sep
    if amb:
        return "ambiguous", amb
    return "same", []


def _aggregate(
    key: str,
    kind: str,
    cands: List[Tuple[str, Source]],
    compat: Callable[[str, str], Optional[bool]],
    corrections: Dict[str, CorrectionIn],
    accept_unknown: bool = False,
) -> Field:
    """Combine one field across documents, keeping every disagreement."""
    clusters: List[Candidate] = []
    for val, src in cands:
        for cl in clusters:
            if compat(cl.value, val):
                cl.sources.append(src)
                break
        else:
            clusters.append(Candidate(val, [src]))
    fld = Field(key=key, kind=kind, candidates=clusters)
    if clusters:
        fld.value = clusters[0].value
        fld.typed = parse_kind(kind, fld.value)[1]
        fld.sources = [s for cl in clusters for s in cl.sources]
        if len(clusters) == 1:
            fld.status = "extracted"
        else:
            unknown_only = all(
                compat(x.value, y.value) is None
                for i, x in enumerate(clusters) for y in clusters[i + 1:]
            )
            if unknown_only and accept_unknown:
                fld.status, fld.note = "extracted", "matched_by_identifier"
            else:
                fld.status = "needs_review"
                fld.note = "different_scripts" if unknown_only else "conflict"
    c = corrections.get(key)
    if c is not None:
        if len(clusters) > 1:
            fld.original_value = " | ".join(cl.value for cl in clusters)
        apply_correction(fld, c)
        known = set(c.context_values)
        fresh = [cl for cl in clusters
                 if norm_text(cl.value) not in known and not compat(cl.value, fld.value or "")]
        if fresh:
            fld.status, fld.note = "needs_review", "new_conflict"
    return fld


def _eq(a: str, b: str) -> Optional[bool]:
    return a == b


def _ident_eq(a: str, b: str) -> Optional[bool]:
    return norm_ident(a) == norm_ident(b)


def _fingerprint(*vals: Any) -> str:
    return ":".join("" if v is None else str(v) for v in vals)


# ===================================================================== build


def build_claim_view(ci: ClaimIn) -> ClaimView:  # noqa: C901 - deliberate single pass
    corr = ci.corrections
    issues: List[Issue] = []
    docs: Dict[str, DocView] = OrderedDict()
    processing = False

    ordered = sorted(ci.docs, key=lambda d: d.position)
    ctxs: Dict[str, DocCtx] = {}

    def add(issue: Issue) -> None:
        issue.acknowledged = issue.acknowledgeable and issue.key in ci.acknowledged
        issues.append(issue)

    # ---- 1. document processing state ------------------------------------
    for d in ordered:
        ctx = DocCtx(d, corr)
        dv = DocView(doc_id=d.id, doc_type=ctx.type, type_overridden=bool(d.type_override), status=d.status)
        docs[d.id] = dv
        if d.status in ("queued", "processing"):
            processing = True
        elif d.status == "unreadable":
            dv.readability = "unreadable"
            dv.readability_note = d.extraction.readability_note if d.extraction else None
            add(Issue(f"unreadable:{d.id}", "document_unreadable", "blocking",
                      {"filename": d.filename},
                      [Action("retake_photo", d.id), Action("replace_document", d.id),
                       Action("remove_document", d.id)], doc_id=d.id))
        elif d.status == "failed":
            add(Issue(f"failed:{d.id}", "extraction_failed", "blocking",
                      {"filename": d.filename, "detail": d.status_detail or "unknown"},
                      [Action("retry_extraction", d.id), Action("replace_document", d.id),
                       Action("remove_document", d.id)], doc_id=d.id))
        elif d.status == "unavailable":
            add(Issue(f"unavailable:{d.id}", "extraction_unavailable", "blocking",
                      {"filename": d.filename},
                      [Action("retry_extraction", d.id), Action("remove_document", d.id)], doc_id=d.id))
        elif d.status == "extracted" and d.extraction is not None:
            ctxs[d.id] = ctx
            dv.readability = d.extraction.readability
            dv.readability_note = d.extraction.readability_note
            if ctx.type == "other":
                add(Issue(f"type_unknown:{d.id}", "document_type_unknown", "warning",
                          {"filename": d.filename}, [Action("set_document_type", d.id)], doc_id=d.id))
            if d.extraction.readability == "partially_readable":
                add(Issue(f"partial:{d.id}", "document_partially_readable", "warning",
                          {"filename": d.filename},
                          [Action("review_document", d.id), Action("retake_photo", d.id)], doc_id=d.id))

    usable = [ctxs[d.id] for d in ordered if d.id in ctxs]
    inv_ctx = [c for c in usable if c.type == "invoice"]
    pay_ctx = [c for c in usable if c.type == "payment_receipt"]
    med_ctx = [c for c in usable if c.type in MEDICAL_TYPES]

    # ---- 2. invoices, duplicate detection ------------------------------
    invoices: Dict[str, InvoiceView] = OrderedDict()
    for c in inv_ctx:
        invoices[c.id] = InvoiceView(
            doc_id=c.id, label=c.label,
            fields={n: c.f(n) for n in INVOICE_FIELDS},
            lines=c.lines(), category_hint=c.category_hint,
        )
    seen: List[DocCtx] = []
    for c in inv_ctx:
        num = c.typed("invoice_number")
        if num:
            for prev in seen:
                if prev.typed("invoice_number") != num:
                    continue
                if providers_compatible(prev.value("provider_name"), c.value("provider_name")) is False:
                    continue
                t_prev, t_cur = prev.typed("total"), c.typed("total")
                if t_prev is not None and t_cur is not None and not amounts_equal(t_prev, t_cur):
                    add(Issue(f"dup_conflict:{c.id}", "duplicate_invoice_conflict", "blocking",
                              {"invoice": c.label, "a": money(t_prev), "b": money(t_cur)},
                              [Action("review_field", f"doc:{c.id}:total"), Action("remove_document", c.id)],
                              doc_id=c.id))
                else:
                    invoices[c.id].duplicate_of = prev.id
                    add(Issue(f"dup_invoice:{c.id}", "duplicate_invoice", "warning",
                              {"invoice": c.label},
                              [Action("remove_document", c.id)], doc_id=c.id))
                break
        seen.append(c)
    nondup = [c for c in inv_ctx if not invoices[c.id].duplicate_of]

    # ---- 3. visit grouping -------------------------------------------
    claim_cat_corr = corr.get("claim:service_category")
    claim_cat_hint = claim_cat_corr.value if claim_cat_corr and claim_cat_corr.value in CATEGORIES else None
    groups: List[dict] = []  # {"key","kind","anchor","invoices":[ids],"reasons"}
    # The visit anchor is the first uploaded invoice with a known visit date that the member
    # has not merely attached to another visit; it falls back to the first invoice.
    anchor_first = next((c for c in nondup if c.doc.grouping_decision != "same_visit" and _visit(c)["date"]),
                        nondup[0] if nondup else None)
    if anchor_first is not None:
        nondup = [anchor_first] + [c for c in nondup if c is not anchor_first]
    for c in nondup:
        if not groups:
            groups.append({"key": "primary", "kind": "primary", "anchor": _visit(c),
                           "invoices": [c.id], "reasons": []})
            continue
        primary = groups[0]
        if c.doc.grouping_decision == "same_visit":
            primary["invoices"].append(c.id)
            invoices[c.id].member_confirmed_group = True
            continue
        verdict, reasons = compare_visit(primary["anchor"], _visit(c), claim_cat_hint)
        if verdict == "same":
            primary["invoices"].append(c.id)
            continue
        if verdict == "ambiguous":
            groups.append({"key": f"pending:{c.id}", "kind": "pending", "anchor": _visit(c),
                           "invoices": [c.id], "reasons": reasons})
            continue
        for g in groups[1:]:
            if g["kind"] == "separate" and compare_visit(g["anchor"], _visit(c))[0] == "same":
                g["invoices"].append(c.id)
                break
        else:
            groups.append({"key": f"separate:{c.id}", "kind": "separate", "anchor": _visit(c),
                           "invoices": [c.id], "reasons": reasons})
    group_of: Dict[str, str] = {}
    for g in groups:
        for inv_id in g["invoices"]:
            group_of[inv_id] = g["key"]
            invoices[inv_id].group = g["kind"]
            invoices[inv_id].group_reasons = list(g["reasons"])
    for inv in invoices.values():
        if inv.duplicate_of:
            group_of[inv.doc_id] = group_of.get(inv.duplicate_of, "primary")
            inv.group = invoices[inv.duplicate_of].group
    primary_invoice_ids = [i for i in (groups[0]["invoices"] if groups else [])]

    # ---- 4. payments: duplicates + link to invoices -------------------
    payments: Dict[str, PaymentView] = OrderedDict()
    seen_refs: Dict[str, str] = {}
    inv_by_num: Dict[str, str] = {}
    for c in nondup:
        num = c.typed("invoice_number")
        if num and num not in inv_by_num:
            inv_by_num[num] = c.id
    for p in pay_ctx:
        pv = PaymentView(doc_id=p.id, fields={n: p.f(n) for n in PAYMENT_FIELDS})
        payments[p.id] = pv
        ref = p.typed("transaction_reference")
        if ref and ref in seen_refs:
            pv.duplicate_of = seen_refs[ref]
            add(Issue(f"dup_payment:{p.id}", "duplicate_payment", "warning",
                      {"filename": p.doc.filename}, [Action("remove_document", p.id)], doc_id=p.id))
            continue
        if ref:
            seen_refs[ref] = p.id
        member_link = p.doc.linked_invoice_doc_id
        if member_link == "none":
            pv.link_basis = "member_unrelated"
        elif member_link and member_link in invoices and not invoices[member_link].duplicate_of:
            pv.linked_invoice_id, pv.link_basis = member_link, "member_confirmed"
        else:
            ref_inv = p.typed("referenced_invoice_number")
            amt, cur = p.typed("paid_amount"), p.typed("currency")
            prov = p.value("provider_name")
            if ref_inv and ref_inv in inv_by_num:
                pv.linked_invoice_id, pv.link_basis = inv_by_num[ref_inv], "invoice_number"
            else:
                matches = [
                    c for c in nondup
                    if amt is not None and amounts_equal(c.typed("total"), amt)
                    and (cur is None or c.typed("currency") in (None, cur))
                    and providers_compatible(prov, c.value("provider_name")) is not False
                ]
                if len(matches) == 1:
                    pv.linked_invoice_id, pv.link_basis = matches[0].id, "amount_match"
                elif (len(nondup) == 1 and not ref_inv
                      and providers_compatible(prov, nondup[0].value("provider_name")) is not False):
                    pv.linked_invoice_id, pv.link_basis = nondup[0].id, "single_invoice"
        if pv.linked_invoice_id:
            invoices[pv.linked_invoice_id].payments.append(pv)
            pv.group = invoices[pv.linked_invoice_id].group
        elif pv.link_basis == "member_unrelated":
            pv.group = "excluded"
            add(Issue(f"payment_unrelated:{p.id}", "payment_unrelated", "warning",
                      {"filename": p.doc.filename}, [Action("remove_document", p.id)], doc_id=p.id))
        else:
            pv.group = "primary"
            if nondup:
                add(Issue(f"link_payment:{p.id}", "payment_link_needed", "blocking",
                          {"filename": p.doc.filename}, [Action("link_payment", p.id)], doc_id=p.id))

    # ---- 5. claim patient (from primary invoices) + medical docs --------
    def _patient_of(ids: List[str]) -> Tuple[List[str], List[str]]:
        names, pids = [], []
        for i in ids:
            c = ctxs[i]
            if c.value("patient_name"):
                names.append(c.value("patient_name"))
            if c.typed("patient_identifier"):
                pids.append(c.typed("patient_identifier"))
        return names, pids

    inv_names, _ = _patient_of(primary_invoice_ids)
    medical: Dict[str, MedicalView] = OrderedDict()
    for m in med_ctx:
        mv = MedicalView(doc_id=m.id, doc_type=m.type, fields={n: m.f(n) for n in MEDICAL_FIELDS})
        medical[m.id] = mv
        m_pid, m_name = m.typed("patient_identifier"), m.value("patient_name")
        # identifiers are only compared with invoices from the same provider
        same_prov_pids = [
            ctxs[i].typed("patient_identifier") for i in primary_invoice_ids
            if ctxs[i].typed("patient_identifier")
            and providers_compatible(ctxs[i].value("provider_name"), m.value("provider_name")) is True
        ]
        mismatch = False
        if m.doc.grouping_decision != "same_visit":
            if m_pid and same_prov_pids and m_pid not in same_prov_pids:
                mismatch = True
            elif not (m_pid and m_pid in same_prov_pids) and m_name and inv_names:
                if all(names_compatible(m_name, n) is False for n in inv_names):
                    mismatch = True
        if mismatch:
            mv.group, mv.patient_mismatch = "excluded", True
            add(Issue(f"patient_mismatch:{m.id}", "document_patient_mismatch", "blocking",
                      {"filename": m.doc.filename, "name": m_name or m.doc.filename},
                      [Action("remove_document", m.id), Action("confirm_same_patient", m.id)], doc_id=m.id))
            continue
        # attach to the visit whose date matches; default to the primary visit
        m_date = m.typed("service_date")
        target = "primary"
        if m_date and len(groups) > 1 and m.doc.grouping_decision != "same_visit":
            primary_date = groups[0]["anchor"]["date"]
            if primary_date != m_date:
                for g in groups[1:]:
                    if g["anchor"]["date"] == m_date:
                        target = g["kind"]
                        group_of[m.id] = g["key"]
                        break
        mv.group = target
        if target == "primary":
            group_of[m.id] = "primary"

    # ---- 6. group proposals (separate / pending) -------------------
    proposals: List[GroupProposal] = []
    for g in groups[1:]:
        member_ids = [k for k, v in group_of.items() if v == g["key"]]
        for inv_id in g["invoices"]:
            for pv in invoices[inv_id].payments:
                member_ids.append(pv.doc_id)
        member_ids = [d.id for d in ordered if d.id in set(member_ids)]
        prop = GroupProposal(key=g["key"], kind=g["kind"], doc_ids=member_ids, invoice_ids=list(g["invoices"]),
                             reasons=list(g["reasons"]), category=g["anchor"]["category"],
                             visit_date=g["anchor"]["date"], provider=g["anchor"]["provider"])
        proposals.append(prop)
        first = g["invoices"][0]
        if g["kind"] == "separate":
            add(Issue(f"separate:{first}", "separate_claim_proposed", "blocking",
                      {"invoice": invoices[first].label, "reasons": prop.reasons},
                      [Action("split_claim", first), Action("confirm_same_visit", first)], doc_id=first))
        else:
            add(Issue(f"pending:{first}", "grouping_unclear", "blocking",
                      {"invoice": invoices[first].label, "reasons": prop.reasons},
                      [Action("confirm_same_visit", first), Action("split_claim", first)], doc_id=first))

    primary_ids: List[str] = []
    for d in ordered:
        if d.id not in ctxs:
            continue
        c = ctxs[d.id]
        if c.type == "invoice":
            if group_of.get(d.id) == "primary":
                primary_ids.append(d.id)
        elif c.type == "payment_receipt":
            if payments[d.id].group == "primary":
                primary_ids.append(d.id)
        elif c.type in MEDICAL_TYPES:
            if medical[d.id].group == "primary":
                primary_ids.append(d.id)
        else:
            primary_ids.append(d.id)

    p_inv = [ctxs[i] for i in primary_ids if ctxs[i].type == "invoice" and not invoices[i].duplicate_of]
    p_med = [ctxs[i] for i in primary_ids if ctxs[i].type in MEDICAL_TYPES]
    p_pay = [ctxs[i] for i in primary_ids if ctxs[i].type == "payment_receipt"]

    # ---- 7. claim-level fields -----------------------------------------
    def cands(ctx_list: List[DocCtx], name: str) -> List[Tuple[str, Source]]:
        out = []
        for c in ctx_list:
            fld = c.f(name)
            if fld.ok:
                out.append((fld.value, fld.sources[0] if fld.sources else c.source(None, None)))
        return out

    pid_values = {c.typed("patient_identifier") for c in p_inv + p_med + p_pay if c.typed("patient_identifier")}
    ids_agree = len(pid_values) == 1 and sum(
        1 for c in p_inv + p_med + p_pay if c.typed("patient_identifier")) >= 2

    claim_fields: Dict[str, Field] = OrderedDict()
    claim_fields["patient_name"] = _aggregate(
        "claim:patient_name", "text", cands(p_inv + p_med + p_pay, "patient_name"),
        names_compatible, corr, accept_unknown=ids_agree)
    claim_fields["patient_identifier"] = _aggregate(
        "claim:patient_identifier", "ident", cands(p_inv + p_med + p_pay, "patient_identifier"), _ident_eq, corr)
    claim_fields["provider_name"] = _aggregate(
        "claim:provider_name", "text", cands(p_inv, "provider_name"),
        lambda a, b: providers_compatible(a, b), corr)

    # service category: from invoices, falling back to medical documents
    cat_cands = [(c.category_hint, c.source(None, c.x.service_category_excerpt)) for c in p_inv if c.category_hint]
    if not cat_cands:
        cat_cands = [(c.category_hint, c.source(None, c.x.service_category_excerpt)) for c in p_med if c.category_hint]
    claim_fields["service_category"] = _aggregate("claim:service_category", "category", cat_cands, _eq, corr)

    loc_cands = []
    for c in p_inv:
        fld = c.f("provider_country")
        if fld.ok:
            loc = country_to_location(fld.value)
            if loc:
                loc_cands.append((loc, fld.sources[0]))
    claim_fields["claim_location"] = _aggregate("claim:claim_location", "location", loc_cands, _eq, corr)

    claim_fields["service_date"] = _aggregate(
        "claim:service_date", "date", cands(p_inv, "service_date"), _eq, corr)

    diag = cands(p_med + p_inv, "diagnosis")
    dfield = Field(key="claim:diagnosis", kind="text")
    if diag:
        uniq: List[Candidate] = []
        for val, src in diag:
            for u in uniq:
                if norm_text(u.value) == norm_text(val):
                    u.sources.append(src)
                    break
            else:
                uniq.append(Candidate(val, [src]))
        dfield.candidates = uniq
        dfield.value = "; ".join(u.value for u in uniq)
        dfield.sources = [s for u in uniq for s in u.sources]
        dfield.status = "extracted"
    if "claim:diagnosis" in corr:
        apply_correction(dfield, corr["claim:diagnosis"])
    claim_fields["diagnosis"] = dfield

    category = claim_fields["service_category"].typed if claim_fields["service_category"].ok else None
    location = claim_fields["claim_location"].typed if claim_fields["claim_location"].ok else None

    # ---- 8. claim-level issues -------------------------------------------
    if p_inv or p_med:
        pn = claim_fields["patient_name"]
        if pn.status == "missing":
            add(Issue("missing:patient_name", "patient_name_missing", "blocking", {},
                      [Action("review_field", pn.key)], field_key=pn.key))
        elif pn.status == "needs_review":
            add(Issue(f"review:patient_name:{pn.note}", f"patient_name_{pn.note}", "blocking",
                      {"values": [cl.value for cl in pn.candidates]},
                      [Action("resolve_mismatch", pn.key)], field_key=pn.key))
        pi = claim_fields["patient_identifier"]
        if pi.status == "needs_review":
            # Different providers use different file numbers, so this is a note, not a blocker.
            add(Issue("review:patient_identifier", "patient_identifier_conflict", "warning",
                      {"values": [cl.value for cl in pi.candidates]},
                      [Action("resolve_mismatch", pi.key)], field_key=pi.key))
    if p_inv:
        pv_ = claim_fields["provider_name"]
        if pv_.status == "missing":
            add(Issue("missing:provider_name", "provider_missing", "blocking", {},
                      [Action("review_field", pv_.key)], field_key=pv_.key))
        elif pv_.status == "needs_review":
            add(Issue(f"review:provider_name:{pv_.note}", "provider_conflict", "blocking",
                      {"values": [cl.value for cl in pv_.candidates]},
                      [Action("resolve_mismatch", pv_.key)], field_key=pv_.key))
        sc = claim_fields["service_category"]
        if sc.status == "missing":
            add(Issue("missing:service_category", "category_needed", "blocking", {},
                      [Action("choose_category", sc.key)], field_key=sc.key))
        elif sc.status == "needs_review":
            add(Issue("review:service_category", "category_conflict", "blocking", {},
                      [Action("choose_category", sc.key)], field_key=sc.key))
        lc = claim_fields["claim_location"]
        if lc.status in ("missing", "needs_review"):
            add(Issue("missing:claim_location", "location_needed", "blocking", {},
                      [Action("choose_location", lc.key)], field_key=lc.key))
        sd = claim_fields["service_date"]
        if sd.status == "missing":
            inv_date = next((c.value("invoice_date") for c in p_inv if c.value("invoice_date")), None)
            add(Issue("missing:service_date", "service_date_missing", "blocking", {"invoice_date": inv_date},
                      [Action("use_invoice_date", sd.key, value=inv_date) if inv_date else Action("review_field", sd.key),
                       Action("review_field", sd.key)], field_key=sd.key))
        elif sd.status == "needs_review":
            add(Issue("review:service_date", "service_date_conflict", "blocking",
                      {"values": [cl.value for cl in sd.candidates]},
                      [Action("resolve_mismatch", sd.key)], field_key=sd.key))
        if sd.ok and isinstance(sd.typed, date):
            if sd.typed > ci.today:
                add(Issue("future:service_date", "date_in_future", "blocking", {"date": sd.value},
                          [Action("review_field", sd.key)], field_key=sd.key))
            elif ci.rules.service_window_days and (ci.today - sd.typed).days > ci.rules.service_window_days:
                add(Issue("window:service_date", "service_window", "warning",
                          {"days": (ci.today - sd.typed).days, "limit": ci.rules.service_window_days}, []))
        if location == "international":
            for req in ci.rules.location_requirements.get("international", []):
                add(Issue(f"reminder:{req.id}", "location_reminder", "info", {"label": req.label}, []))
    elif pay_ctx or med_ctx:
        add(Issue("missing:invoice", "invoice_missing", "blocking", {},
                  [Action("add_document", doc_type="invoice")]))

    # ---- 9. per-invoice checks -----------------------------------------
    cat_rules = ci.rules.category(category)
    field_checks = cat_rules.field_checks if cat_rules else ["itemized_lines"]
    for c in p_inv:
        inv = invoices[c.id]
        label = inv.label
        for name in REQUIRED_INVOICE_FIELDS:
            fld = c.f(name)
            if fld.status == "missing":
                add(Issue(f"missing:{c.id}:{name}", "invoice_field_missing", "blocking",
                          {"invoice": label, "field": name}, [Action("review_field", fld.key)],
                          doc_id=c.id, field_key=fld.key))
            elif fld.status == "needs_review":
                add(Issue(f"review:{c.id}:{name}", "invoice_field_review", "blocking",
                          {"invoice": label, "field": name, "note": fld.note or "", "raw": fld.value},
                          [Action("review_field", fld.key)], doc_id=c.id, field_key=fld.key))
        for name in ("service_date", "subtotal", "tax"):
            fld = c.f(name)
            if fld.status == "needs_review":
                add(Issue(f"review:{c.id}:{name}", "invoice_field_review", "blocking",
                          {"invoice": label, "field": name, "note": fld.note or "", "raw": fld.value},
                          [Action("review_field", fld.key)], doc_id=c.id, field_key=fld.key))
        for name in ("invoice_date", "service_date"):
            dt = c.typed(name)
            if dt and dt > ci.today:
                add(Issue(f"future:{c.id}:{name}", "date_in_future", "blocking", {"date": c.value(name)},
                          [Action("review_field", f"doc:{c.id}:{name}")], doc_id=c.id))

        total, sub, tax = c.typed("total"), c.typed("subtotal"), c.typed("tax")
        if total is not None and sub is not None and tax is not None and not amounts_equal(sub + tax, total):
            add(Issue(f"arith:{c.id}:{_fingerprint(sub, tax, total)}", "invoice_arithmetic", "blocking",
                      {"invoice": label, "subtotal": money(sub), "tax": money(tax), "total": money(total)},
                      [Action("review_field", f"doc:{c.id}:total"), Action("review_field", f"doc:{c.id}:subtotal")],
                      acknowledgeable=True, doc_id=c.id))
        line_amounts = [li.amount for li in inv.lines if li.amount is not None]
        if not inv.lines and "itemized_lines" in field_checks:
            add(Issue(f"not_itemized:{c.id}", "invoice_not_itemized", "warning", {"invoice": label},
                      [Action("add_document", doc_type="invoice")], doc_id=c.id))
        elif line_amounts and len(line_amounts) == len(inv.lines):
            base = sub if sub is not None else (total if tax is None else None)
            line_sum = sum(line_amounts, Decimal("0"))
            if base is not None and not amounts_equal(line_sum, base):
                add(Issue(f"lines_sum:{c.id}", "line_items_sum", "warning",
                          {"invoice": label, "lines": money(line_sum), "expected": money(base)},
                          [Action("review_document", c.id)], doc_id=c.id))
        if "tooth_numbers" in field_checks and category == "dental" and inv.lines:
            if not any(li.tooth_number for li in inv.lines):
                add(Issue(f"tooth:{c.id}", "dental_tooth_numbers", "warning", {"invoice": label},
                          [Action("add_document", doc_type="invoice")], doc_id=c.id))

        # payment evidence
        cur = c.typed("currency")
        linked = [pv for pv in inv.payments if not pv.duplicate_of]
        embedded = c.typed("paid_amount")
        inv.embedded_payment = embedded is not None
        if not linked and embedded is None:
            inv.payment_status = "no_proof"
            add(Issue(f"missing_pop:{c.id}", "missing_proof_of_payment", "blocking", {"invoice": label},
                      [Action("add_document", doc_type="payment_receipt")], doc_id=c.id))
            continue
        paid_sum: Optional[Decimal] = Decimal("0")
        currency_differs = False
        for pv in linked:
            pc = ctxs[pv.doc_id]
            amt = pc.typed("paid_amount")
            pcur = pc.typed("currency")
            if amt is None:
                paid_sum = None
                fld = pc.f("paid_amount")
                add(Issue(f"pay_amount:{pc.id}", "payment_amount_missing", "blocking",
                          {"filename": pc.doc.filename}, [Action("review_field", fld.key)],
                          doc_id=pc.id, field_key=fld.key))
                continue
            if pcur and cur and pcur != cur:
                currency_differs = True
                continue
            if paid_sum is not None:
                paid_sum += amt
        if not linked:
            paid_sum = embedded
        inv.paid_total = paid_sum
        if currency_differs:
            inv.payment_status = "currency_differs"
            add(Issue(f"pay_currency:{c.id}", "payment_currency_differs", "warning", {"invoice": label}, []))
        elif paid_sum is not None and total is not None:
            if amounts_equal(paid_sum, total):
                inv.payment_status = "paid_on_invoice" if not linked else "proof_attached"
            else:
                inv.payment_status = "amount_mismatch"
                first_pay = linked[0].doc_id if linked else c.id
                pay_field = f"doc:{first_pay}:paid_amount"
                add(Issue(f"pay_mismatch:{c.id}:{_fingerprint(money(paid_sum), money(total))}",
                          "payment_amount_mismatch", "blocking",
                          {"invoice": label, "paid": money(paid_sum), "total": money(total), "currency": cur or ""},
                          [Action("resolve_mismatch", pay_field), Action("review_field", f"doc:{c.id}:total")],
                          acknowledgeable=True, doc_id=c.id))
        else:
            inv.payment_status = "proof_attached"

    # ---- 10. requirements checklist -----------------------------------
    requirements: List[RequirementView] = []
    if p_inv or p_med:
        p_all = p_inv + p_med + p_pay
        line_cats = {li.category for c in p_inv for li in invoices[c.id].lines if li.category}
        flags = set()
        for c in p_med + p_inv:
            if c.x and c.x.medication_prescribed:
                flags.add("medication_prescribed")
            if c.x and c.x.implant_or_device_mentioned:
                flags.add("implant_or_device_mentioned")
        trigger_lines = {}
        for c in p_inv:
            for li in invoices[c.id].lines:
                if li.category and li.category not in trigger_lines:
                    trigger_lines[li.category] = li.description
        for req in ci.rules.requirements_for(category):
            requirements.append(_evaluate_requirement(req, p_all, invoices, line_cats, flags, trigger_lines, add))

    # ---- 11. totals per currency (never mixed) ----------------------------
    totals: "OrderedDict[str, Decimal]" = OrderedDict()
    paid_totals: "OrderedDict[str, Decimal]" = OrderedDict()
    for c in p_inv:
        t, cur = c.typed("total"), c.typed("currency")
        if t is not None and cur:
            totals[cur] = totals.get(cur, Decimal("0")) + t
        inv = invoices[c.id]
        for pv in inv.payments:
            if pv.duplicate_of:
                continue
            pc = ctxs[pv.doc_id]
            amt, pcur = pc.typed("paid_amount"), pc.typed("currency") or cur
            if amt is not None and pcur:
                paid_totals[pcur] = paid_totals.get(pcur, Decimal("0")) + amt
        if not inv.payments and inv.embedded_payment and cur:
            paid_totals[cur] = paid_totals.get(cur, Decimal("0")) + c.typed("paid_amount")
    if len(totals) > 1:
        add(Issue("multi_currency", "multiple_currencies", "info", {"currencies": list(totals.keys())}, []))

    # ---- 12. document views -------------------------------------------
    for d in ordered:
        dv = docs[d.id]
        if d.id not in ctxs:
            continue
        c = ctxs[d.id]
        dv.fields = [c.f(n) for n in DOC_FIELD_KINDS if c.f(n).status != "missing"]
        if c.type == "invoice":
            dv.role = "invoice"
            dv.lines = invoices[d.id].lines
            dv.group = invoices[d.id].group
            dv.group_reasons = invoices[d.id].group_reasons
            dv.duplicate_of = invoices[d.id].duplicate_of
        elif c.type == "payment_receipt":
            dv.role = "payment"
            pv = payments[d.id]
            dv.group = pv.group
            dv.duplicate_of = pv.duplicate_of
            dv.link_basis = pv.link_basis
            if pv.linked_invoice_id:
                dv.linked_invoice_label = invoices[pv.linked_invoice_id].label
        elif c.type in MEDICAL_TYPES:
            dv.role = "medical"
            dv.group = medical[d.id].group
        else:
            dv.role = "other"
            dv.group = "primary"

    severity_order = {"blocking": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (0 if i.blocking else severity_order.get(i.severity, 3) + 1))

    return ClaimView(
        claim_fields=claim_fields,
        invoices=[invoices[c.id] for c in p_inv],
        all_invoices=invoices,
        payments=list(payments.values()),
        medical=list(medical.values()),
        docs=docs,
        proposals=proposals,
        issues=issues,
        requirements=requirements,
        totals=totals,
        paid_totals=paid_totals,
        category=category,
        location=location,
        processing=processing,
        has_documents=bool(ordered),
        primary_doc_ids=primary_ids,
    )


def _evaluate_requirement(req: Requirement, p_all: List[DocCtx], invoices: Dict[str, InvoiceView],
                          line_cats: Set[str], flags: Set[str], trigger_lines: Dict[str, Optional[str]],
                          add: Callable[[Issue], None]) -> RequirementView:
    rv = RequirementView(id=req.id, label=req.label, when=req.when, status="missing")
    matching = [c for c in p_all if c.type in req.doc_types]
    if req.requires_field:
        with_field = [c for c in matching if c.value(req.requires_field)]
    else:
        with_field = matching
    if req.satisfied_by_line_categories:
        with_field = [c for c in p_all if c.type == "invoice"
                      and any(li.category in req.satisfied_by_line_categories for li in invoices[c.id].lines)]

    if req.when == "per_invoice":
        missing = [i for i in invoices.values() if i.group == "primary" and not i.duplicate_of
                   and i.payment_status == "no_proof"]
        rv.status = "missing" if missing else "satisfied"
        rv.satisfied_by = [pv.doc_id for i in invoices.values() if i.group == "primary" for pv in i.payments]
        return rv
    if req.when == "reminder":
        rv.status = "reminder"
        return rv
    if req.when == "if_available":
        rv.status = "satisfied" if matching else "optional"
        rv.satisfied_by = [c.id for c in matching]
        if not matching:
            add(Issue(f"optional:{req.id}", "optional_document", "info", {"label": req.label},
                      [Action("add_document", doc_type=(req.doc_types or [None])[0])]))
        return rv

    triggered = True
    trigger_desc = None
    if req.when == "if_triggered":
        hit_lines = [lc for lc in req.trigger_line_categories if lc in line_cats]
        hit_flags = [f for f in req.trigger_flags if f in flags]
        triggered = bool(hit_lines or hit_flags)
        if hit_lines:
            trigger_desc = trigger_lines.get(hit_lines[0])
        elif hit_flags:
            trigger_desc = hit_flags[0]
        rv.trigger = trigger_desc
        if not triggered:
            rv.status = "satisfied" if with_field else "not_triggered"
            rv.satisfied_by = [c.id for c in with_field]
            return rv

    if with_field:
        rv.status = "satisfied"
        rv.satisfied_by = [c.id for c in with_field]
        return rv

    doc_type = (req.doc_types or [None])[0]
    if req.requires_field and matching:
        issue = Issue(f"req_field:{req.id}", "requirement_field_missing", "blocking",
                      {"label": req.label, "field": req.requires_field},
                      [Action("add_document", doc_type=doc_type),
                       Action("review_field", f"doc:{matching[0].id}:{req.requires_field}")],
                      acknowledgeable=req.when == "if_triggered")
    elif req.when == "if_triggered":
        issue = Issue(f"cond:{req.id}", "conditional_document_missing", "blocking",
                      {"label": req.label, "trigger": trigger_desc or ""},
                      [Action("add_document", doc_type=doc_type)], acknowledgeable=True)
    else:
        issue = Issue(f"req:{req.id}", "required_document_missing", "blocking", {"label": req.label},
                      [Action("add_document", doc_type=doc_type)])
    add(issue)
    rv.status = "acknowledged" if issue.acknowledged else "missing"
    return rv
