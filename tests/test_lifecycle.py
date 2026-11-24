"""Claim lifecycle, workbook import, ownership/roles, duplicate and policy checks."""
import re
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app import lifecycle, seed
from app.config import settings
from app.db import SessionLocal
from app.models import (CheckJob, CheckResult, Claim, Correction, Document, InvoiceRecord, Member, Policy,
                        PolicyRule, SessionLink, StatusEvent, utcnow)

ROOT = Path(__file__).resolve().parent.parent
FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture(scope="session")
def workbook() -> Path:
    import importlib.util
    spec = importlib.util.spec_from_file_location("bdw", ROOT / "scripts" / "build_demo_workbook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = settings.data_dir / "Bupa_Prototype_Data.xlsx"
    mod.build(out)
    return out


@pytest.fixture
def seeded(workbook):
    seed.run_import(workbook)
    return workbook


def cid(number: str) -> str:
    return lifecycle.seed_uuid(number)


def claim(number: str) -> Claim:
    with SessionLocal() as db:
        return db.get(Claim, cid(number))


def count(model) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def as_member(c, member_id):
    """Link this test client's session to a member directly in the database (there is no public switch)."""
    c.get("/home")
    sid = c.cookies.get("proto_sid")
    with SessionLocal() as db:
        link = db.get(SessionLink, sid) or SessionLink(sid=sid)
        link.member_id, link.role = member_id, "member"
        db.merge(link)
        db.commit()


def live_claim(member_id, number, stage="employee_review", minutes_ago=0, decision=None, reason=None):
    """A submitted claim recorded by the app (not imported/sample data), created directly for list tests."""
    from datetime import timedelta
    now = utcnow() - timedelta(minutes=minutes_ago)
    with SessionLocal() as db:
        c = Claim(session_id="x" * 40, status="submitted", member_id=member_id, claim_number=number, reference=number,
                  category="outpatient", claimed_halalas=10000, currency="SAR", stage=stage, submitted_at=now,
                  decision=decision, decision_reason=reason, origin="app", check_round=1)
        db.add(c)
        db.flush()
        lifecycle.add_event(db, c, "submitted", "submitted", "member", member_id, at=now)
        db.commit()
        return c.id


# ------------------------------------------------------------------ workbook import


def test_workbook_opens_with_expected_sheets_and_english_only(workbook):
    wb = load_workbook(workbook)
    assert wb.sheetnames == ["Members", "Policies", "PolicyRules", "Claims", "Invoices", "Documents", "Checks",
                             "StatusHistory", "SLA", "TestCases", "DataDictionary"]
    arabic = re.compile(r"[؀-ۿ]")
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                assert not (isinstance(v, str) and arabic.search(v)), f"Arabic text in {ws.title}: {v}"


def test_import_is_validated_atomic_and_repeatable(seeded):
    assert count(Claim) == 12 and count(Member) == 2 and count(StatusEvent) == 36
    with pytest.raises(seed.ImportFailed):
        seed.run_import(seeded)  # same keys again without --upsert
    before = (count(Claim), count(InvoiceRecord), count(CheckResult), count(StatusEvent))
    seed.run_import(seeded, upsert=True)
    assert (count(Claim), count(InvoiceRecord), count(CheckResult), count(StatusEvent)) == before
    t1 = claim("CLM-DEMO-001")
    assert t1.claimed_halalas == 35000 and t1.stage == "auto_check" and t1.action_required
    with SessionLocal() as db:
        assert db.get(InvoiceRecord, "INVROW-001").total_halalas is None  # missing, not zero


def test_import_reports_row_errors_and_writes_nothing(workbook, tmp_path):
    bad = tmp_path / "bad.xlsx"
    shutil.copy(workbook, bad)
    wb = load_workbook(bad)
    claims = wb["Claims"]
    head = [c.value for c in claims[1]]
    claims.cell(row=3, column=head.index("claim_id") + 1, value="CLM-DEMO-001")  # duplicate key
    claims.cell(row=4, column=head.index("member_id") + 1, value="MEM-NOPE")  # broken foreign key
    claims.cell(row=5, column=head.index("claimed_amount_sar") + 1, value="abc")  # wrong type
    wb.save(bad)
    with pytest.raises(seed.ImportFailed) as exc:
        seed.run_import(bad, dry_run=True)
    text = "\n".join(exc.value.errors)
    assert "Claims row 5, claimed_amount_sar: expected a number" in text
    wb = load_workbook(bad)
    claims = wb["Claims"]
    claims.cell(row=5, column=head.index("claimed_amount_sar") + 1, value=800)
    wb.save(bad)
    with pytest.raises(seed.ImportFailed) as exc:
        seed.run_import(bad)
    text = "\n".join(exc.value.errors)
    assert "duplicate key 'CLM-DEMO-001'" in text and "'MEM-NOPE' not found in Members.member_id" in text
    assert count(Claim) == 0 and count(Member) == 0


def test_lifecycle_rules_in_workbook_are_enforced(workbook, tmp_path):
    bad = tmp_path / "rules.xlsx"
    shutil.copy(workbook, bad)
    wb = load_workbook(bad)
    claims = wb["Claims"]
    head = [c.value for c in claims[1]]
    # CLM-DEMO-009 (row 10) is approved but unpaid: claim it was paid without a payment time
    claims.cell(row=10, column=head.index("payout_status") + 1, value="paid")
    wb.save(bad)
    with pytest.raises(seed.ImportFailed) as exc:
        seed.run_import(bad, dry_run=True)
    assert any("payout_status 'paid' needs an approved decision and paid_at" in e for e in exc.value.errors)


def test_scenarios_t01_to_t08_from_actual_files(seeded):
    results = seed.verify(seeded)
    assert [r["test_id"] for r in results] == [f"T0{i}" for i in range(1, 9)]
    assert all(r["ok"] for r in results), results


# ------------------------------------------------------------------ ownership & roles


def test_member_sees_only_own_submitted_claims(seeded, client, other_client):
    a1 = live_claim("MEM-DEMO-01", "CLM-20261124-AAA001", minutes_ago=30)
    a2 = live_claim("MEM-DEMO-01", "CLM-20261124-AAA002", minutes_ago=5)
    b1 = live_claim("MEM-DEMO-02", "CLM-20261124-BBB001")
    page = client.get("/claims").text
    assert "CLM-20261124-AAA001" in page and "CLM-20261124-AAA002" in page and "CLM-20261124-BBB001" not in page
    assert page.index("AAA002") < page.index("AAA001")  # newest first
    assert "2 مطالبة مرسلة" in page
    assert not re.search(r"CLM-DEMO-\d{3}", page)  # imported test records are never listed or counted
    assert client.get(f"/claims/{b1}/track").status_code == 404
    with SessionLocal() as db:
        doc_id = db.get(Claim, cid("CLM-DEMO-012")).documents[0].id
    assert client.get(f"/documents/{doc_id}/file").status_code == 404
    as_member(other_client, "MEM-DEMO-02")
    page2 = other_client.get("/claims").text
    assert "CLM-20261124-BBB001" in page2 and "AAA001" not in page2
    assert other_client.get(f"/claims/{a1}/track").status_code == 404
    assert other_client.get(f"/claims/{a2}").status_code == 404


def test_admin_dashboard_is_removed(seeded, client):
    c4 = claim("CLM-DEMO-004")
    assert client.get("/staff").status_code == 404
    assert client.get(f"/staff/claims/{c4.id}").status_code == 404
    r = client.post(f"/staff/claims/{c4.id}/approve", data={"version": c4.version, "amount": "100"}, headers=FETCH)
    assert r.status_code in (404, 405)
    assert client.get("/demo").status_code == 404
    for page in ("/home", "/claims", "/claims/new", f"/claims/{c4.id}/track"):
        html = client.get(page).text
        assert "/staff" not in html and "Employee dashboard" not in html
    after = claim("CLM-DEMO-004")
    assert after.stage == "employee_review" and after.decision is None  # nothing decides automatically


def test_drafts_are_listed_separately_and_not_counted(seeded, client):
    from tests.conftest import png_bytes
    r = client.post("/claims/start", files=[("files", ("visit.png", png_bytes(), "image/png"))], headers=FETCH)
    draft_id = r.json()["redirect"].rsplit("/", 1)[1]
    client.post("/claims/sample")  # sample data is never listed
    page = client.get("/claims").text
    assert "0 مطالبة مرسلة" in page and "مسوداتي" in page and "متابعة الطلب" in page
    assert f'href="/claims/{draft_id}"' in page  # a draft continues the submission flow
    assert client.get(f"/claims/{draft_id}").status_code == 200


# ------------------------------------------------------------------ member tracking & completion


def test_needs_completion_is_not_a_rejection_and_resubmission_keeps_the_claim(seeded, client):
    t1 = claim("CLM-DEMO-001")
    page = client.get(f"/claims/{t1.id}/track").text
    assert "يحتاج مستندات" in page and "إجمالي الفاتورة غير ظاهر" in page and "استكمال المستندات" in page
    assert "تم الرفض" not in page and "سبب الرفض" not in page
    assert client.get(f"/claims/{t1.id}").status_code == 200  # editable while waiting for documents
    with SessionLocal() as db:  # the member re-uploads a complete invoice (synthetic file, test only)
        assert seed.attach_correction(db, db.get(Claim, t1.id)) is not None
        db.commit()
    r = client.post(f"/claims/{t1.id}/resubmit", data={"version": t1.version}, follow_redirects=False)
    assert r.headers["location"].endswith("/track")
    after = claim("CLM-DEMO-001")
    assert after.id == t1.id and after.claim_number == "CLM-DEMO-001"
    assert after.stage == "employee_review" and not after.action_required and after.review_due_at is not None
    with SessionLocal() as db:
        types = [e.event_type for e in lifecycle.events_of(db, t1.id)]
    assert types[:3] == ["submitted", "checks_started", "needs_completion"]  # history retained
    assert types[-2:] == ["resubmitted", "queued_for_review"]
    page = client.get(f"/claims/{t1.id}/track").text
    assert "قيد المعالجة" in page and "تقدير تجريبي" not in page
    # a repeated resubmission with the old version changes nothing
    client.post(f"/claims/{t1.id}/resubmit", data={"version": t1.version})
    with SessionLocal() as db:
        assert len(lifecycle.events_of(db, t1.id)) == len(types)


def test_paid_duplicate_alert_links_to_own_earlier_claim(seeded, client):
    page = client.get(f"/claims/{cid('CLM-DEMO-005')}/track").text
    assert "هذه الفاتورة مرتبطة بمطالبة تم تعويضها مسبقًا" in page and "CLM-DEMO-007" in page
    assert "طلب مراجعة" in page
    client.post(f"/claims/{cid('CLM-DEMO-005')}/request-review")
    client.post(f"/claims/{cid('CLM-DEMO-005')}/request-review")
    with SessionLocal() as db:
        n = sum(1 for e in lifecycle.events_of(db, cid("CLM-DEMO-005")) if e.event_type == "member_requested_review")
    assert n == 1
    assert claim("CLM-DEMO-005").stage == "employee_review"  # never auto-rejected
    pending = client.get(f"/claims/{cid('CLM-DEMO-010')}/track").text
    assert "توجد مطالبة قيد المراجعة لهذه الفاتورة" in pending and "تعويضها مسبقًا" not in pending


def test_tracker_shows_four_stages_with_alternative_outcomes(seeded, client):
    approved = client.get(f"/claims/{cid('CLM-DEMO-009')}/track").text
    assert "تمت الموافقة" in approved and "لم يُصرف المبلغ بعد" in approved and "160.00" in approved
    paid = client.get(f"/claims/{cid('CLM-DEMO-007')}/track").text
    assert "تم صرف المبلغ" in paid
    rejected = client.get(f"/claims/{cid('CLM-DEMO-008')}/track").text
    assert "تم الرفض" in rejected and "سبب الرفض" in rejected
    assert claim("CLM-DEMO-008").decision_reason in rejected  # the stored reason, never an invented one
    assert "سبب الرفض" not in approved and "تمت الموافقة" not in rejected
    assert approved.count('class="tl-step') == 4 and rejected.count('class="tl-step') == 4
    for label in ("تم الإرسال", "قيد المراجعة", "قيد المعالجة"):
        assert label in approved


# ------------------------------------------------------------------ employee actions


def test_final_decisions_are_validated_versioned_and_logged(seeded, client):
    """The employee-review and final-decision states remain in the model (no dashboard UI)."""
    with SessionLocal() as db:
        c4 = db.get(Claim, cid("CLM-DEMO-004"))
        v = c4.version
        for kwargs, code in ((dict(amount=Decimal("2500")), "amount_above_claimed"),
                             (dict(amount=Decimal("0")), "zero_needs_reason"),
                             (dict(amount=None), "amount_required")):
            with pytest.raises(lifecycle.DecisionError) as exc:
                lifecycle.approve(db, c4, v, "EMP-DEMO-01", **kwargs)
            assert exc.value.code == code
            db.rollback()
        with pytest.raises(lifecycle.DecisionError) as exc:
            lifecycle.reject(db, c4, v, "EMP-DEMO-01", " ")
        assert exc.value.code == "reason_required"
        db.rollback()
        lifecycle.approve(db, c4, v, "EMP-DEMO-01", Decimal("2160"))
        db.commit()
    with SessionLocal() as db:
        c4 = db.get(Claim, cid("CLM-DEMO-004"))
        with pytest.raises(lifecycle.DecisionError) as exc:  # a stale version can't overwrite the decision
            lifecycle.reject(db, c4, v, "EMP-DEMO-01", "late")
        assert exc.value.code == "version_conflict"
        db.rollback()
    done = claim("CLM-DEMO-004")
    assert done.decision == "approved" and done.approved_halalas == 216000 and done.payout_status == "unpaid"
    with SessionLocal() as db:
        last = lifecycle.events_of(db, done.id)[-1]
        assert (last.event_type, last.actor_role) == ("approved", "employee")
    page = client.get(f"/claims/{done.id}/track").text
    assert "تمت الموافقة" in page and "لم يُصرف المبلغ بعد" in page and "2,160.00" in page


def test_request_documents_returns_to_stage_two_and_pauses_the_clock(seeded, client):
    with SessionLocal() as db:
        c6 = db.get(Claim, cid("CLM-DEMO-006"))
        lifecycle.request_documents(db, c6, c6.version, "EMP-DEMO-01",
                                    "Please upload the optician's prescription.", ["medical_report"])
        db.commit()
    after = claim("CLM-DEMO-006")
    assert after.stage == "auto_check" and after.action_required and after.review_due_at is None
    page = client.get(f"/claims/{after.id}/track").text  # member view
    assert "يحتاج مستندات" in page and "optician" in page and "تم الرفض" not in page
    with SessionLocal() as db:
        types = [e.event_type for e in lifecycle.events_of(db, after.id)]
    assert types[-1] == "documents_requested" and "queued_for_review" in types


def test_customer_navigation_has_no_broken_links(seeded, client):
    """Follow every same-site link reachable from the customer pages."""
    import re as _re
    live_claim("MEM-DEMO-01", "CLM-20261124-NAV001")
    start = ["/home", "/claims", "/claims/new", f"/claims/{cid('CLM-DEMO-001')}/track",
             f"/claims/{cid('CLM-DEMO-007')}/track", f"/claims/{cid('CLM-DEMO-001')}"]
    seen, queue = set(), list(start)
    while queue:
        url = queue.pop(0)
        if url in seen or len(seen) > 120:
            continue
        seen.add(url)
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        for href in _re.findall(r'href="(/[^"#]*)', r.text):
            href = href.replace("&amp;", "&")
            if href.startswith(("/static/", "/lang/", "/documents/")) or href.endswith((".json",)):
                if href.startswith("/documents/"):
                    assert client.get(href).status_code == 200, href
                continue
            queue.append(href)
    assert "/claims/new" in seen and "/claims" in seen


def test_english_is_the_default_language(monkeypatch):
    import dataclasses
    from fastapi.testclient import TestClient

    import app.main as main
    from app.config import load_settings

    monkeypatch.setenv("DEFAULT_LANG", "en")
    assert load_settings().default_lang == "en"
    monkeypatch.setenv("DEFAULT_LANG", "xx")
    assert load_settings().default_lang == "en"
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, default_lang="en"))
    with TestClient(main.app) as c:
        page = c.get("/claims/new").text
        assert 'lang="en"' in page and "Add documents" in page and "Upload your invoice and proof of payment." in page
        c.get("/lang/ar?next=/claims/new")
        assert 'lang="ar"' in c.get("/claims/new").text


