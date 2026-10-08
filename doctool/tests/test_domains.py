"""Данные доменов из manager (Sd, S), сверка с документами, WHOIS — без сети и без браузера."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import domains, whois  # noqa: E402
from doctool.models import Extraction  # noqa: E402

# Ответы расширения — как content/manager.js возвращает их на обезличенных страницах manager
SD_PP = {"group": "ru_pp", "trustee": "Нет", "status": "Активна (A)", "fields": {
    "birth_date": "15.03.1990", "code": "500100732259", "country": "RU", "last_admin_change": "2025-11-20 10:00:00",
    "passport_date": "20.04.2010", "passport_number_short": "123456", "passport_place": "ГУ МВД РОССИИ ПО Г. МОСКВЕ",
    "passport_place_id": "770-001", "passport_series": "4509", "person_r_name": "Пётр",
    "person_r_patronimic": "Сергеевич", "person_r_surname": "Тестов", "is_entrepreneur": "0"}}
S_PP = {"provider": "cctldru", "dname": "testdomain-pp.ru", "contype": "ru_pp", "state": "A", "user_id": "1000"}
SD_ORG = {"group": "ru_org", "trustee": "Нет", "status": "Активна (A)", "fields": {
    "code": "7701234567", "country": "RU", "org": "Test Org LLC", "org_r": "ООО \"Тест\""}}
S_ORG = {"provider": "cctldru", "dname": "testdomain-org.ru", "contype": "ru_org", "state": "A", "user_id": "2000"}


def reply(sd, s, domain, sid="1"):
    return {"ok": True, "data": {"domain": domain, "service_id": sid, "account": s["user_id"], "sd": sd, "s": s,
                                 "urls": {"sd": f"https://manager.reg.ru/tech/srv_details?service_id={sid}"}}}


def passport(surname="ТЕСТОВ", name="ПЁТР", patr="СЕРГЕЕВИЧ", birth="15.03.1990", sn="4509 123456",
             issue="20.04.2010", by="ГУ МВД России по г. Москве"):
    pas = Extraction("passport_rf", "p.pdf")
    for k, v in (("surname", surname), ("given_name", name), ("patronymic", patr), ("birth_date", birth),
                 ("series_number", sn), ("issue_date", issue), ("issued_by", by), ("department_code", "770-001")):
        pas.set(k, v, 0.99, "mrz")
    return pas


def checks_for(items, pas=None, app=None):
    ref = domains.reference(app or Extraction("none", ""), pas or passport())
    ch, flags = domains.domain_checks(items, ref)
    return {c.name: c for c in ch}, flags


def test_split_domains():
    assert domains.split_domains("Example.RU, пример.рф;\nhttps://a.com/x  example.ru") == ["example.ru", "пример.рф", "a.com"]


def test_from_extension_person_and_org():
    p = domains.from_extension("testdomain-pp.ru", reply(SD_PP, S_PP, "testdomain-pp.ru"))
    assert p.status == "found" and p.kind == "person" and p.provider == "cctldru" and p.account == "1000"
    assert p.fio == "Тестов Пётр Сергеевич" and p.passport == "4509 123456"
    assert p.to_dict()["last_admin_change"] == "20.11.2025"
    o = domains.from_extension("testdomain-org.ru", reply(SD_ORG, S_ORG, "testdomain-org.ru"))
    assert o.kind == "org" and o.holder == "ООО \"Тест\"" and o.sd["code"] == "7701234567"
    nf = domains.from_extension("x.ru", {"ok": False, "error": {"code": "not_found", "message": "нет"}})
    assert nf.status == "not_found"
    er = domains.from_extension("x.ru", {"ok": False, "error": {"code": "not_logged_in", "message": "вход"}})
    assert er.status == "error" and er.error_code == "not_logged_in"


def test_person_domain_matches():
    p = domains.from_extension("testdomain-pp.ru", reply(SD_PP, S_PP, "testdomain-pp.ru"))
    ch, flags = checks_for([p])
    assert ch[domains.CHECK_FOUND].status == "ok"
    assert ch[domains.CHECK_ADMIN].status == "ok"
    assert ch[domains.CHECK_PASSPORT].status == "ok"
    assert p.verdict == "ok" and not flags


def test_person_domain_other_person_is_mismatch():
    p = domains.from_extension("testdomain-pp.ru", reply(SD_PP, S_PP, "testdomain-pp.ru"))
    ch, flags = checks_for([p], pas=passport(surname="ИВАНОВ", name="ИВАН", patr="ИВАНОВИЧ", birth="01.01.1980",
                                             sn="4510 654321"))
    assert ch[domains.CHECK_ADMIN].status == "fail" and "admin_mismatch" in flags


def test_person_domain_with_replaced_passport_is_info():
    p = domains.from_extension("testdomain-pp.ru", reply(SD_PP, S_PP, "testdomain-pp.ru"))
    ch, flags = checks_for([p], pas=passport(sn="4515 111222", issue="20.03.2015"))
    assert ch[domains.CHECK_ADMIN].status == "ok"
    assert ch[domains.CHECK_PASSPORT].status == "info" and not flags


def test_org_domain_needs_review_and_different_admins_warn():
    p = domains.from_extension("testdomain-pp.ru", reply(SD_PP, S_PP, "testdomain-pp.ru"))
    o = domains.from_extension("testdomain-org.ru", reply(SD_ORG, S_ORG, "testdomain-org.ru", sid="2"))
    ch, flags = checks_for([p, o])
    assert ch[domains.CHECK_ADMIN].status == "review"
    assert ch[domains.CHECK_SAME].status == "warn"


def test_not_found_domain_fails_with_whois():
    nf = domains.from_extension("free-domain.ru", {"ok": False, "error": {"code": "not_found", "message": "нет"}})
    nf.whois = whois.parse("free-domain.ru", "No entries found for the selected source(s).").to_dict()
    ch, _ = checks_for([nf])
    assert ch[domains.CHECK_FOUND].status == "fail" and "WHOIS: свободен" in ch[domains.CHECK_FOUND].detail


def test_whois_parse_tcinet_and_verisign():
    tci = """% TCI Whois Service. Terms of use:
