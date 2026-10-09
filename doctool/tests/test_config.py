"""Описания бланков и типов заявлений, реестр проверок, роли полей — без OCR.

Здесь же проверяется, что новый бланк подключается только YAML-файлом: поля с другими именами
сопоставлены ролям (role:), проверки, таблица, правки оператора и выгрузка работают без изменений кода."""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import checks as checklib  # noqa: E402
from doctool import formspec  # noqa: E402
from doctool.application import FIELD_PARSERS, field_type, parse_value  # noqa: E402
from doctool.models import Extraction  # noqa: E402
from doctool.verdict import load_case_types  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_config_is_valid():
    assert formspec.validate_config() == []


def test_every_check_has_row_and_rules_use_known_names():
    names = checklib.all_names()
    assert "Домен(ы)" in names and names["Кем выдан ↔ код подразделения"] == ("issued_by", "department_code")
    for ct in load_case_types().values():
        for kind in ("reject", "review"):
            for rule in (ct.get("rules") or {}).get(kind) or []:
                if "check" in rule["when"]:
                    assert rule["when"]["check"] in names, rule


def test_ids_for():
    types = load_case_types()
    assert checklib.ids_for(types["passport_only"]) == list(checklib.PASSPORT_ONLY)
    assert checklib.ids_for(types["admin_change_person"]) == list(checklib.REGISTRY)   # в YAML перечислены все
    assert checklib.ids_for({"form": None}) == list(checklib.PASSPORT_ONLY)            # тип без бланка и без checks
    assert checklib.ids_for({"form": "x"}) == list(checklib.REGISTRY)


def test_register_same_id_twice_is_an_error():
    with pytest.raises(ValueError):
        @checklib.register("inn", {"ИНН ИП: контрольные цифры": "inn"})
        def other_inn(ctx):  # noqa: ARG001
            return None


def test_validate_finds_mistakes(tmp_path):
    forms = tmp_path / "forms"
    forms.mkdir()
    (forms / "bad.yaml").write_text(yaml.safe_dump({
        "id": "bad", "detect": {"keywords": []},
        "labels_re": ["(незакрытая"],
        "fields": {
            "a": {"type": "fio_x", "text": ["нет группы"]},
            "b": {"type": "fio", "role": "applicant_fio", "scan": {"anchor": ["x"], "region": {"dy0": 0}}},
            "c": {"type": "text", "role": "applicant_fio", "scan": {"anchor": ["y"], "block_start": ["z"],
                                                                     "region": {"dy0": 0, "dy1": 1, "x0": 0, "x1": 1}}},
            "d": {"type": "text", "role": "no_such_role"},
        },
        "rows": [{"id": "fio", "title": "дубль"}, {"id": "x", "title": "X", "app": ["nothing"]}],
    }, allow_unicode=True), encoding="utf-8")
    types = {
        "t1": {"form": "missing_form", "checks": ["inn", "no_such_check"], "rules": {
            "reject": [{"when": {"check": "Домен(ы)", "status": ["fail"]}, "reason": "не включена"},
                       {"when": {"check": "Опечатка", "status": ["fail"]}, "reason": "нет такой"},
                       {"when": {"flag": "no_flag"}, "reason": "флаг"},
                       {"when": {"any_status": ["bad"]}, "reason": "статус"}]}},
        "t2": {"form": None, "enabled": True},
    }
    probs = "\n".join(formspec.validate_config(forms, types))
    for part in ("detect.keywords", "labels_re", "неизвестный type 'fio_x'", "нет группы", "scan.region нет dy1",
                 "роль 'applicant_fio' уже у поля b", "block_start поддерживается только", "неизвестная роль 'no_such_role'",
                 "совпадает со стандартной", "'nothing'", "бланк 'missing_form' не найден", "'no_such_check'",
                 "не включена в checks", "проверки «Опечатка» нет", "флаг 'no_flag'", "статус 'bad'",
                 "t2: тип без бланка должен перечислить проверки"):
        assert part in probs, part


def test_field_type_registry():
    assert parse_value("no_such_type", "  значение  ") == {"": "значение"}
    assert parse_value("fio", "Тестов Пётр Сергеевич (фамилия, имя, отчество)") == {"": "Тестов Пётр Сергеевич"}

    @field_type("upper_test")
    def _upper(s, raw):
        return {"": s.upper(), "len": len(s)}
    try:
        assert parse_value("upper_test", "abc") == {"": "ABC", "len": 3}
    finally:
        FIELD_PARSERS.pop("upper_test")


def test_labels_and_prefixes_from_yaml_and_legacy_default():
    form = formspec.form_by_id("regru_transfer_person")
    assert formspec.labels_regex(form).sub(" ", "ГУ МВД (кем выдан)").strip() == "ГУ МВД"
    assert formspec.prefix_regex(form).sub("", "от Тестова").strip() == "Тестова"
    # бланк без ключей (YAML от 0.3.x) — прежние списки; пустой список — ничего не вырезать
    old = {"id": "old", "fields": {}}
    assert formspec.labels_regex(old).pattern == formspec.labels_regex(form).pattern
    assert formspec.prefix_regex(old).pattern == formspec.prefix_regex(form).pattern
    assert formspec.prefix_regex({"strip_prefixes": []}) is None


# ------------------------------------------------------------------ новый бланк только через YAML

