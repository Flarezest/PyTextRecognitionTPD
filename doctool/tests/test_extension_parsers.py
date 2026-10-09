"""Разбор страниц manager скриптом расширения (doctool_extension/content/manager.js) на обезличенных копиях
страниц. Нужен Playwright с Chromium (pip install playwright && python -m playwright install chromium);
без него тест пропускается. Если manager поменяет вёрстку — сохраните новую страницу, обезличьте её,
положите в tests/fixtures/manager/ и прогоните тест."""
import json
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "manager"
JS = (ROOT / "doctool_extension" / "content" / "manager.js").read_text(encoding="utf-8")
BG = (ROOT / "doctool_extension" / "background.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium для Playwright не установлен: {e}")
        yield browser.new_page()
        browser.close()


def run(page, fixture, expr):
    page.goto((FIX / fixture).as_uri())
    page.add_script_tag(content=JS)
    return page.evaluate(f"() => {{ const M = window.__doctoolManager; return JSON.stringify({expr}); }}")


def test_bills(page):
    r = json.loads(run(page, "bills_pp.html", 'M.pickService(M.parseBills(document).rows, ["testdomain-pp.ru"])'))
    assert r["service"]["service_id"] == "120862733" and r["service"]["state"] == "A"   # строка SSL пропущена
    r = json.loads(run(page, "bills_notfound.html", "M.parseBills(document)"))
    assert r == {"total": 0, "rows": []}


def test_sd_and_s(page):
    sd = json.loads(run(page, "sd_pp.html", "M.parseSd(document)"))
    assert sd["group"] == "ru_pp" and sd["fields"]["person_r_surname"] == "Тестов"
    assert sd["fields"]["last_admin_change"] == "2025-11-20 10:00:00" and "authinfo" not in sd["fields"]
    assert sd["trustee"] == "Нет" and sd["status"] == "Активна (A)"
    org = json.loads(run(page, "sd_org.html", "M.parseSd(document)"))
    assert org["group"] == "ru_org" and org["fields"]["code"] == "7701234567" and "FOA_token" not in org["fields"]
    s = json.loads(run(page, "s_org.html", "M.parseS(document)"))
    assert s["provider"] == "cctldru" and s["dname"] == "testdomain-org.ru" and s["user_id"] == "2000"


def test_sd_email_and_intl(page):
    # .RU: e-mail администратора — поле e_mail группы ru_pp/ru_org (с 0.5.0 передаётся в doctool)
    sd = json.loads(run(page, "sd_pp.html", "M.parseSd(document)"))
    assert sd["fields"]["e_mail"].splitlines()[0] == "person@example.com"
    # международная зона: группа владельца o_deti_org (не a_/t_, хотя в них больше строк), e-mail — o_email
    intl = json.loads(run(page, "sd_intl.html", "M.parseSd(document)"))
    assert intl["group"] == "o_deti_org" and intl["status"] == "Активна (A)"
    assert intl["fields"]["o_email"] == "owner@example.com\nsecond@example.org"
    assert intl["fields"]["o_company"] == "Test Holding LP" and "o_phone" not in intl["fields"]
    assert "authinfo" not in intl["fields"] and "o_addr" not in intl["fields"]


# ------------------------------------------------------------------ 0.6.0: ЕСИА, аккаунт, базовая анкета

def test_sd_links_to_esia(page):
    # ссылка «Идентификация через Госуслуги» берётся со страницы Sd домена: у физлица ?user_id=, у персоны — ?person_id=
    sd = json.loads(run(page, "sd_pp.html", "M.parseSd(document)"))
    assert sd["links"]["esia"] == "https://manager.reg.ru/manager/esia_identifications?user_id=1000"
    assert sd["links"]["person"] == "https://manager.reg.ru/manager/user_details?user_id=1000"
    org = json.loads(run(page, "sd_org.html", "M.parseSd(document)"))
    assert org["links"]["esia"].endswith("esia_identifications?person_id=3000")


