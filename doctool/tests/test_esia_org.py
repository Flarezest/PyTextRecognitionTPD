"""0.7.2: ЕСИА у доменов юрлиц (Sd ru_org ↔ файл ЕСИА организации), provider отдельной колонкой в выгрузке
по доменам, без поля «Название дела». Без сети и без браузера."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doctool import domain_report, domains, fieldnorm  # noqa: E402

# Ответ расширения для домена юрлица: поля Sd — как parseSd на обезличенной странице sd_org.html (+ КПП),
# данные ЕСИА — как esiaFields() из background.js по tests/fixtures/manager/esia_org.json
SD_ORG = {"group": "ru_org", "trustee": "Нет", "status": "Активна (A)", "fields": {
    "code": "7701234567", "kpp": "770101001", "country": "RU", "org": "Test Org LLC",
    "org_r": 'Общество с ограниченной ответственностью "Тест"', "address_r_street": "ул. Тестовая",
    "address_r_house": "1", "address_r_frame": "", "address_r_building": "", "address_r_zip": "140000",
    "e_mail": "org@example.com"}}
S_ORG = {"provider": "cctldru", "dname": "testdomain-org.ru", "contype": "ru_org", "state": "A", "user_id": "2000"}
ESIA_ORG = {"status": "success", "kind": "org", "full_name": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТЕСТ"',
            "inn": "7701234567", "kpp": "770101001", "ogrn": "1000000000001", "is_liquidated": False, "trusted": True,
            "legal_address": {"zip_code": "140000", "region": "МОСКОВСКАЯ ОБЛАСТЬ", "area": "Р-Н ТЕСТОВЫЙ",
                              "street": "УЛ. ТЕСТОВАЯ", "house": "Д. 1",
                              "address_str": "МОСКОВСКАЯ ОБЛАСТЬ, Р-Н ТЕСТОВЫЙ, УЛ. ТЕСТОВАЯ"}}
ESIA_PAGE = {"url": "https://manager.reg.ru/manager/esia_identifications?person_id=3000", "count": 1, "state": "approved",
             "file_url": "https://identity.reg.ru/esia/fedcba9876543210fedcba9876543210.json",
             "latest": {"state": "approved", "creation_date": "2026-10-06 14:33:59", "processed_date": "2026-10-06 14:34:04",
                        "action": "identification"}}


def org_domain(esia=None, sd_fields=None, domain="testdomain-org.ru", provider="cctldru"):
    sd = {**SD_ORG, "fields": {**SD_ORG["fields"], **(sd_fields or {})}}
    s = {**S_ORG, "dname": domain, "provider": provider}
    r = {"ok": True, "data": {"domain": domain, "service_id": "2", "account": "2000", "sd": sd, "s": s, "esia": esia,
                              "urls": {"sd": "https://manager.reg.ru/tech/srv_details?service_id=2",
                                       "s": "https://manager.reg.ru/tech/service_details?service_id=2"}}}
    return domains.from_extension(domain, r)


def with_data(**changes):
    data = json.loads(json.dumps(ESIA_ORG))
    for k, v in changes.items():
        if k.startswith("la_"):
            data["legal_address"][k[3:]] = v
        else:
            data[k] = v
    return {**ESIA_PAGE, "data": data}


# ------------------------------------------------------------------ сравнение значений

def test_org_name_legal_forms():
    same = lambda a, b: fieldnorm.same("org", a, b)[0]  # noqa: E731
    full = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "АЛЬСТРОМ ТВЕРЬ"'
    assert same("ООО «Альстром Тверь»", full) == "ok"                 # ООО = полностью, «» = ""
    assert same("ООО Альстром-Тверь", full) == "ok"                   # без кавычек, дефис
    assert same('АО "Ромашка"', 'НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОМАШКА"') == "ok"
    assert same('НПАО "Ромашка"', 'АКЦИОНЕРНОЕ ОБЩЕСТВО "РОМАШКА"') == "ok"
    assert same('ОАО "Ромашка"', 'ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОМАШКА"') == "ok"
    assert same('ПК "Заря"', 'ПРОИЗВОДСТВЕННЫЙ КООПЕРАТИВ "ЗАРЯ"') == "ok"
    assert same('Хозяйственное товарищество "Заря"', 'ПОЛНОЕ ТОВАРИЩЕСТВО "ЗАРЯ"') == "ok"
    assert same('АО "ТВ Центр"', 'АКЦИОНЕРНОЕ ОБЩЕСТВО "ТВ ЦЕНТР"') == "ok"   # «ТВ» в кавычках — часть названия
    st, why = fieldnorm.same("org", "ООО «Альстром»", full)
    assert st == "fail" and "название" in why
    st, why = fieldnorm.same("org", 'ООО "Ромашка"', 'АКЦИОНЕРНОЕ ОБЩЕСТВО "РОМАШКА"')
    assert st == "fail" and "форма" in why
    assert same('ОАО "Ромашка"', 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОМАШКА"') == "fail"
    assert same("Альстром Тверь", full) == "warn"                     # форма указана только с одной стороны
    assert same("", full) == ""


def test_house_and_street():
    house = lambda a, b: fieldnorm.same("house", a, b)[0]  # noqa: E731
    street = lambda a, b: fieldnorm.same("street", a, b)[0]  # noqa: E731
    assert house("11", "Д. 11") == "ok" and house("дом 11", "д.11") == "ok" and house("11А", "Д. 11 А") == "ok"
    assert house("11, корп. 2", "Д. 11 КОРП. 2") == "ok"
    assert house("11", "Д. 11 КОРП. 2") == "warn" and house("12", "Д. 11") == "fail"
    assert street("ул. Промышленная", "УЛ. ПРОМЫШЛЕННАЯ") == "ok"
    assert street("Промышленная улица", "УЛ. ПРОМЫШЛЕННАЯ") == "ok" and street("Промышленная", "УЛ. ПРОМЫШЛЕННАЯ") == "ok"
    assert street("пр-кт Ленинский", "ЛЕНИНСКИЙ ПРОСПЕКТ") == "ok"
    assert street("пер. Ленинский", "УЛ. ЛЕНИНСКИЙ") == "warn"
    assert street("ул. Тестовая", "УЛ. САДОВАЯ") == "fail"
    assert fieldnorm.same("digits", "171261", "171260")[0] == "fail"


# ------------------------------------------------------------------ сверка Sd ↔ ЕСИА у домена юрлица

def test_esia_org_match():
    info = org_domain({**ESIA_PAGE, "data": ESIA_ORG})
    d = info.to_dict()
    assert info.kind == "org" and d["esia_status"] == "match" and d["esia_state"] == "approved"
    assert d["esia_ru"] == "approved · Sd = ЕСИА (6 из 6)"
    rows = {r["field"]: r for r in d["esia_rows"]}
    assert [r["field"] for r in d["esia_rows"]] == ["code", "kpp", "org_r", "address_r_street", "address_r_house",
                                                    "address_r_zip"]
    assert rows["code"]["esia_field"] == "inn" and rows["org_r"]["esia_field"] == "full_name"
    assert rows["address_r_house"]["sd"] == "1" and rows["address_r_house"]["esia"] == "Д. 1"
    assert all(r["status"] == "ok" for r in d["esia_rows"])
    assert info.verdict == "" and domains.problem(info) == "unchecked"    # на вердикт не влияет


def test_esia_org_mismatch_and_partial():
    d = org_domain(with_data(la_zip_code="140001")).to_dict()            # индекс другой → красный
    assert d["esia_status"] == "mismatch" and d["esia_ru"].endswith("Sd ≠ ЕСИА: Индекс (юр. адрес)")
    d = org_domain(with_data(full_name='АКЦИОНЕРНОЕ ОБЩЕСТВО "ТЕСТ"')).to_dict()
    row = next(r for r in d["esia_rows"] if r["field"] == "org_r")
    assert row["status"] == "fail" and "форма" in row["detail"]
    d = org_domain(with_data(la_house="Д. 1 КОРП. 2")).to_dict()          # совпал только номер дома → жёлтый
    assert d["esia_status"] == "warn" and "Дом (юр. адрес)" in d["esia_ru"]
    d = org_domain(with_data(la_house="Д. 1 КОРП. 2"), {"address_r_frame": "2"}).to_dict()   # корпус в Sd отдельно
    row = next(r for r in d["esia_rows"] if r["field"] == "address_r_house")
    assert row["status"] == "ok" and row["sd"] == "1, корп. 2"
    d = org_domain({**ESIA_PAGE, "data": ESIA_ORG}, {"kpp": ""}).to_dict()   # КПП в Sd пустой — без цвета
    row = next(r for r in d["esia_rows"] if r["field"] == "kpp")
    assert row["status"] == "" and row["detail"] == "нет в Sd" and d["esia_status"] == "match"
    d = org_domain(with_data(is_liquidated=True)).to_dict()
    assert d["esia_status"] == "mismatch" and "Организация действует" in d["esia_ru"]
    # файл ЕСИА физлица у домена юрлица: сверять нечего
    d = org_domain({**ESIA_PAGE, "data": {"status": "success", "last_name": "Тестов"}}).to_dict()
    assert d["esia_status"] == "no_data"


def test_user_sample_shape():
    """Форма файла ЕСИА организации из manager (поля как в присланном пользователем файле, значения обезличены):
    дом «Д. 11» = «11», улица «УЛ. …» = «ул. …», «ООО «…»» = «ОБЩЕСТВО С …», индекс 171261 ≠ 171260."""
    info = org_domain(with_data(full_name='ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ПРИМЕР ТВЕРЬ"', la_house="Д. 11",
                                la_street="УЛ. ПРОМЫШЛЕННАЯ", la_zip_code="171260"),
                      {"org_r": "ООО «Пример Тверь»", "address_r_house": "11", "address_r_street": "ул. Промышленная",
                       "address_r_zip": "171261"})
    st = {r["field"]: r["status"] for r in info.to_dict()["esia_rows"]}
    assert st == {"code": "ok", "kpp": "ok", "org_r": "ok", "address_r_street": "ok", "address_r_house": "ok",
                  "address_r_zip": "fail"}


# ------------------------------------------------------------------ выгрузка по доменам и веб-интерфейс

def test_domain_report_provider_column_sort_and_org_esia():
    a = org_domain({**ESIA_PAGE, "data": ESIA_ORG}).to_dict()
    b = org_domain(with_data(la_zip_code="140001"), domain="second-org.ru", provider="RU-CENTER-RU").to_dict()
    c = org_domain(None, domain="third-org.ru", provider="").to_dict()            # нет ссылки ЕСИА на Sd
    html = domain_report.build([a, b, c])
    heads = re.findall(r'<th title="Сортировать">([^<]+)<span class="arr">', html)
    assert heads == ["Домен", "manager", "Администратор (Sd)", "Сверка", "ЕСИА", "Provider", "Прочее"]
    assert "<td>RU-CENTER-RU</td>" in html and "<td>cctldru</td>" in html and "provider:" not in html
    assert 'colspan="7"' in html and 'id="bygroups"' in html and "localeCompare" in html
    # кнопка ESIA — у всех найденных доменов юрлиц, и когда сверки нет (окно объяснит почему)
    for dom in ("testdomain-org.ru", "second-org.ru", "third-org.ru"):
        assert f'class="esia" data-d="{dom}"' in html
    data = json.loads(html.split('id="esia-data">')[1].split("</script>")[0])
    assert data["testdomain-org.ru"]["group"] == "ru_org" and data["testdomain-org.ru"]["ogrn"] == "1000000000001"
    assert data["third-org.ru"]["status"] == "no_link"
    assert "Идентификация через Госуслуги (ЕСИА) по Sd доменов (физлица и юрлица)" in html
    # фильтр находит и по provider
    assert any("RU-CENTER-RU" in q for q in re.findall(r'data-q="([^"]*)"', html))


def test_web_has_no_case_name_field():
    page = (ROOT / "doctool" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="caseId"' not in page and "Название дела" not in page and "case_id:" not in page
    assert "d.esia_status ||" in page                                   # кнопка ESIA и у доменов без файла ЕСИА
