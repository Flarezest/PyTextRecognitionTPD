"""Смена владельца личного кабинета (аккаунта) — вкладка «Смена владельца ЛК» веб-интерфейса. Пока только физлица.

1. Аккаунт. parse_query() понимает, что ввёл оператор: номер аккаунта, «Данные пользователя #N»,
   «Базовая анкета пользователя #N», ссылку из manager, «договор № N», логин/e-mail или домен.
   resolve_account() получает данные через расширение Chrome (команда account; для домена — сначала lookup,
   номер аккаунта — владелец услуги из S): базовая анкета (скрытый блок страницы «Данные пользователя»),
   логин, обслуживающая организация, статус идентификации аккаунта, тип анкеты (Физик / ИП / Юрик).
2. Текущий владелец. compare_owner(): базовая анкета ↔ данные заявления (пока вводятся вручную).
3. Новый владелец. prepare_fill(): данные из формы → значения полей базовой анкеты (/user/N/runic_details).
   Расширение (команда fill_runic) вписывает их во вкладку «Базовая анкета пользователя #N», которую видит
   оператор. Кнопку «Сохранить» нажимает оператор. verify_saved() — сверка анкеты после сохранения.

Поля сверки и заполнения — списки OWNER_FIELDS и FILL_FIELDS: новое поле добавляется одной строкой
(и, для заполнения, в RUNIC_FILL в doctool_extension/content/manager.js).
"""
from __future__ import annotations

import re

from . import fieldnorm, parsers
from .domains import FOUND, NOT_FOUND, from_extension, split_domains, unicode_name

# ------------------------------------------------------------------ что сверяем и что заполняем

# Текущий владелец: поле базовой анкеты (ru_pp.<поле>), заголовок, вид сравнения (см. fieldnorm)
OWNER_FIELDS = (
    ("person_r_surname", "Фамилия", "text"),
    ("person_r_name", "Имя", "text"),
    ("person_r_patronimic", "Отчество", "text"),
    ("birth_date", "Дата рождения", "date"),
    ("country", "Гражданство (страна)", "country"),
    ("passport_number", "Серия и номер паспорта", "digits"),
    ("passport_date", "Дата выдачи паспорта", "date"),
    ("passport_place", "Кем выдан", "issuer"),
    ("phone", "Телефон", "phone"),
)

# Новый владелец: поле формы базовой анкеты в manager и заголовок. Порядок — как на странице.
FILL_FIELDS = (
    ("person_r_surname", "Фамилия"),
    ("person_r_name", "Имя"),
    ("person_r_patronimic", "Отчество"),
    ("passport_number", "Номер и серия"),
    ("passport_date", "Дата выдачи"),
    ("passport_place", "Кем выдан"),
    ("birth_date", "Дата рождения"),
    ("p_addr_zip", "Индекс"),
    ("p_addr_city", "Город"),
    ("p_addr_addr", "Почтовый адрес"),
    ("p_addr_recipient", "Получатель"),
    ("phone", "Телефон"),
    ("e_mail", "Email"),
)
FILL_TITLES = dict(FILL_FIELDS)
# Что doctool в анкете не трогает (выставляет оператор или страница manager сама)
MANUAL_FIELDS = ("Гражданство", "Страна почтового адреса")
UNTOUCHED_FIELDS = ("English name (пересчитывает страница manager)", "Область", "Телефонный номер sms-безопасности",
                    "Факс", "флажок «Внесены данные нового владельца аккаунта»")
_VERIFY_KIND = {"passport_date": "date", "birth_date": "date", "phone": "phone", "passport_number": "digits",
                "p_addr_zip": "digits"}
TYPE_RU = {"pp": "физлицо", "ip": "ИП", "org": "юрлицо"}


class AccountError(RuntimeError):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ 1. аккаунт

_ID_PATTERNS = (
    r"user_id=(\d+)",
    r"/user/(\d+)(?:/|$|\?)",
    r"#\s*(\d{3,})",
    r"пользователя\s+(\d{3,})",
    r"договор\w*\s*(?:№|N|No)?\s*(\d{3,})",
    r"^\s*(?:№\s*)?(\d{3,})\s*$",
)
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")


