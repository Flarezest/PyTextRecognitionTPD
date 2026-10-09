"""Выгрузка по доменам из manager — отдельный HTML-файл (кнопка «Выгрузка по доменам» в веб-интерфейсе).

На входе — домены в виде DomainInfo.to_dict() (как в таблице интерфейса и в export.json), на выходе — одна
самодостаточная страница: итог по группам «проблемности», проверки, администраторы найденных доменов,
замечания «к сведению» и таблица доменов с фильтром. Внешних ресурсов нет — файл можно переслать.

С 0.6.0 у доменов физлиц — колонка «ЕСИА» (идентификация через Госуслуги) и кнопка ESIA рядом с Sd / S:
окно сверки Sd ↔ ЕСИА встроено в файл (данные — в блоке <script type="application/json" id="esia-data">).
"""
from __future__ import annotations

import difflib
import html
import json
import re
from collections import Counter, OrderedDict
from datetime import datetime
from urllib.parse import quote

from .compare import STATUS_RU, Check
from .domains import CHECK_NAMES, FOUND, NOT_FOUND, PROBLEMS, DomainInfo, sort_by_problem, summary_checks

MANAGER = "https://manager.reg.ru"
WHOIS_URL = "https://www.reg.ru/whois/?dname={}"
_e = html.escape


def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


def _link(url: str, text: str) -> str:
    return f'<a href="{_e(url)}" target="_blank" rel="noopener">{_e(text)}</a>'


def admin_key(d: dict) -> tuple:
    """Кто администратор (как в проверке «Администратор у доменов один»): физлицо — по паспорту или ФИО,
    юрлицо — по ИНН или названию, остальные зоны — по названию/ФИО."""
    sd = d.get("sd") or {}
    if d.get("kind") == "person":
        return ("person", _digits(d.get("passport")) or (d.get("fio") or "").upper())
    if d.get("kind") == "org":
        return ("org", _digits(sd.get("code")) or (d.get("holder") or "").upper())
    return (d.get("group") or "?", (d.get("holder") or "").upper())


def _name_part(domain: str) -> str:
    return domain.rsplit(".", 1)[0].replace("-", "")


def similar_known(items: list[dict], threshold: float = 0.85) -> dict[str, str]:
    """Не найденные в manager домены, очень похожие на найденные (вероятная ошибка распознавания заявления)."""
    known = [d["domain"] for d in items if d.get("status") != NOT_FOUND]
    out = {}
    for d in items:
        if d.get("status") != NOT_FOUND or not known:
            continue
        best = max(known, key=lambda k: difflib.SequenceMatcher(None, _name_part(d["domain"]), _name_part(k)).ratio())
        if difflib.SequenceMatcher(None, _name_part(d["domain"]), _name_part(best)).ratio() >= threshold:
            out[d["domain"]] = best
    return out


def _services(d: dict) -> list[tuple[str, str]]:
    """«По домену найдено несколько услуг: 112886607 (S), 48792715 (D)» → [(id, статус), …]."""
    return re.findall(r"(\d+) \((\w+|\?)\)", d.get("error") or "")


