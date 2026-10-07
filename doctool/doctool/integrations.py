"""Связь с внутренней системой (взаимодействие с реестром).

Данные доменов из manager (Sd и S) с версии 0.3.0 загружает расширение Chrome «doctool — manager»:
см. manager_bridge.py (связь с расширением) и domains.py (сверка с заявлением и паспортом).
AdminData ниже — данные администратора, введённые вручную или JSON-файлом; автозаполнение форм
manager (push_to_system) — следующий этап.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class AdminData:
    """Текущий администратор домена по данным внутренней системы."""
    fio: str = ""
    birth_date: str = ""
    passport: str = ""            # «СССС НННННН»
    passport_issue_date: str = ""
    passport_issued_by: str = ""  # «кем выдан» по данным системы
    domains: list[str] = field(default_factory=list)
    source: str = ""

    def is_empty(self) -> bool:
        return not (self.fio or self.passport)

    def to_dict(self) -> dict:
        return asdict(self)


class AdminDataProvider:
    def get_admin(self, domains: list[str]) -> AdminData | None:  # pragma: no cover - интерфейс
        raise NotImplementedError


class ManualAdminData(AdminDataProvider):
    """Данные, введённые оператором в интерфейсе."""

    def __init__(self, data: AdminData):
        self.data = data

    def get_admin(self, domains):
        return None if self.data.is_empty() else self.data


class JsonFileAdminData(AdminDataProvider):
    """Данные из JSON-файла (например, выгрузка из системы): объект AdminData
    или словарь {домен: AdminData}."""

    def __init__(self, path: str | Path):
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))

    def get_admin(self, domains):
        if "fio" in self.raw:
            return AdminData(**{**self.raw, "source": "файл"})
        for d in domains:
            if d in self.raw:
                return AdminData(**{**self.raw[d], "source": "файл"})
        return None


class InternalSystemProvider(AdminDataProvider):
    """TODO: чтение данных администратора домена из внутренней системы."""

    def get_admin(self, domains):
        raise NotImplementedError("Подключение к внутренней системе ещё не реализовано")


def push_to_system(record: dict) -> None:
    """TODO: автозаполнение формы во внутренней системе по выгруженной записи (export.build_record)."""
    raise NotImplementedError("Автозаполнение во внутренней системе ещё не реализовано")
