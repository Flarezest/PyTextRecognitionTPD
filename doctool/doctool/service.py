"""Сервисный слой: одна проверка «дела» (заявление + паспорт) целиком.

Используется и командной строкой, и обоими интерфейсами (Qt и веб):
    res = run_case(CaseInput(...))         # распознать, сверить, вынести вердикт, сохранить отчёты
    recompute(res, overrides, confirmed)   # применить правки оператора и пересчитать
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import cv2

from . import names, parsers
from .application import _store, detect_form, extract_application, load_forms
from .compare import OK, REVIEW, WARN, Check, compare, extra_checks
from .integrations import AdminData
from .models import Extraction, FieldValue
from .ocr import OllamaVLM, Page, best_orientation, downscale, keyword_score, load_document, tesseract_text
from .passport_rf import KEYWORDS as PASSPORT_KW
from .passport_rf import extract_passport, find_previous_passports
from .progress import log
from .report import append_excel, save_html
from .verdict import Decision, decide, load_case_types

APP_MODES = {"auto": "Авто", "electronic": "Электронное", "scan": "Скан", "photo": "Фото"}
PAS_MODES = {"auto": "Авто", "scan": "Скан", "photo": "Фото"}


@dataclass
class CaseInput:
    case_type: str = "admin_change_person"
    application: str | None = None
    passport: str | None = None
    combined: bool = False                 # заявление и паспорт в одном файле (application)
    page_roles: dict[int, str] | None = None  # для combined: № страницы (с 0) → application|passport|skip
    app_mode: str = "auto"                 # auto | electronic | scan | photo
    app_handwritten: bool = False          # True → рукопись читает локальная модель
    pas_mode: str = "auto"                 # auto | scan | photo
    vlm_model: str | None = None           # напр. qwen3-vl:4b-instruct
    ollama: str = "http://127.0.0.1:11434"
    admin: object = None                   # AdminData или AdminDataProvider (данные администратора из системы)
    check_date: str | None = None          # ДД.ММ.ГГГГ — на какую дату проверять срок действия
    case_id: str | None = None
    out_root: str = "results"


@dataclass
class CaseResult:
    inp: CaseInput
    case_type: dict
    case_id: str
    case_dir: Path
    app: Extraction | None = None
    pas: Extraction | None = None
    previous_passports: list = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    flags: set = field(default_factory=set)
    decision: Decision | None = None
    record: dict = field(default_factory=dict)
    page_roles: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    confirmed: set = field(default_factory=set)
    notes: list = field(default_factory=list)
    admin: AdminData | None = None


# ------------------------------------------------------------------ строки таблицы (общие для GUI и выгрузки)

ROWS = [
    {"id": "fio", "title": "ФИО", "app": ["applicant_fio", "applicant_header"], "pas": "@fio",
     "checks": ["ФИО в шапке («от …»)", "ФИО в тексте («Я, …»)", "ФИО у подписи", "ФИО: шапка ↔ «Я, …»",
                "Заявитель — текущий администратор"]},
    {"id": "birth_date", "title": "Дата рождения", "app": ["applicant_header.birth_date"], "pas": "birth_date",
     "checks": ["Дата рождения"]},
    {"id": "passport", "title": "Серия и номер паспорта", "app": ["passport"], "pas": "series_number",
     "checks": ["Серия и номер паспорта", "Указан прежний паспорт", "Паспорт в системе устарел"]},
    {"id": "issue_date", "title": "Дата выдачи паспорта", "app": ["passport.issue_date"], "pas": "issue_date",
     "checks": ["Дата выдачи паспорта", "Срок действия паспорта", "Серия ↔ год выдачи"]},
    {"id": "issued_by", "title": "Кем выдан", "app": ["issued_by"], "pas": "issued_by",
     "checks": ["Кем выдан", "Кем выдан ↔ код подразделения", "Кем выдан ↔ данные системы"]},
    {"id": "department_code", "title": "Код подразделения", "app": [], "pas": "department_code",
     "checks": ["Кем выдан ↔ код подразделения"]},
    {"id": "birth_place", "title": "Место рождения", "app": [], "pas": "birth_place", "checks": []},
    {"id": "address", "title": "Адрес регистрации", "app": ["address"], "pas": None, "checks": ["Адрес регистрации"]},
    {"id": "inn", "title": "ИНН ИП", "app": ["inn"], "pas": None, "checks": ["ИНН ИП: контрольные цифры"]},
    {"id": "domains", "title": "Домен(ы)", "app": ["domains"], "pas": None,
     "checks": ["Домен(ы)", "Домены администратора"]},
    {"id": "new_admin", "title": "Новый администратор", "app": ["new_admin_fio", "new_admin_org", "new_admin_inline"],
     "pas": None, "checks": ["Новый администратор указан", "Новый администратор: текст ↔ таблица",
                             "Новый администратор ≠ заявитель"]},
    {"id": "new_admin_contact", "title": "Контакты нового администратора",
     "app": ["new_admin_contact", "new_admin_org_contact"], "pas": None, "checks": ["Контакты нового администратора"]},
    {"id": "contract", "title": "Договор / аккаунт", "app": ["contract"], "pas": None,
     "checks": ["Договор/аккаунт нового администратора"]},
    {"id": "application_date", "title": "Дата заявления", "app": ["application_date"], "pas": None,
     "checks": ["Дата заявления"]},
]
_STATUS_RANK = {"fail": 4, "review": 3, "warn": 2, "info": 1, "ok": 0}


def _pas_value(pas: Extraction | None, key: str | None) -> str:
    if pas is None or not key:
        return ""
    if key == "@fio":
        return " ".join(x for x in (pas.get("surname"), pas.get("given_name"), pas.get("patronymic")) if x)
    return pas.get(key) or ""


def table_rows(res: CaseResult) -> list[dict]:
    """Строки для таблицы интерфейса: значение заявления, паспорта, фрагмент, статус."""
    out = []
    for r in ROWS:
        app_val, crop, app_src = "", None, ""
        for k in r["app"]:
            f = res.app.fields.get(k) if res.app else None
            if f is not None:
                crop = crop or f.crop
                if f.value:
                    app_val = ", ".join(f.value) if isinstance(f.value, list) else str(f.value)
                    app_src = f.source
                    break
        if not crop and res.app is not None:  # фрагмент берём у «родительского» поля
            base = r["app"][0].split(".")[0] if r["app"] else None
            f = res.app.fields.get(base) if base else None
            crop = f.crop if f is not None else None
        pas_val = _pas_value(res.pas, r["pas"])
        pas_src = ""
        if res.pas is not None and r["pas"] and r["pas"] != "@fio" and r["pas"] in res.pas.fields:
            pas_src = res.pas.fields[r["pas"]].source
        rel = [c for c in res.checks if c.name in r["checks"]]
        status = max((c.status for c in rel), key=lambda s: _STATUS_RANK[s], default="")
        if not app_val and not pas_val and not crop and not rel:
            continue
        out.append({"id": r["id"], "title": r["title"], "app": app_val, "app_source": app_src,
                    "passport": pas_val, "passport_source": pas_src, "crop": crop, "status": status,
                    "confirmed": r["id"] in res.confirmed, "editable_app": bool(r["app"]),
                    "editable_passport": bool(r["pas"]),
                    "details": [f"{c.name}: {c.detail}" for c in rel if c.detail]})
    return out


# ------------------------------------------------------------------ общий файл: роли страниц

def classify_pages(pages: list[Page], forms: list[dict]) -> dict[int, str]:
    """Определяет для каждой страницы: application / passport / skip."""
    roles: dict[int, str] = {}
    form_kw = sorted({k for f in forms for k in f["detect"]["keywords"]})
    for p in pages:
        log(f"Определяю содержимое страницы {p.index + 1} из {len(pages)}…")
        if p.text_layer and len(re.sub(r"\s", "", p.text_layer)) > 300 and detect_form(p.text_layer, forms):
            roles[p.index] = "application"
            continue
        if p.image is None:
            roles[p.index] = "skip"
            continue
        _, sc_pas = best_orientation(p.image, PASSPORT_KW)
        txt, _ = tesseract_text(downscale(cv2.cvtColor(p.image, cv2.COLOR_BGR2GRAY), 1800), psm=3)
        sc_app = sum(1 for k in form_kw if k.lower() in txt.lower())
        if sc_app >= 3 and sc_app >= sc_pas:
            roles[p.index] = "application"
        elif sc_pas >= 3:
            roles[p.index] = "passport"
        else:
            roles[p.index] = "unknown"
    # неизвестные страницы после первой страницы паспорта — тоже паспорт (прописка, ранее выданные и т. п.)
    seen_pas = False
    for i in sorted(roles):
        if roles[i] == "passport":
            seen_pas = True
        elif roles[i] == "unknown":
            roles[i] = "passport" if seen_pas else "skip"
    return roles


# ------------------------------------------------------------------ основной сценарий

def _vlm(inp: CaseInput) -> OllamaVLM | None:
    if not inp.vlm_model:
        return None
    v = OllamaVLM(model=inp.vlm_model, host=inp.ollama)
    log(f"Проверяю локальную модель {inp.vlm_model}…")
    if not v.available():
        log(f"[!] Модель {inp.vlm_model} недоступна в Ollama ({inp.ollama}) — продолжаю без неё")
        return None
    return v


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")[:60] or "дело"


def run_case(inp: CaseInput) -> CaseResult:
    types = load_case_types()
    ct = types[inp.case_type]
    forms = load_forms()
    case_id = inp.case_id or Path(inp.application or inp.passport or "дело").stem
    case_dir = Path(inp.out_root) / f"{datetime.now():%Y%m%d_%H%M%S}_{_safe_name(case_id)}"
    case_dir.mkdir(parents=True, exist_ok=True)
    res = CaseResult(inp=inp, case_type=ct, case_id=case_id, case_dir=case_dir)
    vlm = _vlm(inp)

    # --- файлы и страницы
    app_pages: list[Page] = []
    pas_pages: list[Page] = []
    if inp.combined:
        log("Общий файл: читаю страницы…")
        pages = load_document(inp.application or inp.passport)
        roles = inp.page_roles or classify_pages(pages, forms)
        res.page_roles = roles
        app_pages = [p for p in pages if roles.get(p.index) == "application"]
        pas_pages = [p for p in pages if roles.get(p.index) == "passport"]
        log(f"Страницы: заявление — {[p.index + 1 for p in app_pages]}, паспорт — {[p.index + 1 for p in pas_pages]}")
    else:
        if inp.application:
            app_pages = load_document(inp.application)
        if inp.passport:
            pas_pages = load_document(inp.passport)

    # --- заявление
    if app_pages and ct.get("form"):
        res.app = extract_application(app_pages, case_dir, vlm if inp.app_handwritten else None, forms=forms,
                                      mode=inp.app_mode, handwritten=True if inp.app_handwritten else None,
                                      form_id=ct.get("form"))
    else:
        res.app = Extraction(doc_type="none", source_file=inp.application or "")
        if app_pages and not ct.get("form"):
            res.notes.append("Для этого типа заявления бланк ещё не настроен — проверяется только паспорт.")
    # --- паспорт
    if pas_pages:
        res.pas = extract_passport(pas_pages, vlm, mode=inp.pas_mode)
    elif ct.get("needs_passport"):
        res.notes.append("Паспорт не загружен — сверка с паспортом не выполнялась.")

    # --- ранее выданные паспорта: ищем, только если номера не совпали
    if res.pas is not None and res.app.get("passport") and res.pas.get("series_number"):
        a = re.sub(r"\D", "", res.app.get("passport"))
        p = re.sub(r"\D", "", res.pas.get("series_number"))
        alts = {re.sub(r"\D", "", x) for x in res.pas.fields["series_number"].alternatives}
        if a != p and a not in alts and len(pas_pages) > 1:
            main_idx = (res.pas.debug.get("page") or 1) - 1
            res.previous_passports = find_previous_passports(pas_pages, skip_index=main_idx)

    # --- данные администратора из системы (провайдер выбирает запись по доменам заявления)
    if inp.admin is not None and hasattr(inp.admin, "get_admin"):
        try:
            res.admin = inp.admin.get_admin(res.app.get("domains.list") or [])
        except NotImplementedError as e:
            res.notes.append(str(e))
    else:
        res.admin = inp.admin
    if inp.admin is not None and res.admin is None:
        res.notes.append("Данные администратора для доменов заявления не найдены.")

    _evaluate(res)
    save_outputs(res)
    log(f"Готово: {res.decision.title}")
    return res


def _evaluate(res: CaseResult) -> None:
    inp = res.inp
    on = parsers.parse_date(inp.check_date) if inp.check_date else None
    checks = compare(res.app, res.pas, on)
    more, flags = extra_checks(res.app, res.pas, res.previous_passports, res.admin, checks)
    checks += more
    if not res.case_type.get("form"):  # тип без бланка — только проверки паспорта и данных из системы
        keep = {"Срок действия паспорта", "Серия ↔ год выдачи", "MRZ паспорта", "Заявитель — текущий администратор",
                "Паспорт в системе устарел", "Домены администратора", "Кем выдан", "Кем выдан ↔ код подразделения",
                "Кем выдан ↔ данные системы"}
        checks = [c for c in checks if c.name in keep]
    # подтверждения оператора: «нужна ручная проверка»/«проверить» по подтверждённой строке → в порядке
    for r in ROWS:
        if r["id"] in res.confirmed:
            for c in checks:
                if c.name in r["checks"] and c.status in (REVIEW, WARN):
                    c.status, c.detail = OK, (c.detail + "; " if c.detail else "") + "подтверждено оператором"
    res.checks, res.flags = checks, flags
    res.decision = decide(res.case_type, checks, flags, res.app.fields)
    res.record = build_record(res)


# ------------------------------------------------------------------ правки оператора

def _form_types() -> dict[str, str]:
    out = {}
    for f in load_forms():
        for k, spec in f["fields"].items():
            out[k] = spec["type"]
    return out


def recompute(res: CaseResult, overrides: dict | None = None, confirmed: set | list | None = None) -> CaseResult:
    """overrides: {row_id: {"app": "...", "passport": "..."}}; confirmed: id строк, подтверждённых оператором."""
    types = _form_types()
    for row_id, vals in (overrides or {}).items():
        row = next((r for r in ROWS if r["id"] == row_id), None)
        if row is None:
            continue
        res.overrides[row_id] = {**res.overrides.get(row_id, {}), **vals}
        if "app" in vals and row["app"] and res.app is not None:
            v = (vals["app"] or "").strip()
            keys = ["applicant_fio", "applicant_header"] if row_id == "fio" else row["app"][:1]
            if row_id == "fio":
                if "signature_fio" in res.app.fields:
                    keys.append("signature_fio")
            for key in keys:
                if "." in key:
                    res.app.fields[key] = FieldValue(v, 1.0, "manual")
                    continue
                old = res.app.fields.get(key)
                _store(res.app, key, types.get(key, "text"), v, 1.0, "manual", crop=old.crop if old else None)
                if key in res.app.fields:
                    res.app.fields[key].needs_review = False
            if row_id == "fio":
                res.app.fields["applicant_header.raw_fio"] = FieldValue(v, 1.0, "manual")
        if "passport" in vals and row["pas"] and res.pas is not None:
            v = (vals["passport"] or "").strip()
            if row["pas"] == "@fio":
                toks = names.norm(v).split()
                for k, t in zip(("surname", "given_name", "patronymic"), toks + ["", "", ""]):
                    res.pas.fields[k] = FieldValue(t, 1.0, "manual")
            else:
                if row["pas"] == "series_number":
                    v = parsers.normalize_passport(v) or v
                elif row["pas"] in ("issue_date", "birth_date"):
                    v = parsers.fmt(parsers.parse_date(v)) or v
                res.pas.fields[row["pas"]] = FieldValue(v, 1.0, "manual")
    if confirmed is not None:
        res.confirmed = set(confirmed)
    _evaluate(res)
    save_outputs(res, registry=False)
    return res


# ------------------------------------------------------------------ выгрузка «актуальных данных»

def build_record(res: CaseResult) -> dict:
    """Запись для автозаполнения формы во внутренней системе (схема doctool.case.v1).
    Личные данные берутся из паспорта (если он есть), остальное — из заявления."""
    a, p = res.app, res.pas
    ag = a.get if a is not None else (lambda k, d=None: d)
    pg = p.get if p is not None else (lambda k, d=None: d)
    sn = pg("series_number") or ag("passport") or ""
    m = re.match(r"(\d{4})\s?(\d{6})", re.sub(r"[^\d ]", "", sn))
    if p is not None and pg("surname"):
        surname, given, patr = pg("surname"), pg("given_name") or "", pg("patronymic") or ""
    else:
        toks = names.to_nominative(ag("applicant_fio") or ag("applicant_header") or "").split()
        surname, given, patr = (toks + ["", "", ""])[:3]
    doms = ag("domains.list") or []

    def src(row_id):
        r = next(x for x in ROWS if x["id"] == row_id)
        if r["pas"] and p is not None:
            k = "surname" if r["pas"] == "@fio" else r["pas"]
            if k in p.fields:
                return p.fields[k].source
        for k in r["app"]:
            if a is not None and k in a.fields and a.fields[k].value:
                return a.fields[k].source
        return ""

    rec = {
        "schema": "doctool.case.v1",
        "created": datetime.now().isoformat(timespec="seconds"),
        "case_id": res.case_id,
        "case_type": {"id": res.inp.case_type, "title": res.case_type.get("title")},
        "decision": res.decision.to_dict() if res.decision else None,
        "applicant": {
            "surname": names.norm(surname).title(), "given_name": names.norm(given).title(),
            "patronymic": names.norm(patr).title(),
            "birth_date": pg("birth_date") or ag("applicant_header.birth_date"),
            "birth_place": pg("birth_place"),
            "sex": pg("sex"),
            "passport": {"series": m.group(1) if m else "", "number": m.group(2) if m else "",
                         "issue_date": pg("issue_date") or ag("passport.issue_date"),
                         "issued_by": pg("issued_by") or ag("issued_by"),
                         "department_code": pg("department_code")},
            "address": ag("address"),
            "inn": ag("inn"),
        },
        "domains": [{"name": d, "punycode": parsers.to_punycode(d)} for d in doms],
        "services": ag("services"),
        "new_admin": {
            "kind": ag("new_admin_fio.kind") or ("org" if ag("new_admin_org") else ""),
            "name": ag("new_admin_fio") or ag("new_admin_org") or ag("new_admin_inline"),
            "emails": ag("new_admin_contact.emails") or ag("new_admin_org_contact.emails") or [],
            "phones": ag("new_admin_contact.phones") or ag("new_admin_org_contact.phones") or [],
            "contacts_raw": ag("new_admin_contact") or ag("new_admin_org_contact"),
        },
        "contract": ag("contract"),
        "application_date": ag("application_date"),
        "confirmed_by_operator": sorted(res.confirmed),
        "manual_edits": res.overrides,
        "sources": {r["id"]: src(r["id"]) for r in ROWS},
        "previous_passports": res.previous_passports,
        "system_update": _system_update(res),
        "checks": [c.to_dict() for c in res.checks],
        "files": {"application": res.inp.application, "passport": res.inp.passport,
                  "combined": res.inp.combined, "page_roles": {str(k): v for k, v in res.page_roles.items()}},
    }
    return rec


def _system_update(res: CaseResult) -> dict:
    """Что изменится во внутренней системе при автозаполнении (сравнение с текущими данными администратора)."""
    adm = res.admin
    if adm is None or adm.is_empty() or res.pas is None:
        return {}
    upd = {}
    sn = res.pas.get("series_number")
    if sn and adm.passport and re.sub(r"\D", "", sn) != re.sub(r"\D", "", adm.passport):
        upd["passport"] = {"было": adm.passport, "станет": sn}
    iss = res.pas.get("issue_date")
    if iss and adm.passport_issue_date and iss != adm.passport_issue_date:
        upd["passport_issue_date"] = {"было": adm.passport_issue_date, "станет": iss}
    by = res.pas.get("issued_by")
    if by and adm.passport_issued_by and names.norm(by) != names.norm(adm.passport_issued_by):
        upd["passport_issued_by"] = {"было": adm.passport_issued_by, "станет": by}
    return upd


# ------------------------------------------------------------------ сохранение

def save_outputs(res: CaseResult, registry: bool = True) -> None:
    d = res.case_dir
    full = {"case_id": res.case_id, "decision": res.decision.to_dict(),
            "application": res.app.to_dict() if res.app else None,
            "passport": res.pas.to_dict() if res.pas else None,
            "checks": [c.to_dict() for c in res.checks], "notes": res.notes}
    (d / "result.json").write_text(json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (d / "export.json").write_text(json.dumps(res.record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    save_html(d / "report.html", res.case_id, res.app, res.pas, res.checks, decision=res.decision,
              case_title=res.case_type.get("title", ""))
    if registry:
        xlsx = Path(res.inp.out_root) / "реестр_проверок.xlsx"
        try:
            append_excel(xlsx, res.case_id, res.app, res.pas, res.checks, decision=res.decision)
        except PermissionError:
            alt = Path(res.inp.out_root) / f"реестр_проверок_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            log(f"[!] Реестр открыт в Excel — записываю в {alt.name}")
            append_excel(alt, res.case_id, res.app, res.pas, res.checks, decision=res.decision)
