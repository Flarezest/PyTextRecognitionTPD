"""Описания бланков (forms/*.yaml): загрузка с кэшем, роли полей, подписи бланка, проверка конфигов.

Роль поля — его смысл для проверок: «ФИО в тексте», «домены», «новый администратор» (словарь ROLES ниже).
Проверки (compare.py, domains.py), таблица интерфейса, выгрузка и отчёт обращаются к полю заявления
по роли, а не по имени поля в YAML. Поэтому новый бланк может называть поля как угодно: роль задаётся
ключом `role:` у поля (без него роль = имя поля). Для бланка regru_transfer_person имена полей и роли
совпадают.

Модуль без тяжёлых зависимостей: его используют и разбор заявлений, и проверки, и интерфейсы.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

FORMS_DIR = Path(__file__).resolve().parent.parent / "forms"

# Роли полей заявления, на которые опираются проверки, таблица и выгрузка. Новый бланк сопоставляет свои
# поля с этими ролями; новая роль добавляется сюда вместе с проверкой, которая её использует.
ROLES = {
    "applicant_header": "ФИО заявителя в шапке «от …» и дата рождения (подполя birth_date, raw_fio)",
    "applicant_fio": "ФИО заявителя в тексте «Я, …»",
    "signature_fio": "ФИО у подписи",
    "inn": "ИНН заявителя-ИП",
    "passport": "серия и номер паспорта в заявлении (подполе issue_date)",
    "issued_by": "кем выдан паспорт",
    "address": "адрес регистрации",
    "domains": "домены (подполя list, punycode, joined, fragments, mixed, stated_count, uncertain)",
    "services": "дополнительные услуги",
    "new_admin_inline": "новый администратор в тексте («новому Администратору: …»)",
    "new_admin_org": "новый администратор — юрлицо",
    "new_admin_org_contact": "контакты нового администратора-юрлица",
    "new_admin_fio": "новый администратор — физлицо / ИП",
    "new_admin_contact": "контакты нового администратора",
    "contract": "договор / аккаунт нового администратора",
    "application_date": "дата заявления",
}

_cache: dict[str, tuple] = {}


def load_forms(forms_dir: Path | str | None = None) -> list[dict]:
    """Все бланки из папки. Перечитываются, только если файлы изменились. Результат не изменять."""
    d = Path(forms_dir or FORMS_DIR)
    files = sorted(d.glob("*.yaml"))
    key = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    hit = _cache.get(str(d))
    if hit is None or hit[0] != key:
        hit = (key, [yaml.safe_load(p.read_text(encoding="utf-8")) for p in files])
        _cache[str(d)] = hit
    return hit[1]


def form_by_id(form_id: str | None, forms_dir: Path | str | None = None) -> dict | None:
    if not form_id:
        return None
    return next((f for f in load_forms(forms_dir) if f.get("id") == form_id), None)


# ------------------------------------------------------------------ роли

def role_map(form: dict | None) -> dict[str, str]:
    """{роль: имя поля в бланке}."""
    if not form:
        return {}
    return {(spec or {}).get("role", key): key for key, spec in form.get("fields", {}).items()}


class Fields:
    """Поля заявления по ролям: Fields(app).get("applicant_header.birth_date") — значение подполя birth_date
    поля с ролью applicant_header, как бы это поле ни называлось в бланке."""

    def __init__(self, ex, form: dict | None = None):
        self.ex = ex
        if form is None and ex is not None:
            form = form_by_id(getattr(ex, "doc_type", None))
        self.form = form
        self.map = role_map(form)

    def key(self, role: str) -> str:
        base, dot, sub = role.partition(".")
        return self.map.get(base, base) + dot + sub

    def get(self, role: str, default=None):
        return self.ex.get(self.key(role), default) if self.ex is not None else default

    def field(self, role: str):
        return self.ex.fields.get(self.key(role)) if self.ex is not None else None


# ------------------------------------------------------------------ подписи бланка и префиксы значений

# До 0.4.0 подписи и префиксы были зашиты в application.py. Бланк без ключа labels_re / strip_prefixes
# (например, свой YAML от 0.3.x) получает прежний список; пустой список в YAML — «ничего не вырезать».
LEGACY_LABELS = ["ФИО", "кем выдан", "адрес регистрации", "серия, номер паспорта, когда (?:и кем )?выдан",
                 r"ИНН ИП \(при наличии статуса ИП\)", r"наименование домена\(ов\)?", "Primary, Secondary DNS",
                 r"номер договора с компанией РЕГ\.РУ", "фамилия, имя, отчество", "подпись", "дата прописью"]
LEGACY_PREFIXES = [r"от\b", "я,", r"профиля\)\s*:"]


def _form_list(form: dict, key: str) -> list[str]:
    v = form.get(key)
    if v is None:
        return {"labels_re": LEGACY_LABELS, "strip_prefixes": LEGACY_PREFIXES}[key]
    return list(v)


def _all_forms_list(key: str) -> list[str]:
    out: list[str] = []
    for f in load_forms() or [{}]:
        for x in _form_list(f, key):
            if x not in out:
                out.append(x)
    return out


@lru_cache(maxsize=64)
def _labels_rx(patterns: tuple[str, ...]) -> re.Pattern | None:
    if not patterns:
        return None
    # скобка — любая: OCR читает «(» как «{» или «[» («{кем выдан)»)
    return re.compile(r"[({\[]\s*(?:" + "|".join(patterns) + r")[^)}\]]*[)}\]]?", re.I)


def labels_regex(form: dict | None = None) -> re.Pattern | None:
    """Печатные подписи бланка в скобках («(кем выдан)», «(ФИО, дата рождения)»), которые вычищаются из
    значений. Берутся из `labels_re` бланка; без бланка — из всех бланков."""
    pats = _form_list(form, "labels_re") if form else _all_forms_list("labels_re")
    return _labels_rx(tuple(pats or ()))


@lru_cache(maxsize=64)
def _prefix_rx(patterns: tuple[str, ...]) -> re.Pattern | None:
    if not patterns:
        return None
    return re.compile(r"^(" + "|".join(patterns) + r")\s*", re.I)


def prefix_regex(form: dict | None = None) -> re.Pattern | None:
    """Печатные слова перед значением на скане («от», «Я,», «профиля):») — `strip_prefixes` бланка."""
    pats = _form_list(form, "strip_prefixes") if form else _all_forms_list("strip_prefixes")
    return _prefix_rx(tuple(pats or ()))


def extra_rows(form: dict | None) -> list[dict]:
    """Дополнительные строки таблицы интерфейса из бланка (`rows:`)."""
    return list((form or {}).get("rows") or [])


def field_title(form: dict | None, key: str) -> str | None:
    spec = (form or {}).get("fields", {}).get(key)
    return spec.get("title") if spec else None


# ------------------------------------------------------------------ проверка конфигов

STATUSES = {"ok", "warn", "fail", "review", "info"}
REGION_KEYS = {"dy0", "dy1", "x0", "x1"}


def validate_config(forms_dir: Path | str | None = None, case_types: dict | None = None) -> list[str]:
    """Проблемы в forms/*.yaml и config/case_types.yaml: неизвестные типы полей, роли, проверки, бланки,
    флаги, ошибки в регулярных выражениях. Пустой список — всё в порядке.
    Запуск: python -m doctool validate; в тестах — tests/test_config.py."""
    from . import checks as checklib
    from .application import FIELD_PARSERS
    from .verdict import load_case_types
    from .service import BASE_ROWS

    problems: list[str] = []
    forms = load_forms(forms_dir)
    ids = [f.get("id") for f in forms]
    for i in {x for x in ids if ids.count(x) > 1}:
        problems.append(f"бланк {i}: id встречается несколько раз")
    row_ids = {r["id"] for r in BASE_ROWS}

    def rx(where: str, pat: str, need_group: bool = False):
        try:
            c = re.compile(pat)
        except re.error as e:
            problems.append(f"{where}: ошибка в регулярном выражении {pat!r}: {e}")
            return
        if need_group and c.groups < 1:
            problems.append(f"{where}: в выражении {pat!r} нет группы (значение — группа 1)")

    for f in forms:
        fid = f.get("id", "?")
        if not (f.get("detect") or {}).get("keywords"):
            problems.append(f"бланк {fid}: нет detect.keywords — бланк не будет узнаваться")
        roles_seen: dict[str, str] = {}
        for key, spec in (f.get("fields") or {}).items():
            spec = spec or {}
            where = f"бланк {fid}, поле {key}"
            if spec.get("type", "text") not in FIELD_PARSERS:
                problems.append(f"{where}: неизвестный type {spec.get('type')!r} (есть: {', '.join(FIELD_PARSERS)})")
            role = spec.get("role", key)
            if "role" in spec and role not in ROLES:
                problems.append(f"{where}: неизвестная роль {role!r} (есть: {', '.join(ROLES)})")
            if role in roles_seen:
                problems.append(f"{where}: роль {role!r} уже у поля {roles_seen[role]}")
            roles_seen[role] = key
            for pat in spec.get("text") or []:
                rx(where, pat, need_group=True)
            # (0.7.0) нейросеть: llm — описание поля (строка) или false; digits — допустимое число цифр
            if "llm" in spec and not (isinstance(spec["llm"], str) and spec["llm"].strip() or spec["llm"] is False):
                problems.append(f"{where}: llm — описание поля строкой или false")
            dg = spec.get("digits")
            if dg is not None and not all(isinstance(x, int) and x > 0 for x in (dg if isinstance(dg, list) else [dg])):
                problems.append(f"{where}: digits — число или список чисел (сколько цифр в значении)")
            if not spec.get("text") and spec.get("llm") is False and not spec.get("scan"):
                problems.append(f"{where}: поле нечем читать — нет text:, scan:, а llm: false")
            sc = spec.get("scan")
            if sc:
                if not sc.get("anchor"):
                    problems.append(f"{where}: scan без anchor")
                miss = REGION_KEYS - set((sc.get("region") or {}))
                if miss:
                    problems.append(f"{where}: в scan.region нет {', '.join(sorted(miss))}")
                if sc.get("block_start") and spec.get("type") != "domains":
                    problems.append(f"{where}: scan.block_start поддерживается только у type: domains")
        for pat in f.get("labels_re") or []:
            rx(f"бланк {fid}, labels_re", pat)
        for pat in f.get("strip_prefixes") or []:
            rx(f"бланк {fid}, strip_prefixes", pat)
        for r in f.get("rows") or []:
            where = f"бланк {fid}, строка таблицы {r.get('id')}"
            if not r.get("id") or not r.get("title"):
                problems.append(f"{where}: нужны id и title")
            if r.get("id") in row_ids:
                problems.append(f"{where}: id совпадает со стандартной строкой")
            for role in r.get("app") or []:
                if role.split(".")[0] not in ROLES and role.split(".")[0] not in (f.get("fields") or {}):
                    problems.append(f"{where}: неизвестная роль/поле {role!r}")
            row_ids.add(r.get("id"))

    for name, rows in checklib.all_names().items():
        for r in rows:
            if r and r not in row_ids:
                problems.append(f"проверка «{name}»: строка таблицы {r!r} не существует")

    types = case_types if case_types is not None else load_case_types()
    names = checklib.all_names()
    flags = checklib.all_flags()
    for tid, ct in types.items():
        where = f"тип заявления {tid}"
        form = None
        if ct.get("form"):
            form = next((f for f in forms if f.get("id") == ct["form"]), None)
            if form is None:
                problems.append(f"{where}: бланк {ct['form']!r} не найден в forms/")
        for cid in ct.get("checks") or []:
            if cid not in checklib.REGISTRY:
                problems.append(f"{where}: неизвестная проверка {cid!r} (есть: {', '.join(checklib.REGISTRY)})")
        if ct.get("enabled", True) and not ct.get("form") and not ct.get("checks"):
            problems.append(f"{where}: тип без бланка должен перечислить проверки (checks:)")
        produced = {n for cid in checklib.ids_for(ct) if cid in checklib.REGISTRY for n in checklib.REGISTRY[cid].names}
        for kind in ("reject", "review"):
            for rule in ((ct.get("rules") or {}).get(kind) or []):
                cond = rule.get("when") or {}
                if "check" in cond:
                    if cond["check"] not in names:
                        problems.append(f"{where}, правило «{rule.get('reason')}»: проверки «{cond['check']}» нет")
                    elif cond["check"] not in produced:
                        problems.append(f"{where}, правило «{rule.get('reason')}»: проверка «{cond['check']}» "
                                        "не включена в checks этого типа — правило не сработает")
                for st in (cond.get("status") or []) + (cond.get("any_status") or []):
                    if st not in STATUSES:
                        problems.append(f"{where}, правило «{rule.get('reason')}»: неизвестный статус {st!r}")
                if "flag" in cond and cond["flag"] not in flags:
                    problems.append(f"{where}, правило «{rule.get('reason')}»: флаг {cond['flag']!r} никто не ставит "
                                    f"(есть: {', '.join(sorted(flags))})")
                if "missing" in cond:
                    keys = set((form or {}).get("fields", {})) | set(ROLES)
                    if cond["missing"].split(".")[0] not in keys:
                        problems.append(f"{where}, правило «{rule.get('reason')}»: поле {cond['missing']!r} не найдено")
    return problems
