"""Реестр проверок.

Проверка — функция, помеченная декоратором @register в модуле, где лежит её логика:
compare.py — сверка заявления с паспортом и формальные проверки заявления, domains.py — данные доменов
из manager. Описание проверки (CheckDef):
  id     — ключ для config/case_types.yaml (список `checks:` у типа заявления);
  names  — {название проверки (Check.name): id строк таблицы интерфейса, к которым она относится}.
           По названию пишутся правила вердикта (`check:` в case_types.yaml); по строкам таблицы — статус
           строки и «Подтверждаю» оператора;
  flags  — служебные признаки, которые проверка может поставить (правила `flag:`);
  crop   — роль поля заявления, чей фрагмент скана показывать рядом с проверкой;
  needs  — "passport": без паспорта не выполняется; "domains": только когда загружены данные из manager.

Тип заявления перечисляет нужные проверки в `checks:`. Выполняются они в порядке регистрации (порядок
в YAML не важен). Новая проверка = функция с @register + её id в `checks:` нужных типов; service.py,
интерфейсы и выгрузку менять не нужно.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from . import parsers
from .formspec import Fields
from .models import Extraction


@dataclass
class CheckDef:
    id: str
    func: Callable
    names: dict[str, tuple[str, ...]]
    flags: tuple[str, ...] = ()
    crop: str | None = None
    needs: str | None = None
    title: str = ""


REGISTRY: dict[str, CheckDef] = {}


def register(check_id: str, names: dict, flags=(), crop: str | None = None, needs: str | None = None):
    """@register("inn", {"ИНН ИП: контрольные цифры": "inn"}) — names: {название: строка или кортеж строк}."""
    def deco(func):
        old = REGISTRY.get(check_id)
        if old is not None and (old.func.__module__, old.func.__qualname__) != (func.__module__, func.__qualname__):
            raise ValueError(f"проверка {check_id!r} уже зарегистрирована в {old.func.__module__}")
        norm = {n: ((r,) if isinstance(r, str) else tuple(r or ())) for n, r in names.items()}
        REGISTRY[check_id] = CheckDef(check_id, func, norm, tuple(flags), crop, needs,
                                      (func.__doc__ or "").strip().split("\n")[0].rstrip("."))
        return func
    return deco


def _load() -> None:
    """Модули с проверками регистрируют их при импорте."""
    from . import compare, domains  # noqa: F401


@dataclass
class Ctx:
    """Всё, что нужно проверкам: заявление, паспорт, данные из системы и manager, уже полученные проверки."""
    app: Extraction
    pas: Extraction | None = None
    on: date | None = None                 # дата проверки (по умолчанию — дата заявления или сегодня)
    previous: list = field(default_factory=list)   # ранее выданные паспорта (стр. 19 паспорта)
    admin: object = None                   # integrations.AdminData
    domains_sd: list = field(default_factory=list)  # domains.DomainInfo
    form: dict | None = None
    checks: list = field(default_factory=list)
    flags: set = field(default_factory=set)

    def __post_init__(self):
        if self.app is None:
            self.app = Extraction(doc_type="none", source_file="")
        self.f = Fields(self.app, self.form)   # поля заявления по ролям: self.f.get("passport.issue_date")
        self.form = self.f.form
        self.app_date = parsers.parse_date(self.f.get("application_date") or "")
        self.on = self.on or self.app_date or date.today()

    @property
    def pass_fio(self) -> str:
        if self.pas is None:
            return ""
        g = self.pas.get
        return " ".join(x for x in (g("surname"), g("given_name"), g("patronymic")) if x)

    def check(self, name: str):
        return next((c for c in self.checks if c.name == name), None)


# ------------------------------------------------------------------ выбор и запуск

# Тип без бланка и без списка `checks:` — только паспорт и данные из системы/manager (как раньше)
PASSPORT_ONLY = ("issued_by", "issuer_region", "series_year", "mrz", "system_admin", "manager_domains")


def ids_for(case_type: dict) -> list[str]:
    """Какие проверки выполнять для типа заявления."""
    _load()
    if case_type.get("checks") is not None:
        return list(case_type["checks"])
    if not case_type.get("form"):
        return list(PASSPORT_ONLY)
    return list(REGISTRY)


def run(ids, ctx: Ctx) -> tuple[list, set[str]]:
    """Выполняет проверки ids (None — все) в порядке регистрации. Возвращает новые проверки и все флаги."""
    _load()
    from .compare import Check
    want = None if ids is None else set(ids)
    start = len(ctx.checks)
    for cid, d in REGISTRY.items():
        if want is not None and cid not in want:
            continue
        if d.needs == "passport" and ctx.pas is None:
            continue
        if d.needs == "domains" and not ctx.domains_sd:
            continue
        out = d.func(ctx)
        out = [] if out is None else [out] if isinstance(out, Check) else list(out)
        if d.crop:
            fld = ctx.f.field(d.crop)
            for c in out:
                if not c.crop and fld is not None and getattr(fld, "crop", None):
                    c.crop = fld.crop
        ctx.checks.extend(out)
    return ctx.checks[start:], ctx.flags


# ------------------------------------------------------------------ сведения для таблицы, правил и проверки конфигов

def all_names() -> dict[str, tuple[str, ...]]:
    """{название проверки: строки таблицы}."""
    _load()
    out: dict[str, tuple[str, ...]] = {}
    for d in REGISTRY.values():
        for n, rows in d.names.items():
            out[n] = tuple(dict.fromkeys(out.get(n, ()) + rows))
    return out


def all_flags() -> set[str]:
    _load()
    return {f for d in REGISTRY.values() for f in d.flags}


def names_for_row(row_id: str) -> list[str]:
    """Названия проверок, относящихся к строке таблицы."""
    return [n for n, rows in all_names().items() if row_id in rows]
