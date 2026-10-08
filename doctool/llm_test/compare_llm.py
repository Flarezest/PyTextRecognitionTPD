"""Сравнение двух прогонов наборов: регулярки бланка ↔ модель (0.7.0-dev).

    python llm_test/compare_llm.py <папка прогона «регулярки»> <папка прогона «модель»> [-o сводка.html]

В каждой папке — подпапки наборов (set1, set2, …) с результатами doctool (…/<дело>/result.json).
Пишет сводку в HTML, JSON рядом и короткую таблицу в консоль.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import sys

FIELDS = [
    ("applicant_header", "ФИО заявителя (шапка)"), ("applicant_header.birth_date", "Дата рождения"),
    ("inn", "ИНН"), ("passport", "Паспорт"), ("passport.issue_date", "Дата выдачи паспорта"),
    ("issued_by", "Кем выдан"), ("address", "Адрес регистрации"), ("applicant_fio", "ФИО («Я, …»)"),
    ("domains.list", "Домены"), ("services", "Доп. услуги"), ("new_admin_inline", "Новый адм. (в строке)"),
    ("new_admin_org", "Новый адм. — юрлицо"), ("new_admin_org_contact.emails", "E-mail юрлица"),
    ("new_admin_org_contact.phones", "Телефон юрлица"), ("new_admin_fio", "Новый адм. — физлицо/ИП"),
    ("new_admin_contact.emails", "E-mail нового адм."), ("new_admin_contact.phones", "Телефон нового адм."),
    ("contract", "Договор / аккаунт"), ("signature_fio", "ФИО у подписи"), ("application_date", "Дата заявления"),
]


def sort_key(name: str):
    m = re.search(r"\d+", name)
    return (int(m.group()) if m else 10 ** 6, name)


def load(root: str, s: str):
    r = sorted(glob.glob(os.path.join(root, s, "*", "result.json")))
    if not r:
        return None, None, None
    d = os.path.dirname(r[-1])
    res = json.load(open(r[-1], encoding="utf-8"))
    dbg = os.path.join(d, "llm_debug.json")
    dbg = json.load(open(dbg, encoding="utf-8")) if os.path.exists(dbg) else None
    log = os.path.join(root, s + ".log")
    sec = None
    if os.path.exists(log):
        m = re.search(r"--- (\d+) s", open(log, encoding="utf-8", errors="replace").read())
        sec = int(m.group(1)) if m else None
    return res, dbg, sec


def value(res, key):
    f = ((res or {}).get("application") or {}).get("fields", {}).get(key)
    if not f:
        return None, None
    v = f.get("value")
    if isinstance(v, list):
        v = sorted(str(x).lower() for x in v)
    elif v in ("", None):
        v = None
    return v, f


def norm(v):
    if v is None:
        return None
    if isinstance(v, list):
        return v
    return re.sub(r"[\s«»\"']+", " ", str(v)).strip(" ,.;").lower().replace("ё", "е")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("regex")
    ap.add_argument("llm")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args(argv)
    out = a.out or os.path.join(a.llm, "сравнение.html")
    sets = sorted({os.path.basename(p) for p in glob.glob(os.path.join(a.llm, "*")) if os.path.isdir(p)}, key=sort_key)
    rows, summary = [], []
    for s in sets:
        r1, _, t1 = load(a.regex, s)
        r2, dbg, t2 = load(a.llm, s)
        if r2 is None:
            summary.append({"set": s, "missing": True})
            continue
        llm_calls = (dbg or {}).get("llm") or []
        llm_sec = sum((c.get("meta") or {}).get("seconds") or 0 for c in llm_calls)
        tokens = sum((c.get("meta") or {}).get("output_tokens") or 0 for c in llm_calls)
        think = sum((c.get("meta") or {}).get("thinking_chars") or 0 for c in llm_calls)
        not_in_text = [k for c in llm_calls for k, v in (c.get("fields") or {}).items()
                       if (v.get("answer") and v.get("span") is None and "score" in v) or v.get("not_in_text")]
        diffs = []
        for key, title in FIELDS:
            v1, _ = value(r1, key)
            v2, f2 = value(r2, key)
            if norm(v1) != norm(v2):
                diffs.append({"field": title, "key": key, "regex": v1, "llm": v2,
                              "llm_conf": (f2 or {}).get("confidence"), "llm_source": (f2 or {}).get("source")})
        d1 = ((r1 or {}).get("decision") or {}).get("title")
        d2 = ((r2 or {}).get("decision") or {}).get("title")
        ch1 = {c["name"]: c["status"] for c in (r1 or {}).get("checks", [])}
        ch2 = {c["name"]: c["status"] for c in (r2 or {}).get("checks", [])}
        chk = [{"check": n, "regex": ch1.get(n), "llm": ch2.get(n)} for n in dict.fromkeys(list(ch1) + list(ch2))
               if ch1.get(n) != ch2.get(n)]
        item = {"set": s, "decision_regex": d1, "decision_llm": d2, "seconds_regex": t1, "seconds_llm": t2,
                "llm_seconds": round(llm_sec, 1), "llm_calls": len(llm_calls), "output_tokens": tokens,
                "thinking_chars": think, "llm_error": (dbg or {}).get("llm_error"), "not_in_text": not_in_text,
                "field_diffs": diffs, "check_diffs": chk}
        summary.append(item)
    json.dump(summary, open(os.path.splitext(out)[0] + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1,
              default=str)
    # консоль
    print(f"{'набор':7} {'вердикт: регулярки → модель':42} {'модель, с':>9} {'полей ≠':>7}  не в тексте")
    for it in summary:
        if it.get("missing"):
            print(f"{it['set']:7} нет результата")
            continue
        dec = f"{it['decision_regex']} → {it['decision_llm']}"
        print(f"{it['set']:7} {dec:42} {it['llm_seconds']:>9} {len(it['field_diffs']):>7}  "
              f"{', '.join(it['not_in_text']) or '—'}" + (f"  [ошибка модели: {it['llm_error']}]" if it['llm_error'] else ""))
    # HTML
    e = lambda x: html.escape("" if x is None else (", ".join(map(str, x)) if isinstance(x, list) else str(x)))  # noqa
    parts = ["<!doctype html><meta charset='utf-8'><title>Регулярки и модель</title><style>"
             "body{font:14px system-ui,sans-serif;margin:16px;max-width:1300px}table{border-collapse:collapse;width:100%;"
             "margin:8px 0 20px}td,th{border:1px solid #ccc;padding:4px 6px;vertical-align:top;text-align:left}"
             "th{background:#f3f3f3}.d{background:#fff4d6}.q{color:#a00}h2{margin-top:28px}</style>",
             "<h1>Поля заявления: регулярки бланка ↔ модель</h1>",
             f"<p>Регулярки: <code>{e(a.regex)}</code><br>Модель: <code>{e(a.llm)}</code></p>",
             "<table><tr><th>Набор</th><th>Вердикт (регулярки)</th><th>Вердикт (модель)</th><th>Время модели, с</th>"
             "<th>Токенов ответа</th><th>Полей отличается</th><th>Нет в тексте</th></tr>"]
    for it in summary:
        if it.get("missing"):
            parts.append(f"<tr><td>{e(it['set'])}</td><td colspan=6>нет результата</td></tr>")
            continue
        cls = " class=d" if it["decision_regex"] != it["decision_llm"] else ""
        parts.append(f"<tr{cls}><td><a href='#{e(it['set'])}'>{e(it['set'])}</a></td><td>{e(it['decision_regex'])}</td>"
                     f"<td>{e(it['decision_llm'])}</td><td>{e(it['llm_seconds'])}</td><td>{e(it['output_tokens'])}</td>"
                     f"<td>{len(it['field_diffs'])}</td><td>{e(it['not_in_text']) or '—'}"
                     f"{'<br><b>ошибка модели:</b> ' + e(it['llm_error']) if it['llm_error'] else ''}</td></tr>")
    parts.append("</table>")
    for it in summary:
        if it.get("missing") or not (it["field_diffs"] or it["check_diffs"]):
            continue
        parts.append(f"<h2 id='{e(it['set'])}'>{e(it['set'])}</h2><table><tr><th>Поле</th><th>Регулярки</th>"
                     "<th>Модель</th><th>Источник / уверенность</th></tr>")
        for d in it["field_diffs"]:
            q = " class=q" if (d.get("llm_source") or "").endswith("?") else ""
            parts.append(f"<tr><td>{e(d['field'])}</td><td>{e(d['regex'])}</td><td{q}>{e(d['llm'])}</td>"
                         f"<td>{e(d.get('llm_source'))} {e(d.get('llm_conf'))}</td></tr>")
        parts.append("</table>")
        if it["check_diffs"]:
            parts.append("<table><tr><th>Проверка</th><th>Регулярки</th><th>Модель</th></tr>" + "".join(
                f"<tr><td>{e(c['check'])}</td><td>{e(c['regex'])}</td><td>{e(c['llm'])}</td></tr>"
                for c in it["check_diffs"]) + "</table>")
    open(out, "w", encoding="utf-8").write("\n".join(parts))
    print("\nСводка:", out)


if __name__ == "__main__":
    sys.exit(main())
