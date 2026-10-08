"""Галка «Поля заявления — нейросетью» (0.7.0): режим на уровне дела и проверки ответа модели.

Модель не вызывается: ответ подставляется вместо llm_extract.ask. Тексты синтетические, по образцу OCR
реальных заявлений (ФИО, номера и адреса вымышлены). Ошибки модели в ответах — те, что встречались на наборах
PhysicalPersonAdminChange с qwen3:8b: ИНН = номер паспорта, обрезанный «кем выдан», печатная подсказка бланка,
ФИО у подписи из «Я, …», заявитель как новый администратор, пропущенный домен в списке."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import application, formspec, llm_extract  # noqa: E402

FORM = formspec.form_by_id("regru_transfer_person")

TEXT = """Генеральному директору
ООО «Регистратор доменных имен РЕГ.РУ»
от Дымовой Кристины Михайловны
21.06.1990
(ФИО, дата рождения)
4610 966611 выдан 09.07.2010 ТП в
пос.Тестовое О-НИЯ УФМС России по
Московской обл. в Тестовом р-не
зарегистрированного по адресу
Московская обл. Тестовый р-н с.Пример ул.Новая д.1 кв.9
Заявление
Я, Дымова Кристина Михайловна
(фамилия, имя, отчество)
прошу передать права по администрированию домена(ов)
primer-odin.ru, primer-dva.ru,
primer-tri.ru
(наименование домена(ов)
и предоставляемых по этим доменам дополнительных услуг (при необходимости)
(Primary, Secondary DNS)
новому Администратору:
ФИО | Сергееву Петру Сергеевичу
e-mail/тел. | 8-926-000-00-00 petr@example.ru
Номер договора (название аккаунта) нового администратора / партнера (с указанием
профиля): 1234567/NIC-REG
/ (подпись) (ФИО)
"Двадцатое сентября" 2026 г.
(дата прописью)
"""

ANSWER = {
    "applicant_header": "Дымовой Кристины Михайловны, 21.06.1990",     # в тексте — через перевод строки
    "inn": "4610966611",                                               # номер паспорта, а не ИНН
    "passport": "4610 966611 … 09.07.2010",
    "issued_by": "УФМС России по Московской обл. в Тестовом р-не",     # обрезано слева
    "address": "Московская обл. Тестовый р-н с.Пример ул.Новая д.1 кв.9",
    "applicant_fio": "Дымова Кристина Михайловна",
    "domains": ["primer-odin.ru", "primer-dva.ru"],                    # третий домен пропущен
    "domains__count": "",
    "services": "Primary, Secondary DNS",                              # печатная подсказка бланка
    "new_admin_inline": "",
    "new_admin_org": "",
    "new_admin_org_contact": "",
    "new_admin_fio": "Сергееву Петру Сергеевичу",
    "new_admin_contact": "8-926-000-00-00 petr@example.ru",
    "contract": "1234567/NIC-REG",
    "signature_fio": "Дымова Кристина Михайловна",                      # копия «Я, …»: у подписи ФИО нет
    "application_date": "Двадцатое сентября 2026 г.",
}


@pytest.fixture
def fake_model(monkeypatch):
    """Подменяет обращение к модели: возвращает answers[...] по очереди (последний — для всех следующих)."""
    calls = []

    def install(*answers):
        def ask(cfg, messages, schema):
            calls.append(schema)
            a = answers[min(len(calls) - 1, len(answers) - 1)]
            if isinstance(a, Exception):
                raise a
            return dict(a), {"model": cfg.model, "seconds": 1.0, "cached": False}
        monkeypatch.setattr(llm_extract, "ask", ask)
        return calls
    return install


def _llm(text, **kw):
    with llm_extract.session(llm_extract.make_config("test-model", cache_dir=None)):
        return application.text_fields(text, FORM, "test", conf=0.85, source="ocr-text", **kw)


# ------------------------------------------------------------------ режим на уровне дела

def test_without_checkbox_regex_is_used(fake_model):
    calls = fake_model(ANSWER)
    ex = application.text_fields(TEXT, FORM, "test", conf=0.85, source="ocr-text")
    assert calls == [] and "llm" not in ex.debug
    assert ex.get("applicant_fio") == "Дымова Кристина Михайловна"
    assert ex.fields["applicant_fio"].source == "ocr-text"


def test_session_is_per_thread():
    seen = {}
    with llm_extract.session(llm_extract.make_config("m1", cache_dir=None)):
        t = threading.Thread(target=lambda: seen.setdefault("other", llm_extract.config()))
        t.start()
        t.join()
        seen["here"] = llm_extract.config()
    assert seen["other"] is None and seen["here"].model == "m1"
    assert llm_extract.config() is None


def test_model_failure_falls_back_to_regex(fake_model):
    fake_model(llm_extract.LLMError("timed out"))
    ex = _llm(TEXT)
    assert ex.get("applicant_fio") == "Дымова Кристина Михайловна"
    assert any("регулярками бланка" in n for n in ex.notes) and ex.debug.get("llm_error")


def test_empty_text_does_not_call_model(fake_model):
    calls = fake_model(ANSWER)
    _llm("   \n  ")
    assert calls == []


# ------------------------------------------------------------------ проверки ответа модели

def test_answer_checks(fake_model):
    fake_model(ANSWER)
    ex = _llm(TEXT)
    rep = ex.debug["llm"][-1]["fields"]
    # цитата, разорванная переводом строки и знаками, привязана к тексту
    assert ex.get("applicant_header") == "ДЫМОВА КРИСТИНА МИХАЙЛОВНА"
    assert ex.get("applicant_header.birth_date") == "21.06.1990"
    assert ex.fields["applicant_header"].source == "llm-ocr-text" and ex.fields["applicant_header"].confidence == 0.85
    # номер паспорта в поле ИНН отброшен (у физлица ИНН — 12 цифр)
    assert ex.get("inn") is None and "цифр" in rep["inn"]["dropped"]
    assert ex.get("passport") == "4610 966611" and ex.get("passport.issue_date") == "09.07.2010"
    # «кем выдан» расширен до естественных границ
    assert ex.get("issued_by").startswith("ТП в пос.Тестовое О-НИЯ УФМС")
    assert ex.get("issued_by").endswith("в Тестовом р-не")
    # печатная подсказка бланка — не значение
    assert ex.get("services") is None and rep["services"]["dropped"] == "печатная подпись бланка"
    # ФИО у подписи = то же место текста, что «Я, …» → отброшено
    assert ex.get("signature_fio") is None and "applicant_fio" in rep["signature_fio"]["dropped"]
    # пропущенный моделью домен добавлен из строк списка и помечен для ручной проверки
    assert ex.get("domains.list") == ["primer-odin.ru", "primer-dva.ru", "primer-tri.ru"]
    assert ex.get("domains.uncertain") == ["primer-tri.ru"]
    assert ex.get("new_admin_fio") == "СЕРГЕЕВ ПЕТР СЕРГЕЕВИЧ"
    assert ex.get("new_admin_contact.emails") == ["petr@example.ru"]
    assert ex.get("application_date") == "20.09.2026"


def test_value_not_in_text_needs_review(fake_model):
    fake_model({**ANSWER, "address": "г. Москва, ул. Выдуманная, д. 5"})
    ex = _llm(TEXT)
    f = ex.fields["address"]
    assert f.confidence == llm_extract.LOW_CONF and f.needs_review and f.source.endswith("?")
    assert any("нет в тексте" in n for n in ex.notes)


def test_new_admin_equal_to_applicant_is_dropped(fake_model):
    fake_model({**ANSWER, "new_admin_fio": "Дымова Кристина Михайловна", "signature_fio": ""})
    ex = _llm(TEXT)
    assert ex.get("new_admin_fio") is None
    assert ex.debug["llm"][-1]["fields"]["new_admin_fio"]["dropped"] == "совпадает с заявителем"


def test_signature_with_second_occurrence_is_kept(fake_model):
    text = TEXT.replace("/ (подпись) (ФИО)", "/ Дымова Кристина Михайловна /\n(подпись) (ФИО)")
    fake_model(ANSWER)
    ex = _llm(text)
    assert ex.get("signature_fio") == "Дымова Кристина Михайловна"


def test_domain_with_space_and_email_domain(fake_model):
    fake_model({**ANSWER, "domains": ["primer-odin. ru", "primer-dva.ru", "primer-tri.ru", "example.ru"]})
    ex = _llm(TEXT)
    unc = ex.get("domains.uncertain") or []
    assert "primer-odin.ru" not in unc          # пробел внутри имени — не «нет в тексте»
    assert "example.ru" in unc                  # домен только из адреса e-mail


def test_printed_form_text_is_dropped(fake_model):
    fake_model({**ANSWER, "services": "и предоставляемых по этим доменам дополнительных услуг (при необходимости)"})
    ex = _llm(TEXT)
    assert ex.get("services") is None


def test_validate_llm_keys(tmp_path):
    import shutil
    import yaml
    d = tmp_path / "forms"
    d.mkdir()
    src = formspec.FORMS_DIR / "regru_transfer_person.yaml"
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    data["fields"]["inn"]["digits"] = ["двенадцать"]
    data["fields"]["services"]["llm"] = 5
    (d / "f.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    problems = formspec.validate_config(d)
    assert any("digits" in p for p in problems) and any("llm" in p and "services" in p for p in problems)
    shutil.rmtree(d)