def parse_query(text: str) -> dict:
    """Что ввели в поле «Пользователь»: {"kind": "id"|"login"|"domain"|"", "value": …, "label": …}."""
    s = (text or "").strip()
    if not s:
        return {"kind": "", "value": "", "label": "пусто"}
    for rx in _ID_PATTERNS:
        m = re.search(rx, s, re.I)
        if m:
            return {"kind": "id", "value": m.group(1), "label": f"номер аккаунта {m.group(1)}"}
    if "@" in s:
        v = s.strip("<> \t\"'").split()[0] if s.split() else s
        return {"kind": "login", "value": v, "label": f"логин / e-mail {v}"}
    doms = split_domains(s)
    if len(doms) == 1 and " " not in s.strip() and re.search(r"\.(?:[a-zа-яё]{2,}|xn--[a-z0-9\-]+)$", doms[0]):
        return {"kind": "domain", "value": doms[0], "label": f"домен {unicode_name(doms[0])} → аккаунт владельца услуги"}
    if _LOGIN_RE.match(s):
        return {"kind": "login", "value": s, "label": f"логин {s}"}
    return {"kind": "", "value": s, "label": "не распознано — введите номер аккаунта, логин, e-mail или домен"}


def _ba_group(ba_all: dict) -> tuple[str, dict]:
    """Базовая анкета {«ru_pp.birth_date»: …} → ("ru_pp", {"birth_date": …}); группа — самая заполненная."""
    groups: dict[str, dict] = {}
    for k, v in (ba_all or {}).items():
        g, _, f = k.partition(".")
        groups.setdefault(g, {})[f] = v
    if not groups:
        return "", {}
    g = max(groups, key=lambda x: (x in ("ru_pp", "ru_org"), sum(1 for v in groups[x].values() if v)))
    return g, groups[g]


def account_from_extension(data: dict) -> dict:
    """Ответ расширения на account → данные для интерфейса."""
    runic = data.get("runic") or {}
    group, ba = _ba_group(data.get("ba") or {})
    typ = runic.get("type") or ("org" if group == "ru_org" else "ip" if data.get("is_entrepreneur") else
                                "pp" if group == "ru_pp" else "")
    statuses = data.get("statuses") or []
    esia_acc = next((x for x in statuses if "Госуслуг" in x), "")
    notes = []
    if typ and typ != "pp":
        notes.append(f"Аккаунт оформлен на {TYPE_RU.get(typ, typ)}: автозаполнение пока только для физлиц.")
    return {
        "user_id": str(data.get("user_id") or ""), "login": data.get("login") or "", "statuses": statuses,
        "esia_account": esia_acc, "servicing_org": data.get("servicing_org") or "",
        "servicing_org_id": data.get("servicing_org_id") or "", "contypes": data.get("contypes") or "",
        "type": typ, "type_ru": TYPE_RU.get(typ, typ or "?"), "ba_group": group, "ba": ba,
        "ba_all": data.get("ba") or {}, "runic": runic.get("values") or {}, "urls": data.get("urls") or {},
        "notes": notes,
    }


def resolve_account(request, text: str, log=print, timeout: float = 240) -> dict:
    """Данные аккаунта по тому, что ввёл оператор. request(cmd, params, timeout, on_progress) — запрос к расширению
    (manager_bridge.ExtensionBridge.request_sync). Ошибки — AccountError."""
    q = parse_query(text)
    if not q["kind"]:
        raise AccountError("Не понял, какой аккаунт: введите номер аккаунта, логин, e-mail или домен.", "bad_request")
    progress = lambda m: log(f"manager: {m.get('message') or m.get('step')}")  # noqa: E731
    via = None
    params = {"user_id": q["value"]} if q["kind"] == "id" else {"login": q["value"]}
    if q["kind"] == "domain":
        d = q["value"]
        reply = request("lookup", {"domain": unicode_name(d), "ascii": parsers.to_punycode(d), "esia": False},
                        timeout, progress)
        info = from_extension(d, reply)
        if info.status == FOUND and info.account:
            via = {"domain": unicode_name(d), "account": info.account, "bill_owner": info.bill_owner,
                   "service_status": info.service_status, "urls": info.urls}
            params = {"user_id": info.account}
        elif info.status == NOT_FOUND and _LOGIN_RE.match(q["value"]):
            params = {"login": q["value"]}       # похоже на домен, но, может быть, это логин с точкой
        else:
            raise AccountError(f"Домен {unicode_name(d)}: {info.error or 'аккаунт не определён'}",
                               info.error_code or "not_found")
    reply = request("account", params, timeout, progress)
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise AccountError(err.get("message") or "ошибка", err.get("code") or "error")
    acc = account_from_extension(reply.get("data") or {})
    acc["query"] = q
    if via:
        acc["via_domain"] = via
        if via["bill_owner"] and acc["login"] and via["bill_owner"].lower() != acc["login"].lower():
            acc["notes"].insert(0, f"У домена {via['domain']} владелец счёта в «Счетах» — {via['bill_owner']}, "
                                   f"а владелец услуги (аккаунт #{acc['user_id']}) — {acc['login']}.")
    return acc


# ------------------------------------------------------------------ 2. текущий владелец: базовая анкета ↔ заявление

