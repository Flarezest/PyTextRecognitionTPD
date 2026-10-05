"""Типы заявлений и вердикт по правилам из config/case_types.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .compare import Check

CONFIG = Path(__file__).resolve().parent.parent / "config" / "case_types.yaml"

ACCEPT, REJECT, REVIEW = "accept", "reject", "review"
DECISION_RU = {ACCEPT: "Принять", REJECT: "Отклонить", REVIEW: "Ручная проверка"}


def load_case_types(path: Path = CONFIG) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["case_types"]


@dataclass
class Decision:
    code: str
    reasons: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return DECISION_RU[self.code]

    def to_dict(self) -> dict:
        return {"code": self.code, "title": self.title, "reasons": self.reasons}


def _match(cond: dict, checks: list[Check], flags: set[str], app_fields: dict) -> bool:
    if "flag" in cond and cond["flag"] not in flags:
        return False
    if "check" in cond:
        sts = cond.get("status", ["fail"])
        if not any(c.name == cond["check"] and c.status in sts for c in checks):
            return False
    if "any_status" in cond and not any(c.status in cond["any_status"] for c in checks):
        return False
    if "missing" in cond:
        f = app_fields.get(cond["missing"])
        if f is not None and f.value:
            return False
    return True


def decide(case_type: dict, checks: list[Check], flags: set[str], app_fields: dict) -> Decision:
    rules = case_type.get("rules") or {}
    reasons = [r.get("reason", "") for r in rules.get("reject", []) if _match(r["when"], checks, flags, app_fields)]
    if reasons:
        return Decision(REJECT, list(dict.fromkeys(reasons)))
    reasons = [r.get("reason", "") for r in rules.get("review", []) if _match(r["when"], checks, flags, app_fields)]
    if reasons:
        return Decision(REVIEW, list(dict.fromkeys(reasons)))
    return Decision(ACCEPT, ["Все проверки пройдены"])
