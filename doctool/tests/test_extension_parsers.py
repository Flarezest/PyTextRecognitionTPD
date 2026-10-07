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
