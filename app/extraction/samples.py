"""Sample-data mode.

Sample documents are synthetic files shipped in ``fixtures/samples`` together
with prepared extraction JSON. They are only ever added through the sample
gallery: an arbitrary uploaded file is never matched to fixture data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..config import BASE_DIR
from ..schemas import DocumentExtraction

FIXTURE_DIR = BASE_DIR / "fixtures" / "samples"


@dataclass
class Fixture:
    id: str
    file: str
    mime_type: str
    title: Dict[str, str]
    purpose: Dict[str, str]
    starter: bool
    order: int

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / self.file

    def extraction(self) -> DocumentExtraction:
        raw = json.loads((FIXTURE_DIR / f"{self.id}.extraction.json").read_text(encoding="utf-8"))
        return DocumentExtraction.model_validate(raw)


_cache: Optional[List[Fixture]] = None


def list_fixtures() -> List[Fixture]:
    global _cache
    if _cache is None:
        manifest = FIXTURE_DIR / "manifest.json"
        if not manifest.exists():
            _cache = []
        else:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            _cache = [
                Fixture(
                    id=f["id"], file=f["file"], mime_type=f["mime_type"], title=f["title"],
                    purpose=f["purpose"], starter=bool(f.get("starter")), order=i,
                )
                for i, f in enumerate(raw["fixtures"])
                if (FIXTURE_DIR / f["file"]).exists()
            ]
    return _cache


def get_fixture(fixture_id: str) -> Optional[Fixture]:
    return next((f for f in list_fixtures() if f.id == fixture_id), None)


def samples_available() -> bool:
    return bool(list_fixtures())