def _multi_same(kind: str, a: str, b: str) -> tuple[str, str]:
    """Сравнение, когда в анкете может быть несколько значений (телефоны через перевод строки или запятую)."""
    parts = [x for x in re.split(r"[\n,;]+", a or "") if x.strip()] or [a]
    res = [fieldnorm.same(kind, p, b) for p in parts]
    return next((r for r in res if r[0] == fieldnorm.OK), res[0])


def compare_owner(ba: dict, app: dict) -> dict:
    """Базовая анкета (поля без префикса ru_pp.) ↔ данные заявления {поле: значение}.
    → {"rows": [{field, title, ba, app, status, detail}], "mismatch": [заголовки], "ok": n, "total": n}."""
    rows = []
    for key, title, kind in OWNER_FIELDS:
        bv, av = (ba or {}).get(key, "") or "", (app or {}).get(key, "") or ""
        st, det = _multi_same(kind, bv, av) if kind == "phone" else fieldnorm.same(kind, bv, av)
        if st == fieldnorm.NONE:
            det = "не с чем сравнить" if bv else ("в базовой анкете пусто" if av else "")
        rows.append({"field": key, "title": title, "ba": bv, "app": av, "status": st, "detail": det})
    done = [r for r in rows if r["status"]]
    return {"rows": rows, "mismatch": [r["title"] for r in rows if r["status"] == fieldnorm.FAIL],
            "warn": [r["title"] for r in rows if r["status"] == fieldnorm.WARN],
            "ok": sum(1 for r in done if r["status"] == fieldnorm.OK), "total": len(done)}


# ------------------------------------------------------------------ 3. новый владелец → базовая анкета

_FEDERAL = ("Москва", "Санкт-Петербург", "Севастополь")
_CITY_PART = re.compile(r"^(?:г\.|гор\.|город\s|г\s)\s*(.+?)\s*$", re.I)
_CITY_SUFFIX = re.compile(r"^(.+?)\s+(?:г\.?|город)$", re.I)
_SETTLEMENT = re.compile(r"^(?:пгт|рп|р\.\s?п|пос|п|с|село|дер|деревня|д|ст-ца|станица|х|хутор|аул)\.?\s+([А-ЯЁ][^\d]*?)\s*$")


def city_from_address(address: str) -> str:
    """Город из адреса регистрации: «г. …» / «город …» / «… г», для Москвы, Санкт-Петербурга, Севастополя —
    их название; иначе посёлок, село, деревня («пгт …», «с. …», «д. Путилково»). Не нашли — пусто."""
    parts = [p.strip() for p in re.split(r",", address or "") if p.strip()]
    for p in parts:
        m = _CITY_PART.match(p) or _CITY_SUFFIX.match(p)
        if m and not re.match(r"^о\.", m.group(1)):          # «г.о. Красногорск» — городской округ, не город
            return m.group(1).strip(" .")
        bare = re.sub(r"^(?:г\.?\s*)", "", p).strip(" .")
        for f in _FEDERAL:
            if bare.lower().replace("ё", "е") == f.lower():
                return f
    for p in parts:
        m = _SETTLEMENT.match(p)
        if m:
            return m.group(1).strip(" .")
    return ""


