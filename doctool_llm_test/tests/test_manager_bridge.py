"""Связь с расширением Chrome по WebSocket — с поддельным «расширением» (без браузера)."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from doctool.web import create_app  # noqa: E402
from test_domains import S_PP, SD_PP, reply  # noqa: E402

ORIGIN = {"origin": "chrome-extension://abcdefghijklmnop"}


def test_extension_roundtrip(tmp_path):
    app = create_app(out_root=str(tmp_path))
    app.state.manager.use_whois = False
    client = TestClient(app)
    assert client.get("/api/manager/status").json()["connected"] is False
    r = client.post("/api/manager/lookup", json={"domains": "testdomain-pp.ru"})
    assert r.status_code == 409                       # расширение не подключено
    with client.websocket_connect("/ext", headers=ORIGIN) as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "hello", "version": "0.3.0"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        st = client.get("/api/manager/status").json()
        assert st["connected"] and st["version"] == "0.3.0"

        out, log = [], []
        th = threading.Thread(target=lambda: out.extend(app.state.manager.lookup(["testdomain-pp.ru"], log=log.append)))
        th.start()
        req = ws.receive_json()
        assert req["type"] == "request" and req["cmd"] == "lookup"
        task = req["params"].pop("task")                 # номер загрузки (кэш ЕСИА в расширении)
        assert req["params"] == {"domain": "testdomain-pp.ru", "ascii": "testdomain-pp.ru"} and task
        ws.send_json({"type": "progress", "id": req["id"], "step": "sd", "message": "Sd"})
        ws.send_json({"type": "result", "id": req["id"], **reply(SD_PP, S_PP, "testdomain-pp.ru")})
        th.join(10)
        assert out and out[0].status == "found" and out[0].fio == "Тестов Пётр Сергеевич"
        assert any("Sd" in x for x in log)


def test_extension_origin_is_checked(tmp_path):
    client = TestClient(create_app(out_root=str(tmp_path)))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ext", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_job_autoload_domains(tmp_path, monkeypatch):
    """Флажок «подгрузить данные Sd»: после проверки дела данные доменов приходят от расширения и меняют вердикт."""
    import time

    import doctool.jobs as jobs_mod
    from doctool.models import Extraction
    from doctool.service import CaseResult, _evaluate
    from doctool.verdict import load_case_types
    from test_domains import passport

    def fake_run_case(inp):
        res = CaseResult(inp=inp, case_type=load_case_types()["passport_only"], case_id="t", case_dir=tmp_path)
        res.app, res.pas = Extraction("none", ""), passport()
        res.app.set("domains.list", ["testdomain-pp.ru"], 1.0, "text")
        _evaluate(res)
        return res

    monkeypatch.setattr(jobs_mod, "run_case", fake_run_case)
    app = create_app(out_root=str(tmp_path))
    app.state.manager.use_whois = False
    client = TestClient(app)
    with client.websocket_connect("/ext", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "hello", "version": "0.3.0"})
        r = client.post("/api/jobs", data={"options": '{"case_type": "passport_only", "manager_autoload": true}'},
                        files={"passport": ("p.png", b"x", "image/png")})
        job_id = r.json()["job_id"]
        req = ws.receive_json()
        assert req["params"]["domain"] == "testdomain-pp.ru"
        ws.send_json({"type": "result", "id": req["id"], **reply(SD_PP, S_PP, "testdomain-pp.ru")})
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] != "running" and j["status"] != "queued":
                break
            time.sleep(0.05)
        assert j["status"] == "done", j
        res = j["result"]
        assert res["domains"][0]["holder"] == "Тестов Пётр Сергеевич"
        assert any(c["name"] == "Заявитель — администратор доменов (Sd)" and c["status"] == "ok" for c in res["checks"])
        assert res["record"]["manager_domains"][0]["provider"] == "cctldru"
        assert any("Загружаю данные доменов" in x for x in j["log"])


def test_manual_lookup_attaches_to_case(tmp_path, monkeypatch):
    """Кнопка «Загрузить данные из manager» после проверки: данные доменов добавляются к делу."""
    import time

    import doctool.jobs as jobs_mod
    from doctool.models import Extraction
    from doctool.service import CaseResult, _evaluate
    from doctool.verdict import load_case_types
    from test_domains import passport

    def fake_run_case(inp):
        res = CaseResult(inp=inp, case_type=load_case_types()["passport_only"], case_id="t", case_dir=tmp_path)
        res.app, res.pas = Extraction("none", ""), passport(surname="ИВАНОВ", name="ИВАН", patr="ИВАНОВИЧ",
                                                             birth="01.01.1980", sn="4510 654321")
        _evaluate(res)
        return res

    monkeypatch.setattr(jobs_mod, "run_case", fake_run_case)
    app = create_app(out_root=str(tmp_path))
    app.state.manager.use_whois = False
    client = TestClient(app)
    job_id = client.post("/api/jobs", data={"options": '{"case_type": "passport_only"}'},
                         files={"passport": ("p.png", b"x", "image/png")}).json()["job_id"]
    for _ in range(100):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "done":
            break
        time.sleep(0.05)
    with client.websocket_connect("/ext", headers=ORIGIN) as ws:
        ws.receive_json()
        t = client.post("/api/manager/lookup", json={"domains": "testdomain-pp.ru", "job_id": job_id}).json()
        req = ws.receive_json()
        ws.send_json({"type": "result", "id": req["id"], **reply(SD_PP, S_PP, "testdomain-pp.ru")})
        for _ in range(100):
            j = client.get(f"/api/manager/tasks/{t['task_id']}").json()
            if j["status"] != "running":
                break
            time.sleep(0.05)
    assert j["status"] == "done" and j["result"]["domains"][0]["verdict"] == "fail"
    assert j["result"]["decision"]["code"] == "reject"   # в Sd другой человек → «Отклонить»


def test_domains_sorted_by_problem_in_result(tmp_path, monkeypatch):
    """Таблица доменов в интерфейсе: свободные по WHOIS → данные не сходятся → не найдены → в порядке.
    В выгрузке (record) порядок — как в заявлении."""
    import time

    import doctool.jobs as jobs_mod
    import doctool.manager_bridge as mb
    from doctool.models import Extraction
    from doctool.service import CaseResult, _evaluate
    from doctool.verdict import load_case_types
    from doctool.whois import WhoisInfo
    from test_domains import passport

    def fake_run_case(inp):
        res = CaseResult(inp=inp, case_type=load_case_types()["passport_only"], case_id="t", case_dir=tmp_path)
        res.app, res.pas = Extraction("none", ""), passport()
        _evaluate(res)
        return res

    monkeypatch.setattr(jobs_mod, "run_case", fake_run_case)
    monkeypatch.setattr(mb, "whois_lookup", lambda d: WhoisInfo(d, status="free" if d.startswith("free") else "registered"))
    other = {**SD_PP, "fields": {**SD_PP["fields"], "person_r_surname": "Иванов", "birth_date": "01.01.1980",
                                 "passport_series": "4510", "passport_number_short": "654321",
                                 "e_mail": "ivanov@example.com\nIvanov2@example.com"}}
    answers = {"ok.ru": reply(SD_PP, S_PP, "ok.ru"), "other.ru": reply(other, S_PP, "other.ru")}
    app = create_app(out_root=str(tmp_path))
    client = TestClient(app)
    job_id = client.post("/api/jobs", data={"options": '{"case_type": "passport_only"}'},
                         files={"passport": ("p.png", b"x", "image/png")}).json()["job_id"]
    for _ in range(100):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "done":
            break
        time.sleep(0.05)
    doms = ["ok.ru", "busy.ru", "other.ru", "free.ru"]
    with client.websocket_connect("/ext", headers=ORIGIN) as ws:
        ws.receive_json()
        t = client.post("/api/manager/lookup", json={"domains": " ".join(doms), "job_id": job_id}).json()
        for _ in doms:
            req = ws.receive_json()
            d = req["params"]["domain"]
            ans = answers.get(d, {"ok": False, "error": {"code": "not_found", "message": "нет в manager"}})
            ws.send_json({"type": "result", "id": req["id"], **ans})
        for _ in range(100):
            j = client.get(f"/api/manager/tasks/{t['task_id']}").json()
            if j["status"] != "running":
                break
            time.sleep(0.05)
    assert j["status"] == "done"
    shown = [d["domain"] for d in j["result"]["domains"]]
    assert shown == ["free.ru", "other.ru", "busy.ru", "ok.ru"]
    assert [d["problem"] for d in j["result"]["domains"]] == ["free", "mismatch", "not_found", "ok"]
    assert [d["domain"] for d in j["items"]] == shown
    assert [d["domain"] for d in j["result"]["record"]["manager_domains"]] == doms
    # e-mail администратора из Sd (с 0.5.0) — в таблице и в выгрузке по доменам
    assert next(d for d in j["items"] if d["domain"] == "other.ru")["emails"] == ["ivanov@example.com",
                                                                                  "ivanov2@example.com"]
    r = client.get(f"/api/manager/tasks/{t['task_id']}/report")      # домены дела → выгрузка с проверками дела
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    html = r.text
    assert "Свободен по WHOIS" in html and "ivanov@example.com" in html and "Данные в Sd не сходятся" in html
    table = html.split('id="dom"')[1]
    assert table.index("free.ru") < table.index("other.ru") < table.index("busy.ru")
    assert (tmp_path / "domains.html").exists()


def test_domains_report_without_case(tmp_path, monkeypatch):
    """Домены загружены кнопкой без проверки дела: выгрузка с проверками, которым документы не нужны."""
    import time

    import doctool.manager_bridge as mb
    from doctool.whois import WhoisInfo

    monkeypatch.setattr(mb, "whois_lookup", lambda d: WhoisInfo(d, status="free"))
    amb = {"ok": False, "error": {"code": "ambiguous", "message": "По домену найдено несколько услуг: 11 (S), 22 (D)"}}
    answers = {"ok.ru": reply(SD_PP, S_PP, "ok.ru"), "amb.ru": amb}
    client = TestClient(create_app(out_root=str(tmp_path)))
    doms = ["ok.ru", "amb.ru", "o-k.ru"]
    with client.websocket_connect("/ext", headers=ORIGIN) as ws:
        ws.receive_json()
        t = client.post("/api/manager/lookup", json={"domains": " ".join(doms)}).json()
        for _ in doms:
            req = ws.receive_json()
            ans = answers.get(req["params"]["domain"], {"ok": False, "error": {"code": "not_found", "message": "нет"}})
            ws.send_json({"type": "result", "id": req["id"], **ans})
        for _ in range(100):
            if client.get(f"/api/manager/tasks/{t['task_id']}").json()["status"] != "running":
                break
            time.sleep(0.05)
    html = client.get(f"/api/manager/tasks/{t['task_id']}/report").text
    assert "Домены в manager" in html and "Администратор у доменов один" not in html   # найден один домен
    assert "расхождение" in html                                    # «Домены в manager»: o-k.ru не найден
    assert "o-k.ru → ok.ru" in html                                 # похож на найденный — вероятная опечатка
    assert "/tech/srv_details?service_id=11" in html and "Найдено несколько услуг" in html
    assert "Сверка с заявлением и паспортом не выполнялась" in html
    assert client.get("/api/manager/tasks/nope/report").status_code == 404


def test_owner_account_and_fill(tmp_path):
    """Вкладка «Смена владельца ЛК»: аккаунт по «Данные пользователя #N», сверка, заполнение и возврат значений."""
    from test_account import ACCOUNT_DATA

    app = create_app(out_root=str(tmp_path))
    client = TestClient(app)
    assert client.post("/api/owner/account", json={"query": "1000"}).status_code == 409   # расширения нет
    assert client.post("/api/owner/parse", json={"query": "Данные пользователя #1000"}).json()["value"] == "1000"

    def call(path, body):
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("r", client.post(path, json=body)))
        th.start()
        return th, box

    with client.websocket_connect("/ext", headers=ORIGIN) as ws:
        ws.receive_json()
        th, box = call("/api/owner/account", {"query": "Данные пользователя #1000"})
        req = ws.receive_json()
        assert req["cmd"] == "account" and req["params"] == {"user_id": "1000"}
        ws.send_json({"type": "result", "id": req["id"], "ok": True, "data": ACCOUNT_DATA})
        th.join(10)
        j = box["r"].json()
        assert j["ok"] and j["account"]["type"] == "pp" and j["account"]["ba"]["person_r_surname"] == "Тестов"

        cmp_ = client.post("/api/owner/compare", json={"ba": j["account"]["ba"], "app": {"birth_date": "16.03.1990"}}).json()
        assert cmp_["mismatch"] == ["Дата рождения"]

        form = {"surname": "Сидорова", "name": "Анна", "patronymic": "Викторовна", "phone": "8 921 555 12 34",
                "address": "г. Казань, ул. Баумана, д. 1"}
        th, box = call("/api/owner/fill", {"user_id": "1000", "form": form})
        req = ws.receive_json()
        assert req["cmd"] == "fill_runic" and req["params"]["user_id"] == "1000" and req["params"]["restore"] is False
        f = req["params"]["fields"]
        assert f["phone"] == "+79215551234" and f["p_addr_city"] == "Казань" and f["p_addr_recipient"] == "Сидорова Анна Викторовна"
        changed = [{"name": "person_r_surname", "label": "Фамилия", "old": "Тестов", "new": "Сидорова"}]
        ws.send_json({"type": "result", "id": req["id"], "ok": True,
                      "data": {"user_id": "1000", "type": "pp", "changed": changed, "same": [], "missing": [], "values": {}}})
        th.join(10)
        j = box["r"].json()
        assert j["ok"] and j["data"]["changed"][0]["old"] == "Тестов" and j["fields"] == f

        th, box = call("/api/owner/fill", {"user_id": "1000", "restore": True, "fields": {"person_r_surname": "Тестов"}})
        req = ws.receive_json()
        assert req["params"] == {"user_id": "1000", "fields": {"person_r_surname": "Тестов"}, "restore": True}
        ws.send_json({"type": "result", "id": req["id"], "ok": False,
                      "error": {"code": "not_person", "message": "Аккаунт #1000 оформлен на юрлицо"}})
        th.join(10)
        assert box["r"].json()["error"]["code"] == "not_person"
    assert client.post("/api/owner/fill", json={"user_id": "", "form": form}).status_code == 400
    assert client.post("/api/owner/fill", json={"user_id": "1000", "form": {}}).status_code == 400