NEW_FORM = {
    "id": "test_owner_form", "title": "Тестовый бланк",
    "detect": {"keywords": ["тестовый бланк", "владелец"], "min_hits": 2},
    "fields": {
        "owner": {"title": "Владелец", "type": "fio", "role": "applicant_fio",
                  "text": [r"(?m)^Владелец:\s*(.+)$"]},
        "doc": {"title": "Паспорт владельца", "type": "passport", "role": "passport",
                "text": [r"(?m)^Паспорт:\s*(.+)$"]},
        "names": {"title": "Домены", "type": "domains", "role": "domains", "text": [r"(?s)Домены:\s*(.+?)\nТариф"]},
        "tariff": {"title": "Тариф", "type": "text", "text": [r"(?m)^Тариф:\s*(.+)$"]},
    },
    "rows": [{"id": "tariff", "title": "Тариф", "app": ["tariff"], "pas": None}],
}
NEW_TYPE = {"title": "Тест", "form": "test_owner_form", "enabled": True, "needs_passport": True,
            "checks": ["fio_text", "passport_number", "domains"],
            "rules": {"reject": [{"when": {"missing": "applicant_fio"}, "reason": "нет владельца"},
                                 {"when": {"check": "Серия и номер паспорта", "status": ["fail"]},
                                  "reason": "чужой паспорт"}]}}


@pytest.fixture
def new_form(tmp_path, monkeypatch):
    forms = tmp_path / "forms"
    forms.mkdir()
    shutil.copy(ROOT / "forms" / "regru_transfer_person.yaml", forms)
    (forms / "test_owner_form.yaml").write_text(yaml.safe_dump(NEW_FORM, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(formspec, "FORMS_DIR", forms)
    return forms


def _case(tmp_path, app):
    from doctool.service import CaseInput, CaseResult, _evaluate
    from test_domains import passport
    res = CaseResult(inp=CaseInput(case_type="test"), case_type=NEW_TYPE, case_id="t", case_dir=tmp_path)
    res.app, res.pas = app, passport()
    _evaluate(res)
    return res


def test_new_form_works_through_roles(tmp_path, new_form):
    from doctool.application import extract_from_text
    from doctool.service import recompute, table_rows
    assert formspec.validate_config(new_form, {**load_case_types(), "test": NEW_TYPE}) == []
    text = ("Тестовый бланк\nВладелец: Тестов Пётр Сергеевич\nПаспорт: 4509 123456 выдан 20.04.2010\n"
            "Домены: alpha-\nbeta.ru, gamma.ru\nТариф: Премиум\n")
    app = extract_from_text(text, formspec.form_by_id("test_owner_form"), "x.pdf")
    assert app.get("names.list") == ["alpha-beta.ru", "gamma.ru"]
    res = _case(tmp_path, app)
    by = {c.name: c for c in res.checks}
    assert set(by) == {"ФИО в тексте («Я, …»)", "Серия и номер паспорта", "Домен(ы)"}
    assert by["ФИО в тексте («Я, …»)"].status == "ok" and by["Серия и номер паспорта"].status == "ok"
    assert by["Домен(ы)"].application == "alpha-beta.ru, gamma.ru"
    assert res.decision.code == "accept"
    rows = {r["id"]: r for r in table_rows(res)}
    assert rows["fio"]["app"] == "Тестов Пётр Сергеевич" and rows["passport"]["status"] == "ok"
    assert rows["tariff"]["app"] == "Премиум"                       # строка из rows: бланка
    rec = res.record
    assert [d["name"] for d in rec["domains"]] == ["alpha-beta.ru", "gamma.ru"]
    assert rec["applicant"]["passport"]["series"] == "4509"
    assert rec["application_form"] == "test_owner_form" and rec["application_fields"]["tariff"] == "Премиум"
    # правка оператора попадает в поле бланка по роли
    recompute(res, {"domains": {"app": "delta.ru"}, "passport": {"app": "4509 000000"}})
    assert app.get("names.list") == ["delta.ru"] and app.get("doc") == "4509 000000"
    assert res.decision.code == "reject" and "чужой паспорт" in res.decision.reasons


def test_missing_rule_by_role(tmp_path, new_form):
    app = Extraction("test_owner_form", "x.pdf")
    app.set("doc", "4509 123456", 0.98, "text")
    res = _case(tmp_path, app)
    assert res.decision.code == "reject" and "нет владельца" in res.decision.reasons


def test_validate_command(capsys):
    from doctool.__main__ import main
    main(["validate", "--list"])
    out = capsys.readouterr().out
    assert "Конфигурация в порядке" in out and "manager_domains" in out and "applicant_fio" in out


def test_name_and_version_match_changelog_and_extension(capsys):
    """Название программы и версия: __init__.py = верхний раздел CHANGELOG.md; расширение — то же название,
    версия не новее программы (расширение меняется не в каждой версии)."""
    import json
    import re

    from doctool import APP_NAME, __version__
    from doctool.__main__ import main
    assert APP_NAME == "PySimpleManager"
    top = re.search(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    assert top and top.group(1) == __version__
    manifest = json.loads((ROOT / "doctool_extension" / "manifest.json").read_text(encoding="utf-8"))
    ver = lambda v: tuple(map(int, v.split(".")))  # noqa: E731
    assert manifest["name"] == APP_NAME and ver(manifest["version"]) <= ver(__version__)
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"{APP_NAME} {__version__}"
