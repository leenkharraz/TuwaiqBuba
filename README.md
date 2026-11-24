# Bupa Discovery Program: document-first reimbursement claim (prototype)

A working FastAPI web application for the Bupa Arabia Discovery Program reimbursement challenge.
Members photograph or upload their medical documents. The app reads them, combines related
information into one structured claim, flags anything missing or inconsistent, lets the member
review and correct every value, and saves the claim locally with its attachments and
structured JSON.

> **Prototype notice.** Claims are saved to this local application only. Nothing is sent to Bupa,
> and "ready" means only that the prototype's own document and consistency checks passed. It does
> not establish coverage, authenticity, medical necessity or reimbursement. The document
> checklist rules are adapted from the supplied references, not Bupa's complete official rules,
> and the JSON is a provisional integration schema, not Bupa's internal claims schema.

---

## Quick start

Requires Python 3.9 or newer (3.11+ recommended). The app was built and tested on macOS with
Python 3.9.6.

### macOS / Linux

```bash
cd bupa-claims-prototype
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then put your key in .env (see below)
uvicorn app.main:app --reload --port 8000
```

### Windows (PowerShell)

```powershell
cd bupa-claims-prototype
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env     # then put your key in .env (see below)
uvicorn app.main:app --reload --port 8000
```

Open **http://127.0.0.1:8000/**.

### Gemini configuration (live extraction)

Edit `.env` in the project root:

```dotenv
GEMINI_API_KEY=<your key from Google AI Studio>
GEMINI_MODEL=gemini-3.6-flash
GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite
```

- The key is read only by the server and never sent to the browser, rendered in pages or logged.
  `.env` is git-ignored.
- **Restart the server after editing `.env`.** Settings are read at startup, so a running server
  keeps showing "automatic reading is unavailable" until restarted.
- A non-empty shell variable named `GEMINI_API_KEY` overrides `.env`. An empty one does not.
- Transient `429`/`503` responses are retried with backoff. If the main model is overloaded or
  out of quota, the models in `GEMINI_FALLBACK_MODELS` are tried in order, and the model that
  actually read each document is recorded.
- If extraction still fails, the document shows the honest reason (for example "quota used up"
  or "temporarily unavailable") and a **Retry** button. No values are ever filled in for it.

Without a key the app still runs. Use **Try with sample documents** (sample mode).

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

## Demo walkthrough (about 3 minutes, no API key needed)

1. Open the home page. It is Arabic RTL by default; the **English** button switches language.
2. Tap **تجربة بمستندات تجريبية** (Try with sample documents). A sample claim opens with a
   synthetic Arabic/English outpatient invoice and an English PDF prescription.
3. **What does the claim still need?** shows two blocking items:
   - *Please attach proof of payment for this invoice (INV-24-10871).*
   - *Lab results (if performed)*: triggered because the invoice bills a CBC test.
4. Under **Claim details**, each value shows its source ("From the invoice"). Tap a source to
   see the document page and the supporting excerpt. The Arabic and English patient names are
   matched by the shared file number.
5. From the **Sample documents** panel, add **Card payment slip — 575.00**. It links to the
   invoice by the invoice number printed on the slip. The claim total stays **575.00 SAR**
   (receipts never add to the total).
6. Add **Lab results**. The checklist turns green and **Continue to summary** becomes active.
7. Optional edge cases to try:
   - **Receipt with a different amount**: paid vs total mismatch. Both values are kept, and the
     member can correct either one or confirm the documents really differ.
   - **Dental invoice from another visit**: proposed as a separate claim, with the reasons
     (different service type, date and provider). Tap **Move to a separate claim**.
   - **Blurry receipt photo**: unreadable, with **Retake** and **Replace**.
   - **Second photo of the same invoice**: detected as a duplicate and counted once.
   - Edit any field. The original extracted value is kept and shown, and the correction survives
     later uploads. Removing or replacing a document withdraws its values and its corrections.
8. On the **summary**, tick the confirmation and save. The confirmation reads
   **"تم حفظ المطالبة في النموذج التجريبي"** with a local reference, a **JSON download**, and a
   **print / save as PDF** view.

For real extraction, add your Gemini key and upload your own photos or PDFs. The synthetic
files in `fixtures/samples/` are also good test uploads. Uploaded normally (not from the sample
panel), they are read by Gemini, never matched to the prepared sample data.

---

## What it does

