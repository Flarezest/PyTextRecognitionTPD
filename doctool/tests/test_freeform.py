"""Заявления, у которых OCR потерял подписи бланка, и заявления в свободной форме (наборы 8–19, 07.10.2026).
Тексты — синтетические, по образцу OCR реальных сканов (ФИО, номера и адреса вымышлены)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import application, formspec, names, parsers  # noqa: E402
from doctool.application import extract_from_text, parse_new_admin_block  # noqa: E402
from doctool.ocr import Page  # noqa: E402

FORM = formspec.form_by_id("regru_transfer_person")
TOP = ("Заявление в ООО «Регистратор доменных имен РЕГ.РУ» от\nАдминистратора домена - физического лица о передаче права\n"
       "администрирования домена другому лицу\nГенеральному директору\nООО «Регистратор доменных имен РЕГ.РУ»\n")


def _ex(text):
    return extract_from_text(text, FORM, "test", conf=0.85, source="ocr-text")


# ------------------------------------------------------------------ шапка: ФИО и дата рождения

def test_header_after_stamp_and_year_suffix():
    # перед «от» — оттиск штампа «ПОЛУЧЕНО», после даты — «г.»
    ex = _ex(TOP + "ПОЛУЧЕ но от Крайнева Виктора Николаевича,\n23.04.1985 г.\n(ФИО, дата рождения)\n"
             "45 11 500159 29.02.2012\n(серия, номер паспорта, когда выдан)\nОтделом УФМС России по гор. Москве\n(кем выдан)\n")
    assert ex.get("applicant_header") == "КРАЙНЕВ ВИКТОР НИКОЛАЕВИЧ"
    assert ex.get("applicant_header.birth_date") == "23.04.1985"
    assert ex.get("passport") == "4511 500159" and ex.get("passport.issue_date") == "29.02.2012"
    assert ex.get("issued_by") == "Отделом УФМС России по гор. Москве"


def test_header_name_first_order_and_dr_line():
    ex = _ex(TOP + "от Виктора Дмитриевича Андреева\n‚ д/р 13.03.1962\n(ФИО, дата рождения)\n"
             "4511430849, выдан УФМС по гор. Москве по\nрайону Марьина роща, 23.03.2012\n"
             "(серия, номер паспорта, когда и кем выдан)\n")
    assert ex.get("applicant_header") == "АНДРЕЕВ ВИКТОР ДМИТРИЕВИЧ"     # Имя Отчество Фамилия → Ф И О
    assert ex.get("applicant_header.birth_date") == "13.03.1962"
    assert ex.get("issued_by") == "УФМС по гор. Москве по району Марьина роща"   # без «(серия, … кем выдан)»


def test_notary_style_header_with_month_words():
    text = ("Генеральному директору ООО «Регистратор доменных имен\nРЕГ.РУ»\n"
            "от гражданки Российской Федерации, Петровой\nАнны Сергеевны, пол женский, 22 февраля 1987 года\n"
            "рождения, место рождения: гор. Смоленск, паспорт 4018 304191 выдан ГУ МВД России по г Санкт-\n"
            "Петербургу и Ленинградской области 10 апреля 2019 года, код\nподразделения: 780-040, зарегистрированной "
            "по месту\nжительства по адресу: Санкт-Петербург, проспект Тестовый, дом 5, квартира\n"
            "382. ИНН 672403040674.\nЗАЯВЛЕНИЕ\nЯ, Петрова Анна Сергеевна, прошу передать права по администрированию "
            "домена(ов)\nTest100.ru новому Администратору:\nИП Сидоров Олег Иванович. ИНН 780427858714\n"
            "е-тай: sidorov@example.ru\nНомер договора (название аккаунта) нового администратора: 16103009\n")
    ex = _ex(text)
    assert ex.get("applicant_header") == "ПЕТРОВА АННА СЕРГЕЕВНА"
    assert ex.get("applicant_header.birth_date") == "22.02.1987"
    assert ex.get("applicant_fio") == "Петрова Анна Сергеевна"           # без «прошу передать права…»
    assert ex.get("passport") == "4018 304191" and ex.get("passport.issue_date") == "10.04.2019"
    assert ex.get("issued_by") == "ГУ МВД России по г Санкт- Петербургу и Ленинградской области"
    assert ex.get("address").startswith("Санкт-Петербург, проспект Тестовый")
    assert ex.get("inn") == "672403040674"
    assert ex.get("domains.list") == ["test100.ru"]
    assert ex.get("new_admin_fio") == "СИДОРОВ ОЛЕГ ИВАНОВИЧ" and ex.get("new_admin_fio.kind") == "ip"
    assert ex.get("new_admin_contact.emails") == ["sidorov@example.ru"]   # ИНН ИП — не телефон
    assert ex.get("contract") == "16103009"


def test_fio_cut_and_wrapped_patronymic():
    ex = _ex(TOP + "от ШЕЙКО ЮРИЯ БОРИСОВИЧА, 27 апреля 1988 года\nрождения\nЗаявление\n"
             "Я, Шейко Юрий Борисович прошу передать права по администрированию домена\n")
    assert ex.get("applicant_header") == "ШЕЙКО ЮРИЙ БОРИСОВИЧ" and ex.get("applicant_header.birth_date") == "27.04.1988"
    assert ex.get("applicant_fio") == "Шейко Юрий Борисович"
    ex = _ex(TOP + "Заявление\nЯ, Тарасова Инесса\nАлександровна\n(фамилия, имя, отчество)\n")
    assert ex.get("applicant_fio") == "Тарасова Инесса Александровна"
    ex = _ex(TOP + "от Тестова Аскера Сафудиновича\n08.02.1988 гр.\nПаспорт РФ серия 83 07 № 876100\n")
    assert ex.get("applicant_header") == "ТЕСТОВ АСКЕР САФУДИНОВИЧ"     # без «ГР»
    assert ex.get("passport") == "8307 876100"


def test_passport_is_not_taken_from_inn_of_new_admin():
    ex = _ex(TOP + "от Созинова Сергея Валентиновича,\n01.05.1964 г.\nИНН: 753500005229\nДата выдачи: 05.04.2010\n"
             "Выдан: Отделением УФМС России\nпо Забайкальскому краю\n143405, обл. Московская,\n"
             "г. Красногорск, ш. Ильинское, д. 4, кв. 94\n(адрес регистрации)\nЗаявление\n"
             "новому Администратору:\nНаименование юр.лица\nООО «Тест Техника», ИНН 7536167149, ОГРН\n")
    assert ex.get("passport") is None                                   # номера паспорта в заявлении нет
    assert ex.get("inn") == "753500005229"                               # ИНН заявителя, не нового администратора
    assert ex.get("issued_by") == "Отделением УФМС России по Забайкальскому краю"
    assert ex.get("address") == "143405, обл. Московская, г. Красногорск, ш. Ильинское, д. 4, кв. 94"


def test_passport_issue_date_after_label_and_ocr_colon():
    ex = _ex(TOP + "от Иванова Ивана Ивановича,\n09.03.1987 года рождения\nСерия, номер паспорта:\n65 07 077513\n"
             "Паспорт выдан: ОТДЕЛОМ ВНУТРЕННИХ\nДЕЛ ГОРОДА ИРБИТА\nКод подразделения: 662-024\nДата выдачи: 28.03.2007\n"
             "Зарегистрирован: Свердловская область,\nг.Ирбит, ул. Тестовая 1\nЗаявление\n")
    assert ex.get("passport") == "6507 077513" and ex.get("passport.issue_date") == "28.03.2007"
    assert ex.get("issued_by") == "ОТДЕЛОМ ВНУТРЕННИХ ДЕЛ ГОРОДА ИРБИТА"
    assert ex.get("address") == "Свердловская область, г.Ирбит, ул. Тестовая 1"
    assert parsers.parse_date("20.01:2018") == parsers.parse_date("20.01.2018")


# ------------------------------------------------------------------ домены

def test_domains_free_form_endings():
    ex = _ex(TOP + "Я, Иванов Иван Иванович прошу передать права по администрированию\n"
             "домена(ов): https://ROSTEST.RU и https://Metannuka.pyc/ новому Администратору:\n")
    assert ex.get("domains.list") == ["rostest.ru", "metannuka.рус"]
    assert ex.get("domains.mixed") == ["metannuka.рус"]                 # латиница + кириллица → ручная проверка
    ex = _ex(TOP + "прошу передать права по администрированию домена www.dr-test.ru\n(наименование домена\n")
    assert ex.get("domains.list") == ["dr-test.ru"]


# ------------------------------------------------------------------ новый администратор

def test_new_admin_table_without_labels():
    # OCR потерял «Для физ.лиц и ИП:» и «ФИО», контакты — в строке без подписи
    ex = _ex(TOP + "новому Администратору:\nДля юр.лиц:\nНаименование юр.лица\ne-mail/Ten.\nя cu3.nuy uv ИП:\n"
             "Антонова Елена Александровна\nantonova@example.com / 8(985) 282-11-30\nе-та!/тел.\n"
             "Номер договора (название аккаунта) нового администратора / партнера (с указанием\nпрофиля): 1234567\n")
    assert ex.get("new_admin_fio") == "АНТОНОВА ЕЛЕНА АЛЕКСАНДРОВНА"
    assert ex.get("new_admin_contact.emails") == ["antonova@example.com"]
    assert ex.get("new_admin_contact.phones") == ["89852821130"]
    assert ex.get("new_admin_org") is None and ex.get("new_admin_org_contact") is None


def test_new_admin_table_without_section_header():
    ex = _ex(TOP + "новому Администратору:\nФИО | Сергееву Петру Сергеевичу\ne-mail/ten. | 8-926-359-71-13 ps@example.ru |\n"
             "Номер договора (название аккаунта)\n")
    assert ex.get("new_admin_fio") == "СЕРГЕЕВ ПЕТР СЕРГЕЕВИЧ"            # дательный падеж → именительный
    assert ex.get("new_admin_contact.emails") == ["ps@example.ru"]


def test_new_admin_free_form_org():
    block = ("ООО “Тестстем”\nОГРН 1207700242254\nИНН 9724016361 КПП 773101001\nАдрес местонахождения: 121205, г. Москва\n"
             "+7 (495) 768-08-49\n")
    assert parse_new_admin_block(block) == {"org": "ООО “Тестстем”", "org_contact": "+7 (495) 768-08-49"}
    ex = _ex(TOP + "и предоставляемых по этим доменам всех дополнительных услуг: DNS\nновому Администратору:\n" + block
             + "Договор №11950919\n")
    assert ex.get("new_admin_org") == "ООО “Тестстем”" and ex.get("contract") == "Договор №11950919"
    assert ex.get("passport") is None and ex.get("inn") is None


def test_new_admin_org_with_translit_and_inn():
    ex = _ex(TOP + "новому\nАдминистратору:\nНаименование\nОбщество с ограниченной ответственностью \"Тест\"\n"
             "Obshchestvo s ogranichennoi otvetstvennostyu \"Test\"\nюр.лица\nИНН: 9728001461\nКПП: 77280100\n"
             "info@example.net/ +7 (925) 033-87-16\n")
    assert ex.get("new_admin_org") == "Общество с ограниченной ответственностью \"Тест\""
    assert ex.get("new_admin_org_contact.emails") == ["info@example.net"]
    assert ex.get("passport") is None                                   # ИНН организации — не паспорт


def test_names_helpers():
    assert names.ordered(["Виктор", "Дмитриевич", "Андреев"]) == ["Андреев", "Виктор", "Дмитриевич"]
    assert names.extract_fio("ШЕЙКО ЮРИЙ БОРИСОВИЧ АПРЕЛЯ ГОДА") == ["ШЕЙКО", "ЮРИЙ", "БОРИСОВИЧ"]
    assert names.to_nominative("Дымовой Кристины Михайловны") == "ДЫМОВА КРИСТИНА МИХАЙЛОВНА"
    assert names.fio_similarity("Виктор Дмитриевич Андреев", "АНДРЕЕВ ВИКТОР ДМИТРИЕВИЧ")[0] == 100
    # стоп-слова — целые слова: фамилии «Годунов», «Мартынов», «Рождественский» и инициалы не обрезаются
    for fio in ("Годунов Борис Фёдорович", "Мартынов Илья Петрович", "Рождественский Олег Ильич"):
        assert names.extract_fio(fio) == fio.split()
    assert names.extract_fio("Иванов Р.А.") == ["Иванов", "Р", "А"]


# ------------------------------------------------------------------ страницы заявления в многостраничном файле

def test_application_pages_found_after_envelope(monkeypatch):
    texts = {0: "ПОЧТА РОССИИ Кому ООО РЕГ.РУ Куда Москва", 1: "ПОЧТА РОССИИ RUSSIAN POST",
             2: TOP + "от Колесникова Никиты Валентиновича\nЯ, Колесников Никита Валентинович прошу передать права по "
                      "администрированию\nдоменов:\n1) a-test.ru\n2) b-test.ru",
             3: "Российская Федерация нотариус свидетельствую подлинность подписи",
             4: "36) c-test.ru\n37) d-test.ru\nи предоставляемых по этим доменам всех дополнительных услуг\n"
                "новому Администратору:\nООО “Тест”\n(подпись) (ФИО)",
             5: TOP + "Я, Колесников Никита Валентинович прошу передать права по администрированию\nдоменов:\n1) e-test.com",
             6: "РОССИЙСКАЯ ФЕДЕРАЦИЯ ПАСПОРТ ВЫДАН ФАМИЛИЯ ИМЯ ОТЧЕСТВО ДАТА РОЖДЕНИЯ"}
    pages = [Page(image=np.full((50, 50, 3), 255, np.uint8), source="f.pdf", index=i) for i in texts]
    monkeypatch.setattr(application, "_quick_text", lambda p: texts[p.index])
    groups, passport = application._application_groups(pages, FORM, texts[0])
    assert [[p.index + 1 for p in g] for g in groups] == [[3, 5], [6]]   # конверт и нотариус — мимо
    assert passport == [7]
