/* doctool — чтение данных домена из manager.reg.ru.
 *
 * Скрипт внедряется расширением во вкладку https://manager.reg.ru/ (изолированный мир расширения).
 * Страницы manager загружаются отсюда обычными GET-запросами того же сайта — с входом оператора,
 * как при открытии ссылок вручную. Скрипт ничего не нажимает и ничего не отправляет в manager.
 *
 * Порядок для одного домена:
 *   1) /bill/bills?searchstring=<домен>&from_head=1&_csrf=<токен>  — строка домена, service_id;
 *   2) /tech/srv_details?service_id=<id>                           — Sd: данные администратора;
 *   3) /tech/service_details?service_id=<id>                       — S: provider, user_id, dname.
 */
(() => {
  if (window.__doctoolManager) return;

  // Поля Sd, которые передаются в doctool. authinfo, телефоны, адреса и служебные поля не передаются.
  // E-mail администратора передаётся (с 0.5.0): e_mail — зоны .RU/.РФ/.SU, o_email — остальные зоны.
  const SD_FIELDS = [
    // физлицо (.RU/.РФ/.SU)
    'person_r_surname', 'person_r_name', 'person_r_patronimic', 'person_surname', 'person_name', 'person_patronimic',
    'birth_date', 'passport_series', 'passport_number_short', 'passport_date', 'passport_place', 'passport_place_id',
    'passport_expiration_date', 'is_entrepreneur',
    // юрлицо (.RU/.РФ/.SU)
    'org_r', 'org', 'kpp',
    // администратор (владелец) в остальных зонах — группа o_* (o_deti_org, o_gtld…)
    'o_company', 'o_company_ru', 'o_first_name', 'o_first_name_ru', 'o_last_name', 'o_last_name_ru',
    'o_patronimic', 'o_patronimic_ru', 'o_country_code', 'o_birth_date',
    // контактный e-mail
    'e_mail', 'o_email',
    // общее
    'code', 'country', 'last_admin_change', 'date_update_details', 'matches_user_contacts',
  ];
  // Группа контактов администратора: ru_pp/ru_org; иначе o_* (владелец); иначе самая заполненная.
  const pickGroup = groups => {
    const names = Object.keys(groups);
    return names.find(g => g === 'ru_pp' || g === 'ru_org') || names.find(g => /^o_/.test(g))
      || (Object.entries(groups).sort((a, b) => b[1] - a[1])[0] || [''])[0];
  };
  const S_FIELDS = ['provider', 'dname', 'contype', 'state', 'user_id', 'expiration_date', 'creation_date', 'trans_in_from'];

  class LookupError extends Error {
    constructor(code, message, extra) { super(message); this.code = code; this.extra = extra || {}; }
  }

  let lastToken = '';
  const tokenOf = doc => (doc.querySelector('meta[name="_csrf"]') || {}).content || '';
  const isManagerPage = doc => !!doc.querySelector('meta[name="_csrf"]') && !!doc.querySelector('#content');
  const norm = s => (s || '').toString().trim().toLowerCase().replace(/\.$/, '');
  const text = el => ((el && el.textContent) || '').replace(/\s+/g, ' ').trim();

  async function getDoc(path) {
    let r;
    try {
      r = await fetch(path, { credentials: 'include', cache: 'no-store' });
    } catch (e) {
      throw new LookupError('network', 'manager не отвечает: ' + (e && e.message || e));
    }
    if (r.status === 401 || r.status === 403) {
      throw new LookupError('not_logged_in', `manager требует вход (HTTP ${r.status}). Войдите в manager в Chrome и повторите.`);
    }
    if (!r.ok) throw new LookupError('http', `manager вернул HTTP ${r.status} на ${path}`);
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    if (!isManagerPage(doc)) {
      throw new LookupError('not_logged_in', 'Вместо страницы manager открылась другая страница — вероятно, нужен вход.');
    }
    lastToken = tokenOf(doc) || lastToken;
    return doc;
  }

  // ------------------------------------------------------------------ страница счетов

  function parseBills(doc) {
    const m = /Всего позиций:\s*(\d+)/.exec(doc.body ? doc.body.textContent : '');
    const total = m ? +m[1] : null;
    const form = doc.querySelector('form[name="billlist"]');
    const table = form && form.querySelector('table');
    if (!table) return { total: total || 0, rows: [] };
    const header = [...table.rows].find(tr => tr.querySelector('th') && /Название/.test(tr.textContent));
    const heads = header ? [...header.cells].map(c => text(c)) : [];
    const col = re => heads.findIndex(h => re.test(h));
    const iName = col(/^Название/), iSt = col(/^St$/), iOwner = col(/^Владелец/);
    const rows = [];
    for (const tr of table.rows) {
      const sid = tr.querySelector('input[name^="service_id_"]');
      if (!sid) continue;
      const n = sid.name.slice('service_id_'.length);
      const val = k => (tr.querySelector(`input[name="${k}_${n}"]`) || {}).value || '';
      const titled = tr.querySelector('[data-service-title]');
      const cells = tr.cells;
      const owner = iOwner >= 0 && cells[iOwner] ? cells[iOwner].querySelector('a[href*="user_details"]') : null;
      rows.push({
        service_id: sid.value, user_id: val('user_id'), bill_id: val('bill_id'), pos_id: val('pos_id'),
        title: titled ? titled.getAttribute('data-service-title') : '',
        name_cell: iName >= 0 ? text(cells[iName]) : '',
        state: iSt >= 0 ? text(cells[iSt]) : '',
        bill_owner: text(owner),
      });
    }
    return { total: total === null ? rows.length : total, rows };
  }

  function matchTitle(title, names) {
    const t = norm(title);
    if (!t) return false;
    if (names.includes(t)) return true;
    const m = /^(?:регистрация|перенос|продление|трансфер)\s+домена\s+(\S+)$/i.exec(title.trim());
    return !!(m && names.includes(norm(m[1])));
  }

  function pickService(rows, names) {
    const cand = rows.filter(r => matchTitle(r.title, names) || (!r.title && names.includes(norm(r.name_cell))));
    const byId = new Map();
    for (const r of cand) {
      const prev = byId.get(r.service_id);
      if (!prev) byId.set(r.service_id, { ...r });
      else if (r.state === 'A') prev.state = 'A';
    }
    const services = [...byId.values()];
    if (!services.length) return {};
    const active = services.filter(s => s.state === 'A');
    if (active.length === 1) return { service: active[0] };
    if (!active.length && services.length === 1) return { service: services[0] };
    return {
      error: 'ambiguous',
      message: `По домену найдено несколько услуг: ${services.map(s => `${s.service_id} (${s.state || '?'})`).join(', ')}`,
      candidates: services,
    };
  }

  // ------------------------------------------------------------------ Sd и S

  function parseSd(doc) {
    const form = doc.querySelector('form#srv_details');
    if (!form) throw new LookupError('bad_page', 'На странице Sd нет таблицы полей (form#srv_details).');
    const fields = {}, groups = {};
    for (const tr of form.querySelectorAll('tr')) {
      const c = tr.cells;
      if (c.length < 3) continue;
      const el = c[2].querySelector('input[name^="value__"], textarea[name^="value__"]');
      if (!el) continue;
      let grp = text(c[0]), fld = text(c[1]);
      const m = /^value__(.*?)____(.+)$/.exec(el.name);
      if (m) { grp = grp || m[1]; fld = fld || m[2]; }
      if (grp === 'authinfo' || fld === 'authinfo') continue;
      const val = (el.value || '').trim();
      if (grp && val) groups[grp] = (groups[grp] || 0) + 1;
      if (!SD_FIELDS.includes(fld)) continue;
      if (fld in fields && fields[fld] !== '') continue;
      fields[fld] = val;
    }
    const body = text(doc.querySelector('#content'));
    const trustee = /Trustee:\s*(Да|Нет)/i.exec(body);
    const status = /Статус услуги:\s*(.+?\([A-Z]+\))/.exec(body);
    return { group: pickGroup(groups), fields, trustee: trustee ? trustee[1] : '', status: status ? status[1] : '' };
  }

  function parseS(doc) {
    const form = doc.querySelector('form#service_details');
    if (!form) throw new LookupError('bad_page', 'На странице S нет таблицы свойств (form#service_details).');
    const out = {};
    for (const k of S_FIELDS) {
      const el = form.querySelector(`[name="${k}"]`);
      if (el) out[k] = (el.value || '').trim();
    }
    return out;
  }

  // ------------------------------------------------------------------ один домен целиком

  async function lookup(reqId, domain, ascii) {
    const names = [...new Set([norm(domain), norm(ascii)].filter(Boolean))];
    const progress = (step, message) => {
      try { chrome.runtime.sendMessage({ type: 'progress', reqId, step, message }); } catch (e) { /* вне расширения */ }
    };
    try {
      if (!lastToken) lastToken = tokenOf(document);
      let found = null;
      const searched = [];
      for (const q of names) {
        progress('bills', `${q}: поиск в счетах`);
        const url = `/bill/bills?searchstring=${encodeURIComponent(q)}&from_head=1&_csrf=${encodeURIComponent(lastToken)}`;
        const bills = parseBills(await getDoc(url));
        searched.push({ query: q, total: bills.total, rows: bills.rows.length });
        const pick = pickService(bills.rows, names);
        if (pick.error) throw new LookupError(pick.error, pick.message, { candidates: pick.candidates });
        if (pick.service) { found = { ...pick.service, total: bills.total, query: q }; break; }
      }
      if (!found) {
        const s = searched.map(x => `«${x.query}» — позиций ${x.total}`).join('; ');
        const more = searched.some(x => x.total > x.rows) ? ' (просмотрена только первая страница выдачи)' : '';
        return { ok: false, error: { code: 'not_found', message: `Домен не найден в счетах manager: ${s}${more}`, searched } };
      }
      progress('sd', `${domain}: Sd (service_id ${found.service_id})`);
      const sd = parseSd(await getDoc(`/tech/srv_details?service_id=${encodeURIComponent(found.service_id)}`));
      progress('s', `${domain}: S (service_id ${found.service_id})`);
      const s = parseS(await getDoc(`/tech/service_details?service_id=${encodeURIComponent(found.service_id)}`));
      if (s.dname && !names.includes(norm(s.dname))) {
        throw new LookupError('mismatch', `Страница S относится к другому домену: ${s.dname}`);
      }
      return {
        ok: true,
        data: {
          domain, service_id: found.service_id, account: s.user_id || found.user_id, bill_owner: found.bill_owner,
          bill_state: found.state, sd, s,
          urls: {
            bills: `${location.origin}/bill/bills?searchstring=${encodeURIComponent(found.query)}`,
            sd: `${location.origin}/tech/srv_details?service_id=${found.service_id}`,
            s: `${location.origin}/tech/service_details?service_id=${found.service_id}`,
          },
        },
      };
    } catch (e) {
      if (e instanceof LookupError) return { ok: false, error: { code: e.code, message: e.message, ...e.extra } };
      return { ok: false, error: { code: 'error', message: String((e && e.message) || e) } };
    }
  }

  window.__doctoolManager = { version: '0.5.0', lookup, parseBills, pickService, parseSd, parseS, isManagerPage };
})();
