"""Сверка заявления с паспортом и формальные проверки заявления.

Каждая проверка — функция с @register (реестр и порядок запуска — checks.py). compare() и extra_checks()
оставлены для совместимости: они запускают те же проверки из реестра."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date

from rapidfuzz import fuzz

from . import issuer as issuerlib
from . import names, parsers
from .checks import Ctx, register, run
from .formspec import Fields
from .models import Extraction

OK, WARN, FAIL, REVIEW, INFO = "ok", "warn", "fail", "review", "info"
STATUS_RU = {OK: "в порядке", WARN: "проверить", FAIL: "расхождение", REVIEW: "нужна ручная проверка",
             INFO: "к сведению"}


@dataclass
class Check:
    name: str
    status: str
    application: str = ""
    passport: str = ""
    detail: str = ""
    crop: str | None = None   # фрагмент скана заявления для ручной сверки

    def to_dict(self):
        d = asdict(self)
        d["status_ru"] = STATUS_RU[self.status]
        return d


# ------------------------------------------------------------------ нормализация «кем выдан»

ISSUER_ABBR = {
    "ОВД": "ОТДЕЛ ВНУТРЕННИХ ДЕЛ", "УВД": "УПРАВЛЕНИЕ ВНУТРЕННИХ ДЕЛ", "РОВД": "РАЙОННЫЙ ОТДЕЛ ВНУТРЕННИХ ДЕЛ",
    "ГОВД": "ГОРОДСКОЙ ОТДЕЛ ВНУТРЕННИХ ДЕЛ", "ОУФМС": "ОТДЕЛ УФМС", "ОФМС": "ОТДЕЛ ФМС",
    "ТП": "ТЕРРИТОРИАЛЬНЫЙ ПУНКТ", "ГУ": "ГЛАВНОЕ УПРАВЛЕНИЕ", "Г": "ГОРОД", "ГОР": "ГОРОД",
    "ОБЛ": "ОБЛАСТЬ", "Р-НА": "РАЙОНА", "Р-НЕ": "РАЙОНЕ", "Р-ОН": "РАЙОН", "ОТД": "ОТДЕЛЕНИЕ",
    "АДМ": "АДМИНИСТРАТИВНЫЙ", "КР": "КРАЙ", "РЕСП": "РЕСПУБЛИКА", "ОП": "ОТДЕЛ ПОЛИЦИИ",
}
STOP = {"ПО", "В", "И", "НА"}


def issuer_stems(s: str) -> list[str]:
    s = names.norm(s)
    toks = re.findall(r"[А-Я]+(?:-[А-Я]+)?", s)
    out = []
    for t in toks:
        exp = ISSUER_ABBR.get(t, t)
        for e in exp.split():
            if e not in STOP:
                out.append(e[:5])
    return out


def issuer_similarity(a: str, b: str) -> float:
    sa, sb = issuer_stems(a), issuer_stems(b)
    if not sa or not sb:
        return 0.0
    return float(fuzz.token_set_ratio(" ".join(sa), " ".join(sb)))


# ------------------------------------------------------------------ вспомогательное

def _d(s) -> date | None:
    return parsers.parse_date(s) if isinstance(s, str) else s


def _uncertain(field) -> bool:
    """Значение прочитано ненадёжно: локальной моделью, рукопись или низкая уверенность OCR."""
    if field is None or field.source == "manual":
        return False
    return field.source.startswith("vlm") or "рукопись" in field.source or field.confidence < 0.6


def _with_crop(c: Check, field) -> Check:
    if field is not None and getattr(field, "crop", None):
        c.crop = field.crop
    return c


def _fio_check(title: str, app_val: str, pass_fio: str, field) -> Check:
    return _with_crop(_fio_check0(title, app_val, pass_fio, field), field)


def _value_check(title, a, p, alternatives=(), app_field=None, pas_field=None) -> Check:
    c = _value_check0(title, a, p, alternatives, app_field)
    if c.status == FAIL and _uncertain(pas_field):
        c.status = REVIEW
        c.detail += "; значение в паспорте прочитано ненадёжно"
    return _with_crop(c, app_field)


def _fio_check0(title: str, app_val: str, pass_fio: str, field) -> Check:
    if not pass_fio:
        return Check(title, REVIEW, app_val or "", "", "в паспорте ФИО не распознано — сверьте вручную")
    if not app_val:
        if field is not None and field.crop:
            return Check(title, REVIEW, "", pass_fio, "рукописное поле — сверьте фрагмент с паспортом")
        return Check(title, WARN, "", pass_fio, "в заявлении не заполнено/не найдено")
    score, notes = names.fio_similarity(app_val, pass_fio)
    st = OK if score >= 97 else WARN if score >= 85 else FAIL
    if st != OK and names.ocr_typo_only(app_val, pass_fio):
        st = OK
        notes = notes + ["в паспорте вероятна ошибка распознавания (несловарное имя/отчество)"]
    if _uncertain(field) and st != OK:
        st = REVIEW
    return Check(title, st, app_val, pass_fio, f"сходство {score:.0f}%" + ("; " + "; ".join(notes) if notes else ""))


def _value_check0(title: str, a: str | None, p: str | None, alternatives=(), app_field=None) -> Check:
    if not a:
        if app_field is not None and app_field.crop:
            return Check(title, REVIEW, "", p or "", "рукописное поле — сверьте фрагмент с паспортом")
        return Check(title, WARN, "", p or "", "в заявлении не заполнено/не найдено")
    if not p:
        return Check(title, REVIEW, a, "", "в паспорте не распознано — сверьте вручную")
    na, np_ = re.sub(r"\D", "", a), re.sub(r"\D", "", p)
    if na == np_:
        return Check(title, OK, a, p)
    if any(re.sub(r"\D", "", x) == na for x in alternatives):
        return Check(title, OK, a, p, f"совпадает с одним из вариантов прочтения паспорта ({', '.join(alternatives)})")
    st = REVIEW if _uncertain(app_field) else FAIL
    diff = sum(1 for x, y in zip(na, np_) if x != y) + abs(len(na) - len(np_))
    return Check(title, st, a, p, f"различается символов: {diff}" + ("; возможна ошибка OCR" if diff <= 1 else ""))


# ------------------------------------------------------------------ «кем выдан» без заявления

SERIES_YEAR_LEAD = 3   # на сколько лет год бланка в серии может опережать год выдачи


def issuer_reading_check(pas: Extraction) -> Check:
    """«Кем выдан» в режиме «только паспорт»: распознано ли, похоже ли на название органа, надёжно ли."""
    f = pas.fields.get("issued_by")
    p_iss = pas.get("issued_by") or ""
    if not p_iss:
        return Check("Кем выдан", REVIEW, "", "", "в паспорте не распознано — введите вручную по паспорту")
    notes = []
    fixes = pas.debug.get("issued_by_fixes") if isinstance(pas.debug, dict) else None
    if fixes and f is not None and f.source == "ocr":
        notes.append("исправлены опечатки OCR: " + ", ".join(fixes))
    if not issuerlib.looks_like_authority(p_iss):
        return Check("Кем выдан", REVIEW, "", p_iss,
                     "не похоже на название органа (нет МВД/ОВД/УФМС/отдела…) — сверьте с паспортом")
    if _uncertain(f):
        src = "локальной моделью" if f.source.startswith("vlm") else f"OCR, уверенность {f.confidence:.2f}"
        return Check("Кем выдан", REVIEW, "", p_iss,
                     "; ".join([f"прочитано ненадёжно ({src}) — сверьте с паспортом"] + notes))
    src = {"mrz": "MRZ", "manual": "введено оператором"}.get(f.source, f"OCR, уверенность {f.confidence:.2f}")
    return Check("Кем выдан", OK, "", p_iss, "; ".join([f"прочитано ({src})"] + notes))


def issuer_region_check(pas: Extraction) -> Check | None:
    """Регион в «кем выдан» ↔ регион по коду подразделения (первые две цифры — код субъекта)."""
    p_iss, dept = pas.get("issued_by"), pas.get("department_code")
    if not p_iss or not dept:
        return None
    code = issuerlib.department_region(dept)
    if code is None:
        return Check("Кем выдан ↔ код подразделения", INFO, "", f"{p_iss} / {dept}",
                     f"регион для кода {dept} не известен — не сверялось")
    found = issuerlib.regions_in_text(p_iss)
    reg = f"{issuerlib.region_name(code)} ({code})"
    if not found:
        return Check("Кем выдан ↔ код подразделения", INFO, "", f"{p_iss} / {dept}",
                     f"регион в названии органа не указан; по коду — {reg}")
    if code in found:
        return Check("Кем выдан ↔ код подразделения", OK, "", f"{p_iss} / {dept}",
                     f"регион совпадает: {reg}")
    names_found = ", ".join(f"{issuerlib.region_name(c)} ({c})" for c in sorted(found))
    unreliable = _uncertain(pas.fields.get("issued_by")) or _uncertain(pas.fields.get("department_code"))
    return Check("Кем выдан ↔ код подразделения", REVIEW if unreliable else WARN, "", f"{p_iss} / {dept}",
                 f"в названии органа — {names_found}, а по коду подразделения — {reg}"
                 + ("; значения прочитаны ненадёжно" if unreliable else ""))


# ------------------------------------------------------------------ список доменов

def _domains_check(app: Extraction, doms: list[str], f: Fields | None = None) -> Check:
    """«Домен(ы)»: формат имён + полнота списка. Список считается прочитанным не целиком, если число доменов
    не совпало с «всего N» из заявления, остались обрывки без доменной зоны или OCR-прочтения разошлись."""
    ag = (f or Fields(app)).get
    bad = [d for d in doms if not re.fullmatch(r"(?:[a-zа-яё0-9](?:[a-zа-яё0-9\-]{0,61}[a-zа-яё0-9])?\.)+[a-zа-яё\-]{2,}", d)]
    stated = ag("domains.stated_count")
    frags = ag("domains.fragments") or []
    mixed = ag("domains.mixed") or []
    uncertain = ag("domains.uncertain") or []
    joined = ag("domains.joined") or []
    problems = []
    if stated is not None and stated != len(doms):
        problems.append(f"в заявлении указано доменов: {stated}, распознано: {len(doms)}")
    if frags:
        problems.append("обрывки без доменной зоны (имя не дочитано или перенесено без дефиса): " + ", ".join(frags))
    if mixed:
        problems.append("в имени смешаны кириллица и латиница (вероятна ошибка распознавания): " + ", ".join(mixed))
    if uncertain:
        problems.append("прочитаны по-разному при повторном распознавании: " + ", ".join(uncertain))
    notes = []
    if joined:
        notes.append("собраны из частей на соседних строках: " + ", ".join(joined))
    idn = [f"{d} → {parsers.to_punycode(d)}" for d in doms if parsers.to_punycode(d) != d]
    if len(doms) <= 5:
        notes.append("punycode: " + ", ".join(parsers.to_punycode(d) for d in doms))
    else:
        notes.append(f"доменов: {len(doms)}" + (f" (указано в заявлении: {stated})" if stated is not None else "")
                     + ("; punycode: " + ", ".join(idn) if idn else ""))
    if bad:
        return Check("Домен(ы)", FAIL, ", ".join(doms), "", "некорректно: " + ", ".join(bad))
    return Check("Домен(ы)", REVIEW if problems else OK, ", ".join(doms), "", "; ".join(problems + notes))


# ------------------------------------------------------------------ проверки (реестр — checks.py)
#
# Порядок функций ниже = порядок проверок в отчёте. Поля заявления берутся по ролям (ctx.f.get("роль")),
# поэтому проверки работают с любым бланком, где поля сопоставлены этим ролям (formspec.ROLES).

# --- сверка заявления с паспортом

@register("fio_header", {"ФИО в шапке («от …»)": "fio"}, needs="passport")
def _chk_fio_header(ctx: Ctx):
    """ФИО в шапке заявления ↔ паспорт."""
    f = ctx.f
    return _fio_check("ФИО в шапке («от …»)", f.get("applicant_header.raw_fio") or f.get("applicant_header"),
                      ctx.pass_fio, f.field("applicant_header"))


@register("fio_text", {"ФИО в тексте («Я, …»)": "fio"}, needs="passport")
def _chk_fio_text(ctx: Ctx):
    """ФИО в тексте «Я, …» ↔ паспорт."""
    return _fio_check("ФИО в тексте («Я, …»)", ctx.f.get("applicant_fio"), ctx.pass_fio, ctx.f.field("applicant_fio"))


@register("fio_signature", {"ФИО у подписи": "fio"}, needs="passport")
def _chk_fio_signature(ctx: Ctx):
    """ФИО у подписи ↔ паспорт (если в бланке есть такое поле)."""
    f = ctx.f
    if f.get("signature_fio") or f.field("signature_fio"):
        return _fio_check("ФИО у подписи", f.get("signature_fio"), ctx.pass_fio, f.field("signature_fio"))
    return None


def _alt(pas: Extraction, k: str):
    return pas.fields[k].alternatives if k in pas.fields else ()


@register("birth_date", {"Дата рождения": "birth_date"}, needs="passport")
def _chk_birth_date(ctx: Ctx):
    """Дата рождения в заявлении ↔ паспорт."""
    f, pas = ctx.f, ctx.pas
    return _value_check("Дата рождения", f.get("applicant_header.birth_date"), pas.get("birth_date"),
                        alternatives=_alt(pas, "birth_date"), app_field=f.field("applicant_header"),
                        pas_field=pas.fields.get("birth_date"))


@register("passport_number", {"Серия и номер паспорта": "passport"}, needs="passport")
def _chk_passport_number(ctx: Ctx):
    """Серия и номер паспорта в заявлении ↔ паспорт."""
    f, pas = ctx.f, ctx.pas
    sn = pas.fields.get("series_number")
    return _value_check("Серия и номер паспорта", f.get("passport"), pas.get("series_number"),
                        alternatives=(sn.alternatives if sn else ()), app_field=f.field("passport"), pas_field=sn)


@register("passport_issue_date", {"Дата выдачи паспорта": "issue_date"}, needs="passport")
def _chk_passport_issue_date(ctx: Ctx):
    """Дата выдачи паспорта в заявлении ↔ паспорт."""
    f, pas = ctx.f, ctx.pas
    return _value_check("Дата выдачи паспорта", f.get("passport.issue_date"), pas.get("issue_date"),
                        alternatives=_alt(pas, "issue_date"), app_field=f.field("passport"),
                        pas_field=pas.fields.get("issue_date"))


@register("issued_by", {"Кем выдан": "issued_by"}, crop="issued_by", needs="passport")
def _chk_issued_by(ctx: Ctx):
    """«Кем выдан» в заявлении ↔ паспорт; без заявления — оценка самого прочтения."""
    f, pas = ctx.f, ctx.pas
    a_iss, p_iss = f.get("issued_by"), pas.get("issued_by")
    if ctx.app.doc_type == "none" and not a_iss:
        # проверка только паспорта: сверять не с чем — оцениваем само прочтение
        return issuer_reading_check(pas)
    if a_iss and p_iss:
        sim = issuer_similarity(a_iss, p_iss)
        st = OK if sim >= 85 else WARN if sim >= 60 else FAIL
        if st != OK and (_uncertain(pas.fields.get("issued_by")) or _uncertain(f.field("issued_by"))):
            st = REVIEW
        return Check("Кем выдан", st, a_iss, p_iss, f"сходство {sim:.0f}% (с учётом сокращений)")
    fld = f.field("issued_by")
    return Check("Кем выдан", REVIEW if (fld and fld.crop) or not p_iss else WARN,
                 a_iss or "", p_iss or "", "нет значения для сравнения")


@register("issuer_region", {"Кем выдан ↔ код подразделения": ("issued_by", "department_code")}, needs="passport")
def _chk_issuer_region(ctx: Ctx):
    """Регион в «кем выдан» ↔ код подразделения."""
    return issuer_region_check(ctx.pas)


def _pick_date(ctx: Ctx, pas_key: str, app_role: str, app_field_role: str):
    """Дату берём из самого надёжного источника: паспорт (MRZ/OCR) → текст заявления → модель."""
    cands = [(ctx.pas.get(pas_key), ctx.pas.fields.get(pas_key)), (ctx.f.get(app_role), ctx.f.field(app_field_role))]
    cands = [(v, fl) for v, fl in cands if v and _d(v)]
    reliable = [(v, fl) for v, fl in cands if not _uncertain(fl)]
    v, _ = (reliable or cands or [(None, None)])[0]
    return _d(v) if v else None, bool(reliable)


@register("series_year", {"Серия ↔ год выдачи": "issue_date"}, needs="passport")
def _chk_series_year(ctx: Ctx):
    """Год бланка в серии ↔ год выдачи (бланки печатают с опережением: «46 19» при выдаче в 2018 г. — норма)."""
    s = re.sub(r"\D", "", ctx.pas.get("series_number") or ctx.f.get("passport") or "")
    i, _ = _pick_date(ctx, "issue_date", "passport.issue_date", "passport")
    if not (len(s) >= 4 and i):
        return None
    yy = int(s[2:4]); blank_year = 2000 + yy if yy < 90 else 1900 + yy
    lead = blank_year - i.year
    if lead > SERIES_YEAR_LEAD:
        return Check("Серия ↔ год выдачи", WARN, "", s[:4], f"бланк {blank_year} г., а выдан в {i.year} г. — так не бывает")
    if lead > 1:
        return Check("Серия ↔ год выдачи", WARN, "", s[:4],
                     f"бланк {blank_year} г., выдан в {i.year} г. — опережение {lead} г., "
                     "встречается редко, проверьте дату выдачи")
    if lead == 1:
        return Check("Серия ↔ год выдачи", INFO, "", s[:4],
                     f"бланк {blank_year} г., выдан в {i.year} г. — бланки печатают с опережением, допустимо")
    if i.year - blank_year > 5:
        return Check("Серия ↔ год выдачи", INFO, "", s[:4], f"бланк {blank_year} г., выдан в {i.year} г. (старый бланк)")
    return None


@register("mrz", {"MRZ паспорта": ()}, needs="passport")
def _chk_mrz(ctx: Ctx):
    """Контрольные цифры машиночитаемой зоны."""
    if "mrz" not in ctx.pas.debug:
        return None
    m = ctx.pas.debug["mrz"]
    return Check("MRZ паспорта", OK if m["valid"] else WARN, "", " / ".join(m["raw"]),
                 "контрольные цифры сошлись" if m["valid"] else
                 "не сошлись: " + ", ".join(k for k, v in m["checks"].items() if not v))


@register("passport_loaded", {"Паспорт приложен": "passport"})
def _chk_passport_loaded(ctx: Ctx):
    """Паспорт не загружен — заявление сверить не с чем (без этой проверки такое дело получало «Принять»)."""
    if ctx.pas is not None:
        return None
    return Check("Паспорт приложен", REVIEW, ctx.f.get("passport") or "", "",
                 "паспорт не загружен — сверка заявления с паспортом не выполнялась")


@register("address", {"Адрес регистрации": "address"}, crop="address", needs="passport")
def _chk_address(ctx: Ctx):
    """Адрес регистрации — к сведению (с отметкой в паспорте не сверяется)."""
    return Check("Адрес регистрации", INFO, ctx.f.get("address") or "", "",
                 "с отметкой о регистрации в паспорте автоматически не сверяется")


# --- проверки самого заявления

@register("fio_header_vs_text", {"ФИО: шапка ↔ «Я, …»": "fio"})
def _chk_fio_header_vs_text(ctx: Ctx):
    """ФИО в шапке ↔ ФИО в тексте заявления."""
    f = ctx.f
    hdr, me = f.get("applicant_header"), f.get("applicant_fio")
    if not (hdr and me):
        return None
    sc, _ = names.fio_similarity(f.get("applicant_header.raw_fio") or hdr, me)
    hand = any("рукопись" in (f.field(k).source or "") for k in ("applicant_header", "applicant_fio"))
    if sc < 97:
        return Check("ФИО: шапка ↔ «Я, …»", REVIEW if hand else WARN if sc >= 85 else FAIL, hdr, me,
                     f"сходство {sc:.0f}%" + ("; рукопись — сверьте по фрагментам" if hand else ""))
    return None


@register("inn", {"ИНН ИП: контрольные цифры": "inn"})
def _chk_inn(ctx: Ctx):
    """Контрольные разряды ИНН ИП."""
    inn = ctx.f.get("inn")
    if not inn:
        return None
    ok = parsers.inn_valid(inn)
    return Check("ИНН ИП: контрольные цифры", OK if ok else FAIL, inn, "", "" if ok else "контрольные разряды не сходятся")


@register("domains", {"Домен(ы)": "domains"}, crop="domains")
def _chk_domains(ctx: Ctx):
    """Домены указаны, имена корректны, список прочитан целиком."""
    doms = ctx.f.get("domains.list") or []
    if doms:
        return _domains_check(ctx.app, doms, ctx.f)
    fld = ctx.f.field("domains")
    return Check("Домен(ы)", REVIEW if fld and fld.crop else FAIL, "", "", "домен не указан/не распознан")


def _new_admin(ctx: Ctx) -> str | None:
    f = ctx.f
    return f.get("new_admin_fio") or f.get("new_admin_org") or f.get("new_admin_inline")


@register("new_admin", {"Новый администратор указан": "new_admin"}, crop="new_admin_fio")
def _chk_new_admin(ctx: Ctx):
    """Новый администратор указан."""
    new_admin = _new_admin(ctx)
    fld = ctx.f.field("new_admin_fio")
    return Check("Новый администратор указан", OK if new_admin else (REVIEW if fld and fld.crop else FAIL),
                 new_admin or "", "", "" if new_admin else "не указан/не распознан")


@register("new_admin_text_vs_table", {"Новый администратор: текст ↔ таблица": "new_admin"})
def _chk_new_admin_text_vs_table(ctx: Ctx):
    """Новый администратор в тексте ↔ в таблице бланка."""
    f = ctx.f
    if f.get("new_admin_inline") and f.get("new_admin_fio"):
        sc, _ = names.fio_similarity(f.get("new_admin_inline.raw_fio") or f.get("new_admin_inline"), f.get("new_admin_fio"))
        if sc < 97:
            return Check("Новый администратор: текст ↔ таблица", WARN, f.get("new_admin_inline"),
                         f.get("new_admin_fio"), f"сходство {sc:.0f}%")
    return None


@register("new_admin_not_applicant", {"Новый администратор ≠ заявитель": "new_admin"})
def _chk_new_admin_not_applicant(ctx: Ctx):
    """Права не передаются самому заявителю (по паспорту)."""
    new_admin = _new_admin(ctx)
    if new_admin and ctx.pas is not None and ctx.pas.get("surname"):
        sc, _ = names.fio_similarity(new_admin, ctx.pass_fio)
        if sc >= 90:
            return Check("Новый администратор ≠ заявитель", FAIL, new_admin, ctx.pass_fio, "права передаются тому же лицу?")
    return None


@register("new_admin_contact", {"Контакты нового администратора": "new_admin_contact"}, crop="new_admin_contact")
def _chk_new_admin_contact(ctx: Ctx):
    """Контакты нового администратора указаны."""
    contact = ctx.f.get("new_admin_contact") or ctx.f.get("new_admin_org_contact")
    fld = ctx.f.field("new_admin_contact")
    return Check("Контакты нового администратора", OK if contact else (REVIEW if fld and fld.crop else WARN),
                 contact or "", "", "" if contact else "не указаны/не распознаны")


@register("contract", {"Договор/аккаунт нового администратора": "contract"}, crop="contract")
def _chk_contract(ctx: Ctx):
    """Договор/аккаунт нового администратора указан."""
    val = ctx.f.get("contract")
    fld = ctx.f.field("contract")
    if val and not re.search(r"\d{4,}|@|[A-Za-zА-Яа-яЁё]{3,}", val):
        # «1 © >» — обрывок рукописи, прочитанный OCR: значения по сути нет
        return Check("Договор/аккаунт нового администратора", REVIEW, val, "", "прочитано неразборчиво — сверьте фрагмент")
    if val and _uncertain(fld):
        return Check("Договор/аккаунт нового администратора", REVIEW, val, "", "прочитано ненадёжно — сверьте фрагмент")
    return Check("Договор/аккаунт нового администратора", OK if val else (REVIEW if fld and fld.crop else WARN),
                 val or "", "", "" if val else "не указан/не распознан")


@register("application_date", {"Дата заявления": "application_date"}, crop="application_date")
def _chk_application_date(ctx: Ctx):
    """Дата заявления заполнена и не в будущем."""
    app_date = ctx.app_date
    if app_date:
        st = FAIL if app_date > date.today() else OK
        return Check("Дата заявления", st, f"{app_date:%d.%m.%Y}", "",
                     "дата в будущем" if st == FAIL else f"{(date.today() - app_date).days} дн. назад")
    fld = ctx.f.field("application_date")
    return Check("Дата заявления", REVIEW if fld and fld.crop else WARN, ctx.f.get("application_date.raw") or "",
                 "", "не заполнена/не распознана")


# --- прежний паспорт и данные из внутренней системы

def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


@register("old_passport", {"Указан прежний паспорт": "passport"}, flags=("old_passport",))
def _chk_old_passport(ctx: Ctx):
    """В заявлении указан прежний паспорт (по «ранее выданным» или по совпадению ФИО/даты рождения)."""
    f, pas = ctx.f, ctx.pas
    if not (pas is not None and f.get("passport") and pas.get("series_number")):
        return None
    a_sn, p_sn = _digits(f.get("passport")), _digits(pas.get("series_number"))
    alts = {_digits(x) for x in (pas.fields["series_number"].alternatives or [])}
    if a_sn == p_sn or a_sn in alts:
        return None
    prev = next((r for r in (ctx.previous or []) if _digits(r["series_number"]) == a_sn), None)
    pf = pas.get
    pass_fio = ctx.pass_fio
    fio_score = names.fio_similarity(f.get("applicant_fio") or f.get("applicant_header.raw_fio") or "", pass_fio)[0]
    same_person = fio_score >= 85 or (f.get("applicant_header.birth_date") and
                                      f.get("applicant_header.birth_date") == pf("birth_date"))
    a_iss, p_iss = parsers.parse_date(f.get("passport.issue_date") or ""), parsers.parse_date(pf("issue_date") or "")
    older = bool(a_iss and p_iss and a_iss < p_iss)
    uncertain = _uncertain(f.field("passport")) or _uncertain(pas.fields.get("series_number")) \
        or _uncertain(pas.fields.get("issue_date"))
    if prev:
        ctx.flags.add("old_passport")
        return Check("Указан прежний паспорт", FAIL, f.get("passport"), pas.get("series_number"),
                     f"номер из заявления найден в «Сведениях о ранее выданных паспортах» "
                     f"(стр. {prev['page']}{', код ' + prev['department_code'] if prev['department_code'] else ''})")
    if same_person and (older or not uncertain):
        st = REVIEW if uncertain else FAIL
        why = "дата выдачи в заявлении раньше, чем у предъявленного паспорта" if older else \
              "ФИО/дата рождения совпадают, а серия и номер — нет"
        if st == FAIL:
            ctx.flags.add("old_passport")
        return Check("Указан прежний паспорт", st, f.get("passport"), pas.get("series_number"),
                     f"вероятно, прежний паспорт: {why}")
    return None


@register("system_admin", {"Заявитель — текущий администратор": "fio", "Паспорт в системе устарел": "passport",
                           "Кем выдан ↔ данные системы": "issued_by", "Домены администратора": "domains"},
          flags=("admin_mismatch",))
def _chk_system_admin(ctx: Ctx):
    """Данные текущего администратора из внутренней системы: тот же человек, паспорт, «кем выдан», домены."""
    admin, pas, f = ctx.admin, ctx.pas, ctx.f
    if admin is None or admin.is_empty():
        return None
    out: list[Check] = []
    pf = pas.get if pas is not None else (lambda k, d=None: d)
    cur_fio = ctx.pass_fio or f.get("applicant_fio") or f.get("applicant_header") or ""
    sc = names.fio_similarity(admin.fio, cur_fio)[0] if admin.fio and cur_fio else 0
    bd_ok = not admin.birth_date or not pf("birth_date") or admin.birth_date == pf("birth_date")
    if sc >= 90 and bd_ok:
        out.append(Check("Заявитель — текущий администратор", OK, cur_fio, admin.fio,
                         f"по данным системы ({admin.source or 'введено вручную'})"))
        cur_sn = pf("series_number") or f.get("passport")
        if admin.passport and cur_sn and _digits(admin.passport) != _digits(cur_sn):
            out.append(Check("Паспорт в системе устарел", INFO, cur_sn, admin.passport,
                             "в системе другой паспорт — при автозаполнении будет обновлён"))
        # «кем выдан» в системе — сверяем, только если в системе тот же паспорт
        cur_iss = pf("issued_by") or f.get("issued_by")
        sys_iss = getattr(admin, "passport_issued_by", "")
        same_pas = not admin.passport or not cur_sn or _digits(admin.passport) == _digits(cur_sn)
        if sys_iss and cur_iss and same_pas:
            sim = issuer_similarity(sys_iss, cur_iss)
            fld = pas.fields.get("issued_by") if pas is not None else None
            st = OK if sim >= 85 else (REVIEW if _uncertain(fld) else WARN)
            out.append(Check("Кем выдан ↔ данные системы", st, cur_iss, sys_iss, f"сходство {sim:.0f}% (с учётом сокращений)"))
    else:
        out.append(Check("Заявитель — текущий администратор", FAIL, cur_fio, admin.fio,
                         f"сходство ФИО {sc:.0f}%" + ("" if bd_ok else "; дата рождения отличается")))
        ctx.flags.add("admin_mismatch")
    doms = f.get("domains.list") or []
    if admin.domains and doms:
        foreign = [d for d in doms if d not in [x.lower() for x in admin.domains]]
        if foreign:
            out.append(Check("Домены администратора", FAIL, ", ".join(doms), ", ".join(admin.domains),
                             "не числятся за администратором: " + ", ".join(foreign)))
            ctx.flags.add("admin_mismatch")
    return out


# ------------------------------------------------------------------ прежний интерфейс (тесты, сторонние скрипты)

COMPARE_IDS = ("fio_header", "fio_text", "fio_signature", "birth_date", "passport_number", "passport_issue_date",
               "issued_by", "issuer_region", "series_year", "mrz", "address",
               "fio_header_vs_text", "inn", "domains", "new_admin", "new_admin_text_vs_table",
               "new_admin_not_applicant", "new_admin_contact", "contract", "application_date")
EXTRA_IDS = ("old_passport", "system_admin")


def compare(app: Extraction, pas: Extraction | None, on: date | None = None) -> list[Check]:
    """Сверка с паспортом и формальные проверки заявления (без данных из системы и manager)."""
    return run(COMPARE_IDS, Ctx(app, pas, on=on))[0]


def verdict(checks: list[Check]) -> str:
    sts = {c.status for c in checks}
    if FAIL in sts:
        return "Есть расхождения"
    if REVIEW in sts or WARN in sts:
        return "Требуется ручная проверка"
    return "Расхождений не найдено"


def extra_checks(app: Extraction, pas: Extraction | None, previous: list[dict] | None,
                 admin, checks: list[Check]) -> tuple[list[Check], set[str]]:
    """«Старый паспорт», данные администратора из системы, служебные флаги для правил вердикта."""
    ctx = Ctx(app, pas, previous=list(previous or []), admin=admin, checks=list(checks))
    return run(EXTRA_IDS, ctx)
