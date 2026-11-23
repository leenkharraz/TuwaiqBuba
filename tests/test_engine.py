"""Aggregation, conflicts, grouping and conditional requirements (pure engine tests)."""
from decimal import Decimal

from app.present import issue_text
from tests.helpers import build, codes, invoice, medical, receipt

LAB_LINES = [
    {"description": "Consultation", "category": "consultation", "amount": "300.00"},
    {"description": "CBC", "category": "lab_test", "amount": "120.00"},
    {"description": "CRP", "category": "lab_test", "amount": "80.00"},
]


def test_related_documents_populate_one_claim():
    view = build([
        invoice(lines=LAB_LINES, total="575.00", subtotal="500.00", tax="75.00"),
        receipt(),
        medical("rx1", "prescription"),
        medical("lab1", "lab_result", meds=None),
    ])
    cf = view.claim_fields
    assert cf["patient_name"].value == "Reem Khalid Almutairi"
    assert cf["patient_name"].status == "extracted"
    assert cf["provider_name"].value == "Al Waha Medical Complex"
    assert cf["service_category"].value == "outpatient"
    assert cf["claim_location"].value == "domestic"
    assert cf["service_date"].value == "2026-11-16"
    assert cf["diagnosis"].value == "Acute pharyngitis (J02.9)"
    assert not view.proposals
    assert len(view.invoices) == 1
    assert set(view.primary_doc_ids) == {"inv1", "rcp1", "rx1", "lab1"}
    # every claim-level value keeps its source document
    assert {s.doc_id for s in cf["patient_name"].sources} >= {"inv1", "rx1"}
    assert view.ready, [i.code for i in view.blocking_issues]


def test_payment_receipt_does_not_inflate_total():
    view = build([invoice(total="575.00"), receipt(amount="575.00")])
    assert view.totals == {"SAR": Decimal("575.00")}
    assert view.paid_totals == {"SAR": Decimal("575.00")}
    assert view.invoices[0].payment_status == "proof_attached"
    assert len(view.invoices) == 1


def test_split_payment_receipts_are_summed_against_one_invoice():
    view = build([invoice(total="575.00"), receipt("r1", "300.00", txn="T1"), receipt("r2", "275.00", txn="T2")])
    assert view.totals == {"SAR": Decimal("575.00")}
    assert view.invoices[0].paid_total == Decimal("575.00")
    assert "payment_amount_mismatch" not in codes(view)


def test_missing_proof_of_payment_produces_specific_request():
    view = build([invoice(number="INV-24-10871")])
    issue = next(i for i in view.issues if i.code == "missing_proof_of_payment")
    assert issue.blocking
    assert issue.params["invoice"] == "INV-24-10871"
    assert any(a.kind == "add_document" and a.doc_type == "payment_receipt" for a in issue.actions)
    assert "يرجى إرفاق إثبات السداد لهذه الفاتورة" in issue_text(issue, "ar")
    assert "INV-24-10871" in issue_text(issue, "ar")
    # an invoice on its own is never treated as paid
    assert view.invoices[0].payment_status == "no_proof"
    assert view.paid_totals == {}
    assert not view.ready


def test_conflicting_amounts_require_review_and_are_preserved():
    view = build([invoice(total="575.00"), receipt(amount="500.00")])
    issue = next(i for i in view.issues if i.code == "payment_amount_mismatch")
    assert issue.blocking and issue.acknowledgeable
    assert issue.params["paid"] == "500.00" and issue.params["total"] == "575.00"
    assert not view.ready
    # the claimed total is still the invoice total, the payment is kept separately
    assert view.totals == {"SAR": Decimal("575.00")}
    assert view.paid_totals == {"SAR": Decimal("500.00")}

    # member confirms the documents really show different amounts -> unblocked, still recorded
    acked = build([invoice(total="575.00"), receipt(amount="500.00")], acks=[issue.key])
    assert "payment_amount_mismatch" not in codes(acked, blocking_only=True)
    assert any(i.acknowledged for i in acked.issues)

    # or the member corrects the misread amount -> conflict disappears, original value kept
    fixed = build([invoice(total="575.00"), receipt(amount="500.00")], corrections={"doc:rcp1:paid_amount": "575.00"})
    assert "payment_amount_mismatch" not in codes(fixed)
    fld = fixed.payments[0].fields["paid_amount"]
    assert fld.status == "corrected" and fld.original_value == "500.00" and fld.value == "575.00"


def test_unrelated_visits_are_not_silently_merged():
    dental = invoice("dent1", number="DN-7781", total="586.50", date_="2026-11-09", category="dental",
                     provider="Ibtisama Dental Clinic", pid="D-5521",
                     lines=[{"description": "Filling", "category": "dental_procedure", "tooth_number": "36", "amount": "586.50"}])
    view = build([invoice(), receipt(), dental])
    assert len(view.proposals) == 1
    prop = view.proposals[0]
    assert prop.kind == "separate"
    assert "dent1" in prop.doc_ids
    assert set(prop.reasons) >= {"different_service_type", "different_visit_date"}
    # the unrelated invoice does not contribute to totals or claim fields
    assert view.totals == {"SAR": Decimal("575.00")}
    assert "separate_claim_proposed" in codes(view, blocking_only=True)
    assert not view.ready