def test_static_urls_change_when_assets_change(seeded, client):
    import re as _re
    v = _re.search(r"app\.css\?v=([0-9a-f]{12})", client.get("/home").text).group(1)
    import app.main as main
    assert v == str(main.ASSET_V)


# ------------------------------------------------------------------ deterministic checks


def test_invoice_number_normalisation_keeps_leading_zeros():
    assert lifecycle.norm_invoice_number(" inv-٠٠12 / a ") == "INV0012A"
    assert lifecycle.norm_invoice_number("INV 0012A") == lifecycle.norm_invoice_number("inv-0012-a")
    assert lifecycle.norm_invoice_number("0012") != lifecycle.norm_invoice_number("12")


def test_duplicate_states_are_distinguished(seeded):
    with SessionLocal() as db:
        c5 = db.get(Claim, cid("CLM-DEMO-005"))
        dup = next(c for c in lifecycle.evaluate(db, c5) if c.check_type == "duplicate_check")
        assert dup.reason_code == "paid_duplicate"
        c7 = db.get(Claim, cid("CLM-DEMO-007"))
        c7.payout_status, c7.paid_at = "unpaid", None  # approved but not paid is not prior compensation
        db.flush()
        dup = next(c for c in lifecycle.evaluate(db, c5) if c.check_type == "duplicate_check")
        assert dup.reason_code == "prior_claim_not_paid"
        db.rollback()
        c11 = db.get(Claim, cid("CLM-DEMO-011"))
        dup = next(c for c in lifecycle.evaluate(db, c11) if c.check_type == "duplicate_check")
        assert dup.result == "pass" and dup.evidence["similar_date_and_amount"]  # same date + amount only


