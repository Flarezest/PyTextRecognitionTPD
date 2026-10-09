"""Данные доменов из manager (Sd и S) и их сверка с заявлением и паспортом.

Данные приходят от расширения Chrome «PySimpleManager» (см. manager_bridge.py и doctool_extension/).
Этот модуль не зависит от способа получения данных: на входе — словари от расширения, на выходе —
строки таблицы доменов и проверки для вердикта.

Домены в таблице (веб-интерфейс, отчёт) выводятся по «проблемности» — см. PROBLEMS и sort_by_problem():
сначала свободные по WHOIS, затем те, у которых данные в Sd не сходятся с документами, и т. д.
В выгрузке (export.json) порядок доменов — как в заявлении.

С 0.6.0 у доменов физлиц есть статус идентификации через Госуслуги (ЕСИА): расширение открывает ссылку
«Идентификация через Госуслуги» со страницы Sd домена (идентификация относится к администратору домена,
а не к аккаунту), берёт поле state последней записи и файл данных ЕСИА. Сверка Sd ↔ ЕСИА — esia_compare().
С 0.7.2 — и у доменов юрлиц (ru_org): ИНН, КПП, название организации, юридический адрес (дом, улица, индекс).
На вердикт дела ЕСИА не влияет.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import fieldnorm, names, parsers
from .checks import Ctx, register
from .compare import FAIL, INFO, OK, REVIEW, WARN, Check, issuer_similarity
from .formspec import Fields
from .models import Extraction

# статусы строки «домен» (поиск в manager)
FOUND, NOT_FOUND, ERROR = "found", "not_found", "error"
KIND_RU = {"person": "физлицо", "org": "юрлицо", "": "?"}
PERSON_GROUPS = ("ru_pp",)
ORG_GROUPS = ("ru_org",)
_RANK = {FAIL: 4, REVIEW: 3, WARN: 2, INFO: 1, OK: 0, "": -1}


def split_domains(text: str) -> list[str]:
    """Список доменов из строки, введённой вручную: через пробел, запятую, точку с запятой или с новой строки.
    Имя, перенесённое на следующую строку после дефиса («business-⏎dvd.ru» — так бывает при копировании
    из PDF), сначала склеивается."""
    out = []
    for tok in re.split(r"[\s,;]+", parsers.join_wrapped_domains(text or "")):
        d = re.sub(r"^[a-z]+://", "", tok.strip().lower()).split("/")[0].rstrip(".")
        if d and "." in d and d not in out:
            out.append(d)
    return out


def unicode_name(domain: str) -> str:
    try:
        return domain.encode("ascii").decode("idna") if "xn--" in domain else domain
    except UnicodeError:
        return domain


def _date(s: str) -> str:
    """«2025-11-20 10:00:00» → «20.11.2025»; ДД.ММ.ГГГГ остаётся как есть."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else s


def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


def split_emails(s: str) -> list[str]:
    """«a@x.ru\nb@y.ru» / «a@x.ru, b@y.ru» → список без повторов."""
    out = []
    for e in re.split(r"[\s,;]+", s or ""):
        e = e.strip().strip("<>").lower()
        if "@" in e and e not in out:
            out.append(e)
    return out


def intl_holder(sd: dict) -> str:
    """Администратор (владелец) домена международной зоны — группа o_* в Sd: организация или ФИО."""
    g = sd.get
    org = g("o_company_ru") or g("o_company")
    if org:
        return org
    ru = " ".join(x for x in (g("o_last_name_ru"), g("o_first_name_ru"), g("o_patronimic_ru")) if x)
    return ru or " ".join(x for x in (g("o_last_name"), g("o_first_name"), g("o_patronimic")) if x)


