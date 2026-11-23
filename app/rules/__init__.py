"""Loader for the editable document-checklist rules (``claim_rules.json``).

Replace the JSON file when the team receives Bupa's complete rules; the engine
only depends on the structure documented in the README.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

RULES_PATH = Path(__file__).with_name("claim_rules.json")

VALID_WHEN = {"always", "per_invoice", "if_triggered", "if_available", "reminder"}


@dataclass
class Requirement:
    id: str
    when: str
    label: Dict[str, str]
    doc_types: List[str] = field(default_factory=list)
    trigger_line_categories: List[str] = field(default_factory=list)
    trigger_flags: List[str] = field(default_factory=list)
    requires_field: Optional[str] = None
    satisfied_by_line_categories: List[str] = field(default_factory=list)


@dataclass
class CategoryRules:
    id: str
    label: Dict[str, str]
    reference: str
    requirements: List[Requirement]
    field_checks: List[str]


@dataclass
class RuleSet:
    rule_set_id: str
    version: str
    status: str
    source_note: Dict[str, str]
    separate_claims_note: Dict[str, str]
    service_window_days: Optional[int]
    service_window_note: Dict[str, str]
    categories: Dict[str, CategoryRules]
    common_requirements: List[Requirement]
    location_requirements: Dict[str, List[Requirement]]

    def category(self, cat_id: Optional[str]) -> Optional[CategoryRules]:
        return self.categories.get(cat_id or "")

    def requirements_for(self, cat_id: Optional[str]) -> List[Requirement]:
        cat = self.category(cat_id)
        return list(cat.requirements) if cat else list(self.common_requirements)


def _req(raw: dict) -> Requirement:
    when = raw["when"]
    if when not in VALID_WHEN:
        raise ValueError(f"Unknown requirement condition '{when}' in rule '{raw.get('id')}'")
    trig = raw.get("trigger") or {}
    return Requirement(
        id=raw["id"],
        when=when,
        label=raw["label"],
        doc_types=list(raw.get("doc_types") or []),
        trigger_line_categories=list(trig.get("line_categories") or []),
        trigger_flags=list(trig.get("flags") or []),
        requires_field=raw.get("requires_field"),
        satisfied_by_line_categories=list(raw.get("satisfied_by_line_categories") or []),
    )


def load_rules(path: Path = RULES_PATH) -> RuleSet:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cats = {}
    for c in raw["categories"]:
        cats[c["id"]] = CategoryRules(
            id=c["id"],
            label=c["label"],
            reference=c.get("reference", ""),
            requirements=[_req(r) for r in c["requirements"]],
            field_checks=list(c.get("field_checks") or []),
        )
    return RuleSet(
        rule_set_id=raw["rule_set_id"],
        version=raw["version"],
        status=raw.get("status", "prototype"),
        source_note=raw["source_note"],
        separate_claims_note=raw["separate_claims_note"],
        service_window_days=raw.get("service_window_days"),
        service_window_note=raw.get("service_window_note", {"ar": "", "en": ""}),
        categories=cats,
        common_requirements=[_req(r) for r in raw.get("common_requirements", [])],
        location_requirements={
            k: [_req(r) for r in v] for k, v in (raw.get("location_requirements") or {}).items()
        },
    )


_cached: Optional[RuleSet] = None


def get_rules() -> RuleSet:
    global _cached
    if _cached is None:
        _cached = load_rules()
    return _cached
