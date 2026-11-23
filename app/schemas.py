"""Pydantic schemas for document extraction.

The same models are sent to Gemini as the structured-output schema and are
used to validate whatever comes back. They intentionally have *no default
values* (the Gemini schema format does not accept them); a lenient
``model_validator`` fills in missing keys instead, so a response that omits a
nullable field still validates while a response with wrong types does not.
"""
from __future__ import annotations

import typing
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DocType = Literal[
    "invoice",
    "payment_receipt",
    "medical_report",
    "prescription",
    "lab_result",
    "imaging_result",
    "other",
]
DOC_TYPES: tuple = typing.get_args(DocType)

Readability = Literal["readable", "partially_readable", "unreadable"]

CategoryHint = Literal[
    "dental",
    "outpatient",
    "inpatient",
    "optical",
    "physiotherapy",
    "medication",
    "unknown",
]
CATEGORY_HINTS: tuple = typing.get_args(CategoryHint)

LineCategory = Literal[
    "consultation",
    "lab_test",
    "imaging",
    "medication",
    "procedure",
    "dental_procedure",
    "room_and_board",
    "implant_or_device",
    "therapy_session",
    "optical",
    "other",
]

EXCERPT_MAX = 300


def _fill_missing(cls: type, data: Any) -> Any:
    """Give absent keys an empty value so partial model output still validates."""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    for name, info in cls.model_fields.items():
        if name in data and data[name] is not None:
            continue
        ann = info.annotation
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)
        if origin in (list, List):
            data[name] = []
        elif isinstance(ann, type) and issubclass(ann, BaseModel):
            data[name] = {}
        elif name not in data:
            data[name] = None
        # an explicit None for an Optional field is kept as-is
        _ = args
    return data


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _fill(cls, data: Any) -> Any:
        return _fill_missing(cls, data)


def _clip(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = " ".join(str(v).split())
    if not v:
        return None
    return v[:EXCERPT_MAX]


class ExtractedValue(Lenient):
    """One value read from a document, with the evidence that supports it."""

    value: Optional[str]
    page: Optional[int]
    excerpt: Optional[str]
    ambiguous: Optional[bool]
    note: Optional[str]

    @field_validator("value", "excerpt", "note", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if v is None:
            return None
        return _clip(str(v))

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v: Any) -> Any:
        if v in (None, "", 0):
            return None
        try:
            p = int(v)
        except (TypeError, ValueError):
            return None
        return p if 0 < p < 1000 else None


class LineItem(Lenient):
    description: Optional[str]
    category: Optional[LineCategory]
    code: Optional[str]
    tooth_number: Optional[str]
    quantity: Optional[str]
    amount: Optional[str]
    page: Optional[int]
    excerpt: Optional[str]

    @field_validator("description", "code", "tooth_number", "quantity", "amount", "excerpt", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if v is None:
            return None
        return _clip(str(v))

    @field_validator("category", mode="before")
    @classmethod
    def _cat(cls, v: Any) -> Any:
        allowed = typing.get_args(LineCategory)
        return v if v in allowed else ("other" if v else None)

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v: Any) -> Any:
        return ExtractedValue._page(v)


class DocumentExtraction(Lenient):
    """Everything we try to read from a single uploaded document.

    Every value is optional. A value that is not printed on the document must
    come back as null - the server never fills gaps by guessing.
    """

    document_type: DocType
    readability: Readability
    readability_note: Optional[str]
    language: Optional[str]

    patient_name: ExtractedValue
    patient_identifier: ExtractedValue
    provider_name: ExtractedValue
    provider_country: ExtractedValue

    invoice_number: ExtractedValue
    invoice_date: ExtractedValue
    service_date: ExtractedValue
    admission_date: ExtractedValue
    discharge_date: ExtractedValue
    service_description: ExtractedValue
    service_category_hint: Optional[CategoryHint]
    service_category_excerpt: Optional[str]

    line_items: List[LineItem]
    subtotal: ExtractedValue
    tax: ExtractedValue
    total: ExtractedValue
    currency: ExtractedValue

    paid_amount: ExtractedValue
    payment_date: ExtractedValue
    payment_method: ExtractedValue
    transaction_reference: ExtractedValue
    referenced_invoice_number: ExtractedValue

    diagnosis: ExtractedValue
    medication_prescribed: Optional[bool]
    implant_or_device_mentioned: Optional[bool]

    @field_validator("document_type", mode="before")
    @classmethod
    def _doctype(cls, v: Any) -> Any:
        return v if v in DOC_TYPES else "other"

    @field_validator("readability", mode="before")
    @classmethod
    def _readability(cls, v: Any) -> Any:
        return v if v in ("readable", "partially_readable", "unreadable") else "partially_readable"

    @field_validator("service_category_hint", mode="before")
    @classmethod
    def _hint(cls, v: Any) -> Any:
        return v if v in CATEGORY_HINTS else ("unknown" if v else None)

    @field_validator("readability_note", "language", "service_category_excerpt", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if v is None:
            return None
        return _clip(str(v))

    @field_validator("line_items", mode="before")
    @classmethod
    def _lines(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return []
        return v[:60]


# Field names that hold an ExtractedValue, in display order.
EXTRACTED_VALUE_FIELDS: tuple = tuple(
    name for name, info in DocumentExtraction.model_fields.items() if info.annotation is ExtractedValue
)