def _clean_name(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if s and (s.isupper() or s.islower()):
        s = "-".join(" ".join(w.capitalize() for w in part.split(" ")) for part in s.split("-"))
    return s


def _phone(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    d = fieldnorm.phone_digits(s)
    if len(d) == 11 and d[0] == "7":
        return "+" + d
    return ("+" + d) if s.startswith("+") and d else s


def _date(s: str, title: str, notes: list) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    d = parsers.parse_date(s)
    if d is None:
        notes.append(f"{title}: «{s}» не похоже на дату ДД.ММ.ГГГГ — перенесено как есть.")
        return s
    return d.strftime("%d.%m.%Y")


_DEPT_RE = re.compile(r",?\s*(?:код\s+подразделения|к/п|код)\s*:?\s*\d{3}\s*[-–]?\s*\d{3}", re.I)


def split_issued(text: str) -> tuple[str, str, str]:
    """«Кем и когда выдан документ» из заявления → (кем выдан, дата выдачи ДД.ММ.ГГГГ, код подразделения).
    «ГУ МВД России по г. Москве, 24.08.2021, код подразделения 770-001» → («ГУ МВД России по г. Москве»,
    «24.08.2021», «770-001»)."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    dept = ""
    m = _DEPT_RE.search(s)
    if m:
        dept = re.sub(r"\D", "", m.group(0))
        dept = f"{dept[:3]}-{dept[3:]}" if len(dept) == 6 else dept
        s = (s[:m.start()] + s[m.end():]).strip()
    d = parsers.parse_date(s)
    date_s = d.strftime("%d.%m.%Y") if d else ""
    m = parsers.DATE_RE.search(s) or parsers.DATE_WORDS_RE.search(s) if d else None
    if m:
        # дата вместе со словами вокруг: «дата выдачи: 24.08.2021», «выдан 03 июня 2009 года», «от 12.04.2015 г.»
        start, end = m.start(), m.end()
        pre = re.search(r"(?:дата\s+выдачи|выдан[аоы]?|от)\s*:?\s*$", s[:start], re.I)
        if pre:
            start = pre.start()
        post = re.match(r"\s*(?:года|г\.|г(?=[\s,;]|$))", s[end:], re.I)
        if post:
            end += post.end()
        s = (s[:start] + " " + s[end:]).strip()
    s = re.sub(r"^(?:выдан[аоы]?|кем\s+выдан)\s*:?\s*", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:-")
    return s, date_s, dept


def prepare_fill(form: dict) -> dict:
    """Форма «Новый владелец» → значения полей базовой анкеты в manager (только непустые).
    form: surname, name, patronymic, birth_date, passport, issued («Кем и когда выдан документ» целиком),
          passport_date, passport_place, address (адрес регистрации), city, zip, recipient, phone, email.
    → {"fields": {поле: значение}, "preview": [{field, title, value}], "notes": [...], "manual": [...], "untouched": [...]}"""
    f = {k: (v or "").strip() if isinstance(v, str) else (v or "") for k, v in (form or {}).items()}
    notes: list[str] = []
    out: dict[str, str] = {}
    sur, nam, pat = _clean_name(f.get("surname")), _clean_name(f.get("name")), _clean_name(f.get("patronymic"))
    out["person_r_surname"], out["person_r_name"], out["person_r_patronimic"] = sur, nam, pat
    pas = f.get("passport", "")
    digits = re.sub(r"\D", "", pas)
    out["passport_number"] = digits if len(digits) == 10 else re.sub(r"\s+", " ", pas)
    if pas and len(digits) != 10:
        notes.append("Серия и номер — не 10 цифр (не паспорт РФ?): перенесено как есть.")
    place, pdate = f.get("passport_place", ""), f.get("passport_date", "")
    if f.get("issued"):
        p2, d2, dept = split_issued(f["issued"])
        place = place or p2
        pdate = pdate or d2
        if dept:
            notes.append(f"Код подразделения {dept} в базовой анкете отдельного поля не имеет — не переносится.")
    out["passport_place"] = re.sub(r"\s+", " ", place)
    out["passport_date"] = _date(pdate, "Дата выдачи", notes)
    out["birth_date"] = _date(f.get("birth_date", ""), "Дата рождения", notes)
    address = re.sub(r"\s+", " ", f.get("address", ""))
    out["p_addr_addr"] = address
    out["p_addr_city"] = f.get("city") or city_from_address(address)
    if address and not out["p_addr_city"]:
        notes.append("Город по адресу не определён — впишите его в поле «Город».")
    out["p_addr_zip"] = re.sub(r"\s+", "", f.get("zip", ""))
    out["p_addr_recipient"] = re.sub(r"\s+", " ", f.get("recipient", "")) or " ".join(x for x in (sur, nam, pat) if x)
    out["phone"] = _phone(f.get("phone", ""))
    out["e_mail"] = f.get("email", "").lower()
    fields = {k: v for k, v in out.items() if v}
    if not out["p_addr_zip"]:
        notes.append("Индекс не указан — в анкете останется прежний.")
    return {"fields": fields, "preview": [{"field": k, "title": t, "value": fields.get(k, "")} for k, t in FILL_FIELDS],
            "notes": notes, "manual": list(MANUAL_FIELDS), "untouched": list(UNTOUCHED_FIELDS)}


def verify_saved(values: dict, fields: dict) -> dict:
    """Анкета после «Сохранить» (values — поля формы, перечитанные из manager) ↔ то, что заполняли (fields)."""
    rows = []
    for k, v in (fields or {}).items():
        now = (values or {}).get(k, "")
        st, _ = fieldnorm.same(_VERIFY_KIND.get(k, "text"), now, v)
        if st == fieldnorm.NONE:
            st = fieldnorm.FAIL if v else fieldnorm.OK
        rows.append({"field": k, "title": FILL_TITLES.get(k, k), "expected": v, "actual": now,
                     "status": fieldnorm.OK if st == fieldnorm.OK else fieldnorm.FAIL})
    return {"rows": rows, "ok": all(r["status"] == fieldnorm.OK for r in rows),
            "mismatch": [r["title"] for r in rows if r["status"] != fieldnorm.OK]}