def notes(items: list[dict]) -> list[str]:
    """Замечания «к сведению» — то, что видно только по всему списку сразу."""
    out: list[str] = []
    found = [d for d in items if d.get("status") == FOUND]
    st = Counter(d.get("service_status") or "—" for d in found)
    if found and any(not k.startswith("Активна") for k in st):
        out.append("Статусы услуг найденных доменов: " + ", ".join(f"{_e(k)} — {v}" for k, v in st.most_common()) + ".")
    if found and not any(d.get("verdict") for d in found):
        out.append("Сверка с заявлением и паспортом не выполнялась: данные загружены без проверки дела. "
                   "Проверки «Заявитель — администратор доменов (Sd)» и «Паспорт в Sd» не выполнены.")
    by_acc: dict[str, OrderedDict] = {}
    for d in found:
        by_acc.setdefault(d.get("account") or "—", OrderedDict()).setdefault(admin_key(d), []).append(d)
    for acc, admins in by_acc.items():
        if len(admins) > 1:
            out.append(f"В аккаунте {_e(acc)} домены разных администраторов: "
                       + "; ".join(f"{_e(v[0].get('holder') or '(не заполнено)')} — {len(v)}" for v in admins.values()) + ".")
    by_pas: dict[str, list] = {}
    for d in found:
        if d.get("kind") == "person" and _digits(d.get("passport")):
            by_pas.setdefault(_digits(d.get("passport")), []).append(d)
    for lst in by_pas.values():
        places = Counter(((d.get("sd") or {}).get("passport_place") or "").strip() for d in lst)
        dates = Counter(((d.get("sd") or {}).get("passport_date") or "").strip() for d in lst)
        if len(places) > 1 or len(dates) > 1:
            rare = [d["domain"] for d in lst if places[((d.get("sd") or {}).get("passport_place") or "").strip()]
                    < max(places.values()) or dates[((d.get("sd") or {}).get("passport_date") or "").strip()]
                    < max(dates.values())]
            out.append(f"Паспорт {_e(lst[0].get('passport'))} ({_e(lst[0].get('holder'))}) одинаковый во всех "
                       f"{len(lst)} доменах, но дата выдачи или «кем выдан» в Sd записаны по-разному"
                       + (f": отличаются {', '.join(map(_e, rare[:10]))}{' и др.' if len(rare) > 10 else ''}" if rare else "")
                       + ".")
    sim = similar_known(items)
    if sim:
        st_of = {d["domain"]: d.get("service_status") or "" for d in found}
        out.append("Не найдены в manager, но похожи на домены из списка (возможна ошибка распознавания заявления — "
                   "сверьте со сканом): " + "; ".join(
                       f"{_e(a)} → {_e(b)}" + (f" ({_e(st_of[b])})" if st_of.get(b) else "") for a, b in sim.items()) + ".")
    werr = [d for d in items if (d.get("whois") or {}).get("status") == "error"]
    if werr:
        out.append("WHOIS не дал ответа: " + "; ".join(f"{_e(d['domain'])} — {_e((d.get('whois') or {}).get('error', ''))}"
                                                       for d in werr[:10]) + ".")
    amb = [d for d in items if d.get("error_code") == "ambiguous"]
    if amb:
        all_d = [d["domain"] for d in amb if _services(d) and all(s == "D" for _, s in _services(d))]
        out.append(f"«Найдено несколько услуг» — {len(amb)}: активной (A) услуги нет, расширение не выбирает услугу само. "
                   "Откройте Sd по ссылкам в таблице" + (f"; все услуги удалены (D): {', '.join(map(_e, all_d))}" if all_d else "")
                   + ".")
    esia = [d for d in found if d.get("esia_status")]
    if esia:
        st = Counter(d.get("esia_state") or {"none": "не проходил", "no_link": "нет ссылки на Sd", "error": "ошибка"}
                     .get(d.get("esia_status"), "без state") for d in esia)
        bad = [d["domain"] for d in esia if d.get("esia_status") == "mismatch"]
        out.append("Идентификация через Госуслуги (ЕСИА) по Sd доменов-физлиц: " + ", ".join(f"{_e(k)} — {v}" for k, v in st.most_common())
                   + (f"; данные Sd не совпадают с ЕСИА: {', '.join(map(_e, bad[:15]))}{' и др.' if len(bad) > 15 else ''}" if bad else "")
                   + ". На вердикт не влияет — кнопка ESIA у домена открывает сверку.")
    other = Counter(d.get("group") or "?" for d in found if d.get("kind") not in ("person", "org"))
    if other:
        out.append("Домены с другим типом контактов (не ru_pp/ru_org) — сверка только вручную: "
                   + ", ".join(f"{_e(k)} — {v}" for k, v in other.items()) + ".")
    return out


