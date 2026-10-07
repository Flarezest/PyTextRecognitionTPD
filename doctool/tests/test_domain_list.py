"""Список доменов в заявлении: имя, перенесённое на следующую строку, полнота списка, повторное
распознавание абзаца на скане. Без OCR: распознавание подменяется готовым текстом.

Имена доменов вымышленные, но повторяют случаи из реального заявления (76 доменов, 0.3.1):
перенос после дефиса, пропавшая при OCR зона «.рф», «всего N доменов» в конце списка."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import application, parsers  # noqa: E402
from doctool.compare import compare  # noqa: E402
from doctool.domains import split_domains  # noqa: E402
from doctool.models import Extraction  # noqa: E402
from doctool.ocr import Line, Page, Word  # noqa: E402

# так OCR / текстовый слой PDF отдаёт абзац со списком: строки рвутся после дефиса, который входит в имя
WRAPPED = """alpha-site.ru, beta.ru, gamma-
delta.ru, epsilon.ru, info-
store.ru, zeta.su, take-profit-
fx.ru, пример-сайт.рф, тест-
магазин.рф — всего 8 (восемь) доменов"""
EXPECTED = ["alpha-site.ru", "beta.ru", "gamma-delta.ru", "epsilon.ru", "info-store.ru", "zeta.su",
            "take-profit-fx.ru", "пример-сайт.рф", "тест-магазин.рф"]


def test_wrapped_domain_names_are_joined():
    dl = parsers.parse_domain_list(WRAPPED)
    assert dl.domains == EXPECTED
    assert dl.joined == ["gamma-delta.ru", "info-store.ru", "take-profit-fx.ru", "тест-магазин.рф"]
    assert "delta.ru" not in dl.domains and "store.ru" not in dl.domains and "fx.ru" not in dl.domains
    assert dl.fragments == [] and dl.stated_count == 8 and dl.count_mismatch   # указано 8, найдено 9


def test_wrapped_names_after_lines_were_merged_with_spaces():
    # интерфейс или OCR уже склеили строки пробелом: «gamma- delta.ru» — метка не может кончаться дефисом
    assert parsers.find_domains("a.ru, gamma- delta.ru, b.ru") == ["a.ru", "gamma-delta.ru", "b.ru"]


def test_where_the_domain_name_ends():
    f = parsers.find_domains
    assert f("example.\nru, b.ru") == ["example.ru", "b.ru"]                 # разрыв после точки
    assert f("example\n.ru") == ["example.ru"]                               # зона на новой строке
    assert f("infofi\u00ad\nnances.ru") == ["infofinances.ru"]               # мягкий перенос — не часть имени
    assert f("a.ru -\nb.ru") == ["a.ru", "b.ru"]                             # тире-разделитель, не перенос
    assert f("a.ru, тест.рф —\nвсего 2 домена") == ["a.ru", "тест.рф"]
    assert f("example.ru.\nInfo") == ["example.ru"]                          # конец предложения, а не разрыв
    assert f("distobR-RU") == ["distobr.ru"] and f("distobr. ru") == ["distobr.ru"]   # рукопись, как раньше


def test_fragments_and_stated_count():
    # так Tesseract прочитал конец списка: подчёркивание срезало «рф»
    dl = parsers.parse_domain_list("alpha.ru, beta-\ngamma.ru, клуб-инфо.\nинфоклуб. — всего 4 (четыре) домена")
    assert dl.domains == ["alpha.ru", "beta-gamma.ru"]
    assert dl.fragments == ["клуб-инфо.", "инфоклуб."]
    assert dl.stated_count == 4 and dl.count_mismatch
    # перенос без дефиса посреди имени по тексту не отличить — кусок без зоны остаётся «обрывком»
    dl = parsers.parse_domain_list("cybersant\ninvestor.ru")
    assert dl.domains == ["investor.ru"] and dl.fragments == ["cybersant"]
    # обычные слова и числа — не обрывки
    assert parsers.parse_domain_list("a.ru и b.ru — всего 2 (два) домена.").fragments == []
    assert parsers.stated_domain_count("всего семьдесят шесть доменов") == 76
    assert parsers.stated_domain_count("в количестве 3 шт.") == 3
    assert parsers.stated_domain_count("trading-pips100.ru, a.ru") is None
    assert parsers.parse_domain_list("cybersаnty.ru").mixed == ["cybersаnty.ru"]   # «а» кириллическая


def test_split_domains_joins_wraps():
    assert split_domains("alpha.ru, gamma-\ndelta.ru info-\nstore.ru") == ["alpha.ru", "gamma-delta.ru", "info-store.ru"]


def _app_with_domains(raw: str) -> Extraction:
    app = Extraction("regru_transfer_person", "a.pdf")
    application._store(app, "domains", "domains", raw, 0.98, "text")
    return app


def _domains_check(app):
    return next(c for c in compare(app, None) if c.name == "Домен(ы)")


def test_domains_check_and_verdict():
    from doctool.verdict import decide, load_case_types
    ct = load_case_types()["admin_change_person"]
    # список полный: «в порядке», склеенные имена показаны оператору
    app = _app_with_domains(WRAPPED.replace("всего 8 (восемь)", "всего 9 (девять)"))
    c = _domains_check(app)
    assert c.status == "ok" and "gamma-delta.ru" in c.detail and "доменов: 9" in c.detail
    # домены потеряны: число не сходится, есть обрывки → ручная проверка с понятной причиной
    app = _app_with_domains("alpha.ru, beta-\ngamma.ru, инфоклуб. — всего 3 (три) домена")
    c = _domains_check(app)
    assert c.status == "review" and "указано доменов: 3, распознано: 2" in c.detail and "инфоклуб." in c.detail
    d = decide(ct, [c], set(), app.fields)
    assert d.code == "review" and any("Список доменов нужно сверить" in r for r in d.reasons)


def test_operator_edit_replaces_domain_subfields():
    app = _app_with_domains("alpha.ru, инфоклуб. — всего 2 (два) домена")
    assert app.get("domains.fragments") == ["инфоклуб."]
    application._store(app, "domains", "domains", "alpha.ru, инфоклуб.рф", 1.0, "manual")   # правка оператора
    assert app.get("domains.list") == ["alpha.ru", "инфоклуб.рф"]
    assert app.get("domains.fragments") is None              # старые обрывки не остаются
    assert app.get("domains.stated_count") == 2               # «всего 2» из заявления сохраняется
    assert _domains_check(app).status == "ok"


# ------------------------------------------------------------------ скан: абзац со списком перечитывается

FORM_TOP = ["Заявление в ООО «Регистратор доменных имен РЕГ.РУ» от Администратора домена - физического лица",
            "о передаче права администрирования домена другому лицу", "Генеральному директору",
            "ООО «Регистратор доменных имен РЕГ.РУ»", "Заявление"]
PARA = ["прошу передать права по администрированию домена(ов)"]
LABEL = ["(наименование домена(ов))", "новому Администратору:"]
# OCR всей страницы: «.рф» срезано подчёркиванием, «digitalproduct» прочитано как «digitaloroduct»
PAGE_LIST = ["alpha.ru, beta-", "gamma.ru, digitaloroduct.ru, клуб.", "инфо-клуб. — всего 5 (пять) доменов"]
BAND_LIST = ["alpha.ru, beta-", "gamma.ru, digitalproduct.ru, клуб.рф,", "инфо-клуб.рф — всего 5 (пять) доменов"]


def _lines(texts, y0=100):
    return [Line([Word(t, 90.0, 50, y0 + 40 * i, 1500, 30)]) for i, t in enumerate(texts)]


def _run_scan(monkeypatch, tmp_path, band_texts):
    page_lines = _lines(FORM_TOP + PARA + PAGE_LIST + LABEL)
    calls = []

    def fake_lines(img, lang="rus", psm=3, config=""):
        calls.append(psm)
        if len(calls) == 1:
            return page_lines                                 # страница целиком (psm 4)
        return _lines(PARA + band_texts[min(len(calls) - 2, len(band_texts) - 1)] + LABEL[:1])

    monkeypatch.setattr(application, "tesseract_lines", fake_lines)
    # области полей на скане не нужны: поле берётся из текста страницы (как при «пустой» области)
    monkeypatch.setattr(application, "extract_from_scan",
                        lambda page, form, out_dir, vlm=None, handwritten_mode=None, prefer=None:
                        Extraction(form["id"], page.source))
    page = Page(image=np.full((900, 1700, 3), 255, np.uint8), source="scan.png")
    return application.extract_application([page], tmp_path), calls


def test_scan_rereads_domain_paragraph(monkeypatch, tmp_path):
    ex, calls = _run_scan(monkeypatch, tmp_path, [BAND_LIST])
    assert ex.get("domains.list") == ["alpha.ru", "beta-gamma.ru", "digitalproduct.ru", "клуб.рф", "инфо-клуб.рф"]
    assert ex.get("domains.joined") == ["beta-gamma.ru"]
    assert ex.get("domains.fragments") is None and ex.get("domains.uncertain") is None
    # страница + 2 прочтения абзаца (совпали — третье не нужно) + раздел «новому Администратору» (пустой в тесте)
    assert calls == [4, 6, 6, 6]
    assert ex.debug["domains_reread"]["выбрано"] == "абзац"
    assert any("перечитан" in n for n in ex.notes)
    assert ex.fields["domains"].crop.endswith("scan_domains_list.png")   # фрагмент — весь абзац


def test_scan_reread_disagreement_goes_to_review(monkeypatch, tmp_path):
    other = ["alpha.ru, beta-", "gamma.ru, digitalpraduct.ru, клуб.рф,", "инфо-клуб.рф — всего 5 (пять) доменов"]
    third = ["alpha.ru, beta-", "gamma.ru, digitalprodukt.ru, клуб.рф,", "инфо-клуб.рф — всего 5 (пять) доменов"]
    ex, calls = _run_scan(monkeypatch, tmp_path, [BAND_LIST, other, third])
    assert calls == [4, 6, 6, 4, 6]            # прочтения не сошлись — все три прохода (+ раздел нового админа)
    assert len(ex.get("domains.list")) == 5
    assert ex.get("domains.uncertain") == ["digitalproduct.ru"]   # большинство это имя не подтвердило
    assert _domains_check(ex).status == "review"


def test_scan_fallback_keeps_domain_subfields(monkeypatch, tmp_path):
    # однострочный список без замечаний: абзац не перечитывается, а «domains.list» не теряется,
    # даже если область поля на скане сочтена пустой (в 0.3.0 здесь был вердикт «домен не указан»)
    page_lines = _lines(FORM_TOP + PARA + ["alpha.ru, beta.ru"] + LABEL)
    monkeypatch.setattr(application, "tesseract_lines", lambda img, lang="rus", psm=3, config="": page_lines)
    monkeypatch.setattr(application, "extract_from_scan",
                        lambda page, form, out_dir, vlm=None, handwritten_mode=None, prefer=None:
                        Extraction(form["id"], page.source))
    ex = application.extract_application([Page(image=np.full((900, 1700, 3), 255, np.uint8), source="s.png")],
                                         tmp_path)
    assert ex.get("domains.list") == ["alpha.ru", "beta.ru"] and "domains_reread" not in ex.debug
    assert _domains_check(ex).status == "ok"
