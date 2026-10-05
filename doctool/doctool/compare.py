"""Сверка заявления с паспортом и формальные проверки."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta

from rapidfuzz import fuzz

from . import issuer as issuerlib
from . import names, parsers
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


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 февраля
        return d.replace(year=d.year + years, day=28)


def passport_validity(birth: date, issue: date, on: date) -> tuple[str, str]:
    """Паспорт РФ меняют в 20 и 45 лет; после дня рождения даётся 90 дней на замену."""
    age_at_issue = (issue - birth).days / 365.25
    if age_at_issue < 14 - 0.01:
        return FAIL, f"дата выдачи раньше 14-летия ({age_at_issue:.1f} лет)"
    limit_age = 20 if age_at_issue < 20 else 45 if age_at_issue < 45 else None
    if limit_age is None:
        return OK, "выдан после 45 лет — бессрочный"
    bday = _add_years(birth, limit_age)
    expires = bday + timedelta(days=90)
    if on > expires:
        return FAIL, f"недействителен с {expires:%d.%m.%Y} (достиг {limit_age} лет {bday:%d.%m.%Y})"
    if on >= bday:
        return WARN, f"подлежит замене: {limit_age} лет исполнилось {bday:%d.%m.%Y}, действителен до {expires:%d.%m.%Y}"
    days = (bday - on).days
    st = INFO if days < 180 else OK
    return st, f"действителен до {limit_age}-летия ({bday:%d.%m.%Y}, +90 дней на замену)"


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


# ------------------------------------------------------------------ главная функция

def compare(app: Extraction, pas: Extraction | None, on: date | None = None) -> list[Check]:
    checks: list[Check] = []
    ag = app.get
    app_date = parsers.parse_date(ag("application_date") or "")
    on = on or app_date or date.today()

    if pas is not None:
        pg = pas.get
        pass_fio = " ".join(x for x in (pg("surname"), pg("given_name"), pg("patronymic")) if x)
        checks.append(_fio_check("ФИО в шапке («от …»)", ag("applicant_header.raw_fio") or ag("applicant_header"),
                                 pass_fio, app.fields.get("applicant_header")))
        checks.append(_fio_check("ФИО в тексте («Я, …»)", ag("applicant_fio"), pass_fio, app.fields.get("applicant_fio")))
        if ag("signature_fio") or app.fields.get("signature_fio"):
            checks.append(_fio_check("ФИО у подписи", ag("signature_fio"), pass_fio, app.fields.get("signature_fio")))
        alt = lambda k: (pas.fields[k].alternatives if k in pas.fields else ())  # noqa: E731
        pf = pas.fields.get
        checks.append(_value_check("Дата рождения", ag("applicant_header.birth_date"), pg("birth_date"),
                                   alternatives=alt("birth_date"), app_field=app.fields.get("applicant_header"),
                                   pas_field=pf("birth_date")))
        sn = pas.fields.get("series_number")
        checks.append(_value_check("Серия и номер паспорта", ag("passport"), pg("series_number"),
                                   alternatives=(sn.alternatives if sn else ()), app_field=app.fields.get("passport"),
                                   pas_field=sn))
        checks.append(_value_check("Дата выдачи паспорта", ag("passport.issue_date"), pg("issue_date"),
                                   alternatives=alt("issue_date"), app_field=app.fields.get("passport"),
                                   pas_field=pf("issue_date")))
        a_iss, p_iss = ag("issued_by"), pg("issued_by")
        if app.doc_type == "none" and not a_iss:
            # проверка только паспорта: сверять не с чем — оцениваем само прочтение
            checks.append(issuer_reading_check(pas))
        elif a_iss and p_iss:
            sim = issuer_similarity(a_iss, p_iss)
            st = OK if sim >= 85 else WARN if sim >= 60 else FAIL
            if st != OK and (_uncertain(pas.fields.get("issued_by")) or _uncertain(app.fields.get("issued_by"))):
                st = REVIEW
            checks.append(Check("Кем выдан", st, a_iss, p_iss, f"сходство {sim:.0f}% (с учётом сокращений)"))
        else:
            fld = app.fields.get("issued_by")
            checks.append(Check("Кем выдан", REVIEW if (fld and fld.crop) or not p_iss else WARN,
                                a_iss or "", p_iss or "", "нет значения для сравнения"))
        rc = issuer_region_check(pas)
        if rc:
            checks.append(rc)

        # срок действия
        # даты берём из самого надёжного источника: паспорт (MRZ/OCR) → текст заявления → модель
        def pick(pas_key, app_key, app_field_key):
            cands = [(pg(pas_key), pas.fields.get(pas_key)), (ag(app_key), app.fields.get(app_field_key))]
            cands = [(v, f) for v, f in cands if v and _d(v)]
            reliable = [(v, f) for v, f in cands if not _uncertain(f)]
            v, f = (reliable or cands or [(None, None)])[0]
            return _d(v) if v else None, bool(reliable)
        b, b_ok = pick("birth_date", "applicant_header.birth_date", "applicant_header")
        i, i_ok = pick("issue_date", "passport.issue_date", "passport")
        if b and i:
            st, msg = passport_validity(b, i, on)
            if st in (FAIL, WARN) and not (b_ok and i_ok):
                st, msg = REVIEW, msg + " — но даты прочитаны ненадёжно, подтвердите по документу"
            checks.append(Check("Срок действия паспорта", st, "", f"выдан {i:%d.%m.%Y}, д.р. {b:%d.%m.%Y}",
                                f"на {on:%d.%m.%Y}: {msg}"))
        else:
            # без дат срок действия не проверить — это не «в порядке», а повод посмотреть паспорт
            miss = ", ".join(x for x, v in (("дата выдачи", i), ("дата рождения", b)) if not v)
            checks.append(Check("Срок действия паспорта", REVIEW, "", "",
                                f"не распознано: {miss} — срок действия не проверен, введите дату вручную"))
        # серия: год бланка (бланки печатают с опережением: серия «46 19» при выдаче в 2018 г. — норма)
        s = re.sub(r"\D", "", pg("series_number") or ag("passport") or "")
        if len(s) >= 4 and i:
            yy = int(s[2:4]); blank_year = 2000 + yy if yy < 90 else 1900 + yy
            lead = blank_year - i.year
            if lead > SERIES_YEAR_LEAD:
                checks.append(Check("Серия ↔ год выдачи", WARN, "", s[:4],
                                    f"бланк {blank_year} г., а выдан в {i.year} г. — так не бывает"))
            elif lead > 1:
                checks.append(Check("Серия ↔ год выдачи", WARN, "", s[:4],
                                    f"бланк {blank_year} г., выдан в {i.year} г. — опережение {lead} г., "
                                    "встречается редко, проверьте дату выдачи"))
            elif lead == 1:
                checks.append(Check("Серия ↔ год выдачи", INFO, "", s[:4],
                                    f"бланк {blank_year} г., выдан в {i.year} г. — бланки печатают с опережением, допустимо"))
            elif i.year - blank_year > 5:
                checks.append(Check("Серия ↔ год выдачи", INFO, "", s[:4],
                                    f"бланк {blank_year} г., выдан в {i.year} г. (старый бланк)"))
        # MRZ
        if "mrz" in pas.debug:
            m = pas.debug["mrz"]
            checks.append(Check("MRZ паспорта", OK if m["valid"] else WARN, "", " / ".join(m["raw"]),
                                "контрольные цифры сошлись" if m["valid"] else
                                "не сошлись: " + ", ".join(k for k, v in m["checks"].items() if not v)))
        checks.append(Check("Адрес регистрации", INFO, ag("address") or "", "",
                            "с отметкой о регистрации в паспорте автоматически не сверяется"))

    # проверки самого заявления
    hdr, me = ag("applicant_header"), ag("applicant_fio")
    if hdr and me:
        sc, _ = names.fio_similarity(ag("applicant_header.raw_fio") or hdr, me)
        hand = any("рукопись" in (app.fields[k].source or "") for k in ("applicant_header", "applicant_fio"))
        if sc < 97:
            checks.append(Check("ФИО: шапка ↔ «Я, …»", REVIEW if hand else WARN if sc >= 85 else FAIL, hdr, me,
                                f"сходство {sc:.0f}%" + ("; рукопись — сверьте по фрагментам" if hand else "")))
    inn = ag("inn")
    if inn:
        ok = parsers.inn_valid(inn)
        checks.append(Check("ИНН ИП: контрольные цифры", OK if ok else FAIL, inn, "",
                            "" if ok else "контрольные разряды не сходятся"))
    doms = ag("domains.list") or []
    if doms:
        bad = [d for d in doms if not re.fullmatch(r"(?:[a-zа-яё0-9](?:[a-zа-яё0-9\-]{0,61}[a-zа-яё0-9])?\.)+[a-zа-яё\-]{2,}", d)]
        checks.append(Check("Домен(ы)", FAIL if bad else OK, ", ".join(doms), "",
                            ("некорректно: " + ", ".join(bad)) if bad else
                            "punycode: " + ", ".join(parsers.to_punycode(d) for d in doms)))
    else:
        fld = app.fields.get("domains")
        checks.append(Check("Домен(ы)", REVIEW if fld and fld.crop else FAIL, "", "", "домен не указан/не распознан"))
    new_admin = ag("new_admin_fio") or ag("new_admin_org") or ag("new_admin_inline")
    fld = app.fields.get("new_admin_fio")
    checks.append(Check("Новый администратор указан",
                        OK if new_admin else (REVIEW if fld and fld.crop else FAIL), new_admin or "", "",
                        "" if new_admin else "не указан/не распознан"))
    if ag("new_admin_inline") and ag("new_admin_fio"):
        sc, _ = names.fio_similarity(ag("new_admin_inline.raw_fio") or ag("new_admin_inline"), ag("new_admin_fio"))
        if sc < 97:
            checks.append(Check("Новый администратор: текст ↔ таблица", WARN, ag("new_admin_inline"),
                                ag("new_admin_fio"), f"сходство {sc:.0f}%"))
    if new_admin and pas is not None and pas.get("surname"):
        sc, _ = names.fio_similarity(new_admin, pass_fio)
        if sc >= 90:
            checks.append(Check("Новый администратор ≠ заявитель", FAIL, new_admin, pass_fio,
                                "права передаются тому же лицу?"))
    contact = ag("new_admin_contact") or ag("new_admin_org_contact")
    fld = app.fields.get("new_admin_contact")
    checks.append(Check("Контакты нового администратора", OK if contact else (REVIEW if fld and fld.crop else WARN),
                        contact or "", "", "" if contact else "не указаны/не распознаны"))
    fld = app.fields.get("contract")
    checks.append(Check("Договор/аккаунт нового администратора",
                        OK if ag("contract") else (REVIEW if fld and fld.crop else WARN), ag("contract") or "", "",
                        "" if ag("contract") else "не указан/не распознан"))
    fld = app.fields.get("application_date")
    if app_date:
        st = FAIL if app_date > date.today() else OK
        checks.append(Check("Дата заявления", st, f"{app_date:%d.%m.%Y}", "",
                            "дата в будущем" if st == FAIL else f"{(date.today() - app_date).days} дн. назад"))
    else:
        checks.append(Check("Дата заявления", REVIEW if fld and fld.crop else WARN, ag("application_date.raw") or "",
                            "", "не заполнена/не распознана"))
    crop_map = {"Кем выдан": "issued_by", "Домен(ы)": "domains", "Новый администратор указан": "new_admin_fio",
                "Контакты нового администратора": "new_admin_contact", "Адрес регистрации": "address",
                "Договор/аккаунт нового администратора": "contract", "Дата заявления": "application_date"}
    for c in checks:
        if not c.crop and c.name in crop_map:
            _with_crop(c, app.fields.get(crop_map[c.name]))
    return checks


def verdict(checks: list[Check]) -> str:
    sts = {c.status for c in checks}
    if FAIL in sts:
        return "Есть расхождения"
    if REVIEW in sts or WARN in sts:
        return "Требуется ручная проверка"
    return "Расхождений не найдено"


# ------------------------------------------------------------------ дополнительные проверки

def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


def extra_checks(app: Extraction, pas: Extraction | None, previous: list[dict] | None,
                 admin, checks: list[Check]) -> tuple[list[Check], set[str]]:
    """«Старый паспорт», данные администратора из системы, служебные флаги для правил вердикта."""
    out: list[Check] = []
    flags: set[str] = set()
    ag = app.get
    by_name = {c.name: c for c in checks}

    if by_name.get("Срок действия паспорта") and by_name["Срок действия паспорта"].status == FAIL:
        flags.add("passport_invalid")

    # --- старый паспорт
    if pas is not None and ag("passport") and pas.get("series_number"):
        a_sn, p_sn = _digits(ag("passport")), _digits(pas.get("series_number"))
        alts = {_digits(x) for x in (pas.fields["series_number"].alternatives or [])}
        if a_sn != p_sn and a_sn not in alts:
            prev = next((r for r in (previous or []) if _digits(r["series_number"]) == a_sn), None)
            pf = pas.get
            pass_fio = " ".join(x for x in (pf("surname"), pf("given_name"), pf("patronymic")) if x)
            fio_score = names.fio_similarity(ag("applicant_fio") or ag("applicant_header.raw_fio") or "", pass_fio)[0]
            same_person = fio_score >= 85 or (ag("applicant_header.birth_date") and
                                              ag("applicant_header.birth_date") == pf("birth_date"))
            a_iss, p_iss = parsers.parse_date(ag("passport.issue_date") or ""), parsers.parse_date(pf("issue_date") or "")
            older = bool(a_iss and p_iss and a_iss < p_iss)
            uncertain = _uncertain(app.fields.get("passport")) or _uncertain(pas.fields.get("series_number")) \
                or _uncertain(pas.fields.get("issue_date"))
            if prev:
                out.append(Check("Указан прежний паспорт", FAIL, ag("passport"), pas.get("series_number"),
                                 f"номер из заявления найден в «Сведениях о ранее выданных паспортах» "
                                 f"(стр. {prev['page']}{', код ' + prev['department_code'] if prev['department_code'] else ''})"))
                flags.add("old_passport")
            elif same_person and (older or not uncertain):
                st = REVIEW if uncertain else FAIL
                why = "дата выдачи в заявлении раньше, чем у предъявленного паспорта" if older else \
                      "ФИО/дата рождения совпадают, а серия и номер — нет"
                out.append(Check("Указан прежний паспорт", st, ag("passport"), pas.get("series_number"),
                                 f"вероятно, прежний паспорт: {why}"))
                if st == FAIL:
                    flags.add("old_passport")

    # --- данные текущего администратора из внутренней системы
    if admin is not None and not admin.is_empty():
        pf = pas.get if pas is not None else (lambda k, d=None: d)
        cur_fio = " ".join(x for x in (pf("surname"), pf("given_name"), pf("patronymic")) if x) \
            or ag("applicant_fio") or ag("applicant_header") or ""
        sc = names.fio_similarity(admin.fio, cur_fio)[0] if admin.fio and cur_fio else 0
        bd_ok = not admin.birth_date or not pf("birth_date") or admin.birth_date == pf("birth_date")
        if sc >= 90 and bd_ok:
            out.append(Check("Заявитель — текущий администратор", OK, cur_fio, admin.fio,
                             f"по данным системы ({admin.source or 'введено вручную'})"))
            cur_sn = pf("series_number") or ag("passport")
            if admin.passport and cur_sn and _digits(admin.passport) != _digits(cur_sn):
                out.append(Check("Паспорт в системе устарел", INFO, cur_sn, admin.passport,
                                 "в системе другой паспорт — при автозаполнении будет обновлён"))
            # «кем выдан» в системе — сверяем, только если в системе тот же паспорт
            cur_iss = pf("issued_by") or ag("issued_by")
            sys_iss = getattr(admin, "passport_issued_by", "")
            same_pas = not admin.passport or not cur_sn or _digits(admin.passport) == _digits(cur_sn)
            if sys_iss and cur_iss and same_pas:
                sim = issuer_similarity(sys_iss, cur_iss)
                fld = pas.fields.get("issued_by") if pas is not None else None
                st = OK if sim >= 85 else (REVIEW if _uncertain(fld) else WARN)
                out.append(Check("Кем выдан ↔ данные системы", st, cur_iss, sys_iss,
                                 f"сходство {sim:.0f}% (с учётом сокращений)"))
        else:
            out.append(Check("Заявитель — текущий администратор", FAIL, cur_fio, admin.fio,
                             f"сходство ФИО {sc:.0f}%" + ("" if bd_ok else "; дата рождения отличается")))
            flags.add("admin_mismatch")
        doms = ag("domains.list") or []
        if admin.domains and doms:
            foreign = [d for d in doms if d not in [x.lower() for x in admin.domains]]
            if foreign:
                out.append(Check("Домены администратора", FAIL, ", ".join(doms), ", ".join(admin.domains),
                                 "не числятся за администратором: " + ", ".join(foreign)))
                flags.add("admin_mismatch")
    return out, flags