def test_policy_lookup_uses_service_date_and_seeded_rules(seeded):
    with SessionLocal() as db:
        m = db.get(Member, "MEM-DEMO-01")
        _, _, chk = lifecycle._policy_check(db, m, "outpatient", date(2025, 6, 1), 10000, "SAR", "DEMO-GENERAL")
        assert (chk.result, chk.reason_code) == ("manual_review", "no_active_policy")
        _, _, chk = lifecycle._policy_check(db, m, "physiotherapy", date(2026, 6, 1), 10000, "SAR", "DEMO-GENERAL")
        assert chk.reason_code == "category_unknown"
        _, _, chk = lifecycle._policy_check(db, m, "dental", date(2026, 6, 1), 10000, "SAR", "DEMO-EXCLUDED")
        assert chk.reason_code == "service_excluded" and chk.rule_version == "DEMO-V1"
        _, _, chk = lifecycle._policy_check(db, m, "dental", date(2026, 6, 1), 10000, "SAR", None)
        assert chk.reason_code == "service_code_unavailable"
        _, _, chk = lifecycle._policy_check(db, m, "optical", date(2026, 6, 1), 150000, "SAR", "DEMO-GENERAL")
        assert chk.reason_code == "policy_limit_review"
        _, _, chk = lifecycle._policy_check(db, m, "outpatient", date(2026, 6, 1), 35000, "SAR", "DEMO-GENERAL")
        assert chk.result == "pass" and chk.evidence["indicative_member_share_sar"] == "70.00"


