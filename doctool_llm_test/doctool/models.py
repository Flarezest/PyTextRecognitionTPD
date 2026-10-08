from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class FieldValue:
    value: Any
    confidence: float = 0.0          # 0..1
    source: str = ""                 # text | mrz | ocr | vlm | red-ocr
    raw: str = ""
    needs_review: bool = False
    crop: str | None = None          # путь к вырезанному фрагменту (для ручной проверки)
    alternatives: list = field(default_factory=list)  # другие варианты прочтения

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Extraction:
    doc_type: str
    source_file: str
    fields: dict[str, FieldValue] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    def get(self, name: str, default=None):
        f = self.fields.get(name)
        return f.value if f and f.value not in (None, "") else default

    def set(self, name: str, value, confidence: float, source: str, raw: str = "", **kw):
        if value in (None, ""):
            return
        self.fields[name] = FieldValue(value, round(float(confidence), 2), source, raw, **kw)

    def to_dict(self) -> dict:
        return {"doc_type": self.doc_type, "source_file": self.source_file,
                "fields": {k: v.to_dict() for k, v in self.fields.items()}, "notes": self.notes}
