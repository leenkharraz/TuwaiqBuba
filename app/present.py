"""Presentation helpers shared by templates and the JSON export."""
from __future__ import annotations

from typing import Any, Dict

from .engine import CATEGORIES, LOCATIONS, Field, Issue
from .i18n import t
from .normalize import fmt_date, fmt_money


def field_label(name: str, lang: str) -> str:
    return t(f"field.{name}", lang)


def display_value(fld: Field, lang: str) -> str:
    if fld.value is None:
        return "—"
    if fld.kind == "date" and fld.typed is not None:
        return fmt_date(fld.typed)
    if fld.kind == "amount" and fld.typed is not None:
        return fmt_money(fld.typed)
    if fld.kind == "category":
        return t(f"cat.{fld.value}", lang) if fld.value in CATEGORIES else fld.value
    if fld.kind == "location":
        return t(f"loc.{fld.value}", lang) if fld.value in LOCATIONS else fld.value
    return fld.value


def source_label(src, lang: str) -> str:
    return t(f"source.{src.doc_type or 'other'}", lang) if src else ""


FSI, PDI = "⁨", "⁩"  # Unicode first-strong isolate: keeps IDs/amounts intact inside Arabic text
_ISOLATE = ("invoice", "filename", "name", "paid", "total", "currency", "subtotal", "tax", "a", "b",
            "lines", "expected", "date", "raw", "invoice_date", "days", "limit", "value")


def _iso(v: Any) -> Any:
    if isinstance(v, str) and v:
        return FSI + v + PDI
    if isinstance(v, (list, tuple)):
        return [_iso(x) for x in v]
    return v


def issue_html(issue: Issue, lang: str):
    """Issue message as safe HTML: values are escaped and wrapped in <bdi> so IDs/amounts never break."""
    from markupsafe import Markup, escape

    from .i18n import S, resolve

    params: Dict[str, Any] = dict(issue.params)
    if "field" in params:
        params["field_label"] = field_label(params["field"], lang)
    if "note" in params:
        params["note_text"] = t(f"field.note.{params['note']}", lang) if params["note"] else ""
    if "detail" in params:
        params["detail"] = t(f"extract_error.{params['detail']}", lang)
    if "reasons" in params:
        params["reasons"] = [t(f"reason.{r}", lang) for r in params["reasons"]]
    if params.get("trigger") in ("medication_prescribed", "implant_or_device_mentioned"):
        params["trigger"] = t(f"trigger.{params['trigger']}", lang)
    safe = {}
    for k, v in params.items():
        text = resolve(v, lang)
        if k in _ISOLATE or k == "values":
            cls = "ib-wrap" if k in ("filename", "name", "values") else "ib"
            safe[k] = Markup('<bdi class="{}">{}</bdi>').format(cls, text)
        else:
            safe[k] = text
    pair = S.get(f"issue.{issue.code}")
    template = (pair[0] if lang == "ar" else pair[1]) if pair else issue.code
    try:
        return Markup(escape(template)).format(**safe)
    except (KeyError, IndexError, ValueError):
        return escape(template)


def issue_text(issue: Issue, lang: str) -> str:
    params: Dict[str, Any] = {k: (_iso(v) if k in _ISOLATE or k == "values" else v)
                              for k, v in issue.params.items()}
    if "field" in params:
        params["field_label"] = field_label(params["field"], lang)
    if "note" in params:
        params["note_text"] = t(f"field.note.{params['note']}", lang) if params["note"] else ""
    if "detail" in params:
        params["detail"] = t(f"extract_error.{params['detail']}", lang)
    if "reasons" in params:
        params["reasons"] = [t(f"reason.{r}", lang) for r in params["reasons"]]
    if params.get("trigger") in ("medication_prescribed", "implant_or_device_mentioned"):
        params["trigger"] = t(f"trigger.{params['trigger']}", lang)
    return t(f"issue.{issue.code}", lang, **params)