def _checks_table(checks: list[Check]) -> str:
    rows = "".join(f'<tr class="{c.status}"><td>{_e(c.name)}</td><td><b>{STATUS_RU[c.status]}</b></td>'
                   f"<td>{_e(c.application)}</td><td>{_e(c.passport)}</td><td>{_e(c.detail)}</td></tr>" for c in checks)
    return ("<table><tr><th>Проверка</th><th>Статус</th><th>Заявление</th><th>Sd / документы</th><th>Комментарий</th></tr>"
            + rows + "</table>")


def _admins_table(found: list[dict]) -> str:
    groups: OrderedDict = OrderedDict()
    for d in found:
        groups.setdefault(admin_key(d), []).append(d)
    rows = []
    for lst in groups.values():
        d0, sd = lst[0], lst[0].get("sd") or {}
        if d0.get("kind") == "person":
            who = _e(d0.get("fio") or "") or "<i>ФИО в Sd не заполнено</i>"
            doc = (f"{_e(d0.get('passport') or '—')}<br><span class=src>д. р. {_e(sd.get('birth_date') or '—')}"
                   + (f", выдан {_e(sd['passport_date'])}" if sd.get("passport_date") else "") + "</span>")
        elif d0.get("kind") == "org":
            who, doc = _e(d0.get("holder") or ""), f"ИНН {_e(sd.get('code') or '—')}"
        else:
            who, doc = _e(d0.get("holder") or "—"), "—"
        mails = sorted({m for d in lst for m in d.get("emails") or []})
        st = Counter(d.get("service_status") or "—" for d in lst)
        accs = sorted({d.get("account") or "—" for d in lst})
        doms = ", ".join(_e(d["domain"]) for d in lst)
        cell = doms if len(lst) <= 3 else f"<details><summary>{len(lst)} доменов</summary>{doms}</details>"
        rows.append(f"<tr><td>{who}<br><span class=src>{_e(d0.get('kind_ru') or d0.get('group') or '')}</span></td>"
                    f"<td>{doc}</td><td>{'<br>'.join(map(_e, mails)) or '—'}</td>"
                    f"<td>{'<br>'.join(f'{_e(k)} — {v}' for k, v in st.items())}</td><td>{', '.join(map(_e, accs))}</td>"
                    f"<td class=num>{len(lst)}</td><td>{cell}</td></tr>")
    return ("<table><tr><th>Администратор (Sd)</th><th>Паспорт / ИНН</th><th>E-mail</th><th>Статус услуги</th>"
            "<th>Аккаунт</th><th>Доменов</th><th>Домены</th></tr>" + "".join(rows) + "</table>")