@dataclass
class DomainInfo:
    domain: str
    status: str = ""                 # found | not_found | error
    error_code: str = ""
    error: str = ""
    service_id: str = ""
    account: str = ""                # user_id владельца услуги (страница S)
    bill_owner: str = ""             # логин владельца счёта (колонка «Владелец» в счетах)
    group: str = ""                  # группа контактов Sd: ru_pp, ru_org, …
    kind: str = ""                   # person | org
    sd: dict = field(default_factory=dict)   # поля Sd (белый список из расширения)
    provider: str = ""               # S: текущий регистратор домена
    s: dict = field(default_factory=dict)
    trustee: str = ""
    service_status: str = ""
    urls: dict = field(default_factory=dict)
    whois: dict = field(default_factory=dict)
    compare: list = field(default_factory=list)   # [{field, title, sd, doc, status, detail}]
    esia: dict = field(default_factory=dict)      # идентификация через Госуслуги: state, url, file_url, data (JSON)…
    verdict: str = ""                # итог по домену: ok | warn | review | fail | info
    note: str = ""
    loaded_at: str = ""

    # ---- удобные значения
    @property
    def fio(self) -> str:
        g = self.sd.get
        return " ".join(x for x in (g("person_r_surname"), g("person_r_name"), g("person_r_patronimic")) if x)

    @property
    def passport(self) -> str:
        s, n = self.sd.get("passport_series", ""), self.sd.get("passport_number_short", "")
        return f"{s} {n}".strip()

    @property
    def holder(self) -> str:
        if self.kind == "person":
            return self.fio
        if self.kind == "org":
            return self.sd.get("org_r") or self.sd.get("org", "")
        return intl_holder(self.sd)

    @property
    def emails(self) -> list[str]:
        """Контактный e-mail администратора из Sd: e_mail (.RU/.РФ/.SU) или o_email (остальные зоны)."""
        return split_emails(self.sd.get("e_mail") or self.sd.get("o_email") or "")

    def to_dict(self) -> dict:
        d = asdict(self)
        code = problem(self)
        es = esia_summary(self)
        d.update(fio=self.fio, passport=self.passport, holder=self.holder, kind_ru=KIND_RU.get(self.kind, "?"),
                 emails=self.emails,
                 last_admin_change=_date(self.sd.get("last_admin_change", "")),
                 problem=code, problem_ru=PROBLEM_RU[code], problem_rank=PROBLEM_RANK[code],
                 esia_rows=es["rows"], esia_status=es["status"], esia_state=es["state"], esia_ru=es["text"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DomainInfo":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})


# ------------------------------------------------------------------ «проблемность» домена (порядок вывода)

# (код, заголовок группы в таблице) — сверху вниз от самого проблемного
PROBLEMS = (
    ("free", "Свободен по WHOIS — домена нет в реестре"),
    ("mismatch", "Данные в Sd не сходятся с документами"),
    ("not_found", "Нет в manager (занят у другого регистратора или WHOIS не проверен)"),
    ("error", "Данные из manager не получены"),
    ("review", "Нужна ручная сверка"),
    ("warn", "Мелкие расхождения (дата выдачи, кем выдан)"),
    ("info", "К сведению"),
    ("unchecked", "Найден, сверка не выполнялась"),
    ("ok", "В порядке"),
)
PROBLEM_RU = dict(PROBLEMS)
PROBLEM_RANK = {code: n for n, (code, _) in enumerate(PROBLEMS)}
_VERDICT_PROBLEM = {FAIL: "mismatch", REVIEW: "review", WARN: "warn", INFO: "info", OK: "ok"}


def problem(info: "DomainInfo") -> str:
    """Код группы «проблемности» домена (PROBLEMS)."""
    if info.status == NOT_FOUND:
        return "free" if (info.whois or {}).get("status") == "free" else "not_found"
    if info.status != FOUND:
        return "error"
    return _VERDICT_PROBLEM.get(info.verdict, "unchecked")


def sort_by_problem(items: list) -> list:
    """Домены по убыванию проблемности; внутри группы — порядок заявления. Принимает DomainInfo или
    их to_dict() (по ключу problem_rank)."""
    def rank(x):
        if isinstance(x, dict):
            return x.get("problem_rank", PROBLEM_RANK.get(x.get("problem"), len(PROBLEMS)))
        return PROBLEM_RANK[problem(x)]
    return sorted(items, key=rank)


def from_extension(domain: str, reply: dict) -> DomainInfo:
    """Ответ расширения на lookup → DomainInfo."""
    info = DomainInfo(domain=domain, loaded_at=datetime.now().isoformat(timespec="seconds"))
    if not reply.get("ok"):
        err = reply.get("error") or {}
        info.error_code = err.get("code", "error")
        info.error = err.get("message", "ошибка")
        info.status = NOT_FOUND if info.error_code == "not_found" else ERROR
        return info
    data = reply.get("data") or {}
    sd = data.get("sd") or {}
    info.status = FOUND
    info.service_id = str(data.get("service_id", ""))
    info.s = data.get("s") or {}
    info.account = str(data.get("account") or info.s.get("user_id", ""))
    info.bill_owner = data.get("bill_owner") or ""
    info.esia = data.get("esia") or {}
    info.provider = info.s.get("provider", "")
    info.group = sd.get("group", "") or info.s.get("contype", "")
    info.sd = {k: v for k, v in (sd.get("fields") or {}).items() if k != "authinfo"}
    info.trustee = sd.get("trustee", "")
    info.service_status = sd.get("status", "")
    info.urls = data.get("urls") or {}
    contype = info.s.get("contype") or info.group
    if contype in PERSON_GROUPS or info.sd.get("person_r_surname"):
        info.kind = "person"
    elif contype in ORG_GROUPS or info.sd.get("org_r"):
        info.kind = "org"
    return info


# ------------------------------------------------------------------ ЕСИА (идентификация через Госуслуги)

# Sd (группа ru_pp) ↔ файл данных ЕСИА. В ЕСИА last_name — фамилия, middle_name — отчество.
# (поле Sd, поле ЕСИА, название, вид значения для fieldnorm.same)
ESIA_MAP = (
    ("person_r_surname", "last_name", "Фамилия", "text"),
    ("person_r_name", "first_name", "Имя", "text"),
    ("person_r_patronimic", "middle_name", "Отчество", "text"),
    ("birth_date", "birth_date", "Дата рождения", "date"),
    ("country", "citizenship", "Гражданство", "country"),
    ("passport_series", "rf_passport.series", "Серия паспорта", "digits"),
    ("passport_number_short", "rf_passport.number", "Номер паспорта", "digits"),
    ("passport_date", "rf_passport.issue_date", "Дата выдачи", "date"),
    ("passport_place", "rf_passport.issued_by", "Кем выдан", "issuer"),
)
# Sd (группа ru_org) ↔ файл данных ЕСИА организации (0.7.2). Название: «ООО» = «Общество с ограниченной
# ответственностью» и т. п. (fieldnorm.same_org). Дом в Sd — address_r_house; если отдельно заполнены корпус
# (Sd address_r_frame, ЕСИА frame) или строение (address_r_building, building), они тоже учитываются (_house_variants).
ESIA_MAP_ORG = (
    ("code", "inn", "ИНН", "digits"),
    ("kpp", "kpp", "КПП", "digits"),
    ("org_r", "full_name", "Название организации", "org"),
    ("address_r_street", "legal_address.street", "Улица (юр. адрес)", "street"),
    ("address_r_house", "legal_address.house", "Дом (юр. адрес)", "house"),
    ("address_r_zip", "legal_address.zip_code", "Индекс (юр. адрес)", "digits"),
)
ESIA_STATUS_RU = {"match": "Sd = ЕСИА", "warn": "Sd ≈ ЕСИА", "mismatch": "Sd ≠ ЕСИА", "none": "не проходил",
                  "no_data": "нет файла данных", "no_link": "нет ссылки на Sd", "error": "ошибка", "": ""}


def _esia_value(data: dict, path: str) -> str:
    cur = data
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return "" if cur is None else str(cur).strip()


_BEST = {fieldnorm.OK: 0, fieldnorm.WARN: 1, fieldnorm.FAIL: 2, fieldnorm.NONE: 3}


def _house_variants(house, frame, building) -> list[str]:
    """Варианты дома: вместе с корпусом/строением, если они заполнены отдельно, и только дом."""
    house = str(house or "").strip()
    extra = [f"{w} {str(v).strip()}" for w, v in (("корп.", frame), ("стр.", building)) if str(v or "").strip()]
    return [", ".join([house] + extra), house] if house and extra else [house]


def esia_compare(info: "DomainInfo") -> list[dict]:
    """Строки сверки Sd ↔ ЕСИА: [{field, esia_field, title, sd, esia, status, detail}] (status: ok/warn/fail/"").
    Физлицо (ru_pp) — ESIA_MAP, юрлицо (ru_org) — ESIA_MAP_ORG."""
    data = (info.esia or {}).get("data") or {}
    if info.kind not in ("person", "org") or not data:
        return []
    rows = []
    la = data.get("legal_address") or {}
    for sd_key, es_key, title, kind in (ESIA_MAP if info.kind == "person" else ESIA_MAP_ORG):
        if sd_key == "address_r_house":      # корпус и строение бывают отдельными полями — и в Sd, и в ЕСИА
            sd_vars = _house_variants(info.sd.get(sd_key), info.sd.get("address_r_frame"), info.sd.get("address_r_building"))
            es_vars = _house_variants(la.get("house"), la.get("frame"), la.get("building"))
        else:
            sd_vars, es_vars = [info.sd.get(sd_key, "") or ""], [_esia_value(data, es_key)]
        sv, ev, (st, det) = min(((v, w, fieldnorm.same(kind, v, w)) for v in sd_vars for w in es_vars),
                                key=lambda x: _BEST[x[2][0]])
        if st == fieldnorm.NONE and (sv or ev):
            det = "нет в ЕСИА" if sv else "нет в Sd"
        rows.append({"field": sd_key, "esia_field": es_key, "title": title, "sd": sv, "esia": ev, "status": st,
                     "detail": det})
    if info.kind == "org" and data.get("is_liquidated") is True:
        rows.append({"field": "", "esia_field": "is_liquidated", "title": "Организация действует", "sd": "",
                     "esia": "ликвидирована", "status": fieldnorm.FAIL,
                     "detail": "по данным ЕСИА организация ликвидирована"})
    return rows


def esia_summary(info: "DomainInfo") -> dict:
    """Итог ЕСИА домена: {status, state, text, rows}. status — ключ ESIA_STATUS_RU ("" — не применимо)."""
    e = info.esia or {}
    out = {"status": "", "state": e.get("state", "") or "", "text": "", "rows": []}
    if info.status != FOUND or info.kind not in ("person", "org"):
        return out
    if not e:
        out["status"] = "no_link"
    elif e.get("error") or e.get("json_error"):
        out["status"] = "error"
        out["text"] = "ошибка: " + (e.get("error") or e.get("json_error"))
    elif not e.get("count"):
        out["status"] = "none"
    elif not e.get("data"):
        out["status"] = "no_data"
    elif (e["data"].get("status") or "success") != "success":
        out["status"] = "error"
        out["text"] = f"ошибка: в файле ЕСИА status = {e['data']['status']}"
    else:
        rows = esia_compare(info)
        out["rows"] = rows
        bad = [r["title"] for r in rows if r["status"] == fieldnorm.FAIL]
        warn = [r["title"] for r in rows if r["status"] == fieldnorm.WARN]
        n = sum(1 for r in rows if r["status"] != fieldnorm.NONE)
        if not n:
            out["status"], out["text"] = "no_data", "в файле ЕСИА нет полей для сверки"
        elif bad:
            out["status"], out["text"] = "mismatch", f"Sd ≠ ЕСИА: {', '.join(bad)}"
        elif warn:
            out["status"], out["text"] = "warn", f"Sd ≈ ЕСИА: {', '.join(warn)} — написано по-разному"
        else:
            out["status"], out["text"] = "match", f"Sd = ЕСИА ({n} из {n})"
    if not out["text"]:
        out["text"] = ESIA_STATUS_RU[out["status"]]
    if out["state"]:
        out["text"] = f"{out['state']} · {out['text']}"
    return out


# ------------------------------------------------------------------ сверка одного домена

def reference(app: Extraction | None, pas: Extraction | None, previous: list | None = None) -> dict:
    """С чем сверять Sd: данные заявителя — из паспорта, если он есть, иначе из заявления."""
    ag = Fields(app).get if app is not None else (lambda k, d=None: d)
    pg = pas.get if pas is not None else (lambda k, d=None: d)
    fio = " ".join(x for x in (pg("surname"), pg("given_name"), pg("patronymic")) if x)
    if not fio:
        fio = names.to_nominative(ag("applicant_fio") or ag("applicant_header") or "")
    return {
        "fio": fio,
        "birth_date": pg("birth_date") or ag("applicant_header.birth_date") or "",
        "passport": pg("series_number") or ag("passport") or "",
        "issue_date": pg("issue_date") or ag("passport.issue_date") or "",
        "issued_by": pg("issued_by") or ag("issued_by") or "",
        "inn": ag("inn") or "",
        "previous": [p.get("series_number", "") for p in (previous or [])],
        "source": "паспорт" if pas is not None and pg("surname") else "заявление",
    }


def _row(field_id, title, sd, doc, status, detail=""):
    return {"field": field_id, "title": title, "sd": sd or "", "doc": doc or "", "status": status, "detail": detail}


def compare_domain(info: DomainInfo, ref: dict) -> None:
    """Заполняет info.compare и info.verdict."""
    rows = []
    if info.status != FOUND:
        info.compare, info.verdict = [], ""
        return
    if info.kind == "person":
        sd_fio = info.fio
        if sd_fio and ref["fio"]:
            sc, notes = names.fio_similarity(sd_fio, ref["fio"])
            st = OK if sc >= 90 else REVIEW if sc >= 75 else FAIL
            rows.append(_row("fio", "ФИО", sd_fio, ref["fio"], st, "" if st == OK else f"сходство {sc:.0f}%"))
        else:
            rows.append(_row("fio", "ФИО", sd_fio, ref["fio"], REVIEW, "нет данных для сверки"))
        sd_bd, doc_bd = info.sd.get("birth_date", ""), ref["birth_date"]
        if sd_bd and doc_bd:
            same = parsers.parse_date(sd_bd) == parsers.parse_date(doc_bd)
            rows.append(_row("birth_date", "Дата рождения", sd_bd, doc_bd, OK if same else FAIL))
        else:
            rows.append(_row("birth_date", "Дата рождения", sd_bd, doc_bd, REVIEW, "нет данных для сверки"))
        person_ok = all(r["status"] == OK for r in rows)
        sd_sn, doc_sn = info.passport, ref["passport"]
        same_passport = bool(sd_sn and doc_sn and _digits(sd_sn) == _digits(doc_sn))
        if sd_sn and doc_sn:
            if same_passport:
                rows.append(_row("passport", "Паспорт", sd_sn, doc_sn, OK))
            elif _digits(sd_sn) in {_digits(x) for x in ref["previous"]}:
                rows.append(_row("passport", "Паспорт", sd_sn, doc_sn, INFO,
                                 "в Sd прежний паспорт (есть в «ранее выданных») — данные в Sd нужно обновить"))
            elif person_ok:
                rows.append(_row("passport", "Паспорт", sd_sn, doc_sn, INFO,
                                 "в Sd другой паспорт: вероятно, паспорт заменён — данные в Sd нужно обновить"))
            else:
                rows.append(_row("passport", "Паспорт", sd_sn, doc_sn, FAIL, "другой паспорт и другое лицо"))
        else:
            rows.append(_row("passport", "Паспорт", sd_sn, doc_sn, REVIEW, "нет данных для сверки"))
        if same_passport:
            sd_iss, doc_iss = info.sd.get("passport_date", ""), ref["issue_date"]
            if sd_iss and doc_iss:
                same = parsers.parse_date(sd_iss) == parsers.parse_date(doc_iss)
                rows.append(_row("passport_date", "Дата выдачи", sd_iss, doc_iss, OK if same else WARN))
            sd_by, doc_by = info.sd.get("passport_place", ""), ref["issued_by"]
            if sd_by and doc_by:
                sim = issuer_similarity(sd_by, doc_by)
                rows.append(_row("passport_place", "Кем выдан", sd_by, doc_by, OK if sim >= 85 else WARN,
                                 f"сходство {sim:.0f}%"))
    elif info.kind == "org":
        org = info.sd.get("org_r") or info.sd.get("org", "")
        inn_sd, inn_doc = info.sd.get("code", ""), ref["inn"]
        if inn_sd and inn_doc:
            same = _digits(inn_sd) == _digits(inn_doc)
            rows.append(_row("code", "ИНН", inn_sd, inn_doc, OK if same else FAIL))
        rows.append(_row("org_r", "Организация", org, ref["fio"], REVIEW,
                         "домен зарегистрирован на организацию, заявление — от физлица: сверьте вручную"))
    else:
        rows.append(_row("group", "Тип контактов", info.group, "", REVIEW,
                         f"тип контактов «{info.group or '?'}» не поддерживается — сверьте вручную"))
    if info.trustee.lower() == "да":
        rows.append(_row("trustee", "Trustee", "Да", "", INFO, "подключена услуга Trustee"))
    info.compare = rows
    info.verdict = max((r["status"] for r in rows), key=lambda s: _RANK[s], default="")


# ------------------------------------------------------------------ проверки для вердикта

CHECK_FOUND = "Домены в manager"
CHECK_ADMIN = "Заявитель — администратор доменов (Sd)"
CHECK_PASSPORT = "Паспорт в Sd"
CHECK_SAME = "Администратор у доменов один"
CHECK_NAMES = (CHECK_FOUND, CHECK_ADMIN, CHECK_PASSPORT, CHECK_SAME)


def domain_checks(items: list[DomainInfo], ref: dict) -> tuple[list[Check], set[str]]:
    for it in items:
        compare_domain(it, ref)
    checks: list[Check] = []
    flags: set[str] = set()
    if not items:
        return checks, flags
    found = [i for i in items if i.status == FOUND]
    checks.append(_check_found(items))

    persons = [i for i in found if i.kind == "person"]
    others = [i for i in found if i.kind != "person"]
    if found:
        bad = [i for i in persons if any(r["field"] in ("fio", "birth_date", "passport") and r["status"] == FAIL
                                         for r in i.compare)]
        unsure = [i for i in found if i not in bad and i.verdict == REVIEW]
        holders = ", ".join(f"{i.domain}: {i.holder or '?'}" for i in found)
        if bad:
            why = "; ".join(f"{i.domain}: " + ", ".join(f"{r['title']} {r['sd']!r} ≠ {r['doc']!r}" for r in i.compare
                                                         if r["status"] == FAIL) for i in bad)
            checks.append(Check(CHECK_ADMIN, FAIL, ref["fio"], holders, why))
            flags.add("admin_mismatch")
        elif unsure or others:
            checks.append(Check(CHECK_ADMIN, REVIEW, ref["fio"], holders,
                                "; ".join(f"{i.domain}: " + ", ".join(r["detail"] or r["title"] for r in i.compare
                                                                     if r["status"] in (REVIEW, WARN, FAIL))
                                          for i in (unsure + [o for o in others if o not in unsure]))))
        else:
            checks.append(Check(CHECK_ADMIN, OK, ref["fio"], holders, f"совпадает ФИО и дата рождения ({len(persons)})"))

    pas_rows = [(i, r) for i in persons for r in i.compare
                if r["field"] in ("passport", "passport_date", "passport_place") and r["status"] in (INFO, WARN)]
    if pas_rows:
        st = WARN if any(r["status"] == WARN for _, r in pas_rows) else INFO
        checks.append(Check(CHECK_PASSPORT, st, ref["passport"], "; ".join(f"{i.domain}: {i.passport}" for i, _ in pas_rows),
                            "; ".join(f"{i.domain}: {r['title']} — {r['detail'] or (r['sd'] + ' ≠ ' + r['doc'])}"
                                      for i, r in pas_rows)))
    elif persons and all(any(r["field"] == "passport" and r["status"] == OK for r in i.compare) for i in persons):
        checks.append(Check(CHECK_PASSPORT, OK, ref["passport"], persons[0].passport, "паспорт в Sd тот же"))

    same = _check_same(found)
    if same is not None:
        checks.append(same)
    return checks, flags


def _check_found(items: list[DomainInfo]) -> Check:
    """«Домены в manager»: все ли домены найдены (не найденные — свободные по WHOIS первыми)."""
    found = [i for i in items if i.status == FOUND]
    missing = [i for i in items if i.status == NOT_FOUND]
    errors = [i for i in items if i.status == ERROR]
    if missing:
        missing = sort_by_problem(missing)    # свободные по WHOIS — первыми
        det = "; ".join(f"{i.domain}" + (f" ({_whois_short(i.whois)})" if i.whois else "") for i in missing)
        return Check(CHECK_FOUND, FAIL, ", ".join(i.domain for i in items), "", f"не найдены в manager: {det}")
    if errors:
        return Check(CHECK_FOUND, REVIEW, ", ".join(i.domain for i in items), "",
                     "не удалось получить данные: " + "; ".join(f"{i.domain} — {i.error}" for i in errors))
    return Check(CHECK_FOUND, OK, ", ".join(i.domain for i in items), "", f"найдены все ({len(found)})")


def _check_same(found: list[DomainInfo]) -> Check | None:
    """«Администратор у доменов один» — по паспорту (физлицо) или ИНН (юрлицо)."""
    if len(found) < 2:
        return None
    keys = {(i.kind, _digits(i.passport) or names.norm(i.fio)) if i.kind == "person"
            else (i.kind, _digits(i.sd.get("code", "")) or names.norm(i.holder)) for i in found}
    if len(keys) > 1:
        return Check(CHECK_SAME, WARN, "", "; ".join(f"{i.domain}: {i.holder}" for i in found),
                     "у доменов разные администраторы")
    return Check(CHECK_SAME, OK, "", found[0].holder, f"один администратор у {len(found)} доменов")


def summary_checks(items: list[DomainInfo]) -> list[Check]:
    """Проверки списка доменов без документов (выгрузка по доменам, загруженным кнопкой): все ли домены
    найдены в manager и один ли у них администратор. Длинные перечни сокращаются."""
    if not items:
        return []
    out = [_check_found(items)]
    same = _check_same([i for i in items if i.status == FOUND])
    if same is not None:
        out.append(same)
    for c in out:
        if c.name == CHECK_FOUND and len(items) > 5:
            c.application = f"{len(items)} доменов в списке"
        if c.name == CHECK_SAME and c.status == WARN:
            groups: dict[str, int] = {}
            for i in items:
                if i.status == FOUND:
                    groups[i.holder or "(не заполнено)"] = groups.get(i.holder or "(не заполнено)", 0) + 1
            c.passport = "; ".join(f"{h} — {n}" for h, n in groups.items())
    return out


@register("manager_domains", {CHECK_FOUND: "domains", CHECK_ADMIN: "domains", CHECK_PASSPORT: "passport",
                              CHECK_SAME: "domains"}, flags=("admin_mismatch",), needs="domains")
def _chk_manager_domains(ctx: Ctx):
    """Данные доменов из manager (Sd, S): найдены ли, тот ли администратор, тот ли паспорт."""
    out, flags = domain_checks(ctx.domains_sd, reference(ctx.app, ctx.pas, ctx.previous))
    ctx.flags |= flags
    return out


def _whois_short(w: dict) -> str:
    if not w:
        return ""
    if w.get("status") == "free":
        return "WHOIS: свободен"
    if w.get("status") == "registered":
        return "WHOIS: зарегистрирован" + (f", регистратор {w['registrar']}" if w.get("registrar") else "")
    return "WHOIS недоступен"


# ------------------------------------------------------------------ загрузка через расширение

class ManagerUnavailable(RuntimeError):
    pass


def lookup_domains(request, domains: list[str], log=print, whois_lookup=None, cancel=None,
                   timeout: float = 240, task: str = "") -> list[DomainInfo]:
    """Загружает данные доменов по одному. request(cmd, params, timeout, on_progress) → ответ расширения
    (см. manager_bridge.ExtensionBridge.request_sync). whois_lookup(domain) → WhoisInfo для ненайденных.
    task — номер загрузки: в её пределах расширение читает страницу и файл ЕСИА одной персоны один раз."""
    out: list[DomainInfo] = []
    stop_reason = ""
    for n, d in enumerate(domains, 1):
        if cancel is not None and cancel.is_set():
            stop_reason = "остановлено пользователем"
        if stop_reason:
            out.append(DomainInfo(domain=d, status=ERROR, error_code="skipped", error=f"не проверялся: {stop_reason}"))
            continue
        ascii_name = parsers.to_punycode(d)
        uni = unicode_name(d)
        log(f"manager [{n}/{len(domains)}]: {uni}…")
        try:
            reply = request("lookup", {"domain": uni, "ascii": ascii_name, "task": task}, timeout,
                            lambda m: log(f"manager: {m.get('message') or m.get('step')}"))
        except ManagerUnavailable as e:
            reply = {"ok": False, "error": {"code": "no_extension", "message": str(e)}}
        except TimeoutError:
            reply = {"ok": False, "error": {"code": "timeout", "message": "расширение не ответило вовремя"}}
        info = from_extension(d, reply)
        if info.status == FOUND:
            log(f"manager: {uni} — найден, {KIND_RU.get(info.kind, '?')}, {info.holder or '—'}, provider {info.provider or '—'}")
            es = esia_summary(info)
            if es["status"]:
                log(f"manager: {uni} — ЕСИА: {es['text']}")
        elif info.status == NOT_FOUND:
            log(f"manager: {uni} — не найден в manager")
            if whois_lookup is not None:
                log(f"WHOIS: {uni}…")
                w = whois_lookup(d)
                info.whois = w.to_dict()
                log(w.summary)
        else:
            log(f"[!] manager: {uni} — {info.error}")
            if info.error_code in ("not_logged_in", "no_extension"):
                stop_reason = info.error
        out.append(info)
    return out
