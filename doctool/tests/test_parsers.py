"""Быстрые тесты без OCR: python -m pytest tests -q"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doctool import mrz, names, parsers  # noqa: E402
from doctool.compare import issuer_similarity  # noqa: E402


def test_mrz_ru_internal():
    r = mrz.parse_td3("PNRUSPOPOVA<<EKATERINA<SERGEEVNA<<<<<<<<<<<<<",
                      "5215640342RUS8411280F<<<<<<<6160728550001<76")
    assert r.valid
    assert (r.surname, r.given_names, r.patronymic) == ("ПОПОВА", "ЕКАТЕРИНА", "СЕРГЕЕВНА")
    assert (r.series, r.number, r.department_code) == ("5216", "564034", "550-001")
    assert r.birth_date == date(1984, 11, 28) and r.issue_date == date(2016, 7, 28)


def test_mrz_ocr_noise_repaired():
    # типичные ошибки Tesseract: 5 -> 9 в номере, «<» -> «K» в ФИО
    r = mrz.parse_td3("PNRUSPOPOVAK<EKATERINAKSERGEEVNA<K<<<<<<KKKKK",
                      "9215640342RUS8411280F<<<<<<<6160728550001<76")
    assert r.valid and r.series == "5216" and r.patronymic == "СЕРГЕЕВНА"


def test_names_cases():
    assert names.to_nominative("Поповой Екатерины Сергеевны") == "ПОПОВА ЕКАТЕРИНА СЕРГЕЕВНА"
    assert names.to_nominative("Семичева Дмитрия Викторовича") == "СЕМИЧЕВ ДМИТРИЙ ВИКТОРОВИЧ"
    assert names.fio_similarity("Попова Е.С.", "ПОПОВА ЕКАТЕРИНА СЕРГЕЕВНА")[0] == 100


def test_dates_in_words():
    assert parsers.parse_date_words("Седьмое августа две тысячи двадцать шестого года") == date(2026, 8, 7)
    assert parsers.parse_date_words('"двадцать пятое" сентября 20 26 г.') == date(2026, 9, 25)


def test_inn_and_domains():
    assert parsers.inn_valid("550722926100") and not parsers.inn_valid("550722926101")
    assert parsers.find_domains("домена спортопт.рф, почта a@mail.ru") == ["спортопт.рф"]
    assert parsers.to_punycode("спортопт.рф").endswith(".xn--p1ai")


def test_issuer_abbreviations():
    assert issuer_similarity("ОВД Московского округа г.Калуга",
                             "ОТДЕЛОМ ВНУТРЕННИХ ДЕЛ МОСКОВСКОГО ОКРУГА ГОРОДА КАЛУГИ") >= 85


def test_no_passport_validity_check():
    # с 0.5.0 срок действия паспорта по возрасту (20/45 лет) не проверяется — только сходство данных
    from doctool import checks as checklib
    assert "passport_validity" not in checklib.REGISTRY


def test_handwriting_tolerance():
    assert parsers.find_domains("distobR-RU") == ["distobr.ru"]
    assert parsers.find_domains("distobr. ru") == ["distobr.ru"]
    assert parsers.parse_date_words('"двадцать пятое" сешпября 20 26 г.') == date(2026, 9, 25)


def test_verdict_rules_old_passport():
    from doctool.compare import Check, extra_checks
    from doctool.models import Extraction
    from doctool.verdict import decide, load_case_types
    app, pas = Extraction("a", "a.pdf"), Extraction("p", "p.pdf")
    app.set("passport", "5299 083602", 0.98, "text")
    app.set("applicant_fio", "Попова Екатерина Сергеевна", 0.98, "text")
    app.set("passport.issue_date", "15.07.1999", 0.98, "text")
    for k, v in (("surname", "ПОПОВА"), ("given_name", "ЕКАТЕРИНА"), ("patronymic", "СЕРГЕЕВНА"),
                 ("series_number", "5216 564034"), ("issue_date", "28.07.2016")):
        pas.set(k, v, 0.99, "mrz")
    prev = [{"series_number": "5299 083602", "department_code": "552-001", "issue_date": "", "page": 9}]
    more, flags = extra_checks(app, pas, prev, None, [])
    assert "old_passport" in flags and more[0].status == "fail"
    d = decide(load_case_types()["admin_change_person"], more, flags, app.fields)
    assert d.code == "reject"
    d = decide(load_case_types()["admin_change_person"], [Check("x", "ok")], set(), app.fields)
    assert d.code == "accept"


def test_ocr_typo_in_passport_name():
    assert names.ocr_typo_only("Семичева Дмитрия Викторовича", "СЕМИЧЕВ ДМИТРИЙ РИКТОРОВИЧ")
    assert not names.ocr_typo_only("Иванов Пётр Сергеевич", "ИВАНОВ ПЕТР СЕМЕНОВИЧ")


def test_handwritten_passport_vlm_values_do_not_reject():
    """Набор 4 (Аверин): паспорт заполнен от руки, модель ошиблась в дате выдачи.
    Невозможная дата отбрасывается, а расхождение со значением модели — «ручная проверка», не «отклонить»."""
    from doctool.compare import compare, extra_checks
    from doctool.models import Extraction
    from doctool.passport_rf import plausibility
    from doctool.verdict import decide, load_case_types
    pas = Extraction("passport_rf", "p.pdf")
    for k, v in (("surname", "АВЕРИН"), ("given_name", "ВЯЧЕСЛАВ"), ("patronymic", "ВЛАДИМИРОВИЧ"),
                 ("birth_date", "13.11.1953"), ("issue_date", "24.05.1987"), ("series_number", "4600 818840")):
        pas.set(k, v, 0.55, "vlm")
    notes = plausibility(pas)
    assert pas.get("issue_date") is None and any("1997" in n for n in notes)   # 1987 — невозможно
    pas.set("issue_date", "12.05.2001", 0.55, "vlm")                          # ошибка модели в одной цифре
    app = Extraction("regru_transfer_person", "a.pdf")
    app.set("applicant_header", "АВЕРИН ВЯЧЕСЛАВ ВЛАДИМИРОВИЧ", 0.85, "ocr-text")
    app.set("applicant_header.raw_fio", "Аверина Вячеслава Владимировича", 0.85, "ocr-text")
    app.set("applicant_header.birth_date", "13.11.1953", 0.85, "ocr-text")
    app.set("passport", "4600 818840", 0.85, "ocr-text")
    app.set("passport.issue_date", "22.05.2001", 0.85, "ocr-text")
    app.set("domains", "udvn.ru", 0.85, "ocr-text")
    app.set("domains.list", ["udvn.ru"], 0.85, "ocr-text")
    app.set("new_admin_org", "ООО НТП Годсэнд-сервис", 0.85, "ocr-text")
    app.set("new_admin_org_contact", "office@udvn.ru", 0.85, "ocr-text")
    app.set("contract", "№567529 от 07.08.2013", 0.85, "ocr-text")
    app.set("application_date", "29.09.2026", 0.85, "ocr-text")
    checks = compare(app, pas)
    by = {c.name: c for c in checks}
    assert by["Дата выдачи паспорта"].status == "review"
    assert "Срок действия паспорта" not in by
    more, flags = extra_checks(app, pas, [], None, checks)
    assert decide(load_case_types()["admin_change_person"], checks + more, flags, app.fields).code == "review"


# ------------------------------------------------------------------ «Кем выдан» в режиме «только паспорт» (набор 7 — Опря)

def _oprya(issued_by="ГУ МВД РОССИИ ПО МОСКОВСКОЙ ОБЛАСТИ", src="ocr", conf=0.62, dept="500-050"):
    from doctool.models import Extraction
    pas = Extraction("passport_rf", "PassportSet7.png")
    for k, v in (("surname", "ОПРЯ"), ("given_name", "ВИКТОР"), ("patronymic", "АЛЕКСАНДРОВИЧ"), ("sex", "МУЖ"),
                 ("birth_date", "04.10.1973"), ("series_number", "4619 156125"), ("issue_date", "17.10.2018"),
                 ("department_code", dept)):
        pas.set(k, v, 0.99, "mrz")
    if issued_by:
        pas.set("issued_by", issued_by, conf, src)
    pas.debug["mrz"] = {"valid": True, "raw": ["PNRUSOPR8<<VIKTOR<ALEKSANDROVI3<<<<<<<<<<<<<",
                                               "4611561253RUS7310043M<<<<<<<9181017500050<14"], "checks": {}}
    return pas


def _passport_only(pas, admin=None, tmp_path=None):
    from doctool.models import Extraction
    from doctool.service import CaseInput, CaseResult, _evaluate
    from doctool.verdict import load_case_types
    res = CaseResult(inp=CaseInput(case_type="passport_only", check_date="05.10.2026"),
                     case_type=load_case_types()["passport_only"], case_id="t", case_dir=tmp_path or Path("."))
    res.app, res.pas, res.admin = Extraction("none", ""), pas, admin
    _evaluate(res)
    return res, {c.name: c for c in res.checks}


def test_issuer_regions_and_department_code():
    from doctool import issuer
    assert issuer.department_region("500-050") == "50"
    assert issuer.regions_in_text("ГУ МВД РОССИИ ПО МОСКОВСКОЙ ОБЛАСТИ") == {"50"}
    assert issuer.regions_in_text("ОТДЕЛОМ ВНУТРЕННИХ ДЕЛ МОСКОВСКОГО ОКРУГА ГОРОДА КАЛУГИ") == {"40"}
    assert issuer.regions_in_text("ОУФМС РОССИИ ПО ОМСКОЙ ОБЛ. В КИРОВСКОМ АДМИНИСТРАТИВНОМ ОКРУГЕ ГОРОДА ОМСКА") == {"55"}
    assert issuer.regions_in_text("ОТДЕЛЕНИЕМ УФМС РОССИИ ПО ПЕРМСКОМУ КРАЮ В ОСИНСКОМ РАЙОНЕ") == {"59"}
    assert issuer.regions_in_text("ОВД гор. Фрязино Щелковского УВД Московской обл.") == {"50"}
    assert issuer.regions_in_text("ТП УФМС РОССИИ ПО ГОР. МОСКВЕ В ЗЕЛЕНОГРАДСКОМ АО") == {"77"}


def test_issuer_ocr_fixes():
    from doctool.issuer import correct_ocr
    fixed, fixes = correct_ocr("ГУ МВД РООСИИ ПО МОСКОВСКОЙ ВЛАСТИ")   # так прочитал Tesseract у пользователя
    assert fixed == "ГУ МВД РОССИИ ПО МОСКОВСКОЙ ОБЛАСТИ" and len(fixes) == 2
    # названия районов не «исправляются» под похожие слова бланка
    assert correct_ocr("ОТДЕЛЕНИЕМ УФМС РОССИИ ПО ПЕРМСКОМУ КРАЮ В ОСИНСКОМ РАЙОНЕ")[1] == []


def test_series_year_lead_keeps_mrz_issue_date():
    from doctool.passport_rf import plausibility
    pas = _oprya()
    assert plausibility(pas) == [] and pas.get("issue_date") == "17.10.2018"   # серия 46 19, выдан в 2018
    pas.set("issue_date", "17.10.2014", 0.55, "vlm")                           # опережение 5 лет у модели
    plausibility(pas)
    assert pas.get("issue_date") is None and "17.10.2014" in pas.fields["issue_date"].alternatives


def test_passport_only_issued_by_accept(tmp_path):
    res, ch = _passport_only(_oprya(), tmp_path=tmp_path)
    assert ch["Кем выдан"].status == "ok"
    assert ch["Кем выдан ↔ код подразделения"].status == "ok"
    assert ch["Серия ↔ год выдачи"].status == "info"
    assert res.decision.code == "accept"
    row = next(r for r in __import__("doctool.service", fromlist=["x"]).table_rows(res) if r["id"] == "issued_by")
    assert row["status"] == "ok" and row["passport"] == "ГУ МВД РОССИИ ПО МОСКОВСКОЙ ОБЛАСТИ"


def test_passport_only_issued_by_review(tmp_path):
    # модель / низкая уверенность → ручная проверка
    res, ch = _passport_only(_oprya(src="vlm", conf=0.55), tmp_path=tmp_path)
    assert ch["Кем выдан"].status == "review" and res.decision.code == "review"
    assert "«Кем выдан» нужно подтвердить по паспорту" in res.decision.reasons
    # регион не совпадает с кодом подразделения
    res, ch = _passport_only(_oprya("ОУФМС РОССИИ ПО ОМСКОЙ ОБЛ."), tmp_path=tmp_path)
    assert ch["Кем выдан ↔ код подразделения"].status == "warn" and res.decision.code == "review"
    # не распознано
    res, ch = _passport_only(_oprya(issued_by=None), tmp_path=tmp_path)
    assert ch["Кем выдан"].status == "review" and res.decision.code == "review"
    # сверка с данными системы (тот же паспорт)
    from doctool.integrations import AdminData
    adm = AdminData(fio="Опря Виктор Александрович", passport="4619 156125",
                    passport_issued_by="ГУ МВД России по Московской обл.")
    res, ch = _passport_only(_oprya(), admin=adm, tmp_path=tmp_path)
    assert ch["Кем выдан ↔ данные системы"].status == "ok" and res.decision.code == "accept"


def test_passport_only_without_issue_date(tmp_path):
    # срок действия по возрасту больше не проверяется: без даты выдачи нет и проверки «Срок действия»
    pas = _oprya()
    del pas.fields["issue_date"]
    res, ch = _passport_only(pas, tmp_path=tmp_path)
    assert "Срок действия паспорта" not in ch