def _domain_row(d: dict, similar: dict[str, str]) -> str:
    dom = d["domain"]
    urls = d.get("urls") or {}
    links = [_link(urls.get("bills") or f"{MANAGER}/bill/bills?searchstring={quote(dom)}", "счета")]
    if urls.get("sd"):
        links.insert(0, _link(urls["sd"], "Sd"))
    if urls.get("s"):
        links.insert(1, _link(urls["s"], "S"))
    esia_btn = f' <button class="esia" data-d="{_e(dom)}">ESIA</button>' if d.get("esia") else ""
    head = f"<td><b>{_e(dom)}</b><br><span class=src>{' '.join(links)}</span>{esia_btn}</td>"
    if d.get("status") == NOT_FOUND:
        w = d.get("whois") or {}
        if w.get("status") == "free":
            wt = "WHOIS: домен свободен"
        elif w.get("status") == "registered":
            wt = ("WHOIS: зарегистрирован" + (f", регистратор {_e(w['registrar'])}" if w.get("registrar") else "")
                  + (f", {_e(w['state'])}" if w.get("state") else "")
                  + (f", до {_e(w['paid_till'][:10])}" if w.get("paid_till") else ""))
        elif w.get("status") == "error":
            wt = "WHOIS недоступен: " + _e(w.get("error") or "")
        else:
            wt = "WHOIS не проверялся"
        wt += " · " + _link(w.get("manual_url") or WHOIS_URL.format(quote(dom)), "проверить WHOIS вручную")
        if dom in similar:
            wt += f"<br><span class=hint>похож на {_e(similar[dom])} из списка</span>"
        return (f'<tr class="fail" data-q="{_e(dom)}">{head}<td>не найден</td><td>{_e(d.get("error") or "")}</td>'
                f"<td></td><td></td><td>{wt}</td></tr>")
    if d.get("status") != FOUND:
        svc = "<br>".join(_link(f"{MANAGER}/tech/srv_details?service_id={sid}", f"Sd {sid} ({st})") for sid, st in _services(d))
        return (f'<tr class="review" data-q="{_e(dom)}">{head}<td>ошибка</td><td>{_e(d.get("error") or "")}</td>'
                f"<td></td><td></td><td>{svc}</td></tr>")
    sd = d.get("sd") or {}
    sst = d.get("service_status") or ""
    mail = (f"<br>e-mail: {', '.join(map(_e, d['emails']))}" if d.get("emails") else "")
    if d.get("kind") == "person":
        who = _e(d.get("fio") or "") or "<i>ФИО в Sd не заполнено</i>"
        data = (f"{who}<br><span class=src>физлицо, паспорт {_e(d.get('passport') or '—')} от "
                f"{_e(sd.get('passport_date') or '—')}" + (f", д. р. {_e(sd['birth_date'])}" if sd.get("birth_date") else "")
                + (f"<br>кем выдан (Sd): {_e(sd['passport_place'])}" if sd.get("passport_place") else "") + mail + "</span>")
        q = f"{dom} {d.get('fio', '')} {d.get('passport', '')} {' '.join(d.get('emails') or [])}"
    elif d.get("kind") == "org":
        data = f"{_e(d.get('holder') or '')}<br><span class=src>юрлицо, ИНН {_e(sd.get('code') or '—')}{mail}</span>"
        q = f"{dom} {d.get('holder', '')} {sd.get('code', '')} {' '.join(d.get('emails') or [])}"
    else:
        data = (f"{_e(d.get('holder') or '—')}<br><span class=src>контакты {_e(d.get('group') or '?')}{mail}</span>")
        q = f"{dom} {d.get('holder', '')} {' '.join(d.get('emails') or [])}"
    cmp_rows = [r for r in d.get("compare") or [] if r.get("status") != "ok"]
    if d.get("verdict"):
        cmp_ = (f"<b>{STATUS_RU.get(d['verdict'], d['verdict'])}</b>" + "".join(
            f"<br>{_e(r['title'])}: {STATUS_RU[r['status']]}" + (f" — {_e(r['detail'])}" if r.get("detail") else "")
            for r in cmp_rows))
    else:
        cmp_ = "<span class=src>не выполнялась — нет заявления и паспорта</span>"
    extra = (f"provider: {_e(d.get('provider') or '—')}<br>аккаунт: {_e(d.get('account') or '—')}<br>"
             f"смена админа: {_e(d.get('last_admin_change') or '—')}<br>"
             f"страна: {_e(sd.get('country') or sd.get('o_country_code') or '—')}")
    cls = d.get("verdict") or ""
    q += f" {d.get('esia_ru', '')}"
    return (f'<tr class="{cls}" data-q="{_e(q)}">{head}<td>найден<br><span{"" if sst.startswith("Активна") else " class=bad"}>'
            f"{_e(sst)}</span></td><td>{data}</td><td>{cmp_}</td><td>{_esia_cell(d)}</td><td>{extra}</td></tr>")


_ESIA_CLS = {"match": "ok", "warn": "warn", "mismatch": "fail", "none": "info", "no_data": "review", "no_link": "info",
             "error": "review"}