def test_esia_list(page):
    r = json.loads(run(page, "esia_list.html", "M.parseEsiaList(document)"))
    assert len(r["rows"]) == 2
    latest = r["latest"]                                    # последняя попытка — по дате создания
    assert latest["state"] == "approved" and latest["creation_date"] == "2026-08-01 18:55:32"
    assert latest["file_url"] == "https://identity.reg.ru/esia/0123456789abcdef0123456789abcdef.json"
    assert latest["action"] == "fill_base_contacts" and "file" not in latest
    old = next(x for x in r["rows"] if x["state"] == "rejected")
    assert old["file_url"] == "" and old["reason"] == "passport_not_verified"


def test_user_details(page):
    r = json.loads(run(page, "user_details_pp.html", "M.parseUserDetails(document)"))
    assert r["user_id"] == "1000" and r["login"] == "person@example.com"
    assert "Идентификация через Госуслуги пройдена" in r["statuses"]
    assert r["servicing_org"] == 'ООО "РДХ"' and r["servicing_org_id"] == "3"
    assert r["contypes"] == "RUSURF: ru_pp" and r["is_entrepreneur"] is False
    ba = r["ba"]                                             # базовая анкета — скрытый блок страницы
    assert ba["ru_pp.person_r_surname"] == "Тестов" and ba["ru_pp.passport_number"] == "4509123456"
    assert ba["ru_pp.passport_place"] == "ГУ МВД РОССИИ ПО Г. МОСКВЕ" and ba["ru_pp.fax"] == ""
    assert ba["ru_pp.phone"] == "+79990000000" and ba["ru_pp.birth_date"] == "15.03.1990"
    assert r["links"]["runic"].endswith("/user/1000/runic_details")
    assert r["links"]["esia"].endswith("esia_identifications?user_id=1000")


def test_runic_parse(page):
    r = json.loads(run(page, "runic_pp.html", "M.parseRunic(document)"))
    assert r["user_id"] == "1000" and r["type"] == "pp" and r["has_pp_form"]
    v = r["values"]
    assert v["person_r_surname"] == "Тестов" and v["person"] == "Petr Sergeevich Testov"
    assert v["passport_place"] == "ГУ МВД РОССИИ ПО Г. МОСКВЕ" and v["country"] == "RU" and v["p_addr_zip"] == "101000"
    # страница базовой анкеты — «новая» (Bootstrap): без #content, но это страница manager
    assert page.evaluate("() => window.__doctoolManager.isManagerPage(document)")


NEW_OWNER = {"person_r_surname": "Сидорова", "person_r_name": "Анна", "person_r_patronimic": "Викторовна",
             "passport_number": "4012654321", "passport_date": "03.12.2010", "passport_place": "ТП № 5 ОУФМС России",
             "birth_date": "21.11.1990", "p_addr_city": "Санкт-Петербург", "p_addr_addr": "г. Санкт-Петербург, ул. Новая, д. 7",
             "p_addr_recipient": "Сидорова Анна Викторовна", "phone": "+79215551234", "e_mail": "anna@example.com",
             "country": "KZ", "sms_security_number": "+70000000000", "p_addr_area": "Ленинградская"}