def test_member_can_confirm_same_visit_for_ambiguous_invoice():
    second = invoice("inv2", number="INV-2", total="100.00", date_=None)
    view = build([invoice(), receipt(), second])
    assert view.proposals and view.proposals[0].kind == "pending"
    assert "grouping_unclear" in codes(view, blocking_only=True)
    second_confirmed = invoice("inv2", number="INV-2", total="100.00", date_=None, grouping_decision="same_visit")
    view2 = build([invoice(), receipt(), second_confirmed])
    assert not view2.proposals
    assert view2.totals == {"SAR": Decimal("675.00")}


def test_removed_document_no_longer_contributes():
    with_receipt = build([invoice(), receipt()])
    assert "missing_proof_of_payment" not in codes(with_receipt)
    without = build([invoice()])  # the engine only sees active documents
    assert "missing_proof_of_payment" in codes(without)
    assert without.paid_totals == {}


def test_member_correction_survives_additional_documents():
    corr = {"claim:patient_name": "Reem K. Almutairi"}
    ctx = {"claim:patient_name": ["reem khalid almutairi"]}
    before = build([invoice()], corrections=corr, context=ctx)
    assert before.claim_fields["patient_name"].value == "Reem K. Almutairi"
    after = build([invoice(), receipt(), medical()], corrections=corr, context=ctx)
    fld = after.claim_fields["patient_name"]
    assert fld.value == "Reem K. Almutairi" and fld.status == "corrected"
    # a genuinely new conflicting value is flagged but does not overwrite the correction
    other = medical("rx2", patient="Sara Ahmed Alharbi", pid=None)
    flagged = build([invoice(), other], corrections=corr, context=ctx)
    assert flagged.claim_fields["patient_name"].value == "Reem K. Almutairi"


def test_conditional_lab_requirement_only_when_tests_performed():
    no_lab = build([invoice(), receipt()])
    req = next(r for r in no_lab.requirements if r.id == "lab_results")
    assert req.status == "not_triggered"
    assert "conditional_document_missing" not in codes(no_lab)

    with_lab_line = build([invoice(lines=LAB_LINES), receipt()])
    issue = next(i for i in with_lab_line.issues if i.code == "conditional_document_missing")
    assert issue.blocking and issue.acknowledgeable
    assert issue.params["trigger"] == "CBC"

    attached = build([invoice(lines=LAB_LINES), receipt(), medical("lab1", "lab_result", meds=None)])
    assert next(r for r in attached.requirements if r.id == "lab_results").status == "satisfied"

    acknowledged = build([invoice(lines=LAB_LINES), receipt()], acks=[issue.key])
    assert acknowledged.ready
    assert next(r for r in acknowledged.requirements if r.id == "lab_results").status == "acknowledged"


def test_prescription_must_mention_diagnosis_when_medication_prescribed():
    view = build([invoice(), receipt(), medical("rx1", diagnosis=None, meds=True)])
    assert "requirement_field_missing" in codes(view, blocking_only=True)
    ok = build([invoice(), receipt(), medical("rx1", meds=True)])
    assert next(r for r in ok.requirements if r.id == "prescription_with_diagnosis").status == "satisfied"


def test_dental_imaging_is_optional_not_blocking():
    dental = invoice(category="dental", lines=[{"description": "Filling", "category": "dental_procedure",
                                                "tooth_number": "36", "amount": "575.00"}])
    view = build([dental, receipt()])
    req = next(r for r in view.requirements if r.id == "dental_imaging")
    assert req.status == "optional"
    assert view.ready


def test_different_currencies_are_never_summed():
    usd = invoice("inv2", number="INV-2", total="100.00", currency="USD", grouping_decision="same_visit")
    view = build([invoice(), receipt(), usd, receipt("r2", "100.00", ref="INV-2", currency="USD", txn="T2")])
    assert view.totals == {"SAR": Decimal("575.00"), "USD": Decimal("100.00")}
    assert "multiple_currencies" in codes(view)


def test_duplicate_invoice_is_counted_once():
    view = build([invoice(), invoice("inv1-photo"), receipt()])
    assert view.totals == {"SAR": Decimal("575.00")}
    assert "duplicate_invoice" in codes(view)
    assert view.all_invoices["inv1-photo"].duplicate_of == "inv1"


def test_names_in_two_scripts_match_by_file_number_only():
    arabic_inv = invoice(patient="ريم خالد المطيري")
    matched = build([arabic_inv, receipt(), medical()])
    assert matched.claim_fields["patient_name"].status == "extracted"
    unmatched = build([invoice(patient="ريم خالد المطيري"), receipt(), medical(pid=None)])
    assert "patient_name_different_scripts" in codes(unmatched, blocking_only=True)


def test_location_is_not_inferred_from_currency():
    view = build([invoice(country=None), receipt()])
    assert view.claim_fields["claim_location"].status == "missing"
    assert "location_needed" in codes(view, blocking_only=True)


def test_invoice_arithmetic_mismatch_needs_review():
    view = build([invoice(total="600.00", subtotal="500.00", tax="75.00"), receipt(amount="600.00")])
    assert "invoice_arithmetic" in codes(view, blocking_only=True)


def test_unreadable_and_unavailable_documents_block_with_actions():
    from tests.helpers import _doc
    blurry = _doc("blur", {"document_type": "other", "readability": "unreadable"}, status="unreadable")
    view = build([invoice(), receipt(), blurry])
    issue = next(i for i in view.issues if i.code == "document_unreadable")
    assert issue.blocking and {a.kind for a in issue.actions} >= {"retake_photo", "replace_document"}