domain:        EXAMPLE.RU
nserver:       ns1.example.ru.
state:         REGISTERED, DELEGATED, VERIFIED
org:           EXAMPLE, LLC.
registrar:     RU-CENTER-RU
created:       1997-09-23T09:45:07Z
paid-till:     2026-09-30T21:00:00Z
free-date:     2026-11-01
source:        TCI
"""
    w = whois.parse("example.ru", tci, "whois.tcinet.ru")
    assert w.status == "registered" and w.registrar == "RU-CENTER-RU" and w.paid_till.startswith("2026-09-30")
    assert "регистратор RU-CENTER-RU" in w.summary
    assert whois.parse("x.ru", "No entries found for the selected source(s).\n").status == "free"
    vs = """   Domain Name: EXAMPLE.COM
   Registrar: RESERVED-Internet Assigned Numbers Authority
   Creation Date: 1995-08-14T04:00:00Z
   Registry Expiry Date: 2026-08-13T04:00:00Z
   Domain Status: clientDeleteProhibited https://icann.org/epp#clientDeleteProhibited
   Domain Status: clientTransferProhibited https://icann.org/epp#clientTransferProhibited
"""
    w = whois.parse("example.com", vs)
    assert w.status == "registered" and w.state == "clientDeleteProhibited, clientTransferProhibited"
    assert whois.parse("nope.com", 'No match for "NOPE.COM".\n').status == "free"


def test_lookup_domains_stops_after_login_error():
    calls = []

    def request(cmd, params, timeout, on_progress):
        calls.append(params["domain"])
        on_progress({"step": "bills", "message": "поиск"})
        if params["domain"] == "testdomain-pp.ru":
            return reply(SD_PP, S_PP, "testdomain-pp.ru")
        if params["domain"] == "free.ru":
            return {"ok": False, "error": {"code": "not_found", "message": "Домен не найден"}}
        return {"ok": False, "error": {"code": "not_logged_in", "message": "manager просит вход"}}

    log = []
    items = domains.lookup_domains(request, ["testdomain-pp.ru", "free.ru", "login.ru", "later.ru"], log=log.append,
                                   whois_lookup=lambda d: whois.WhoisInfo(domain=d, status="free"),
                                   cancel=threading.Event())
    assert [i.status for i in items] == ["found", "not_found", "error", "error"]
    assert items[1].whois["status"] == "free"
    assert items[3].error_code == "skipped" and "later.ru" not in calls
    assert any("поиск" in x for x in log)


def test_case_with_domains_changes_verdict(tmp_path):
    from doctool.service import CaseInput, CaseResult, _evaluate, build_record
    from doctool.verdict import load_case_types
    res = CaseResult(inp=CaseInput(case_type="passport_only", check_date="05.10.2026"),
                     case_type=load_case_types()["passport_only"], case_id="t", case_dir=tmp_path)
    res.app, res.pas = Extraction("none", ""), passport()
    res.domains_sd = [domains.from_extension("testdomain-pp.ru", reply(SD_PP, S_PP, "testdomain-pp.ru"))]
    _evaluate(res)
    names = {c.name: c.status for c in res.checks}
    assert names[domains.CHECK_ADMIN] == "ok"
    assert build_record(res)["manager_domains"][0]["provider"] == "cctldru"
    res.pas = passport(surname="ИВАНОВ", name="ИВАН", patr="ИВАНОВИЧ", birth="01.01.1980", sn="4510 654321")
    _evaluate(res)
    assert res.decision.code == "reject"


# ------------------------------------------------------------------ порядок вывода: по «проблемности»

def _sample_items():
    """Домены в порядке заявления: в порядке, свободный, занят у другого, другой администратор, ошибка, юрлицо."""
    other = {**SD_PP, "fields": {**SD_PP["fields"], "person_r_surname": "Иванов", "person_r_name": "Иван",
                                 "person_r_patronimic": "Иванович", "birth_date": "01.01.1980",
                                 "passport_series": "4510", "passport_number_short": "654321"}}
    ok = domains.from_extension("ok.ru", reply(SD_PP, S_PP, "ok.ru"))
    free = domains.from_extension("free.ru", {"ok": False, "error": {"code": "not_found", "message": "нет"}})
    free.whois = {"status": "free"}
    busy = domains.from_extension("busy.ru", {"ok": False, "error": {"code": "not_found", "message": "нет"}})
    busy.whois = {"status": "registered", "registrar": "X-REG"}
    bad = domains.from_extension("other.ru", reply(other, S_PP, "other.ru"))
    err = domains.DomainInfo(domain="err.ru", status="error", error_code="timeout", error="таймаут")
    org = domains.from_extension("org.ru", reply(SD_ORG, S_ORG, "org.ru"))
    free2 = domains.from_extension("free2.ru", {"ok": False, "error": {"code": "not_found", "message": "нет"}})
    free2.whois = {"status": "free"}
    return [ok, free, busy, bad, err, org, free2]


def test_problem_codes_and_sorting():
    items = _sample_items()
    # до сверки с документами у найденных доменов нет вердикта — «сверка не выполнялась»
    assert [domains.problem(i) for i in items] == ["unchecked", "free", "not_found", "unchecked", "error",
                                                    "unchecked", "free"]
    checks_for(items)   # сверка с паспортом заявителя заполняет verdict
    assert [domains.problem(i) for i in items] == ["ok", "free", "not_found", "mismatch", "error", "review", "free"]
    order = [i.domain for i in domains.sort_by_problem(items)]
    # сначала свободные (в порядке заявления), затем «данные не сходятся», затем остальные
    assert order == ["free.ru", "free2.ru", "other.ru", "busy.ru", "err.ru", "org.ru", "ok.ru"]
    assert [i.domain for i in items][0] == "ok.ru"          # исходный список не меняется
    d = items[1].to_dict()
    assert d["problem"] == "free" and d["problem_rank"] == 0 and "Свободен" in d["problem_ru"]
    # словари (to_dict) сортируются так же
    assert [x["domain"] for x in domains.sort_by_problem([i.to_dict() for i in items])] == order


def test_missing_domains_detail_lists_free_first():
    items = _sample_items()
    by, _ = checks_for(items)
    det = by[domains.CHECK_FOUND].detail
    assert det.index("free.ru") < det.index("free2.ru") < det.index("busy.ru")


def test_report_domains_table_grouped():
    from doctool.report import domains_table
    items = _sample_items()
    checks_for(items)
    html_ = domains_table([i.to_dict() for i in items])
    heads = [domains.PROBLEM_RU[c] for c in ("free", "mismatch", "not_found", "error", "review", "ok")]
    pos = [html_.index(h) for h in heads]
    assert pos == sorted(pos)
    assert html_.index("free2.ru") < html_.index("other.ru") < html_.index("busy.ru") < html_.index("ok.ru")
    assert f"{domains.PROBLEM_RU['free']} — 2" in html_


def test_whois_unknown_zone_is_not_free(monkeypatch):
    # «chi.pd» (ошибка распознавания «.рф»): IANA такой зоны не знает — это не «свободен», а ошибка
    monkeypatch.setattr(whois, "_query", lambda server, q, t: "% This query returned 0 objects." if server == "whois.iana.org"
                        else "")
    w = whois.lookup("chi.pd")
    assert w.status == "error" and "зоны .pd не существует" in w.error


def test_email_and_international_holder():
    info = domains.DomainInfo("example.com", status=domains.FOUND, group="o_deti_org",
                              sd={"o_company": "Test Holding LP", "o_email": "Owner@example.com\nsecond@example.org"})
    d = info.to_dict()
    assert d["holder"] == "Test Holding LP" and d["emails"] == ["owner@example.com", "second@example.org"]
    ru = domains.DomainInfo("example.ru", status=domains.FOUND, kind="person",
                            sd={"person_r_surname": "Тестов", "e_mail": "a@example.ru, b@example.ru"})
    assert ru.emails == ["a@example.ru", "b@example.ru"]


# ------------------------------------------------------------------ 0.6.0: идентификация через Госуслуги (ЕСИА)

ESIA_JSON = {"status": "success", "first_name": "Пётр", "last_name": "Тестов", "middle_name": "Сергеевич",
             "birth_date": "15.03.1990", "citizenship": "RUS", "trusted": True,
             "rf_passport": {"series": "4509", "number": "123456", "issue_date": "20.04.2010",
                             "issued_by": "ГУ МВД России по г. Москве", "vrf_stu": "VERIFIED"}}
ESIA_PAGE = {"url": "https://manager.reg.ru/manager/esia_identifications?user_id=1000", "count": 1, "state": "approved",
             "file_url": "https://identity.reg.ru/esia/0123456789abcdef0123456789abcdef.json",
             "latest": {"state": "approved", "creation_date": "2026-08-01 18:55:32", "processed_date": "2026-08-01 18:55:38",
                        "action": "fill_base_contacts"}}


def with_esia(esia, sd=SD_PP, s=S_PP, domain="testdomain-pp.ru"):
    r = reply(sd, s, domain)
    r["data"]["esia"] = esia
    return domains.from_extension(domain, r)


def test_esia_match():
    info = with_esia({**ESIA_PAGE, "data": ESIA_JSON})
    d = info.to_dict()
    assert d["esia_status"] == "match" and d["esia_state"] == "approved"
    assert d["esia_ru"] == "approved · Sd = ЕСИА (9 из 9)"
    rows = {r["field"]: r for r in d["esia_rows"]}
    # фамилия — last_name, отчество — middle_name; гражданство RU ↔ RUS
    assert rows["person_r_surname"]["esia_field"] == "last_name" and rows["person_r_surname"]["esia"] == "Тестов"
    assert rows["person_r_patronimic"]["esia_field"] == "middle_name" and rows["person_r_patronimic"]["status"] == "ok"
    assert rows["country"]["status"] == "ok" and rows["passport_place"]["status"] == "ok"
    # ЕСИА на сверку с документами и вердикт не влияет
    assert info.verdict == "" and domains.problem(info) == "unchecked"
    assert domains.DomainInfo.from_dict(d).esia["state"] == "approved"


def test_esia_mismatch_and_states():
    bad = with_esia({**ESIA_PAGE, "data": {**ESIA_JSON, "rf_passport": {**ESIA_JSON["rf_passport"], "issue_date": "21.04.2010"}}})
    d = bad.to_dict()
    assert d["esia_status"] == "mismatch" and "Дата выдачи" in d["esia_ru"]
    assert [r["status"] for r in d["esia_rows"] if r["field"] == "passport_date"] == ["fail"]
    # нет записей об идентификации / нет файла / ошибка / нет ссылки
    assert with_esia({"url": ESIA_PAGE["url"], "count": 0, "state": ""}).to_dict()["esia_ru"] == "не проходил"
    nd = with_esia({**ESIA_PAGE, "json_error": "HTTP 404"}).to_dict()
    assert nd["esia_status"] == "error" and "HTTP 404" in nd["esia_ru"]
    assert with_esia({**ESIA_PAGE, "file_url": ""}).to_dict()["esia_status"] == "no_data"
    assert with_esia(None).to_dict()["esia_status"] == "no_link"
    assert with_esia({**ESIA_PAGE, "data": {"status": "error"}}).to_dict()["esia_status"] == "error"
    assert with_esia({**ESIA_PAGE, "data": {"status": "success", "rf_passport": None}}).to_dict()["esia_status"] == "no_data"
    # юрлица — без ЕСИА
    org = domains.from_extension("testdomain-org.ru", reply(SD_ORG, S_ORG, "testdomain-org.ru"))
    assert org.to_dict()["esia_status"] == ""
    # значения нет с одной стороны — не красный, а «нет в ЕСИА»
    part = with_esia({**ESIA_PAGE, "data": {**ESIA_JSON, "middle_name": ""}}).to_dict()
    row = next(r for r in part["esia_rows"] if r["field"] == "person_r_patronimic")
    assert row["status"] == "" and row["detail"] == "нет в ЕСИА" and part["esia_status"] == "match"


def test_esia_in_reports():
    from doctool import domain_report, report
    good = with_esia({**ESIA_PAGE, "data": ESIA_JSON}).to_dict()
    bad = with_esia({**ESIA_PAGE, "data": {**ESIA_JSON, "birth_date": "16.03.1990"}}, domain="second-pp.ru",
                    s={**S_PP, "dname": "second-pp.ru"}).to_dict()
    html = domain_report.build([good, bad])
    assert '<th>ЕСИА</th>' in html and 'class="esia" data-d="testdomain-pp.ru"' in html
    assert 'id="esia-data"' in html and '"last_name"' in html and "VERIFIED" in html
    assert "Идентификация через Госуслуги (ЕСИА)" in html and "second-pp.ru" in html.split("К сведению")[1]
    assert "</script" not in html.split('id="esia-data">')[1].split("</script>")[0]
    assert "ЕСИА: approved" in report.domains_table([good])
