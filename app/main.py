"""FastAPI application: member reimbursement journey (prototype)."""
from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import export, services, storage
from .config import settings
from .db import get_db, init_db
from .engine import CATEGORIES, DOC_FIELD_KINDS, LOCATIONS, DocCtx, Field, build_claim_view, parse_kind
from .extraction.samples import get_fixture, list_fixtures, samples_available
from .i18n import LANGS, resolve, t
from .models import Acknowledgement, Claim, Correction, Document, utcnow
from .normalize import fmt_date, fmt_money, norm_text
from .present import display_value, field_label, issue_html, issue_text, source_label
from .rules import get_rules
from .schemas import DOC_TYPES

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("claims")

APP_DIR = Path(__file__).resolve().parent
SESSION_COOKIE = "proto_sid"
LANG_COOKIE = "lang"
_SID_RE = re.compile(r"^[A-Za-z0-9_\-]{32,64}$")
_UUID_RE = re.compile(r"^[0-9a-f\-]{36}$")
CLAIM_FIELDS = ("patient_name", "patient_identifier", "provider_name", "service_category",
                "claim_location", "service_date", "diagnosis")
CLAIM_FIELD_KINDS = {"patient_name": "text", "patient_identifier": "ident", "provider_name": "text",
                     "service_category": "category", "claim_location": "location", "service_date": "date",
                     "diagnosis": "text"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    storage.ensure_dirs()
    services.recover_interrupted()
    if not settings.live_extraction_configured:
        log.info("Live extraction not configured (missing %s). Sample mode is available.",
                 ", ".join(settings.missing_live_config))
    else:
        log.info("Live extraction configured with model %s", settings.gemini_model)
    yield


app = FastAPI(title="Bupa Discovery reimbursement prototype", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; "
    "object-src 'none'"
)


@app.middleware("http")
async def session_and_security(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != "null":
            host = request.headers.get("host", "")
            if urlparse(origin).netloc != host:
                return JSONResponse({"ok": False, "message": "cross-origin request blocked"}, status_code=403)
    sid = request.cookies.get(SESSION_COOKIE) or ""
    new_sid = not _SID_RE.match(sid)
    if new_sid:
        sid = secrets.token_urlsafe(32)
    request.state.sid = sid
    lang = request.cookies.get(LANG_COOKIE)
    request.state.lang = lang if lang in LANGS else "ar"
    response = await call_next(request)
    if new_sid:
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax",
                            secure=settings.secure_cookies, max_age=14 * 86400)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ------------------------------------------------------------------ helpers


def lang_of(request: Request) -> str:
    return getattr(request.state, "lang", "ar")


def is_fetch(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def get_claim(db: Session, request: Request, claim_id: str) -> Claim:
    if not _UUID_RE.match(claim_id or ""):
        raise HTTPException(404)
    claim = db.get(Claim, claim_id)
    if claim is None or claim.session_id != request.state.sid:
        raise HTTPException(404)
    return claim


def get_doc(db: Session, request: Request, doc_id: str) -> Document:
    if not _UUID_RE.match(doc_id or ""):
        raise HTTPException(404)
    doc = db.get(Document, doc_id)
    if doc is None or doc.claim.session_id != request.state.sid:
        raise HTTPException(404)
    return doc


def require_draft(claim: Claim) -> None:
    if claim.status != "draft":
        raise HTTPException(409, detail="claim_locked")


def respond(request: Request, claim_id: Optional[str] = None, ok: bool = True, message: Optional[str] = None,
            redirect: Optional[str] = None, status: int = 200, **extra: Any) -> Response:
    if is_fetch(request):
        body = {"ok": ok, "message": message, "redirect": redirect}
        body.update(extra)
        return JSONResponse(body, status_code=status)
    target = redirect or (f"/claims/{claim_id}" if claim_id else "/")
    return RedirectResponse(target, status_code=303)


def field_name_of(key: str) -> str:
    return key.rsplit(":", 1)[-1]


def base_context(request: Request, **kw: Any) -> Dict[str, Any]:
    lang = lang_of(request)
    ctx = {
        "request": request,
        "lang": lang,
        "dir": "rtl" if lang == "ar" else "ltr",
        "t": partial(t, lang=lang),
        "resolve": lambda v: resolve(v, lang),
        "live_ok": settings.live_extraction_configured,
        "live_missing": ", ".join(settings.missing_live_config),
        "max_mb": storage._fmt_mb(settings.max_upload_mb),
        "max_bytes": settings.max_upload_bytes,
        "samples_available": samples_available(),
        "fmt_money": fmt_money,
        "fmt_date": fmt_date,
        "field_label": lambda n: field_label(n, lang),
        "field_name": field_name_of,
        "display_value": lambda f: display_value(f, lang),
        "source_label": lambda s: source_label(s, lang),
        "issue_text": lambda i: issue_text(i, lang),
        "issue_html": lambda i: issue_html(i, lang),
        "categories": CATEGORIES,
        "locations": LOCATIONS,
        "doc_types": DOC_TYPES,
        "current_path": request.url.path,
        "js_strings": {
            "error": t("msg.error", lang),
            "network": t("msg.network", lang),
            "uploading": t("upload.uploading", lang),
            "rejected": t("upload.rejected", lang, filename="{filename}", reason="{reason}"),
            "unsupported": t("upload.error.unsupported_type", lang),
            "tooLarge": t("upload.error.too_large", lang, mb=storage._fmt_mb(settings.max_upload_mb)),
            "cameraStarting": t("camera.starting", lang),
        },
    }
    ctx.update(kw)
    return ctx


def session_claims(db: Session, request: Request) -> List[Claim]:
    return list(db.scalars(select(Claim).where(Claim.session_id == request.state.sid)
                           .order_by(Claim.created_at.desc())))


def workspace_context(request: Request, db: Session, claim: Claim) -> Dict[str, Any]:
    view = services.build_view(claim)
    docs = {d.id: d for d in claim.active_documents}
    fixtures = list_fixtures() if claim.is_sample else []
    used_fixtures = {d.fixture_id for d in claim.active_documents if d.fixture_id}
    others = [c for c in session_claims(db, request) if c.id != claim.id]
    needed_types = []
    for i in view.blocking_issues:
        for a in i.actions:
            if a.kind == "add_document" and a.doc_type and a.doc_type not in needed_types:
                needed_types.append(a.doc_type)
    step = 1
    if docs:
        step = 3 if (not view.processing and not view.blocking_issues) else 2
    return base_context(
        request, claim=claim, view=view, docs=docs, fixtures=fixtures, used_fixtures=used_fixtures,
        other_claims=others, needed_types=needed_types, step=step,
        version=services.state_version(claim), rules=get_rules(),
    )


# ------------------------------------------------------------------ pages


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "index.html", base_context(
        request, claims=session_claims(db, request), step=1))


@app.get("/lang/{code}")
def switch_lang(code: str, request: Request, next: str = "/"):
    if code not in LANGS:
        code = "ar"
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(LANG_COOKIE, code, samesite="lax", max_age=365 * 86400, secure=settings.secure_cookies)
    return resp


@app.get("/claims/{claim_id}", response_class=HTMLResponse)
def claim_page(claim_id: str, request: Request, db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    if claim.status == "submitted":
        return RedirectResponse(f"/claims/{claim.id}/submitted", status_code=303)
    return templates.TemplateResponse(request, "claim.html", workspace_context(request, db, claim))


@app.get("/claims/{claim_id}/workspace", response_class=HTMLResponse)
def claim_workspace(claim_id: str, request: Request, db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    return templates.TemplateResponse(request, "partials/workspace.html", workspace_context(request, db, claim))


@app.get("/claims/{claim_id}/state")
def claim_state(claim_id: str, request: Request, db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    processing = any(d.status in ("queued", "processing") for d in claim.active_documents)
    return {"version": services.state_version(claim), "processing": processing, "status": claim.status}


# ------------------------------------------------------------------ uploads


async def _ingest(request: Request, db: Session, claim: Claim, files: List[UploadFile], source: str):
    lang = lang_of(request)
    accepted, rejected = [], []
    for f in files:
        name = f.filename or "document"
        data = await f.read(settings.max_upload_bytes + 1)
        try:
            doc = services.add_uploaded_document(db, claim, name, data, source)
            accepted.append(doc.id)
            db.flush()
            db.refresh(claim)
        except storage.UploadRejected as exc:
            reason = t(f"upload.error.{exc.code}", lang, **exc.params)
            rejected.append({"filename": storage.safe_display_name(name), "reason": reason,
                             "message": t("upload.rejected", lang, filename=storage.safe_display_name(name), reason=reason)})
    return accepted, rejected


def _source_of(value: str) -> str:
    return "camera" if value == "camera" else "upload"


@app.post("/claims/start")
async def start_claim(request: Request, files: List[UploadFile] = File(default=[]), source: str = Form("upload"),
                      db: Session = Depends(get_db)):
    lang = lang_of(request)
    files = [f for f in files if f.filename]
    if not files:
        return respond(request, ok=False, message=t("upload.error.no_files", lang), status=400)
    claim = Claim(session_id=request.state.sid)
    db.add(claim)
    db.flush()
    accepted, rejected = await _ingest(request, db, claim, files, _source_of(source))
    if not accepted:
        db.rollback()
        return respond(request, ok=False, message=rejected[0]["message"] if rejected else None,
                       status=400, rejected=rejected)
    db.commit()
    services.schedule_extraction([d for d in accepted if db.get(Document, d).status == "queued"])
    return respond(request, claim.id, redirect=f"/claims/{claim.id}", accepted=accepted, rejected=rejected)


@app.post("/claims/sample")
def start_sample_claim(request: Request, db: Session = Depends(get_db)):
    fixtures = [f for f in list_fixtures() if f.starter]
    if not fixtures:
        raise HTTPException(404)
    claim = Claim(session_id=request.state.sid, is_sample=True)
    db.add(claim)
    db.flush()
    for f in fixtures:
        services.add_fixture_document(db, claim, f)
        db.refresh(claim)
    db.commit()
    return respond(request, claim.id, redirect=f"/claims/{claim.id}")


@app.post("/claims/{claim_id}/documents")
async def upload_documents(claim_id: str, request: Request, files: List[UploadFile] = File(default=[]),
                           source: str = Form("upload"), db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    require_draft(claim)
    lang = lang_of(request)
    files = [f for f in files if f.filename]
    if not files:
        return respond(request, claim.id, ok=False, message=t("upload.error.no_files", lang), status=400)
    accepted, rejected = await _ingest(request, db, claim, files, _source_of(source))
    db.commit()
    services.schedule_extraction([d for d in accepted if db.get(Document, d).status == "queued"])
    msg = t("upload.added", lang, n=len(accepted)) if accepted else None
    return respond(request, claim.id, ok=bool(accepted), message=msg, accepted=accepted, rejected=rejected,
                   status=200 if accepted else 400)


@app.post("/claims/{claim_id}/sample-documents")
def add_sample_document(claim_id: str, request: Request, fixture_id: str = Form(...), db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    require_draft(claim)
    if not claim.is_sample:
        raise HTTPException(400)
    fixture = get_fixture(fixture_id)
    if fixture is None:
        raise HTTPException(404)
    try:
        services.add_fixture_document(db, claim, fixture)
    except storage.UploadRejected as exc:
        return respond(request, claim.id, ok=False, message=t(f"upload.error.{exc.code}", lang_of(request), **exc.params),
                       status=400)
    db.commit()
    return respond(request, claim.id)


@app.post("/documents/{doc_id}/remove")
def remove_document(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    claim = doc.claim
    require_draft(claim)
    if doc.status not in ("removed", "replaced"):
        services.retire_document(db, claim, doc, "removed")
        db.commit()
    return respond(request, claim.id, message=t("doc.removed", lang_of(request)))


@app.post("/documents/{doc_id}/replace")
async def replace_document(doc_id: str, request: Request, file: UploadFile = File(...),
                           source: str = Form("upload"), db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    claim = doc.claim
    require_draft(claim)
    lang = lang_of(request)
    data = await file.read(settings.max_upload_bytes + 1)
    try:
        new_doc = services.add_uploaded_document(db, claim, file.filename or "document", data,
                                                 _source_of(source), replaces=doc)
    except storage.UploadRejected as exc:
        return respond(request, claim.id, ok=False, status=400,
                       message=t(f"upload.error.{exc.code}", lang, **exc.params))
    db.commit()
    if new_doc.status == "queued":
        services.schedule_extraction([new_doc.id])
    return respond(request, claim.id, message=t("doc.replaced", lang))


@app.post("/documents/{doc_id}/retry")
def retry_document(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    require_draft(doc.claim)
    if doc.status in ("failed", "unavailable") and doc.source != "sample_fixture":
        if services.live_extractor() is None:
            doc.status, doc.status_detail = "unavailable", "not_configured"
            db.commit()
        else:
            doc.status, doc.status_detail = "queued", None
            db.commit()
            services.schedule_extraction([doc.id])
    return respond(request, doc.claim_id)


@app.post("/documents/{doc_id}/type")
def set_document_type(doc_id: str, request: Request, document_type: str = Form(...), db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    require_draft(doc.claim)
    if document_type not in DOC_TYPES:
        raise HTTPException(400)
    ext = services.extraction_of(doc)
    doc.type_override = None if (ext and ext.document_type == document_type) else document_type
    db.commit()
    return respond(request, doc.claim_id, message=t("msg.saved", lang_of(request)))


@app.post("/documents/{doc_id}/grouping")
def grouping_decision(doc_id: str, request: Request, decision: str = Form(...), db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    claim = doc.claim
    require_draft(claim)
    lang = lang_of(request)
    if decision == "same_visit":
        doc.grouping_decision = "same_visit"
        db.commit()
        return respond(request, claim.id, message=t("msg.saved", lang))
    if decision != "separate":
        raise HTTPException(400)
    view = services.build_view(claim)
    proposal = next((p for p in view.proposals if doc.id in p.doc_ids), None)
    move_ids = proposal.doc_ids if proposal else [doc.id]
    new_claim = Claim(session_id=claim.session_id, is_sample=claim.is_sample, split_from_claim_id=claim.id)
    db.add(new_claim)
    db.flush()
    for d in claim.active_documents:
        if d.id in move_ids:
            d.claim_id = new_claim.id
            d.grouping_decision = None
            prefix = f"doc:{d.id}:"
            for c in claim.corrections:
                if c.field_key.startswith(prefix):
                    c.claim_id = new_claim.id
    db.commit()
    return respond(request, claim.id, message=t("msg.split_done", lang), new_claim=f"/claims/{new_claim.id}",
                   new_claim_label=t("msg.open_split", lang))


@app.post("/documents/{doc_id}/link")
def link_payment(doc_id: str, request: Request, invoice_doc_id: str = Form(...), db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    claim = doc.claim
    require_draft(claim)
    if invoice_doc_id != "none":
        target = next((d for d in claim.active_documents if d.id == invoice_doc_id), None)
        if target is None:
            raise HTTPException(400)
    doc.linked_invoice_doc_id = invoice_doc_id
    db.commit()
    return respond(request, claim.id, message=t("msg.saved", lang_of(request)))


def _current_field(claim: Claim, key: str) -> Optional[Field]:
    cin = services.claim_input(claim)
    if key.startswith("claim:"):
        view = build_claim_view(cin)
        return view.claim_fields.get(key.split(":", 1)[1])
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != "doc" or parts[2] not in DOC_FIELD_KINDS:
        return None
    docin = next((d for d in cin.docs if d.id == parts[1] and d.extraction is not None), None)
    if docin is None:
        return None
    return DocCtx(docin, cin.corrections).f(parts[2])


@app.post("/claims/{claim_id}/fields")
def save_field(claim_id: str, request: Request, field_key: str = Form(...), value: str = Form(""),
               kind: str = Form("correction"), db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    require_draft(claim)
    lang = lang_of(request)
    fld = _current_field(claim, field_key)
    if fld is None:
        raise HTTPException(400)
    value = " ".join(value.split())[:200]
    if not value:
        return respond(request, claim.id, ok=False, message=t("msg.invalid_value", lang), status=400)
    _, typed, problem = parse_kind(fld.kind, value)
    if problem or typed is None:
        return respond(request, claim.id, ok=False, message=t("msg.invalid_value", lang), status=400)
    if fld.kind == "date":
        value = typed.isoformat()
    elif fld.kind in ("currency",):
        value = typed
    original = fld.original_value if fld.member_set else fld.value
    previous = [c for c in claim.corrections if c.active and c.field_key == field_key]
    for c in previous:
        c.active = False
        c.superseded_reason = "updated"
        if c.original_value is not None:
            original = json.loads(c.original_value)
    db.add(Correction(
        claim_id=claim.id, field_key=field_key,
        kind="confirmation" if kind == "confirmation" else "correction",
        original_value=json.dumps(original, ensure_ascii=False) if original is not None else None,
        corrected_value=json.dumps(value, ensure_ascii=False),
        original_sources_json=json.dumps([s.as_json() for s in fld.sources], ensure_ascii=False),
        context_json=json.dumps(sorted({norm_text(c.value) for c in fld.candidates}), ensure_ascii=False),
    ))
    db.commit()
    return respond(request, claim.id, message=t("msg.saved", lang))


@app.post("/claims/{claim_id}/fields/revert")
def revert_field(claim_id: str, request: Request, field_key: str = Form(...), db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    require_draft(claim)
    for c in claim.corrections:
        if c.active and c.field_key == field_key:
            c.active = False
            c.superseded_reason = "reverted"
    db.commit()
    return respond(request, claim.id, message=t("msg.saved", lang_of(request)))


@app.post("/claims/{claim_id}/acknowledge")
def acknowledge(claim_id: str, request: Request, issue_key: str = Form(...), undo: str = Form(""),
                db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    require_draft(claim)
    issue_key = issue_key[:200]
    existing = [a for a in claim.acknowledgements if a.issue_key == issue_key]
    if undo:
        for a in existing:
            db.delete(a)
    elif not existing:
        view = services.build_view(claim)
        if not any(i.key == issue_key and i.acknowledgeable for i in view.issues):
            raise HTTPException(400)
        db.add(Acknowledgement(claim_id=claim.id, issue_key=issue_key))
    db.commit()
    return respond(request, claim.id, message=t("msg.saved", lang_of(request)))


# ------------------------------------------------------------------ review & submit


@app.get("/claims/{claim_id}/review", response_class=HTMLResponse)
def review_page(claim_id: str, request: Request, error: str = "", db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    if claim.status == "submitted":
        return RedirectResponse(f"/claims/{claim.id}/submitted", status_code=303)
    view = services.build_view(claim)
    preview = export.build_export(claim, view, reference=None, submitted_at=None)
    return templates.TemplateResponse(request, "review.html", base_context(
        request, claim=claim, view=view, step=4, error=error, snap=preview,
        resolved=export.resolved_items(claim, view),
        json_preview=json.dumps(preview, ensure_ascii=False, indent=2)))


@app.post("/claims/{claim_id}/submit")
def submit_claim(claim_id: str, request: Request, reviewed: str = Form(""), submit_token: str = Form(""),
                 db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    if claim.status == "submitted":
        return RedirectResponse(f"/claims/{claim.id}/submitted", status_code=303)
    if not reviewed:
        return RedirectResponse(f"/claims/{claim.id}/review?error=confirm", status_code=303)
    if not secrets.compare_digest(submit_token, claim.submit_token):
        return RedirectResponse(f"/claims/{claim.id}/review?error=stale", status_code=303)
    view = services.build_view(claim)  # server-side revalidation
    if not view.ready:
        return RedirectResponse(f"/claims/{claim.id}/review?error=blocked", status_code=303)
    now = utcnow()
    reference = export.new_reference(now)
    snapshot = export.build_export(claim, view, reference=reference, submitted_at=now)
    result = db.execute(
        update(Claim)
        .where(Claim.id == claim.id, Claim.status == "draft", Claim.submit_token == submit_token)
        .values(status="submitted", reference=reference, submitted_at=now, member_reviewed_at=now,
                snapshot_json=json.dumps(snapshot, ensure_ascii=False), submit_token=secrets.token_urlsafe(24))
    )
    if result.rowcount != 1:  # someone (a double click) got there first
        db.rollback()
        return RedirectResponse(f"/claims/{claim.id}/submitted", status_code=303)
    db.commit()
    log.info("Claim %s saved locally with reference %s", claim.id, reference)
    return RedirectResponse(f"/claims/{claim.id}/submitted", status_code=303)


def _submitted_snapshot(claim: Claim) -> Dict[str, Any]:
    if claim.status != "submitted" or not claim.snapshot_json:
        raise HTTPException(404)
    return json.loads(claim.snapshot_json)


@app.get("/claims/{claim_id}/submitted", response_class=HTMLResponse)
def submitted_page(claim_id: str, request: Request, db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    if claim.status != "submitted":
        return RedirectResponse(f"/claims/{claim.id}", status_code=303)
    snap = _submitted_snapshot(claim)
    return templates.TemplateResponse(request, "submitted.html", base_context(
        request, claim=claim, snap=snap, step=4))


@app.get("/claims/{claim_id}/print", response_class=HTMLResponse)
def print_page(claim_id: str, request: Request, db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    snap = _submitted_snapshot(claim)
    return templates.TemplateResponse(request, "print.html", base_context(request, claim=claim, snap=snap))


@app.get("/claims/{claim_id}/export.json")
def export_json(claim_id: str, request: Request, db: Session = Depends(get_db)):
    claim = get_claim(db, request, claim_id)
    snap = _submitted_snapshot(claim)
    body = json.dumps(snap, ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="claim-{claim.reference}.json"'})


# ------------------------------------------------------------------ private files


def _private_file(path: Path, media_type: str) -> FileResponse:
    return FileResponse(path, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/documents/{doc_id}/file")
def document_file(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    if not doc.storage_name:
        raise HTTPException(404)
    return _private_file(storage.file_path(doc.storage_name), doc.mime_type)


@app.get("/documents/{doc_id}/thumb")
def document_thumb(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    if not doc.thumb_name:
        raise HTTPException(404)
    return _private_file(storage.thumb_path(doc.thumb_name), "image/jpeg")


@app.get("/documents/{doc_id}/page/{page}")
def document_page(doc_id: str, page: int, request: Request, db: Session = Depends(get_db)):
    doc = get_doc(db, request, doc_id)
    if not doc.storage_name:
        raise HTTPException(404)
    if doc.mime_type != "application/pdf":
        return _private_file(storage.file_path(doc.storage_name), doc.mime_type)
    if page < 1 or page > (doc.page_count or 1):
        raise HTTPException(404)
    path = storage.render_page(doc.storage_name, page)
    if path is None:
        raise HTTPException(404)
    return _private_file(path, "image/png")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    lang = lang_of(request)
    msg = t("msg.claim_locked", lang) if exc.detail == "claim_locked" else (
        t("msg.not_found", lang) if exc.status_code == 404 else t("msg.error", lang))
    if is_fetch(request) or request.url.path.startswith(("/documents/", "/claims/")) and request.method == "POST":
        return JSONResponse({"ok": False, "message": msg}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", base_context(request, message=msg, step=1),
                                      status_code=exc.status_code)