def _esia_cell(d: dict) -> str:
    if not d.get("esia_status"):
        return "<span class=src>—</span>"
    state = d.get("esia_state") or ""
    text = (d.get("esia_ru") or "").replace(f"{state} · ", "", 1) if state else d.get("esia_ru") or ""
    return ((f"<b>{_e(state)}</b><br>" if state else "")
            + f'<span class="tag {_ESIA_CLS.get(d["esia_status"], "info")}">{_e(text)}</span>')


def _esia_data(items: list[dict]) -> str:
    """Данные для окна ESIA (JSON внутри страницы): по домену — шапка, строки сверки, история попыток."""
    out = {}
    for d in items:
        e = d.get("esia") or {}
        if not e:
            continue
        data, latest = e.get("data") or {}, e.get("latest") or {}
        out[d["domain"]] = {
            "status": d.get("esia_status"), "text": d.get("esia_ru"), "state": e.get("state") or "",
            "created": latest.get("creation_date") or "", "processed": latest.get("processed_date") or "",
            "action": latest.get("action") or "", "reason": latest.get("reason") or "", "comment": latest.get("comment") or "",
            "trusted": data.get("trusted"), "vrf": (data.get("rf_passport") or {}).get("vrf_stu") or "",
            "url": e.get("url") or "", "file_url": e.get("file_url") or "", "sd_url": (d.get("urls") or {}).get("sd") or "",
            "error": e.get("error") or e.get("json_error") or "", "rows": d.get("esia_rows") or [],
            "history": e.get("history") or [] if (e.get("count") or 0) > 1 else [],
        }
    return json.dumps(out, ensure_ascii=False).replace("</", "<\\/")


_JS = """
(function(){
  var q=document.getElementById('q'), shown=document.getElementById('shown');
  var rows=[].slice.call(document.querySelectorAll('#dom tr'));
  function fin(h){ if(!h) return; var c=h.row.querySelector('.cnt'), t=q.value.trim();
    c.textContent=t?(h.n+' из '+c.dataset.total):c.dataset.total; h.row.style.display=(h.n||!t)?'':'none'; }
  function apply(){
    var t=q.value.trim().toLowerCase(), head=null, n=0, total=0;
    rows.forEach(function(r){
      if(r.classList.contains('grp')){ fin(head); head={row:r,n:0}; return; }
      if(!r.dataset.q) return;
      total++; var ok=!t || r.dataset.q.toLowerCase().indexOf(t)>=0;
      r.style.display=ok?'':'none'; if(ok){ n++; if(head) head.n++; }
    });
    fin(head); shown.textContent=t?('показано '+n+' из '+total):'';
  }
  q.addEventListener('input', apply);
})();
"""

