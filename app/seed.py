"""Validated, repeatable import of the fictional demo workbook.

Usage (from the project root):

    python -m app.seed import  [WORKBOOK] [--dry-run] [--upsert]
    python -m app.seed verify  [WORKBOOK]        # run the automatic checker on T01-T08
    python -m app.seed reset-demo [WORKBOOK]     # delete imported demo claims, then import again

Rules
- Only the data sheets are imported: Members, Policies, PolicyRules, Claims, Invoices,
  Documents, Checks, StatusHistory and SLA. TestCases and DataDictionary are documentation.
- Row 1 holds field names. Every sheet is validated against the workbook's DataDictionary
  (types, empty cells, allowed values, primary keys, foreign keys) before anything is written.
- The write is a single transaction. ``--dry-run`` validates and rolls back.
- Without ``--upsert`` an already-imported key is an error. With ``--upsert`` reference rows
  are updated and seeded claims are refreshed, except claims that have live activity
  (member or employee actions recorded by the app): those are left untouched.
- Each fixture:// document must exist under data/demo_fixtures with the SHA-256 recorded in
  the workbook. Run ``python scripts/build_demo_workbook.py`` to (re)generate them.
- Imports only run with DEMO_MODE enabled: the workbook holds fictional identities.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import demo_fixtures, export, lifecycle, services, storage
from .config import settings
from .db import SessionLocal, init_db
from .models import (CheckJob, CheckResult, Claim, Document, InvoiceRecord, Member, Policy, PolicyRule, SlaTarget,
                     StatusEvent, utcnow)

IMPORT_SHEETS = ("Members", "Policies", "PolicyRules", "Claims", "Invoices", "Documents", "Checks",
                 "StatusHistory", "SLA")
DOC_SHEETS = ("TestCases", "DataDictionary")
# Fields the application consumes. The workbook's DataDictionary must describe at least these.
REQUIRED = {
    "Members": ("member_id", "display_name", "member_identity"),
    "Policies": ("policy_id", "member_id", "valid_from", "valid_to", "rule_version"),
    "PolicyRules": ("rule_id", "policy_id", "category", "limit_sar", "member_share_rate", "invoice_required",
                    "payment_proof_required", "excluded_service_codes", "rule_version"),
    "Claims": ("claim_id", "member_id", "policy_id", "category", "claimed_amount_sar", "currency", "stage",
               "action_required", "decision", "approved_amount_sar", "payout_status", "paid_amount_sar", "paid_at",
               "submitted_at", "review_started_at", "updated_at", "decision_reason", "version", "scenario"),
    "Invoices": ("invoice_id", "claim_id", "provider_id", "provider_name", "invoice_number", "service_date",
                 "total_sar", "currency", "extracted_member_identity", "service_code"),
    "Documents": ("document_id", "claim_id", "invoice_id", "document_type", "file_uri", "file_name", "file_sha256",
                  "uploaded_at"),
    "Checks": ("check_id", "claim_id", "check_type", "result", "reason_code", "message", "evidence_ref",
               "related_claim_id", "rule_version", "checked_at"),
    "StatusHistory": ("event_id", "claim_id", "stage", "event_type", "actor_role", "actor_id", "occurred_at", "reason"),
    "SLA": ("category", "category_label", "initial_check_target_minutes", "employee_estimate_hours", "clock_basis",
            "estimate_label"),
    "TestCases": ("test_id", "claim_id", "expected_stage", "expected_reason_code"),
}


class ImportFailed(Exception):
    def __init__(self, errors: List[str]):
        super().__init__(f"{len(errors)} problem(s)")
        self.errors = errors


@dataclass
class FieldSpec:
    name: str
    dtype: str
    nullable: bool
    pk: bool
    fk: Optional[Tuple[str, str]]
    allowed: List[str]


@dataclass
class Parsed:
    rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    specs: Dict[str, List[FieldSpec]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ====================================================================== parsing & validation


def _to_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _coerce(v: Any, dtype: str) -> Any:
    """Convert one cell. Raises ValueError with a human-readable reason."""
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return None
    if v is None:
        return None
    dtype = dtype.lower()
    if dtype == "text":
        return _to_text(v)
    if dtype == "integer":
        if isinstance(v, bool):
            raise ValueError("expected a whole number")
        if isinstance(v, float) and not v.is_integer():
            raise ValueError("expected a whole number")
        try:
            return int(str(v)) if not isinstance(v, float) else int(v)
        except ValueError:
            raise ValueError("expected a whole number")
    if dtype in ("decimal", "decimal / number"):
        if isinstance(v, bool):
            raise ValueError("expected a number")
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001")).normalize()
        except InvalidOperation:
            raise ValueError("expected a number")
    if dtype == "boolean":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
        raise ValueError("expected TRUE or FALSE")
    if dtype == "date":
        if isinstance(v, datetime):
            if v.time() != datetime.min.time():
                raise ValueError("expected a date without a time")
            return v.date()
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v))
        except ValueError:
            raise ValueError("expected a date (YYYY-MM-DD)")
    if dtype.startswith("datetime"):
        if isinstance(v, datetime):
            dt = v
        elif isinstance(v, date):
            dt = datetime(v.year, v.month, v.day)
        else:
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("expected a date-time (YYYY-MM-DD HH:MM, UTC)")
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt  # naive values are UTC by workbook convention
    raise ValueError(f"unknown data type '{dtype}' in DataDictionary")


def _read_dictionary(wb, errors: List[str]) -> Dict[str, List[FieldSpec]]:
    if "DataDictionary" not in wb.sheetnames:
        errors.append("Workbook: missing sheet 'DataDictionary'.")
        return {}
    ws = wb["DataDictionary"]
    head = [(_to_text(c.value) or "").lower() for c in ws[1]]
    need = ("table_name", "field_name", "data_type", "nullable", "primary_key", "foreign_key", "allowed_values")
    missing = [n for n in need if n not in head]
    if missing:
        errors.append(f"DataDictionary row 1: missing column(s) {', '.join(missing)}.")
        return {}
    idx = {n: head.index(n) for n in need}
    specs: Dict[str, List[FieldSpec]] = {}
    for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if all(v is None for v in row):
            continue
        table, name = _to_text(row[idx["table_name"]]), _to_text(row[idx["field_name"]])
        if not table or not name:
            errors.append(f"DataDictionary row {r}: table_name and field_name are required.")
            continue
        fk_raw = _to_text(row[idx["foreign_key"]])
        fk = None
        if fk_raw:
            if "." not in fk_raw:
                errors.append(f"DataDictionary row {r}: foreign_key must look like Table.field.")
            else:
                fk = tuple(fk_raw.split(".", 1))  # type: ignore[assignment]
        allowed = [a.strip() for a in (_to_text(row[idx["allowed_values"]]) or "").split(",") if a.strip()]
        specs.setdefault(table, []).append(FieldSpec(
            name=name, dtype=_to_text(row[idx["data_type"]]) or "text",
            nullable=(_to_text(row[idx["nullable"]]) or "no").lower() == "yes",
            pk=(_to_text(row[idx["primary_key"]]) or "").lower() == "yes", fk=fk, allowed=allowed))
    return specs


def parse_workbook(path: Path) -> Parsed:
    from openpyxl import load_workbook

    errors: List[str] = []
    if not path.exists():
        raise ImportFailed([f"Workbook not found: {path}"])
    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # corrupt or not an xlsx
        raise ImportFailed([f"Workbook can't be opened ({type(exc).__name__})."])
    out = Parsed(specs=_read_dictionary(wb, errors))
    for sheet in IMPORT_SHEETS + ("TestCases",):
        if sheet not in wb.sheetnames:
            errors.append(f"Workbook: missing sheet '{sheet}'.")
            continue
        specs = out.specs.get(sheet)
        if not specs:
            errors.append(f"DataDictionary: no fields described for sheet '{sheet}'.")
            continue
        described = {s.name for s in specs}
        for f in REQUIRED.get(sheet, ()):
            if f not in described:
                errors.append(f"DataDictionary: field '{sheet}.{f}' is required by the application but not described.")
        rows_iter = wb[sheet].iter_rows(values_only=True)
        header = [_to_text(h) for h in next(rows_iter, ())]
        for s in specs:
            if s.name not in header:
                errors.append(f"{sheet} row 1: column '{s.name}' (described in DataDictionary) is missing.")
        extra = [h for h in header if h and h not in described]
        if extra:
            out.warnings.append(f"{sheet}: ignoring undescribed column(s) {', '.join(extra)}.")
        pos = {h: i for i, h in enumerate(header) if h}
        rows: List[Dict[str, Any]] = []
        for r, raw in enumerate(rows_iter, start=2):
            if raw is None or all(v is None or (isinstance(v, str) and not v.strip()) for v in raw):
                continue
            rec: Dict[str, Any] = {"_row": r}
            for s in specs:
                if s.name not in pos:
                    continue
                cell = raw[pos[s.name]] if pos[s.name] < len(raw) else None
                try:
                    val = _coerce(cell, s.dtype)
                except ValueError as exc:
                    errors.append(f"{sheet} row {r}, {s.name}: {exc} (got {cell!r}).")
                    continue
                if val is None and not s.nullable:
                    errors.append(f"{sheet} row {r}, {s.name}: value is required.")
                if val is not None and s.allowed and s.dtype.lower() != "boolean" and str(val) not in s.allowed:
                    errors.append(f"{sheet} row {r}, {s.name}: '{val}' is not one of {', '.join(s.allowed)}.")
                rec[s.name] = val
            rows.append(rec)
        out.rows[sheet] = rows
    if errors:
        raise ImportFailed(errors)
    _check_keys(out, errors)
    _check_rules(out, errors)
    if errors:
        raise ImportFailed(errors)
    return out


def _check_keys(p: Parsed, errors: List[str]) -> None:
    keys: Dict[str, set] = {}
    for sheet, specs in p.specs.items():
        if sheet not in p.rows:
            continue
        for s in specs:
            if not s.pk:
                continue
            seen: Dict[Any, int] = {}
            for rec in p.rows[sheet]:
                k = rec.get(s.name)
                if k in seen:
                    errors.append(f"{sheet} row {rec['_row']}, {s.name}: duplicate key '{k}' (first at row {seen[k]}).")
                seen[k] = rec["_row"]
            keys[f"{sheet}.{s.name}"] = set(seen)
    for sheet, specs in p.specs.items():
        if sheet not in p.rows:
            continue
        for s in specs:
            if not s.fk:
                continue
            target = f"{s.fk[0]}.{s.fk[1]}"
            if target not in keys:
                errors.append(f"DataDictionary: {sheet}.{s.name} references {target}, which is not a primary key.")
                continue
            for rec in p.rows[sheet]:
                v = rec.get(s.name)
                if v is not None and v not in keys[target]:
                    errors.append(f"{sheet} row {rec['_row']}, {s.name}: '{v}' not found in {target}.")


def _check_rules(p: Parsed, errors: List[str]) -> None:
    """Cross-field rules of the claim lifecycle."""
    for c in p.rows.get("Claims", []):
        r, stage, decision = c["_row"], c.get("stage"), c.get("decision")
        where = f"Claims row {r} ({c.get('claim_id')})"
        if stage == "decided" and decision not in ("approved", "rejected"):
            errors.append(f"{where}: a decided claim needs decision approved or rejected.")
        if stage != "decided" and decision:
            errors.append(f"{where}: decision is only allowed at stage 'decided' (employee decision).")
        if decision == "rejected" and not c.get("decision_reason"):
            errors.append(f"{where}: a rejection needs decision_reason.")
        if decision == "approved":
            amt = c.get("approved_amount_sar")
            if amt is None:
                errors.append(f"{where}: an approval needs approved_amount_sar.")
            elif amt < 0 or amt > c["claimed_amount_sar"]:
                errors.append(f"{where}: approved_amount_sar must be between 0 and claimed_amount_sar.")
            elif amt == 0 and not c.get("decision_reason"):
                errors.append(f"{where}: an approval of 0 needs decision_reason.")
        elif c.get("approved_amount_sar") is not None:
            errors.append(f"{where}: approved_amount_sar is only allowed for approved claims.")
        if c.get("payout_status") == "paid":
            if decision != "approved" or c.get("paid_at") is None:
                errors.append(f"{where}: payout_status 'paid' needs an approved decision and paid_at.")
        elif c.get("paid_at") is not None or c.get("paid_amount_sar") is not None:
            errors.append(f"{where}: paid_at/paid_amount_sar are only allowed when payout_status is 'paid'.")
        if c.get("action_required") and stage != "auto_check":
            errors.append(f"{where}: action_required is only valid at stage 'auto_check' (Needs Documents).")
    for d in p.rows.get("Documents", []):
        where = f"Documents row {d['_row']} ({d.get('document_id')})"
        uri = d.get("file_uri") or ""
        try:
            path = demo_fixtures.uri_to_path(uri)
        except ValueError as exc:
            errors.append(f"{where}: {exc}.")
            continue
        if not path.exists():
            errors.append(f"{where}: file for {uri} not found at {path}. Run scripts/build_demo_workbook.py.")
            continue
        digest = storage.sha256(path.read_bytes())
        if d.get("file_sha256") and d["file_sha256"] != digest:
            errors.append(f"{where}: file at {path} doesn't match file_sha256 (the file changed).")
        d["_bytes_sha"] = digest


# ====================================================================== write


@dataclass
class Report:
    inserted: Dict[str, int] = field(default_factory=dict)
    updated: Dict[str, int] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def bump(self, kind: str, table: str) -> None:
        bucket = self.inserted if kind == "insert" else self.updated
        bucket[table] = bucket.get(table, 0) + 1


def _halalas(v: Optional[Decimal]) -> Optional[int]:
    return lifecycle.to_halalas(v) if v is not None else None


def _has_live_activity(db: Session, claim_id: str) -> bool:
    return db.scalar(select(StatusEvent.id).where(StatusEvent.claim_id == claim_id, StatusEvent.origin != "seed")
                     .limit(1)) is not None


def _merge(db: Session, model, key: Any, values: Dict[str, Any], upsert: bool, report: Report, table: str):
    obj = db.get(model, key)
    if obj is None:
        obj = model(**values)
        db.add(obj)
        report.bump("insert", table)
        return obj
    if not upsert:
        raise ImportFailed([f"{table}: '{key}' already exists. Re-run with --upsert to refresh imported data."])
    for k, v in values.items():
        setattr(obj, k, v)
    report.bump("update", table)
    return obj


def write(db: Session, p: Parsed, upsert: bool) -> Report:
    report = Report(warnings=list(p.warnings))
    members = {m["member_id"]: m for m in p.rows["Members"]}
    for m in p.rows["Members"]:
        _merge(db, Member, m["member_id"], dict(member_id=m["member_id"], display_name_ar=m["display_name"],
                                                member_identity=m["member_identity"],
                                                is_synthetic=bool(m.get("is_synthetic", True))), upsert, report, "Members")
    db.flush()
    for pol in p.rows["Policies"]:
        _merge(db, Policy, pol["policy_id"], dict(policy_id=pol["policy_id"], member_id=pol["member_id"],
                                                  valid_from=pol["valid_from"], valid_to=pol["valid_to"],
                                                  version=pol["rule_version"], is_synthetic=True), upsert, report, "Policies")
    db.flush()
    for r in p.rows["PolicyRules"]:
        _merge(db, PolicyRule, r["rule_id"], dict(
            rule_id=r["rule_id"], policy_id=r["policy_id"], category=r["category"],
            limit_halalas=_halalas(r["limit_sar"]), member_share_rate=str(r["member_share_rate"]),
            invoice_required=r["invoice_required"], payment_proof_required=r["payment_proof_required"],
            excluded_service_codes=r.get("excluded_service_codes") or "", version=r["rule_version"],
            is_synthetic=True), upsert, report, "PolicyRules")
    for s in p.rows["SLA"]:
        _merge(db, SlaTarget, s["category"], dict(
            category=s["category"], category_ar=s["category_label"],
            initial_check_target_minutes=int(s["initial_check_target_minutes"]),
            employee_estimate_hours=int(s["employee_estimate_hours"]), clock_basis=s["clock_basis"],
            estimate_label_ar=s["estimate_label"], is_synthetic=True), upsert, report, "SLA")
    db.flush()

    invoices = {i["invoice_id"]: i for i in p.rows["Invoices"]}
    docs_by_claim: Dict[str, List[Dict[str, Any]]] = {}
    for d in p.rows["Documents"]:
        docs_by_claim.setdefault(d["claim_id"], []).append(d)
    protected: set = set()
    for c in p.rows["Claims"]:
        cid = lifecycle.seed_uuid(c["claim_id"])
        existing = db.get(Claim, cid)
        if existing is not None and (existing.origin != "seed" or _has_live_activity(db, cid)):
            protected.add(c["claim_id"])
            report.skipped.append(f"{c['claim_id']}: has live activity in the app; left unchanged.")
            continue
        if existing is not None and not upsert:
            raise ImportFailed([f"Claims: '{c['claim_id']}' already exists. Re-run with --upsert."])
        sla = db.get(SlaTarget, c["category"])
        values = dict(
            id=cid, session_id="seed", status="submitted", is_sample=True, reference=c["claim_id"],
            claim_number=c["claim_id"], member_id=c["member_id"], policy_id=c["policy_id"], category=c["category"],
            claimed_halalas=_halalas(c["claimed_amount_sar"]), currency=c["currency"], stage=c["stage"],
            action_required=bool(c["action_required"]), decision=c.get("decision"),
            approved_halalas=_halalas(c.get("approved_amount_sar")), decision_reason=c.get("decision_reason"),
            payout_status=c["payout_status"], paid_at=c.get("paid_at"), paid_halalas=_halalas(c.get("paid_amount_sar")),
            submitted_at=c["submitted_at"], member_reviewed_at=c["submitted_at"], created_at=c["submitted_at"],
            review_started_at=c.get("review_started_at"),
            review_due_at=(c["review_started_at"] + timedelta(hours=sla.employee_estimate_hours)
                           if c.get("review_started_at") and sla and c["stage"] == "employee_review" else None),
            decided_at=c["updated_at"] if c["stage"] == "decided" else None,
            last_update_at=c["updated_at"], version=int(c["version"]), origin="seed", scenario=c["scenario"],
            check_round=1 if c["stage"] != "submitted" else 0)
        claim = _merge(db, Claim, cid, values, upsert, report, "Claims")
        db.flush()
        member = members[c["member_id"]]
        for d in docs_by_claim.get(c["claim_id"], []):
            did = lifecycle.seed_uuid(d["document_id"])
            if db.get(Document, did) is not None:
                continue  # never duplicate a document or its stored file
            inv = invoices[d["invoice_id"]]
            spec = demo_fixtures.spec_from_rows(d, inv, member, c)
            data = demo_fixtures.uri_to_path(d["file_uri"]).read_bytes()
            mime, pages = storage.validate_upload(d["file_name"], data)
            name = storage.save_file(data, mime)
            doc = Document(id=did, claim_id=cid, position=len(claim.documents) + 1,
                           original_filename=d["file_name"], storage_name=name, mime_type=mime, size_bytes=len(data),
                           sha256=d["_bytes_sha"], page_count=pages, source="sample_fixture",
                           fixture_id=d["document_id"], status="extracted",
                           extraction_json=demo_fixtures.extraction(spec).model_dump_json(),
                           extraction_mode="sample_fixture", extracted_at=d["uploaded_at"], created_at=d["uploaded_at"])
            doc.thumb_name = storage.make_thumbnail(name, mime)
            db.add(doc)
            db.flush()
            db.refresh(claim)
            report.bump("insert", "Documents")
        if claim.snapshot_json is None or upsert:
            view = services.build_view(claim)
            claim.snapshot_json = json.dumps(export.build_export(claim, view, reference=c["claim_id"],
                                                                 submitted_at=c["submitted_at"]), ensure_ascii=False)
    db.flush()

    for i in p.rows["Invoices"]:
        if i["claim_id"] in protected:
            continue
        inv_doc = next((d for d in p.rows["Documents"] if d["invoice_id"] == i["invoice_id"]
                        and d["document_type"] == "invoice"), None)
        claim_member = next(c["member_id"] for c in p.rows["Claims"] if c["claim_id"] == i["claim_id"])
        _merge(db, InvoiceRecord, i["invoice_id"], dict(
            id=i["invoice_id"], claim_id=lifecycle.seed_uuid(i["claim_id"]),
            document_id=lifecycle.seed_uuid(inv_doc["document_id"]) if inv_doc else None, member_id=claim_member,
            provider_id=i["provider_id"], provider_name=i["provider_name"], invoice_number=i["invoice_number"],
            invoice_number_norm=lifecycle.norm_invoice_number(i["invoice_number"]), service_date=i["service_date"],
            total_halalas=_halalas(i.get("total_sar")), currency=i["currency"],
            extracted_member_identity=i.get("extracted_member_identity"), service_code=i["service_code"],
            file_sha256=inv_doc["_bytes_sha"] if inv_doc else None, source="seed"), True, report, "Invoices")
    for ch in p.rows["Checks"]:
        if ch["claim_id"] in protected:
            continue
        ar = lifecycle.REASON_AR.get(ch["reason_code"], ch["message"])
        _merge(db, CheckResult, ch["check_id"], dict(
            id=ch["check_id"], claim_id=lifecycle.seed_uuid(ch["claim_id"]), run_id="seed", check_type=ch["check_type"],
            result=ch["result"], reason_code=ch["reason_code"], message_ar=ar, message_en=ch["message"],
            evidence_ref=ch.get("evidence_ref"),
            related_claim_id=lifecycle.seed_uuid(ch["related_claim_id"]) if ch.get("related_claim_id") else None,
            rule_version=ch["rule_version"], checked_at=ch["checked_at"], origin="seed"), True, report, "Checks")
    for ev in p.rows["StatusHistory"]:
        if ev["claim_id"] in protected:
            continue
        _merge(db, StatusEvent, ev["event_id"], dict(
            id=ev["event_id"], claim_id=lifecycle.seed_uuid(ev["claim_id"]), stage=ev["stage"],
            event_type=ev["event_type"], actor_role=ev["actor_role"], actor_id=ev["actor_id"],
            occurred_at=ev["occurred_at"], reason=ev.get("reason"), origin="seed"), True, report, "StatusHistory")
    db.flush()
    return report


def run_import(path: Path, dry_run: bool = False, upsert: bool = False) -> Report:
    if not settings.demo_mode:
        raise ImportFailed(["Imports of fictional demo identities are only allowed with DEMO_MODE=true."])
    parsed = parse_workbook(path)
    init_db()
    storage.ensure_dirs()
    db = SessionLocal()
    try:
        report = write(db, parsed, upsert)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reset_demo(path: Path) -> Report:
    """Remove every seeded claim (and its children), then import again."""
    if not settings.demo_mode:
        raise ImportFailed(["reset-demo is only available with DEMO_MODE=true."])
    parsed = parse_workbook(path)  # validate before deleting anything
    init_db()
    with SessionLocal() as db:
        ids = list(db.scalars(select(Claim.id).where(Claim.origin == "seed")))
        for cid in ids:
            claim = db.get(Claim, cid)
            for d in claim.documents:
                storage.delete_files(d.storage_name, d.thumb_name)
        for model in (CheckResult, StatusEvent, InvoiceRecord, CheckJob):
            db.execute(delete(model).where(model.claim_id.in_(ids)))
        for cid in ids:
            db.delete(db.get(Claim, cid))
        db.commit()
    del parsed
    return run_import(path, upsert=True)  # reference rows are refreshed; claims are recreated


def verify(path: Path) -> List[Dict[str, Any]]:
    """Run the automatic checker (read-only) on each test case's actual files."""
    parsed = parse_workbook(path)
    results = []
    with SessionLocal() as db:
        for t in parsed.rows["TestCases"]:
            claim = db.get(Claim, lifecycle.seed_uuid(t["claim_id"]))
            if claim is None:
                results.append({**t, "ok": False, "got": "claim not imported"})
                continue
            checks = lifecycle.evaluate(db, claim)
            fails = [c.reason_code for c in checks if c.result == "fail"]
            flags = [c.reason_code for c in checks if c.result == "manual_review"]
            dup = next((c for c in checks if c.check_type == "duplicate_check"), None)
            outcome = "auto_check" if fails else "employee_review"
            code = t["expected_reason_code"]
            if t["expected_stage"] == "auto_check":
                ok = outcome == "auto_check" and code in fails
            elif t["expected_stage"] == "employee_review":
                ok = outcome == "employee_review" and (code == "documents_consistent" and not flags or code in flags)
            else:  # T08: stays at submitted until checks run; must not be flagged as a duplicate
                ok = dup is not None and dup.reason_code == code and claim.stage == t["expected_stage"]
            results.append({"test_id": t["test_id"], "claim_id": t["claim_id"], "expected": f"{t['expected_stage']}/{code}",
                            "got": f"{outcome}; fail={fails} review={flags}", "ok": ok})
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.seed", description="Import the fictional demo workbook.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("import", "verify", "reset-demo"):
        sp = sub.add_parser(name)
        sp.add_argument("workbook", nargs="?", type=Path, default=settings.demo_workbook)
        if name == "import":
            sp.add_argument("--dry-run", action="store_true", help="validate and roll back")
            sp.add_argument("--upsert", action="store_true", help="refresh rows that already exist")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            rep = run_import(args.workbook, dry_run=args.dry_run, upsert=args.upsert)
            print(("DRY RUN - nothing written. " if args.dry_run else "Imported. ")
                  + f"inserted={rep.inserted} updated={rep.updated}")
            for line in rep.skipped + rep.warnings:
                print("  note:", line)
        elif args.cmd == "reset-demo":
            rep = reset_demo(args.workbook)
            print(f"Demo reset. inserted={rep.inserted}")
        else:
            ok = True
            for r in verify(args.workbook):
                ok &= r["ok"]
                print(f"{'PASS' if r['ok'] else 'FAIL'} {r['test_id']} {r['claim_id']}: expected {r['expected']}; {r['got']}")
            return 0 if ok else 1
    except ImportFailed as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        for e in exc.errors:
            print("  -", e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())