def test_check_jobs_are_idempotent_on_retry(seeded):
    with SessionLocal() as db:
        c11 = db.get(Claim, cid("CLM-DEMO-011"))
        job = lifecycle.start_seeded_checks(db, c11, "EMP-DEMO-01")
        assert lifecycle.start_seeded_checks(db, c11, "EMP-DEMO-01") is None
        db.commit()
        job_id = job.id
    lifecycle.run_job(job_id)
    with SessionLocal() as db:
        results = db.scalar(select(func.count()).select_from(CheckResult).where(CheckResult.run_id == job_id))
        events = len(lifecycle.events_of(db, cid("CLM-DEMO-011")))
        db.get(CheckJob, job_id).status = "queued"  # simulate an interrupted run being resumed
        db.commit()
    lifecycle.run_job(job_id)
    lifecycle.run_job(job_id)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(CheckResult).where(CheckResult.run_id == job_id)) == results
        assert len(lifecycle.events_of(db, cid("CLM-DEMO-011"))) == events
        c11 = db.get(Claim, cid("CLM-DEMO-011"))
        assert c11.stage == "employee_review"  # T08: not treated as a duplicate


# ------------------------------------------------------------------ live submission path


def test_submission_starts_the_tracked_lifecycle(seeded, client):
    r = client.post("/claims/sample", follow_redirects=False)
    new_id = r.headers["location"].rsplit("/", 1)[-1]
    for fid in ("payment_receipt_en", "lab_result_ar"):
        client.post(f"/claims/{new_id}/sample-documents", data={"fixture_id": fid}, headers=FETCH)
    page = client.get(f"/claims/{new_id}/review").text
    token = re.search(r'name="submit_token" value="([^"]+)"', page).group(1)
    r = client.post(f"/claims/{new_id}/submit", data={"submit_token": token, "reviewed": "1"}, follow_redirects=False)
    assert r.headers["location"] == f"/claims/{new_id}/track?notice=submitted"
    c = claim_by_id(new_id)
    assert re.fullmatch(r"CLM-\d{8}-[0-9A-F]{6}", c.claim_number) and c.member_id == "MEM-DEMO-01"
    assert c.stage == "employee_review"  # sample identity differs from the member record → employee decides
    with SessionLocal() as db:
        types = [e.event_type for e in lifecycle.events_of(db, new_id)]
        codes = {r.reason_code for r in lifecycle.latest_checks(db, c)}
    assert types == ["submitted", "checks_started", "queued_for_review"]
    assert "identity_mismatch" in codes
    assert new_id not in client.get("/claims").text  # sample data is excluded from customer lists
    # submitting again with the spent token creates nothing new
    client.post(f"/claims/{new_id}/submit", data={"submit_token": token, "reviewed": "1"})
    with SessionLocal() as db:
        assert len(lifecycle.events_of(db, new_id)) == 3


