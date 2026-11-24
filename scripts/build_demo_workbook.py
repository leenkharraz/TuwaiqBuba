"""Build the fictional demo workbook and its synthetic documents.

    python scripts/build_demo_workbook.py            # writes data/Bupa_Prototype_Data.xlsx
    python scripts/build_demo_workbook.py --out PATH

Everything produced here is FICTIONAL test data: members, policies, rules, amounts,
processing-time estimates and documents. Nothing represents Bupa Arabia policy terms,
real people or real service commitments. All content is in English.

The script also renders one PDF per Documents row under data/demo_fixtures/ (the target of
each fixture:// URI) and records the file's SHA-256 in the workbook, so the importer can
verify that each referenced file exists and is the one the workbook describes.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from app import demo_fixtures  # noqa: E402

FICTIONAL = "Fictional test data. Not Bupa Arabia data, policy terms or service commitments."
PROVIDER = ("PROV-DEMO-01", "Demo Care Clinic (fictional)")

# ---------------------------------------------------------------- reference data
MEMBERS = [
    {"member_id": "MEM-DEMO-01", "display_name": "Nora Example", "member_identity": "SYN-MEMBER-001"},
    {"member_id": "MEM-DEMO-02", "display_name": "Sara Example", "member_identity": "SYN-MEMBER-002"},
]
POLICIES = [
    {"policy_id": "POL-DEMO-01", "member_id": "MEM-DEMO-01", "policy_name": "Fictional Demo Plan A",
     "valid_from": date(2026, 1, 1), "valid_to": date(2026, 12, 31), "rule_version": "DEMO-V1"},
    {"policy_id": "POL-DEMO-02", "member_id": "MEM-DEMO-02", "policy_name": "Fictional Demo Plan A",
     "valid_from": date(2026, 1, 1), "valid_to": date(2026, 12, 31), "rule_version": "DEMO-V1"},
]
RULE_LIMITS = {"optical": (1000, "0.20", ""), "dental": (2000, "0.20", "DEMO-EXCLUDED"),
               "inpatient": (20000, "0.10", ""), "outpatient": (3000, "0.20", "")}
SLA = [("optical", "Optical", 3, 24), ("dental", "Dental", 3, 48),
       ("inpatient", "Inpatient", 3, 72), ("outpatient", "Outpatient", 3, 24)]


def dt(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, day, h, m)


# claim_id, member, category, claimed, stage, action_required, decision, approved, payout, paid_amount, paid_at,
# submitted_at, review_started_at, decision_reason, scenario, invoice_number, service_date, total, identity?,
# service_code, documents
C = []


def claim(cid, member, cat, claimed, stage, scenario, inv_no, sdate, submitted, *, total="same", identity=True,
          code="DEMO-GENERAL", docs=("invoice", "payment_receipt", "medical_report"), action=False,
          decision=None, approved=None, payout="unpaid", paid_amount=None, paid_at=None, review=None,
          reason=None):
    C.append(dict(claim_id=cid, member_id=member, cat=cat, claimed=claimed, stage=stage, scenario=scenario,
                  inv_no=inv_no, sdate=sdate, submitted=submitted, total=claimed if total == "same" else total,
                  identity=identity, code=code, docs=docs, action=action, decision=decision, approved=approved,
                  payout=payout, paid_amount=paid_amount, paid_at=paid_at, review=review, reason=reason))


M1, M2 = "MEM-DEMO-01", "MEM-DEMO-02"
claim("CLM-DEMO-001", M1, "outpatient", 350, "auto_check", "invoice_total_missing", "INV-DEMO-001",
      date(2026, 11, 22), dt(24, 8), total=None, action=True)
claim("CLM-DEMO-002", M1, "optical", 650, "auto_check", "identity_missing", "INV-DEMO-002",
      date(2026, 11, 22), dt(24, 8, 15), identity=False, action=True)
claim("CLM-DEMO-003", M1, "dental", 800, "auto_check", "payment_proof_missing", "INV-DEMO-003",
      date(2026, 11, 22), dt(24, 8, 30), docs=("invoice", "medical_report"), action=True)
claim("CLM-DEMO-004", M1, "inpatient", 2400, "employee_review", "complete", "INV-COMPLETE-004",
      date(2026, 11, 23), dt(24, 8, 45), review=dt(24, 8, 47))
claim("CLM-DEMO-005", M1, "outpatient", 350, "employee_review", "paid_duplicate", "INV-PAID-007",
      date(2026, 11, 19), dt(24, 9), review=dt(24, 9, 2))
claim("CLM-DEMO-006", M1, "optical", 1500, "employee_review", "policy_limit_review", "INV-DEMO-006",
      date(2026, 11, 22), dt(24, 9, 15), review=dt(24, 9, 17))
claim("CLM-DEMO-007", M1, "outpatient", 350, "decided", "paid_history", "INV-PAID-007",
      date(2026, 11, 19), dt(20, 8), review=dt(20, 8, 2), decision="approved", approved=280, payout="paid",
      paid_amount=280, paid_at=dt(21, 10), reason="Approved at the fictional test member share. Payment recorded.")
claim("CLM-DEMO-008", M1, "dental", 500, "decided", "rejected_history", "INV-DEMO-008",
      date(2026, 11, 20), dt(21, 8), review=dt(21, 8, 2), code="DEMO-EXCLUDED", decision="rejected",
      reason="The service code is excluded by the fictional test policy.")
claim("CLM-DEMO-009", M1, "outpatient", 200, "decided", "approved_unpaid_history", "INV-DEMO-009",
      date(2026, 11, 21), dt(22, 8), review=dt(22, 8, 2), decision="approved", approved=160,
      reason="Approved at the fictional test member share. Not paid yet.")
claim("CLM-DEMO-010", M1, "inpatient", 2400, "employee_review", "pending_duplicate", "INV-COMPLETE-004",
      date(2026, 11, 23), dt(24, 9, 30), review=dt(24, 9, 32))
claim("CLM-DEMO-011", M1, "inpatient", 2400, "submitted", "different_invoice", "INV-DEMO-011",
      date(2026, 11, 23), dt(24, 9, 45))
claim("CLM-DEMO-012", M2, "outpatient", 120, "submitted", "other_member", "INV-DEMO-012",
      date(2026, 11, 22), dt(24, 9, 50))

# Checks seeded for claims that have passed stage 2 (reason codes match the app's checker).
CHECKS = {
    "CLM-DEMO-001": ("fail", "invoice_total_missing", "The invoice total isn't visible. Attach a clear, complete invoice."),
    "CLM-DEMO-002": ("fail", "identity_missing", "The membership or ID number isn't shown on the documents."),
    "CLM-DEMO-003": ("fail", "payment_proof_missing", "Proof of payment is missing. Attach the proof of payment for this visit."),
    "CLM-DEMO-004": ("pass", "documents_consistent", "All automatic checks passed. Waiting for an employee decision."),
    "CLM-DEMO-005": ("manual_review", "paid_duplicate", "This invoice is linked to a claim that was already compensated (CLM-DEMO-007)."),
    "CLM-DEMO-006": ("manual_review", "policy_limit_review", "The amount exceeds the fictional test policy limit and needs employee review."),
    "CLM-DEMO-007": ("pass", "documents_consistent", "All automatic checks passed."),
    "CLM-DEMO-008": ("manual_review", "service_excluded", "The service code is excluded by the fictional test policy. Employee decision required."),
    "CLM-DEMO-009": ("pass", "documents_consistent", "All automatic checks passed."),
    "CLM-DEMO-010": ("manual_review", "pending_duplicate", "Another claim for this invoice is still under review (CLM-DEMO-004)."),
}
CHECK_TYPE = {"invoice_total_missing": "invoice_completeness", "identity_missing": "identity_match",
              "payment_proof_missing": "payment_evidence", "paid_duplicate": "duplicate_check",
              "pending_duplicate": "duplicate_check", "policy_limit_review": "policy_check",
              "service_excluded": "policy_check", "documents_consistent": "document_bundle"}
RELATED = {"CLM-DEMO-005": "CLM-DEMO-007", "CLM-DEMO-010": "CLM-DEMO-004"}

TESTS = [
    ("T01", "CLM-DEMO-001", "Invoice uploaded but its total is missing", "auto_check", "Needs Documents",
     "invoice_total_missing", "Member re-uploads a complete invoice; same claim ID is re-checked", "required"),
    ("T02", "CLM-DEMO-002", "Member identifier absent from the uploaded documents", "auto_check", "Needs Documents",
     "identity_missing", "Ask for a document showing the member ID; never infer it from the account", "required"),
    ("T03", "CLM-DEMO-003", "Proof of payment not attached", "auto_check", "Needs Documents",
     "payment_proof_missing", "Ask the member to upload proof of payment", "required"),
    ("T04", "CLM-DEMO-004", "Complete, consistent documents within the fictional policy", "employee_review",
     "Employee Review", "documents_consistent", "Waits for the employee; never approved automatically", "required"),
    ("T05", "CLM-DEMO-005", "Duplicate of an invoice already paid in CLM-DEMO-007", "employee_review",
     "Employee Review", "paid_duplicate", "Show prior-compensation alert; only the employee can reject", "additional"),
    ("T06", "CLM-DEMO-006", "Claimed amount above the fictional policy limit", "employee_review", "Employee Review",
     "policy_limit_review", "Show the policy rule and limit to the employee", "additional"),
    ("T07", "CLM-DEMO-010", "Same invoice as a claim still under review (CLM-DEMO-004)", "employee_review",
     "Employee Review", "pending_duplicate", "Show pending-claim alert without claiming prior payment", "additional"),
    ("T08", "CLM-DEMO-011", "Same date and amount as CLM-DEMO-004 but a different invoice number", "submitted",
     "Submitted", "no_duplicate", "Same date and amount alone is not a duplicate", "additional"),
]

# ---------------------------------------------------------------- dictionary
D = []


def col(table, name, dtype, nullable="no", pk="", fk="", allowed="", desc=""):
    D.append((table, name, dtype, nullable, pk, fk, allowed, desc))


col("Members", "member_id", "text", pk="yes", desc="Stable member key.")
col("Members", "display_name", "text", desc="Fictional member name shown in the app.")
col("Members", "member_identity", "text", desc="Fictional membership number. Not a national ID.")
col("Members", "is_synthetic", "boolean", allowed="TRUE", desc=FICTIONAL)
col("Policies", "policy_id", "text", pk="yes", desc="Stable policy key.")
col("Policies", "member_id", "text", fk="Members.member_id", desc="Policy holder.")
col("Policies", "policy_name", "text", desc="Fictional plan name.")
col("Policies", "valid_from", "date", desc="First day the fictional policy applies.")
col("Policies", "valid_to", "date", desc="Last day the fictional policy applies.")
col("Policies", "rule_version", "text", desc="Version of the fictional rules in force.")
col("Policies", "is_synthetic", "boolean", allowed="TRUE", desc=FICTIONAL)
col("PolicyRules", "rule_id", "text", pk="yes", desc="Stable rule key.")
col("PolicyRules", "policy_id", "text", fk="Policies.policy_id", desc="Policy the rule belongs to.")
col("PolicyRules", "category", "text", allowed="optical, dental, inpatient, outpatient", desc="Service category.")
col("PolicyRules", "limit_sar", "decimal", desc="Fictional per-claim limit in SAR.")
col("PolicyRules", "member_share_rate", "decimal", desc="Fictional member share (0.20 = 20%).")
col("PolicyRules", "invoice_required", "boolean", desc="An invoice must be attached.")
col("PolicyRules", "payment_proof_required", "boolean", desc="Proof of payment must be attached.")
col("PolicyRules", "excluded_service_codes", "text", "yes", desc="Comma-separated fictional excluded service codes.")
col("PolicyRules", "rule_version", "text", desc="Rule version recorded with each check.")
col("PolicyRules", "is_synthetic", "boolean", allowed="TRUE", desc="Fictional test rule, not a Bupa policy term.")
col("Claims", "claim_id", "text", pk="yes", desc="Claim number shown to the member and employee.")
col("Claims", "member_id", "text", fk="Members.member_id", desc="Claim owner.")
col("Claims", "policy_id", "text", fk="Policies.policy_id", desc="Policy the claim is assessed under.")
col("Claims", "category", "text", allowed="optical, dental, inpatient, outpatient", desc="Claim category.")
col("Claims", "claimed_amount_sar", "decimal", desc="Amount requested by the member.")
col("Claims", "currency", "text", allowed="SAR", desc="Currency code.")
col("Claims", "stage", "text", allowed="submitted, auto_check, employee_review, decided",
    desc="Tracking stage: Submitted, Document Verification, Employee Review, Final Decision.")
col("Claims", "action_required", "boolean",
    desc="TRUE at Document Verification means Needs Documents (paused for the member). Never a final rejection.")
col("Claims", "decision", "text", "yes", allowed="approved, rejected",
    desc="Final decision. Only an employee sets it; blank until stage = decided.")
col("Claims", "approved_amount_sar", "decimal", "yes", desc="Employee-approved amount. Blank unless approved.")
col("Claims", "payout_status", "text", allowed="unpaid, paid", desc="Separate from approval. Approved may still be unpaid.")
col("Claims", "paid_amount_sar", "decimal", "yes", desc="Amount actually paid, from a payment record. Blank if unpaid.")
col("Claims", "paid_at", "datetime (UTC)", "yes", desc="When payment was recorded. Required when payout_status = paid.")
col("Claims", "submitted_at", "datetime (UTC)", desc="Submission time.")
col("Claims", "review_started_at", "datetime (UTC)", "yes", desc="When the claim entered Employee Review.")
col("Claims", "updated_at", "datetime (UTC)", desc="Last change.")
col("Claims", "decision_reason", "text", "yes", desc="Employee reason. Required for rejection.")
col("Claims", "version", "integer", desc="Optimistic-lock version.")
col("Claims", "scenario", "text", desc="Test scenario key.")
col("Claims", "is_synthetic", "boolean", allowed="TRUE", desc=FICTIONAL)
col("Invoices", "invoice_id", "text", pk="yes", desc="Stable invoice row key.")
col("Invoices", "claim_id", "text", fk="Claims.claim_id", desc="Claim the invoice belongs to.")
col("Invoices", "provider_id", "text", desc="Fictional provider key.")
col("Invoices", "provider_name", "text", desc="Fictional provider name printed on the documents.")
col("Invoices", "invoice_number", "text", desc="Printed invoice number. Kept as text (leading zeros matter).")
col("Invoices", "service_date", "date", desc="Date of service.")
col("Invoices", "total_sar", "decimal", "yes", desc="Printed total. Blank means missing, never zero (T01).")
col("Invoices", "currency", "text", allowed="SAR", desc="Currency code.")
col("Invoices", "extracted_member_identity", "text", "yes", desc="Member ID printed on the documents. Blank in T02.")
col("Invoices", "service_code", "text", desc="Fictional service code checked against exclusions.")
col("Invoices", "is_synthetic", "boolean", allowed="TRUE", desc=FICTIONAL)
col("Documents", "document_id", "text", pk="yes", desc="Stable document key.")
col("Documents", "claim_id", "text", fk="Claims.claim_id", desc="Claim the document belongs to.")
col("Documents", "invoice_id", "text", fk="Invoices.invoice_id", desc="Invoice the document relates to.")
col("Documents", "document_type", "text", allowed="invoice, payment_receipt, medical_report", desc="Document type.")
col("Documents", "file_uri", "text", desc="fixture:// path under data/demo_fixtures, generated by scripts/build_demo_workbook.py.")
col("Documents", "file_name", "text", desc="File name shown in the app.")
col("Documents", "file_sha256", "text", desc="SHA-256 of the generated file bytes; the importer verifies it.")
col("Documents", "uploaded_at", "datetime (UTC)", desc="Upload time.")
col("Documents", "is_synthetic", "boolean", allowed="TRUE", desc="Fictional generated document.")
col("Checks", "check_id", "text", pk="yes", desc="Stable check key.")
col("Checks", "claim_id", "text", fk="Claims.claim_id", desc="Checked claim.")
col("Checks", "check_type", "text", desc="invoice_completeness, identity_match, payment_evidence, policy_check, duplicate_check or document_bundle.")
col("Checks", "result", "text", allowed="pass, fail, manual_review",
    desc="fail = member must complete documents. manual_review = employee decides. Never an automatic rejection.")
col("Checks", "reason_code", "text", desc="Machine-readable reason.")
col("Checks", "message", "text", desc="Short explanation in English.")
col("Checks", "evidence_ref", "text", "yes", fk="Invoices.invoice_id", desc="Invoice row the check is based on.")
col("Checks", "related_claim_id", "text", "yes", fk="Claims.claim_id", desc="Earlier claim matched by the duplicate check.")
col("Checks", "rule_version", "text", desc="Rule version applied.")
col("Checks", "checked_at", "datetime (UTC)", desc="When the check ran.")
col("Checks", "is_synthetic", "boolean", allowed="TRUE", desc=FICTIONAL)
col("StatusHistory", "event_id", "text", pk="yes", desc="Stable event key. History is append-only.")
col("StatusHistory", "claim_id", "text", fk="Claims.claim_id", desc="Claim.")
col("StatusHistory", "stage", "text", allowed="submitted, auto_check, employee_review, decided", desc="Stage entered.")
col("StatusHistory", "event_type", "text", desc="What happened, e.g. submitted, needs_completion, queued_for_review, approved, rejected, payout_recorded.")
col("StatusHistory", "actor_role", "text", allowed="member, system, employee", desc="Who acted. Decisions are always employee.")
col("StatusHistory", "actor_id", "text", desc="Fictional actor key.")
col("StatusHistory", "occurred_at", "datetime (UTC)", desc="Actual event time.")
col("StatusHistory", "reason", "text", "yes", desc="Reason or note.")
col("StatusHistory", "is_synthetic", "boolean", allowed="TRUE", desc=FICTIONAL)
col("SLA", "category", "text", pk="yes", allowed="optical, dental, inpatient, outpatient", desc="Category.")
col("SLA", "category_label", "text", desc="English label.")
col("SLA", "initial_check_target_minutes", "integer", desc="Fictional target for the initial automatic check.")
col("SLA", "employee_estimate_hours", "integer", desc="Fictional employee-review estimate in elapsed hours after documents are complete.")
col("SLA", "clock_basis", "text", allowed="elapsed_hours", desc="Elapsed hours, not business days.")
col("SLA", "estimate_label", "text", desc="Label shown with every estimate.")
col("SLA", "is_synthetic", "boolean", allowed="TRUE", desc="Fictional processing-time estimate, not a Bupa commitment.")
col("TestCases", "test_id", "text", pk="yes", desc="Test case key.")
col("TestCases", "claim_id", "text", fk="Claims.claim_id", desc="Seeded claim used by the test.")
col("TestCases", "scenario", "text", desc="What the test covers.")
col("TestCases", "expected_stage", "text", allowed="submitted, auto_check, employee_review, decided", desc="Expected stage.")
col("TestCases", "expected_status", "text", desc="Expected status label.")
col("TestCases", "expected_reason_code", "text", desc="Expected check reason code.")
col("TestCases", "expected_action", "text", desc="Expected behaviour.")
col("TestCases", "priority", "text", allowed="required, additional", desc="T01-T04 required, T05-T08 additional.")
col("DataDictionary", "table_name", "text", desc="Sheet name.")
col("DataDictionary", "field_name", "text", desc="Column heading (row 1 of the sheet).")
col("DataDictionary", "data_type", "text", desc="text, integer, decimal, boolean, date or datetime (UTC).")
col("DataDictionary", "nullable", "text", allowed="yes, no", desc="Whether the cell may be empty.")
col("DataDictionary", "primary_key", "text", "yes", allowed="yes", desc="Primary key column.")
col("DataDictionary", "foreign_key", "text", "yes", desc="Referenced Table.column.")
col("DataDictionary", "allowed_values", "text", "yes", desc="Permitted values.")
col("DataDictionary", "description", "text", desc="Meaning of the field.")


def build(out: Path) -> dict:
    fx_root = demo_fixtures.fixture_root()
    members = {m["member_id"]: m for m in MEMBERS}
    rows = {k: [] for k in ("Members", "Policies", "PolicyRules", "Claims", "Invoices", "Documents", "Checks",
                            "StatusHistory", "SLA", "TestCases")}
    for m in MEMBERS:
        rows["Members"].append({**m, "is_synthetic": True})
    for p in POLICIES:
        rows["Policies"].append({**p, "is_synthetic": True})
        for cat, (limit, share, excl) in RULE_LIMITS.items():
            rows["PolicyRules"].append({
                "rule_id": f"RULE-{p['policy_id'][-2:]}-{cat}", "policy_id": p["policy_id"], "category": cat,
                "limit_sar": limit, "member_share_rate": float(share), "invoice_required": True,
                "payment_proof_required": True, "excluded_service_codes": excl or None,
                "rule_version": p["rule_version"], "is_synthetic": True})
    for cat, label, mins, hours in SLA:
        rows["SLA"].append({"category": cat, "category_label": label, "initial_check_target_minutes": mins,
                            "employee_estimate_hours": hours, "clock_basis": "elapsed_hours",
                            "estimate_label": "Fictional estimate", "is_synthetic": True})

    n_files = 0
    for i, c in enumerate(C, start=1):
        cid = c["claim_id"]
        policy = next(p for p in POLICIES if p["member_id"] == c["member_id"])
        rows["Claims"].append({
            "claim_id": cid, "member_id": c["member_id"], "policy_id": policy["policy_id"], "category": c["cat"],
            "claimed_amount_sar": c["claimed"], "currency": "SAR", "stage": c["stage"],
            "action_required": c["action"], "decision": c["decision"], "approved_amount_sar": c["approved"],
            "payout_status": c["payout"], "paid_amount_sar": c["paid_amount"], "paid_at": c["paid_at"],
            "submitted_at": c["submitted"], "review_started_at": c["review"], "updated_at": None,
            "decision_reason": c["reason"], "version": 1, "scenario": c["scenario"], "is_synthetic": True})
        inv_id = f"INVROW-{i:03d}"
        inv = {"invoice_id": inv_id, "claim_id": cid, "provider_id": PROVIDER[0], "provider_name": PROVIDER[1],
               "invoice_number": c["inv_no"], "service_date": c["sdate"], "total_sar": c["total"], "currency": "SAR",
               "extracted_member_identity": members[c["member_id"]]["member_identity"] if c["identity"] else None,
               "service_code": c["code"], "is_synthetic": True}
        rows["Invoices"].append(inv)
        claim_row = rows["Claims"][-1]
        for dtype in c["docs"]:
            name = f"{dtype}.pdf"
            uri = f"{demo_fixtures.FIXTURE_SCHEME}{cid}/{name}"
            spec = demo_fixtures.spec_from_rows({"document_type": dtype}, inv, members[c["member_id"]], claim_row)
            data = demo_fixtures.render_pdf(spec)
            path = demo_fixtures.uri_to_path(uri)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            n_files += 1
            rows["Documents"].append({
                "document_id": f"DOC-{i:03d}-{dtype}", "claim_id": cid, "invoice_id": inv_id, "document_type": dtype,
                "file_uri": uri, "file_name": f"{cid.lower()}-{dtype.replace('_', '-')}.pdf",
                "file_sha256": hashlib.sha256(data).hexdigest(), "uploaded_at": c["submitted"], "is_synthetic": True})
        # corrected copies for the demo panel (not workbook documents)
        if c["scenario"] in demo_fixtures.CORRECTIONS:
            from decimal import Decimal
            base = demo_fixtures.spec_from_rows({"document_type": "invoice"}, inv, members[c["member_id"]], claim_row)
            spec = demo_fixtures.corrected_spec(c["scenario"], base, members[c["member_id"]]["member_identity"],
                                                Decimal(str(c["claimed"])))
            p = fx_root / cid / demo_fixtures.CORRECTIONS[c["scenario"]][1]
            p.write_bytes(demo_fixtures.render_pdf(spec))
            n_files += 1
        if cid in CHECKS:
            result, code, msg = CHECKS[cid]
            rows["Checks"].append({
                "check_id": f"CHK-{i:03d}", "claim_id": cid, "check_type": CHECK_TYPE[code], "result": result,
                "reason_code": code, "message": msg, "evidence_ref": inv_id, "related_claim_id": RELATED.get(cid),
                "rule_version": "DEMO-V1", "checked_at": c["submitted"] + timedelta(minutes=1),
                "is_synthetic": True})
        ev = []
        sub = c["submitted"]
        ev.append(("submitted", "submitted", "member", c["member_id"], sub, None))
        if c["stage"] != "submitted":
            ev.append(("auto_check", "checks_started", "system", "checks", sub, None))
        if c["stage"] == "auto_check" and c["action"]:
            ev.append(("auto_check", "needs_completion", "system", "checks", sub + timedelta(minutes=1),
                       CHECKS[cid][2]))
        if c["review"]:
            ev.append(("employee_review", "queued_for_review", "system", "checks", c["review"], None))
        if c["stage"] == "decided":
            at = c["review"] + timedelta(minutes=1)
            ev.append(("decided", c["decision"], "employee", "EMP-DEMO-01", at, c["reason"]))
            if c["payout"] == "paid":
                ev.append(("decided", "payout_recorded", "employee", "EMP-DEMO-01", c["paid_at"], "Fictional payment record"))
        claim_row["updated_at"] = max(e[4] for e in ev)
        for j, (stage, etype, role, actor, at, reason) in enumerate(ev, start=1):
            rows["StatusHistory"].append({"event_id": f"EVT-{i:03d}-{j}", "claim_id": cid, "stage": stage,
                                          "event_type": etype, "actor_role": role, "actor_id": actor,
                                          "occurred_at": at, "reason": reason, "is_synthetic": True})
    for t in TESTS:
        rows["TestCases"].append(dict(zip(("test_id", "claim_id", "scenario", "expected_stage", "expected_status",
                                           "expected_reason_code", "expected_action", "priority"), t)))

    wb = Workbook()
    wb.remove(wb.active)
    head_fill, head_font = PatternFill("solid", fgColor="0079C8"), Font(bold=True, color="FFFFFF")
    order = ["Members", "Policies", "PolicyRules", "Claims", "Invoices", "Documents", "Checks", "StatusHistory",
             "SLA", "TestCases", "DataDictionary"]
    for sheet in order:
        ws = wb.create_sheet(sheet)
        fields = [d[1] for d in D if d[0] == sheet]
        ws.append(fields)
        if sheet == "DataDictionary":
            for d in D:
                ws.append(list(d))
        else:
            for r in rows[sheet]:
                ws.append([r.get(f) for f in fields])
        for cell in ws[1]:
            cell.fill, cell.font = head_fill, head_font
        ws.freeze_panes = "A2"
        for idx, f in enumerate(fields, start=1):
            width = max([len(str(f))] + [len(str(c.value)) for c in ws[get_column_letter(idx)][1:] if c.value is not None])
            ws.column_dimensions[get_column_letter(idx)].width = min(60, max(12, width + 2))
            dtype = next((d[2] for d in D if d[0] == sheet and d[1] == f), "")
            for c in ws[get_column_letter(idx)][1:]:
                if dtype == "date":
                    c.number_format = "yyyy-mm-dd"
                elif dtype.startswith("datetime"):
                    c.number_format = "yyyy-mm-dd hh:mm"
                elif dtype == "decimal":
                    c.number_format = "0.00"
                if sheet == "DataDictionary":
                    c.alignment = Alignment(wrap_text=True, vertical="top")
    wb.properties.title = "Fictional claims demo data"
    wb.properties.description = FICTIONAL
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"path": str(out), "files": n_files, **{k: len(v) for k, v in rows.items()}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "Bupa_Prototype_Data.xlsx")
    args = ap.parse_args()
    print(build(args.out))


if __name__ == "__main__":
    main()