def attach_correction(db: Session, claim: Claim) -> Optional[Document]:
    """Internal test helper: attach the corrected synthetic document for an imported T01-T03 claim,
    the way a member's re-upload would. Not reachable from the website."""
    if claim.origin != "seed" or not lifecycle.can_complete(claim):
        return None
    item = demo_fixtures.CORRECTIONS.get(claim.scenario or "")
    inv = db.scalar(select(InvoiceRecord).where(InvoiceRecord.claim_id == claim.id, InvoiceRecord.source == "seed"))
    member = db.get(Member, claim.member_id) if claim.member_id else None
    if item is None or inv is None or member is None:
        return None
    fixture_id = f"{claim.claim_number}:correction"
    existing = next((d for d in claim.active_documents if d.fixture_id == fixture_id), None)
    if existing is not None:
        return existing
    inv_row = {"provider_name": inv.provider_name, "invoice_number": inv.invoice_number,
               "service_date": inv.service_date, "total_sar": lifecycle.from_halalas(inv.total_halalas),
               "currency": inv.currency, "extracted_member_identity": inv.extracted_member_identity,
               "service_code": inv.service_code}
    claim_row = {"claim_id": claim.claim_number, "category": claim.category,
                 "claimed_amount_sar": lifecycle.from_halalas(claim.claimed_halalas)}
    base = demo_fixtures.spec_from_rows({"document_type": "invoice"}, inv_row,
                                        {"display_name": member.display_name_ar}, claim_row)
    spec = demo_fixtures.corrected_spec(claim.scenario, base, member.member_identity,
                                        lifecycle.from_halalas(claim.claimed_halalas))
    path = demo_fixtures.fixture_root() / claim.claim_number / item[1]
    data = path.read_bytes() if path.exists() else demo_fixtures.render_pdf(spec)
    mime, pages = storage.validate_upload(item[1], data)
    name = storage.save_file(data, mime)
    replaced = next((d for d in claim.active_documents
                     if services.extraction_of(d) and services.extraction_of(d).document_type == spec.doc_type), None)
    doc = Document(claim_id=claim.id, position=services.next_position(db, claim.id),
                   original_filename=f"{claim.claim_number.lower()}-{item[1].replace('_', '-')}", storage_name=name,
                   mime_type=mime, size_bytes=len(data), sha256=storage.sha256(data), page_count=pages,
                   source="sample_fixture", fixture_id=fixture_id, status="extracted",
                   extraction_json=demo_fixtures.extraction(spec).model_dump_json(),
                   extraction_mode="sample_fixture", extracted_at=utcnow(),
                   replaces_doc_id=replaced.id if replaced else None)
    doc.thumb_name = storage.make_thumbnail(name, mime)
    db.add(doc)
    db.flush()
    if replaced is not None:
        services.retire_document(db, claim, replaced, "replaced", replaced_by=doc.id)
    return doc