def claim_by_id(claim_id: str) -> Claim:
    with SessionLocal() as db:
        return db.get(Claim, claim_id)


def test_splash_shows_once_then_home(seeded, client):
    first = client.get("/", follow_redirects=False)
    assert first.status_code == 200 and "مرحبًا بكم في بوبا العربية" in first.text
    again = client.get("/", follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/home"
    home = client.get("/home").text
    assert "قدم مطالبة" in home and "حالة المطالبات" in home and "Nora" in home
    assert "What should I attach" not in client.get("/claims/new").text


def test_reset_demo_restores_seeded_claims_but_keeps_live_ones(seeded, client):
    client.post("/claims/sample")  # a live draft created in the app
    with SessionLocal() as db:
        c4 = db.get(Claim, cid("CLM-DEMO-004"))
        lifecycle.approve(db, c4, c4.version, "EMP-DEMO-01", Decimal("100"))
        db.commit()
    live_before = count(Claim) - 12
    seed.reset_demo(seeded)
    assert claim("CLM-DEMO-004").stage == "employee_review"  # back to the workbook state
    assert count(Claim) - 12 == live_before and count(StatusEvent) == 36


# ------------------------------------------------------------------ customer-facing corrections


def test_no_public_demo_or_sample_access(seeded, client, monkeypatch):
    import dataclasses
    import app.main as main
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, test_fixture_routes=False))
    assert client.get("/demo").status_code == 404
    assert client.post("/demo/switch", data={"member_id": "MEM-DEMO-02"}).status_code in (404, 405)
    assert client.post("/claims/sample", follow_redirects=False).status_code == 404
    t1 = claim("CLM-DEMO-001")
    assert client.post(f"/claims/{t1.id}/sample-documents", data={"fixture_id": "payment_receipt_en"},
                       headers=FETCH).status_code == 404
    for url in ("/home", "/claims", "/claims/new", f"/claims/{t1.id}", f"/claims/{t1.id}/track"):
        html = client.get(url).text
        for word in ("/demo", "بيئة تجريبية", "بيانات تجريبية", "مستند تجريبي", "تقدير تجريبي", "Gemini",
                     "fixture", "data-source-doc", "demo-chip", ">Demo<", "Prototype", "النموذج التجريبي"):
            assert word not in html, (url, word)