_ESIA_JS = """
(function(){
  var data = JSON.parse(document.getElementById('esia-data').textContent || '{}');
  var dlg = document.getElementById('esiaDlg');
  function e(s){ return (s === undefined || s === null ? '' : String(s)).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function a(u, t){ return u ? '<a href="' + e(u) + '" target="_blank" rel="noopener">' + t + '</a>' : ''; }
  var WHY = {none: 'Записей об идентификации через Госуслуги нет.', no_link: 'На странице Sd нет ссылки «Идентификация через Госуслуги».',
             no_data: 'Файла данных ЕСИА нет или он не загрузился.'};
  document.addEventListener('click', function(ev){
    var b = ev.target.closest ? ev.target.closest('button.esia') : null;
    if (!b) return;
    var x = data[b.getAttribute('data-d')]; if (!x) return;
    document.getElementById('esiaTitle').textContent = 'ЕСИА ↔ Sd — ' + b.getAttribute('data-d');
    var h = '<table class="kv"><tr><th>state</th><td>' + e(x.state || '—') + '</td><th>создано / обработано</th><td>' + e(x.created || '—')
      + ' / ' + e(x.processed || '—') + '</td></tr><tr><th>action</th><td>' + e(x.action || '—') + (x.reason ? '<br>reason: ' + e(x.reason) : '')
      + (x.comment ? '<br>' + e(x.comment) : '') + '</td><th>данные ЕСИА</th><td>' + (x.trusted === undefined || x.trusted === null ? '—'
      : 'trusted: ' + (x.trusted ? 'да' : 'нет')) + (x.vrf ? ' · паспорт: ' + e(x.vrf) : '') + '</td></tr><tr><th>ссылки</th><td colspan="3">'
      + [a(x.url, 'Идентификация через Госуслуги'), a(x.file_url, 'JSON-файл'), a(x.sd_url, 'Sd')].filter(Boolean).join(' · ') + '</td></tr></table>';
    var why = x.error ? 'Ошибка: ' + x.error : WHY[x.status];
    if (why) h += '<p class="notes">' + e(why) + '</p>';
    if (x.rows.length) {
      h += '<table><tr><th>Поле</th><th>Sd (ru_pp)</th><th>ЕСИА (JSON)</th><th></th></tr>';
      x.rows.forEach(function(r){
        var c = r.status ? ' class="' + r.status + '"' : '';
        h += '<tr><td>' + e(r.title) + '</td><td' + c + '>' + e(r.sd) + '<br><span class=src>' + e(r.field) + '</span></td><td' + c + '>'
          + e(r.esia) + '<br><span class=src>' + e(r.esia_field) + '</span></td><td class=src>' + e(r.detail) + '</td></tr>';
      });
      h += '</table><p class=src>Зелёный — совпадает, красный — расходится, жёлтый — «кем выдан» написан по-разному; без цвета — значения нет с одной из сторон.</p>';
    }
    if (x.history.length) {
      h += '<h3>Все попытки идентификации</h3><table><tr><th>state</th><th>создано</th><th>обработано</th><th>action</th><th>reason / comment</th></tr>';
      x.history.forEach(function(r){ h += '<tr><td>' + e(r.state) + '</td><td>' + e(r.creation_date) + '</td><td>' + e(r.processed_date)
        + '</td><td>' + e(r.action) + '</td><td>' + e([r.reason, r.comment].filter(Boolean).join(' / ')) + '</td></tr>'; });
      h += '</table>';
    }
    document.getElementById('esiaBody').innerHTML = h;
    dlg.showModal();
  });
})();
"""