| Step | Implementation |
|---|---|
| 1. Add documents | Camera (in-page rear camera with sequential shots, or the device camera as fallback), multi-file upload, add more later, remove, replace, retake. JPG/JPEG/PNG/PDF, 5 MB per file (configurable), PDF page cap, duplicate-file detection. Oversized phone photos and HEIC are converted to JPEG in the browser. No need to choose a document category first. |
| 2. Extract and review | Gemini reads each file with an extraction-only prompt and a strict schema. Type is detected automatically (invoice, payment receipt, medical report, prescription, lab, imaging, other) and can be corrected. Every value keeps its document, page and supporting excerpt. States are **Extracted**, **Needs review**, **Not found**, **Corrected by you** and **Confirmed by you**, with no invented confidence scores. Corrections are stored separately from the original extraction. |
| 3. Combine and validate | Groups documents by patient, visit/admission and service type. Unrelated or unclear invoices are proposed as separate claims or held for confirmation, never merged silently. Receipts link to invoices (by printed invoice number, matching amount and provider, or member choice) and never add to the total. Decimal money, totals per currency (never mixed), duplicate invoice and payment detection, arithmetic checks, and only like-for-like comparisons. Disagreements are preserved as candidates. |
| Checklists | `app/rules/claim_rules.json`: outpatient, dental, inpatient, physiotherapy, optical and medication. Conditions: `always`, `per_invoice`, `if_triggered` (by billed line categories or document flags, e.g. lab tests, imaging, medication, implants), `if_available`, `reminder`. Every blocking issue has an action: add document, retake, review field, resolve mismatch, confirm grouping, link payment, or acknowledge where that is reasonable. |
| 4. Summary and save | Server-side revalidation, review confirmation, one-time submit token plus an atomic status update (no duplicate saves), immutable snapshot, local reference (`DP-YYYYMMDD-XXXXXX`), JSON download, print view. No approval decision is shown. |

### Structured output

`GET /claims/{id}/export.json` (after saving), with a live preview on the summary page. It
includes `schema_version`, claim ID/reference/status, patient, provider, service, invoices
(with line items and payment status), payments (marked `counts_toward_claimed_total: false`),
services, per-currency totals, attachments (sha256, type, extraction mode/model), flat
`source_references`, the full correction history (original value, member value, sources,
superseded flag), validation (checklist, unresolved, acknowledged, warnings) and grouping
decisions.

### Design

Arabic-first RTL with a working English switch. IBM Plex Sans Arabic, white rounded cards,
pill buttons, and the facilitator HTML's palette (used as reference colours, not as an official
brand manual), with darker text tones for AA contrast. The Bupa logo appears only inside the
supplied claim-form PDF and no standalone asset was supplied, so a plain text wordmark is used.

- **Mobile:** single column; on phones "what's missing" moves up next to the upload card. Large
  camera/upload buttons, expandable details, and a sticky bottom action with safe-area padding.
  Checked for horizontal overflow at a 390 px viewport.
- **Desktop:** documents on one side, claim details and checks on the other, in RTL order.
- **Footer:** "نموذج تجريبي لبرنامج بوبا ديسكفري".

### Privacy and safety

- Files are stored under `DATA_DIR` (default `./data`) with random names, outside the static
  directory. The SQLite database (`data/claims.db`) holds metadata only.
- Files are validated by content (magic bytes plus Pillow/pypdf), size and page count; encrypted
  PDFs are rejected.
- Every claim and document is scoped to a random HttpOnly session cookie. Other sessions get 404.
- Security headers: CSP without inline scripts or styles, `nosniff`, `no-store` on private files,
  and a same-origin check on POST requests.
- Document text is treated as untrusted data. The prompt is extraction-only, the output is
  schema-constrained and re-validated, and unknown keys are dropped.
- Logs contain document IDs and status codes only, never document content or keys.
- Removing or replacing a document deletes its file and withdraws its values. Results that
  arrive after removal are discarded.

### Camera and HTTPS

Browsers allow the in-page camera only on **HTTPS or `localhost`**. On `http://127.0.0.1:8000`
it works on the same computer. When you open the app from a phone over your LAN
(`uvicorn app.main:app --host 0.0.0.0 --port 8000`, then `http://<your-computer-ip>:8000`), the
page is not a secure context, so **Take a photo** falls back to the phone's own camera through
the file picker. That fallback still works. To use the in-page camera on a phone, serve over
HTTPS:

```bash
# option A: a tunnel (prints an https URL you can open on the phone)
cloudflared tunnel --url http://localhost:8000      # or: ngrok http 8000
# option B: local certificate (e.g. created with mkcert)
uvicorn app.main:app --host 0.0.0.0 --port 8443 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Set `SECURE_COOKIES=true` in `.env` when serving over HTTPS. Camera streams are stopped when the
dialog closes, when the tab is hidden, and when the page unloads. Permission denial shows guidance
and the device-camera fallback.

---

## Project layout

```
app/
  main.py            routes (pages, uploads, corrections, grouping, submit, private files)
  engine.py          pure claim engine: aggregation, grouping, payment linking, validation
  rules/             claim_rules.json (editable checklists) + loader
  extraction/        gemini.py (SDK client, retries, fallbacks), prompt.py, samples.py
  schemas.py         Pydantic extraction schema (sent to Gemini and used for validation)
  normalize.py       Decimal amounts, ISO dates, currencies, name/provider/ID matching
  services.py        storage <-> engine glue, background extraction
  export.py          provisional JSON schema
  storage.py         private file storage, validation, thumbnails, PDF page rendering
  i18n.py            Arabic/English strings
  templates/, static/
fixtures/samples/    synthetic sample documents + prepared extraction JSON + manifest
scripts/generate_fixtures.py   regenerates the synthetic documents
references/          supplied screenshots, facilitator HTML and claim form (local only, git-ignored)
tests/               engine, API and normalisation tests
```

Replacing the rules: edit `app/rules/claim_rules.json` (categories → requirements with `when`,
`doc_types`, optional `trigger.line_categories` / `trigger.flags`, `requires_field`). The engine
reads only this structure.

---

## Limitations and unfinished items

- **QR/barcode decoding is not implemented.** It was optional and left out to keep the core flow
  solid.
- **Live extraction quality is unmeasured.** The Gemini integration (SDK call, schema,
  validation, retries and fallbacks) is implemented and covered by mocked tests. A small
  structured live call succeeded during development, but full-document live runs were blocked at
  build time by Google returning `429 quota exceeded` and `503 high demand` for the provided key.
  The app reports those states honestly. Accuracy on real Arabic photos still needs testing with
  real users.
- One document type per file. A PDF that contains several invoices is treated as one document.
- Hijri or ambiguous day/month dates are flagged for review, not converted.
- No user accounts: access is scoped to a browser session cookie. Data at rest is not encrypted.
  This is not production-ready for real personal health information.
- Fonts load from Google Fonts, with system Arabic fonts as fallback offline.
- The 90-day service window from the claim form is shown as a warning only.

---

## Claim tracking and test data

### Load or reset the test data

```bash
pip install -r requirements.txt                 # adds openpyxl
python scripts/build_demo_workbook.py           # writes data/Bupa_Prototype_Data.xlsx + data/demo_fixtures/*.pdf
python -m app.seed import --dry-run             # validate only
python -m app.seed import                       # atomic import (use --upsert to refresh)
python -m app.seed verify                       # run the automatic checker on T01–T08 from the actual files
python -m app.seed reset-demo                   # restore the seeded claims; live app claims are kept
                                                # (imported claims are test data and never listed to customers)
```

The workbook, its documents, policy rules, limits and time estimates are **fictional test data** (English
only). The database schema migrates automatically at start-up (`app/db.py`, schema version 2).

### Screens

- `/` welcome screen (once per browser session) → `/home` insurance home.
- `قدم مطالبة` → `/claims/new` (the existing upload, extraction and review flow).
- `حالة المطالبات` → `/claims`: `مطالباتي` (submitted claims, newest first) and `مسوداتي` (drafts).
- `/claims/{id}/track`: four stages (Submitted, Document Verification, Employee Review, Final Decision).
  Missing documents pause stage 2 as **Needs Documents**; the member completes them and resubmits under
  the same claim number. Only an employee approves or rejects. Approved ≠ paid.
There is no employee dashboard and no public demo page. Claims that reach Under Processing stay there:
the model keeps the employee-review and final-decision states, and nothing is approved or rejected
automatically. Imported workbook claims and sample-document claims are excluded from customer lists.
The synthetic sample-document routes exist only for the automated tests
(`ENABLE_TEST_FIXTURE_ROUTES=1`, set by `tests/conftest.py`).

The interface opens in English by default (`DEFAULT_LANG=en`); Arabic (RTL) is one tap away, or set
`DEFAULT_LANG=ar`.