def _sample_with_receipt(client):
    r = client.post("/claims/sample", follow_redirects=False)
    claim_id = r.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/claims/{claim_id}/sample-documents", data={"fixture_id": "payment_receipt_en"}, headers=FETCH)
    return claim_id


def test_extracted_details_collapsed_without_sources_and_edit_only_allowed_fields(seeded, client):
    claim_id = _sample_with_receipt(client)
    html = client.get(f"/claims/{claim_id}/workspace").text
    assert '<details class="card xd" id="extracted-details">' in html  # no "open": collapsed by default
    for word in ("مستخرج من", "data-source-doc", "source-link", "data-source-page", "fstate", "chip-"):
        assert word not in html, word  # no provenance badges, page references or field metadata
    edit_targets = re.findall(r'data-act="field" data-field="([^"]+)"', html)
    assert "claim__patient_name" in edit_targets and "claim__service_date" in edit_targets
    assert not any(t.startswith("doc__") for t in edit_targets) and "claim__provider_name" not in edit_targets
    assert 'id="edit-claim__patient_name"' in html and "edit-doc__" not in html
    assert html.count('type="reset" data-cancel-edit') == html.count('class="field-edit"')  # Save + Cancel


def test_payment_document_fields_are_read_only_on_the_server(seeded, client):
    claim_id = _sample_with_receipt(client)
    with SessionLocal() as db:
        c = db.get(Claim, claim_id)
        receipt = next(d for d in c.active_documents if d.fixture_id == "payment_receipt_en")
        invoice = next(d for d in c.active_documents if d.fixture_id == "outpatient_invoice_ar")
        before = receipt.extraction_json
    for key, value in ((f"doc:{receipt.id}:paid_amount", "1.00"), (f"doc:{receipt.id}:payment_date", "2026-01-01"),
                       (f"doc:{receipt.id}:transaction_reference", "X"), (f"doc:{invoice.id}:total", "1.00"),
                       ("claim:provider_name", "Someone else")):
        r = client.post(f"/claims/{claim_id}/fields", data={"field_key": key, "value": value}, headers=FETCH)
        assert r.status_code == 403 and r.json()["ok"] is False, key
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Correction).where(Correction.claim_id == claim_id)) == 0
    # an allowed patient/visit correction saves, reruns validation and leaves the receipt untouched
    r = client.post(f"/claims/{claim_id}/fields", data={"field_key": "claim:patient_name", "value": "Reem Khalid Almutairi"},
                    headers=FETCH)
    assert r.json()["ok"]
    html = client.get(f"/claims/{claim_id}/workspace").text
    assert "Reem Khalid Almutairi" in html
    with SessionLocal() as db:
        assert db.get(Document, receipt.id).extraction_json == before
        keys = [c.field_key for c in db.scalars(select(Correction).where(Correction.claim_id == claim_id))]
    assert keys == ["claim:patient_name"]