_CSS = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;color:#1d1d1f;background:#fff}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 8px}
.verdict{display:inline-block;padding:6px 12px;border-radius:6px;font-weight:600;margin:8px 0}
.verdict a{color:inherit}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border:1px solid #d0d0d5;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f3f3f6;position:sticky;top:0;z-index:1}
.ok{background:#e6f4ea} .warn{background:#fff4ce} .fail{background:#fde2e1} .review{background:#e3eefb} .info{background:#f3f3f3}
.src{color:#666;font-size:13px} .notes{color:#7a4b00;font-size:13.5px;line-height:1.5}
tr.grp td{background:#f3f3f6;font-weight:600;font-size:13px}
.hint{color:#7a4b00;font-size:12.5px} .bad{color:#9b1c1c;font-weight:600} .num{text-align:right}
.tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:6px 0 10px}
.tools input{font:inherit;padding:6px 9px;border:1px solid #d0d0d5;border-radius:6px;min-width:280px}
details summary{cursor:pointer;color:#2b6cb0} a{color:#2b6cb0}
button.esia{font:inherit;font-size:12px;padding:0 7px;margin-left:4px;border:1px solid #2b6cb0;color:#2b6cb0;background:#fff;border-radius:5px;cursor:pointer}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12.5px}
dialog{border:1px solid #d0d0d5;border-radius:10px;max-width:900px;width:94vw;padding:0 16px 14px}
dialog::backdrop{background:rgba(0,0,0,.4)}
.dhd{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 0;position:sticky;top:0;background:#fff}
dialog table.kv{margin-bottom:10px} dialog h3{font-size:14px;margin:14px 0 6px}
@media print{.tools{display:none} th{position:static} button.esia{display:none}}
"""


def build(domains: list[dict], case_id: str = "", case_title: str = "", checks: list[Check] | None = None) -> str:
    """HTML-выгрузка по доменам. checks — проверки дела (если домены загружены после проверки заявления);
    без них считаются проверки, которым документы не нужны («Домены в manager», «Администратор у доменов один»)."""
    items = [d if isinstance(d, dict) else d.to_dict() for d in domains]
    items = sort_by_problem(items)
    counts = Counter(d.get("problem", "") for d in items)
    titles = dict(PROBLEMS)
    if checks:
        dom_checks = [c for c in checks if c.name in CHECK_NAMES]
    else:
        dom_checks = summary_checks([DomainInfo.from_dict(d) for d in items])
    found = [d for d in items if d.get("status") == FOUND]
    sim = similar_known(items)
    rows, group = [], None
    for d in items:
        if d.get("problem") != group:
            group = d.get("problem")
            rows.append(f'<tr class="grp" id="g-{_e(group or "")}"><td colspan="6">{_e(titles.get(group, group or ""))} — '
                        f'<span class="cnt" data-total="{counts[group]}">{counts[group]}</span></td></tr>')
        rows.append(_domain_row(d, sim))
    summary = " · ".join(f'<a href="#g-{code}">{_e(title)}: <b>{counts[code]}</b></a>'
                         for code, title in PROBLEMS if counts.get(code))
    worst = next((code for code, _ in PROBLEMS if counts.get(code)), "ok")
    vcls = {"free": "fail", "mismatch": "fail", "not_found": "fail", "error": "review", "review": "review",
            "warn": "warn", "info": "info", "unchecked": "warn", "ok": "ok"}[worst]
    nts = notes(items)
    head = f"Домены в manager{' — ' + _e(case_id) if case_id else ''}"
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{head} ({len(items)})</title>
<style>{_CSS}</style></head><body>
<h1>{head}</h1>
{f'<div class="src">{_e(case_title)}</div>' if case_title else ''}
<div class="src">Сформировано {datetime.now():%d.%m.%Y %H:%M}. Доменов: {len(items)}. Данные Sd и S — из manager через расширение
«PySimpleManager»; все данные обработаны локально.</div>
<div class="verdict {vcls}">{summary or 'нет доменов'}</div>
<h2>Сверка</h2>
{_checks_table(dom_checks) if dom_checks else '<p class=src>нет</p>'}
{('<h2>Администраторы найденных доменов</h2>' + _admins_table(found)) if found else ''}
{('<h2>К сведению</h2><ul class="notes">' + ''.join(f'<li>{n}</li>' for n in nts) + '</ul>') if nts else ''}
<h2>Домены</h2>
<div class="tools"><input type="search" id="q" placeholder="Фильтр: домен, ФИО, паспорт, ИНН, e-mail">
<span class="src" id="shown"></span></div>
<table id="dom"><tr><th>Домен</th><th>manager</th><th>Администратор (Sd)</th><th>Сверка</th><th>ЕСИА</th><th>Прочее</th></tr>
{''.join(rows)}</table>
<dialog id="esiaDlg"><div class="dhd"><b id="esiaTitle"></b><button onclick="this.closest('dialog').close()">Закрыть</button></div>
<div id="esiaBody"></div></dialog>
<script type="application/json" id="esia-data">{_esia_data(items)}</script>
<script>{_JS}{_ESIA_JS}</script>
</body></html>
"""


def filename(case_id: str = "") -> str:
    base = re.sub(r'[\\/:*?"<>|\s]+', "_", case_id).strip("_")[:40]
    return f"domains_{base + '_' if base else ''}{datetime.now():%Y%m%d_%H%M}.html"