def test_fill_runic(page):
    page.goto((FIX / "runic_pp.html").as_uri())
    page.add_script_tag(content=JS)
    r = page.evaluate("f => window.__doctoolManager.fillRunic('1000', f)", NEW_OWNER)
    assert r["ok"], r
    names = {c["name"] for c in r["data"]["changed"]}
    # гражданство, SMS-безопасность, область не заполняются, даже если их передать
    assert names == {"person_r_surname", "person_r_name", "person_r_patronimic", "passport_number", "passport_date",
                     "passport_place", "birth_date", "p_addr_city", "p_addr_addr", "p_addr_recipient", "phone", "e_mail"}
    val = lambda n: page.evaluate(f"() => document.querySelector('#ru_pp_contacts [name={n}]').value")  # noqa: E731
    assert val("person_r_surname") == "Сидорова" and val("passport_place") == "ТП № 5 ОУФМС России"
    assert val("country") == "RU" and val("sms_security_number") == "" and val("p_addr_area") == "Москва"
    assert val("p_addr_zip") == "101000"                    # индекс не передан — остался прежний
    # форма юрлица с теми же именами полей не тронута
    assert page.evaluate("() => document.querySelector('#ru_org_contacts [name=phone]').value") == "+79990000000"
    # события страницы: поле помечено изменённым, English name пересчитан по blur
    assert "unsaved" in page.evaluate("() => document.querySelector('#ru_pp_contacts [name=phone]').className")
    assert val("person") == "Anna Viktorovna Sidorova"
    old = {c["name"]: c["old"] for c in r["data"]["changed"]}
    assert old["person_r_surname"] == "Тестов" and old["p_addr_addr"] == "Тестовая, д 1, кв 1"
    # «Вернуть как было»
    back = page.evaluate("f => window.__doctoolManager.fillRunic('1000', f, {restore: true})", old)
    assert back["ok"] and val("person_r_surname") == "Тестов" and val("phone") == "+79990000000"


def test_fill_runic_guards(page):
    page.goto((FIX / "runic_pp.html").as_uri())
    page.add_script_tag(content=JS)
    r = page.evaluate("f => window.__doctoolManager.fillRunic('2000', f)", NEW_OWNER)
    assert not r["ok"] and r["error"]["code"] == "wrong_account"
    page.evaluate("() => { document.querySelector('input[name=type][value=pp]').checked = false;"
                  " document.querySelector('input[name=type][value=org]').checked = true; }")
    r = page.evaluate("f => window.__doctoolManager.fillRunic('1000', f)", NEW_OWNER)
    assert not r["ok"] and r["error"]["code"] == "not_person" and "юрлиц" in r["error"]["message"]
    assert page.evaluate("() => document.querySelector('#ru_pp_contacts [name=person_r_surname]').value") == "Тестов"


# ------------------------------------------------------------------ 0.7.2: ЕСИА у юрлиц

def test_sd_org_legal_address(page):
    # юридический адрес юрлица — для сверки с ЕСИА; почтовый адрес (p_addr_*) и квартира/офис не передаются
    org = json.loads(run(page, "sd_org.html", "M.parseSd(document)"))
    f = org["fields"]
    assert f["address_r_street"] == "ул. Тестовая" and f["address_r_house"] == "1" and f["address_r_zip"] == "140000"
    assert f["address_r_frame"] == "" and f["address_r_building"] == ""
    assert "address_r_flat" not in f and "p_addr_street" not in f and "phone" not in f


def test_esia_fields_org_and_person(page):
    # esiaFields() из background.js: что из файла ЕСИА уходит в программу
    import re
    src = re.search(r"function esiaFields\(j\) \{.*?\n\}\n", BG, re.S).group(0)
    page.goto("about:blank")
    org_json = json.loads((FIX / "esia_org.json").read_text(encoding="utf-8"))
    org = page.evaluate(f"j => {{ {src}; return esiaFields(j); }}", org_json)
    assert org["kind"] == "org" and org["inn"] == "7701234567" and org["kpp"] == "770101001"
    assert org["full_name"].startswith("ОБЩЕСТВО С ОГРАНИЧЕННОЙ") and org["ogrn"] == "1000000000001"
    assert org["is_liquidated"] is False and org["trusted"] is True
    la = org["legal_address"]
    assert la["house"] == "Д. 1" and la["street"] == "УЛ. ТЕСТОВАЯ" and la["zip_code"] == "140000"
    text = json.dumps(org, ensure_ascii=False)
    for secret in ("prs_auth", "Тестов", "+7(999)", "org@example.com", "mail_address", "oid"):
        assert secret not in text, secret
    pp = page.evaluate(f"j => {{ {src}; return esiaFields(j); }}",
                       json.loads((FIX / "esia_pp.json").read_text(encoding="utf-8")))
    assert pp["last_name"] == "Тестов" and "inn" not in pp and "snils" not in pp and "kind" not in pp