def test_patient_correction_reruns_validation(seeded, client):
    claim_id = _sample_with_receipt(client)
    client.post(f"/claims/{claim_id}/sample-documents", data={"fixture_id": "lab_result_ar"}, headers=FETCH)
    before = client.get(f"/claims/{claim_id}/workspace").text
    assert "is-ready" in before
    r = client.post(f"/claims/{claim_id}/fields", data={"field_key": "claim:service_date", "value": "2099-01-01"},
                    headers=FETCH)
    assert r.json()["ok"]
    after = client.get(f"/claims/{claim_id}/workspace").text
    assert "is-ready" not in after  # the future service date is now flagged by validation


def test_submitted_claims_open_tracking_and_drafts_continue(seeded, client):
    sub = live_claim("MEM-DEMO-01", "CLM-20261124-TRK001")
    page = client.get("/claims").text
    assert f'href="/claims/{sub}/track"' in page
    r = client.get(f"/claims/{sub}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/claims/{sub}/track"
    r = client.get(f"/claims/{sub}/submitted", follow_redirects=False)  # old confirmation address
    assert r.headers["location"] == f"/claims/{sub}/track"
    track = client.get(f"/claims/{sub}/track").text
    assert "حالة المطالبة" in track and "CLM-20261124-TRK001" in track and "قيد المعالجة" in track
    assert "تم إرسال مطالبتك" not in track  # the success note appears only right after submitting


def test_timeline_has_no_fictional_estimates_or_invented_outcomes(seeded, client):
    review = client.get(f"/claims/{cid('CLM-DEMO-004')}/track").text
    assert "الموعد المتوقع للرد" not in review  # imported SLA values are fictional and never shown
    assert "is-current" in review and review.count("is-done") == 2 and review.count("is-upcoming") == 1
    assert "موافقة أو رفض" in review and "تمت الموافقة" not in review and "تم الرفض" not in review
