"""Сервисный слой: одна проверка «дела» (заявление + паспорт) целиком.

Используется и командной строкой, и обоими интерфейсами (Qt и веб):
    res = run_case(CaseInput(...))         # распознать, сверить, вынести вердикт, сохранить отчёты
    recompute(res, overrides, confirmed)   # применить правки оператора и пересчитать

Какие проверки выполнять, задаёт тип заявления (`checks:` в config/case_types.yaml), сами проверки —
в реестре checks.py. Строки таблицы — BASE_ROWS + `rows:` бланка; поля заявления берутся по ролям
(formspec.Fields), поэтому для нового бланка этот модуль менять не нужно.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import cv2

from . import checks as checklib
from . import llm_extract, names, parsers
from .application import _store, detect_form, extract_application, page_kind
from .compare import OK, REVIEW, WARN, Check
from .formspec import Fields, extra_rows, load_forms
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
    manager_autoload: bool = False         # после проверки загрузить данные Sd доменов заявления из manager
    llm_fields: bool = False               # (0.7.0) поля заявления читает нейросеть, а не регулярки бланка
    llm_model: str | None = None           # модель Ollama для полей (по умолчанию llm_extract.DEFAULT_MODEL, qwen3:8b)
    llm_think: bool | None = True          # модель «рассуждает» перед ответом (qwen3); None — как у модели


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
    domains_sd: list = field(default_factory=list)   # domains.DomainInfo — данные доменов из manager


# ------------------------------------------------------------------ строки таблицы (общие для GUI и выгрузки)

# Стандартные строки. app — роли полей заявления (formspec.ROLES; подполе через точку), первое непустое
# значение показывается в таблице; pas — поле паспорта («@fio» — фамилия, имя, отчество); edit — куда
# записывается правка оператора (по умолчанию — первая роль из app; «?» — только если поле есть в заявлении).
# Проверки строки берутся из реестра (checks.py: names), бланк может добавить свои строки (`rows:` в YAML).
BASE_ROWS = [
    {"id": "fio", "title": "ФИО", "app": ["applicant_fio", "applicant_header"], "pas": "@fio",
     "edit": ["applicant_fio", "applicant_header", "signature_fio?", "applicant_header.raw_fio"]},
    {"id": "birth_date", "title": "Дата рождения", "app": ["applicant_header.birth_date"], "pas": "birth_date"},
    {"id": "passport", "title": "Серия и номер паспорта", "app": ["passport"], "pas": "series_number"},
    {"id": "issue_date", "title": "Дата выдачи паспорта", "app": ["passport.issue_date"], "pas": "issue_date"},
    {"id": "issued_by", "title": "Кем выдан", "app": ["issued_by"], "pas": "issued_by"},
    {"id": "department_code", "title": "Код подразделения", "app": [], "pas": "department_code"},
    {"id": "birth_place", "title": "Место рождения", "app": [], "pas": "birth_place"},
    {"id": "address", "title": "Адрес регистрации", "app": ["address"], "pas": None},
    {"id": "inn", "title": "ИНН ИП", "app": ["inn"], "pas": None},
    {"id": "domains", "title": "Домен(ы)", "app": ["domains"], "pas": None},
    {"id": "new_admin", "title": "Новый администратор", "app": ["new_admin_fio", "new_admin_org", "new_admin_inline"],
     "pas": None},
    {"id": "new_admin_contact", "title": "Контакты нового администратора",
     "app": ["new_admin_contact", "new_admin_org_contact"], "pas": None},
    {"id": "contract", "title": "Договор / аккаунт", "app": ["contract"], "pas": None},
    {"id": "application_date", "title": "Дата заявления", "app": ["application_date"], "pas": None},
]


def rows_for(form: dict | None) -> list[dict]:
    """Строки таблицы для бланка: стандартные + `rows:` из YAML; у каждой — названия её проверок (checks)."""
    out = []
    for r in BASE_ROWS + extra_rows(form):
        r = {"app": [], "pas": None, **r}
        r["checks"] = list(dict.fromkeys(checklib.names_for_row(r["id"]) + list(r.get("checks") or [])))
        out.append(r)
    return out


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
    F = Fields(res.app)
    for r in rows_for(F.form):
        app_val, crop, app_src = "", None, ""
        for k in r["app"]:
            f = F.field(k) if res.app else None
            if f is not None:
                crop = crop or f.crop
                if f.value:
                    app_val = ", ".join(f.value) if isinstance(f.value, list) else str(f.value)
                    app_src = f.source
                    break
        if not crop and res.app is not None:  # фрагмент берём у «родительского» поля
            base = r["app"][0].split(".")[0] if r["app"] else None
            f = F.field(base) if base else None
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
        txt, _ = tesseract_text(downscale(cv2.cvtColor(p.image, cv2.COLOR_BGR2GRAY), 1800), psm=3)
        sc_app = sum(1 for k in form_kw if k.lower() in txt.lower())
        kind = next((k for f in forms for k in [page_kind(txt, f)] if k), None)   # заголовок бланка / продолжение
        if kind == "start" or (kind == "cont" and sc_app >= 1):
            roles[p.index] = "application"
            continue
        _, sc_pas = best_orientation(p.image, PASSPORT_KW)
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


def _llm(inp: CaseInput, res: "CaseResult") -> llm_extract.LLMConfig | None:
    """Галка «Поля заявления — нейросетью»: настройки модели для этого дела (None — регулярки бланка)."""
    if not inp.llm_fields:
        return None
    cfg = llm_extract.make_config(inp.llm_model, host=inp.ollama, think=inp.llm_think,
                                  cache_dir=str(Path(inp.out_root) / ".llm_cache"))
    log(f"Поля заявления читает нейросеть {cfg.model}" + (" (с рассуждением)" if cfg.think else ""))
    why = llm_extract.available(cfg)
    if why:
        log(f"[!] {why} — поля заявления будут прочитаны регулярками бланка")
        res.notes.append(f"Поля заявления: нейросеть недоступна ({why}) — прочитаны регулярками бланка.")
        return None
    return cfg


def _llm_notes(res: "CaseResult", cfg: llm_extract.LLMConfig | None) -> None:
    calls = (res.app.debug.get("llm") or []) if res.app is not None else []
    if cfg is None or not calls:
        return
    sec = sum((c.get("meta") or {}).get("seconds") or 0 for c in calls)
    res.notes.append(f"Поля заявления прочитаны нейросетью {cfg.model} ({round(sec)} с).")


def _fields_reader(res: "CaseResult") -> str:
    a = res.app
    if a is None or not a.fields:
        return ""
    calls = a.debug.get("llm") or []
    if calls:
        return "llm:" + str((calls[0].get("meta") or {}).get("model") or "")
    return "regex"


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
        llm_cfg = _llm(inp, res)
        with llm_extract.session(llm_cfg):
            res.app = extract_application(app_pages, case_dir, vlm if inp.app_handwritten else None, forms=forms,
                                          mode=inp.app_mode, handwritten=True if inp.app_handwritten else None,
                                          form_id=ct.get("form"))
        _llm_notes(res, llm_cfg)
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
    F = Fields(res.app)
    if res.pas is not None and F.get("passport") and res.pas.get("series_number"):
        a = re.sub(r"\D", "", F.get("passport"))
        p = re.sub(r"\D", "", res.pas.get("series_number"))
        alts = {re.sub(r"\D", "", x) for x in res.pas.fields["series_number"].alternatives}
        if a != p and a not in alts and len(pas_pages) > 1:
            main_idx = (res.pas.debug.get("page") or 1) - 1
            res.previous_passports = find_previous_passports(pas_pages, skip_index=main_idx)

    # --- данные администратора из системы (провайдер выбирает запись по доменам заявления)
    if inp.admin is not None and hasattr(inp.admin, "get_admin"):
        try:
            res.admin = inp.admin.get_admin(F.get("domains.list") or [])
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
    ctx = checklib.Ctx(res.app, res.pas, on=on, previous=res.previous_passports, admin=res.admin,
                       domains_sd=res.domains_sd)
    # проверки типа заявления (checks: в case_types.yaml; тип без бланка — только паспорт и данные из системы)
    checks, flags = checklib.run(checklib.ids_for(res.case_type), ctx)
    # подтверждения оператора: «нужна ручная проверка»/«проверить» по подтверждённой строке → в порядке
    for r in rows_for(ctx.form):
        if r["id"] in res.confirmed:
            for c in checks:
                if c.name in r["checks"] and c.status in (REVIEW, WARN):
                    c.status, c.detail = OK, (c.detail + "; " if c.detail else "") + "подтверждено оператором"
    res.checks, res.flags = checks, flags
    res.decision = decide(res.case_type, checks, flags, ctx.f)
    res.record = build_record(res)


def attach_domains(res: CaseResult, items: list) -> CaseResult:
    """Данные доменов из manager → проверки и вердикт пересчитываются, отчёты перезаписываются."""
    res.domains_sd = list(items)
    _evaluate(res)
    save_outputs(res, registry=False)
    return res


def case_domains(res: CaseResult) -> list[str]:
    """Домены из заявления (для загрузки данных из manager)."""
    return list(Fields(res.app).get("domains.list") or []) if res.app is not None else []


# ------------------------------------------------------------------ правки оператора

def _field_types(form: dict | None) -> dict[str, str]:
    """{поле: type} бланка заявления; без бланка — всех бланков."""
    out = {}
    for f in ([form] if form else load_forms()):
        for k, spec in f["fields"].items():
            out[k] = (spec or {}).get("type", "text")
    return out


def recompute(res: CaseResult, overrides: dict | None = None, confirmed: set | list | None = None) -> CaseResult:
    """overrides: {row_id: {"app": "...", "passport": "..."}}; confirmed: id строк, подтверждённых оператором."""
    F = Fields(res.app)
    types = _field_types(F.form)
    rows = {r["id"]: r for r in rows_for(F.form)}
    for row_id, vals in (overrides or {}).items():
        row = rows.get(row_id)
        if row is None:
            continue
        res.overrides[row_id] = {**res.overrides.get(row_id, {}), **vals}
        if "app" in vals and row["app"] and res.app is not None:
            v = (vals["app"] or "").strip()
            for role in row.get("edit") or row["app"][:1]:
                key = F.key(role.rstrip("?"))
                if role.endswith("?") and key not in res.app.fields:
                    continue
                if "." in key:
                    res.app.fields[key] = FieldValue(v, 1.0, "manual")
                    continue
                old = res.app.fields.get(key)
                _store(res.app, key, types.get(key, "text"), v, 1.0, "manual", crop=old.crop if old else None)
                if key in res.app.fields:
                    res.app.fields[key].needs_review = False
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
    F = Fields(a)
    ag = F.get if a is not None else (lambda k, d=None: d)
    pg = p.get if p is not None else (lambda k, d=None: d)
    sn = pg("series_number") or ag("passport") or ""
    m = re.match(r"(\d{4})\s?(\d{6})", re.sub(r"[^\d ]", "", sn))
    if p is not None and pg("surname"):
        surname, given, patr = pg("surname"), pg("given_name") or "", pg("patronymic") or ""
    else:
        toks = names.to_nominative(ag("applicant_fio") or ag("applicant_header") or "").split()
        surname, given, patr = (toks + ["", "", ""])[:3]
    doms = ag("domains.list") or []

    def src(r):
        if r["pas"] and p is not None:
            k = "surname" if r["pas"] == "@fio" else r["pas"]
            if k in p.fields:
                return p.fields[k].source
        for role in r["app"]:
            k = F.key(role)
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
        # все поля бланка как есть (ключи — имена полей из forms/<бланк>.yaml): новый бланк попадает
        # в выгрузку без изменений кода
        "application_form": a.doc_type if a is not None else None,
        "application_fields_reader": _fields_reader(res),      # (0.7.0) regex | llm:<модель>
        "application_fields": {k: f.value for k, f in a.fields.items() if "." not in k} if a is not None else {},
        "confirmed_by_operator": sorted(res.confirmed),
        "manual_edits": res.overrides,
        "sources": {r["id"]: src(r) for r in rows_for(F.form)},
        "previous_passports": res.previous_passports,
        "manager_domains": [d.to_dict() for d in res.domains_sd],
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
            "checks": [c.to_dict() for c in res.checks], "notes": res.notes,
            "manager_domains": [x.to_dict() for x in res.domains_sd]}
    (d / "result.json").write_text(json.dumps(full, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (d / "export.json").write_text(json.dumps(res.record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if res.app is not None and any(k in res.app.debug for k in ("llm", "llm_error")):
        # (0.7.0-dev) режим модели: что модель получила и ответила, время — для разбора и сравнения с регулярками
        dbg = {k: res.app.debug[k] for k in ("mode", "llm", "llm_error", "page_text", "application_pages")
               if k in res.app.debug}
        (d / "llm_debug.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    save_html(d / "report.html", res.case_id, res.app, res.pas, res.checks, decision=res.decision,
              case_title=res.case_type.get("title", ""), domains=[x.to_dict() for x in res.domains_sd])
    if res.domains_sd:
        from . import domain_report
        (d / "domains.html").write_text(domain_report.build([x.to_dict() for x in res.domains_sd], case_id=res.case_id,
                                                            case_title=res.case_type.get("title", ""),
                                                            checks=res.checks), encoding="utf-8")
    if registry:
        xlsx = Path(res.inp.out_root) / "реестр_проверок.xlsx"
        try:
            append_excel(xlsx, res.case_id, res.app, res.pas, res.checks, decision=res.decision)
        except PermissionError:
            alt = Path(res.inp.out_root) / f"реестр_проверок_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            log(f"[!] Реестр открыт в Excel — записываю в {alt.name}")
            append_excel(alt, res.case_id, res.app, res.pas, res.checks, decision=res.decision)
