"""Смена владельца ЛК (account.py): что ввели в «Пользователь», сверка базовой анкеты с заявлением,
подготовка значений для автозаполнения — без сети и без браузера."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import account  # noqa: E402

# Ответ расширения на account — как content/manager.js возвращает его на обезличенных страницах manager
BA = {"ru_pp.birth_date": "15.03.1990", "ru_pp.country": "RU", "ru_pp.e_mail": "person@example.com", "ru_pp.fax": "",
      "ru_pp.p_addr_city": "Москва", "ru_pp.passport_date": "20.04.2010", "ru_pp.passport_number": "4509123456",
      "ru_pp.passport_place": "ГУ МВД РОССИИ ПО Г. МОСКВЕ", "ru_pp.person_r_name": "Пётр",
      "ru_pp.person_r_patronimic": "Сергеевич", "ru_pp.person_r_surname": "Тестов", "ru_pp.phone": "+79990000000"}
ACCOUNT_DATA = {"user_id": "1000", "login": "person@example.com", "ba": BA, "is_entrepreneur": False,
                "statuses": ["E-mail подтвержден", "Идентификация через Госуслуги пройдена"],
                "servicing_org": 'ООО "РДХ"', "servicing_org_id": "3", "contypes": "RUSURF: ru_pp",
                "runic": {"user_id": "1000", "type": "pp", "values": {"person_r_surname": "Тестов", "p_addr_zip": "101000"}},
                "urls": {"runic": "https://manager.reg.ru/user/1000/runic_details"}, "links": {}}


def test_parse_query():
    p = account.parse_query
    for s in ("Данные пользователя #15904265", "Базовая анкета пользователя #15904265", "15904265",
              "https://manager.reg.ru/manager/user_details?user_id=15904265",
              "https://manager.reg.ru/user/15904265/runic_details", "договор № 15904265 от 23.10.2023 г."):
        assert p(s)["kind"] == "id" and p(s)["value"] == "15904265", s
    assert p("wakeup@example.com") == {"kind": "login", "value": "wakeup@example.com", "label": "логин / e-mail wakeup@example.com"}
    assert p("ivanov_77")["kind"] == "login"
    assert p("Example.RU")["kind"] == "domain" and p("Example.RU")["value"] == "example.ru"
    assert p("пример.рф")["kind"] == "domain"
    assert p("непонятно что")["kind"] == "" and p("")["kind"] == ""


def test_account_from_extension():
    a = account.account_from_extension(ACCOUNT_DATA)
    assert a["type"] == "pp" and a["type_ru"] == "физлицо" and a["ba_group"] == "ru_pp"
    assert a["ba"]["person_r_surname"] == "Тестов" and a["ba"]["passport_number"] == "4509123456"
    assert a["esia_account"] == "Идентификация через Госуслуги пройдена" and a["servicing_org"] == 'ООО "РДХ"'
    assert not a["notes"]
    org = account.account_from_extension({**ACCOUNT_DATA, "runic": {"type": "org", "values": {}}})
    assert org["type"] == "org" and "юрлицо" in org["notes"][0]
    # без страницы анкеты тип — по группе базовой анкеты и флажку ИП
    ip = account.account_from_extension({**ACCOUNT_DATA, "runic": {}, "is_entrepreneur": True})
    assert ip["type"] == "ip"


def test_compare_owner():
    ba = account.account_from_extension(ACCOUNT_DATA)["ba"]
    app = {"person_r_surname": "ТЕСТОВ", "person_r_name": "Петр", "person_r_patronimic": "Сергеевич",
           "birth_date": "15.03.1990", "country": "Россия", "passport_number": "4509 123456", "passport_date": "20.04.2011",
           "passport_place": "ГУ МВД России по г. Москве", "phone": "8 (999) 000-00-00"}
    r = account.compare_owner(ba, app)
    st = {x["field"]: x["status"] for x in r["rows"]}
    assert st["person_r_surname"] == "ok" and st["person_r_name"] == "ok"          # регистр, ё/е
    assert st["country"] == "ok" and st["passport_number"] == "ok" and st["phone"] == "ok"
    assert st["passport_date"] == "fail" and r["mismatch"] == ["Дата выдачи паспорта"]
    assert st["passport_place"] == "ok"                                              # регистр и точки не важны
    assert r["ok"] == 8 and r["total"] == 9
    empty = account.compare_owner(ba, {"phone": ""})
    assert all(x["status"] == "" for x in empty["rows"]) and empty["total"] == 0
    # «кем выдан» написан по-разному, но похоже — жёлтый
    w = account.compare_owner(ba, {"passport_place": "Главное управление МВД России по г. Москве"})
    assert {x["field"]: x["status"] for x in w["rows"]}["passport_place"] in ("warn", "fail")


def test_city_from_address():
    c = account.city_from_address
    assert c("г. Санкт-Петербург, ул. Чекистов, д. 38, кв. 579") == "Санкт-Петербург"
    assert c("198206, Санкт-Петербург, ул. Чекистов, д 38") == "Санкт-Петербург"
    assert c("350087, Краснодарский край, г Краснодар, пр-д 3-й Куликова Поля, д. 32/4") == "Краснодар"
    assert c("141190, г.Фрязино, ул. Проспект Мира, д.19, кв.187") == "Фрязино"
    assert c("Москва г, ул. Тверская, д 1") == "Москва"
    assert c("Московская обл., г.о. Красногорск, д. Путилково, ул. Новая, д. 5") == "Путилково"
    assert c("Пермский край, Осинский р-н, с. Крылово, ул. Ленина 3") == "Крылово"
    assert c("ул. Ленина, д. 3") == ""


def test_split_issued():
    s = account.split_issued
    assert s("ГУ МВД РОССИИ ПО Г. САНКТ-ПЕТЕРБУРГУ И ЛЕНИНГРАДСКОЙ ОБЛАСТИ, 24.08.2021") == (
        "ГУ МВД РОССИИ ПО Г. САНКТ-ПЕТЕРБУРГУ И ЛЕНИНГРАДСКОЙ ОБЛАСТИ", "24.08.2021", "")
    assert s("Отделом УФМС России по г. Москве 12.04.2015 г., код подразделения 770-001") == (
        "Отделом УФМС России по г. Москве", "12.04.2015", "770-001")
    assert s("выдан 03 июня 2009 года ОВД Тверского района г. Москвы") == ("ОВД Тверского района г. Москвы", "03.06.2009", "")
    assert s("ТП № 5 ОУФМС России по Санкт-Петербургу, дата выдачи 03.12.2010")[:2] == (
        "ТП № 5 ОУФМС России по Санкт-Петербургу", "03.12.2010")
    assert s("ГУ МВД России по Пермскому краю") == ("ГУ МВД России по Пермскому краю", "", "")


def test_prepare_fill():
    r = account.prepare_fill({"surname": "СИДОРОВА", "name": "анна", "patronymic": "Викторовна", "birth_date": "21.11.1990",
                              "passport": "40 12 654321", "issued": "ТП № 5 ОУФМС России по Санкт-Петербургу, 03.12.2010",
                              "address": "г. Санкт-Петербург,  пр-кт Большевиков, д. 7, кв. 15",
                              "phone": "8 (921) 555-12-34", "email": "Anna@Example.com"})
    f = r["fields"]
    assert f["person_r_surname"] == "Сидорова" and f["person_r_name"] == "Анна"
    assert f["passport_number"] == "4012654321" and f["passport_date"] == "03.12.2010"
    assert f["passport_place"] == "ТП № 5 ОУФМС России по Санкт-Петербургу"
    assert f["p_addr_addr"] == "г. Санкт-Петербург, пр-кт Большевиков, д. 7, кв. 15"      # 1 в 1 (без двойных пробелов)
    assert f["p_addr_city"] == "Санкт-Петербург" and f["p_addr_recipient"] == "Сидорова Анна Викторовна"
    assert f["phone"] == "+79215551234" and f["e_mail"] == "anna@example.com"
    assert "p_addr_zip" not in f and any("Индекс" in n for n in r["notes"])                  # пусто → останется прежний
    assert set(f) <= {k for k, _ in account.FILL_FIELDS}
    # гражданство, страна почтового адреса, область, SMS-безопасность — не заполняются
    assert not {"country", "p_addr_country", "p_addr_area", "sms_security_number"} & set(f)
    # отдельные поля важнее строки «кем и когда выдан»; город и получатель можно задать руками
    r2 = account.prepare_fill({"issued": "ОВД г. Перми, 01.02.2003", "passport_date": "02.02.2003", "city": "Пермь",
                               "recipient": "Иванов И. И.", "zip": "614 000"})
    assert r2["fields"]["passport_date"] == "02.02.2003" and r2["fields"]["passport_place"] == "ОВД г. Перми"
    assert r2["fields"]["p_addr_city"] == "Пермь" and r2["fields"]["p_addr_recipient"] == "Иванов И. И."
    assert r2["fields"]["p_addr_zip"] == "614000"
    # иностранный паспорт — как есть, с пометкой
    r3 = account.prepare_fill({"passport": "AB 1234567"})
    assert r3["fields"]["passport_number"] == "AB 1234567" and r3["notes"]
    assert account.prepare_fill({})["fields"] == {}


def test_verify_saved():
    v = account.verify_saved({"person_r_surname": "Сидорова", "phone": "+79215551234", "birth_date": "21.11.1990"},
                             {"person_r_surname": "Сидорова", "phone": "+7 921 555-12-34", "birth_date": "21.11.1990",
                              "e_mail": "anna@example.com"})
    assert not v["ok"] and v["mismatch"] == ["Email"]


def test_resolve_account_by_domain():
    from test_domains import S_PP, SD_PP, reply
    calls = []

    def request(cmd, params, timeout, on_progress):
        calls.append((cmd, params))
        if cmd == "lookup":
            r = reply(SD_PP, S_PP, "testdomain-pp.ru")
            r["data"]["bill_owner"] = "other@example.com"
            return r
        return {"ok": True, "data": ACCOUNT_DATA}

    a = account.resolve_account(request, "testdomain-pp.ru", log=lambda m: None)
    assert calls[0][0] == "lookup" and calls[0][1]["esia"] is False
    assert calls[1] == ("account", {"user_id": "1000"})
    assert a["user_id"] == "1000" and a["via_domain"]["domain"] == "testdomain-pp.ru"
    assert "other@example.com" in a["notes"][0]                           # владелец счёта ≠ владелец услуги

    calls.clear()
    account.resolve_account(request, "person@example.com", log=lambda m: None)
    assert calls == [("account", {"login": "person@example.com"})]

    def fail(cmd, params, timeout, on_progress):
        return {"ok": False, "error": {"code": "not_found", "message": "Аккаунт #5 не найден в manager."}}
    try:
        account.resolve_account(fail, "5555", log=lambda m: None)
        raise AssertionError("ожидалась ошибка")
    except account.AccountError as e:
        assert e.code == "not_found" and "не найден" in str(e)
