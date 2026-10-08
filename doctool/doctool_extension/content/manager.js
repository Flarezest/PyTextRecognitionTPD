/* doctool — чтение данных из manager.reg.ru и заполнение базовой анкеты.
 *
 * Скрипт внедряется расширением во вкладку https://manager.reg.ru/ (изолированный мир расширения).
 * Страницы manager загружаются отсюда обычными GET-запросами того же сайта — с входом оператора,
 * как при открытии ссылок вручную. Скрипт ничего не нажимает и ничего не отправляет в manager.
 *
 * Порядок для одного домена (lookup):
 *   1) /bill/bills?searchstring=<домен>&from_head=1&_csrf=<токен>  — строка домена, service_id;
 *   2) /tech/srv_details?service_id=<id>                           — Sd: данные администратора;
 *   3) /tech/service_details?service_id=<id>                       — S: provider, user_id, dname;
 *   4) (0.6.0, физлица) ссылка «Идентификация через Госуслуги» со страницы Sd — state и ссылка на JSON
 *      ЕСИА. Сам JSON лежит на другом сайте (identity.reg.ru) — его загружает background.js.
 *
 * Аккаунт (account, 0.6.0): /manager/user_details?user_id=N (базовая анкета — скрытый блок страницы,
 * логин, обслуживающая организация) и /user/N/runic_details (тип анкеты, значения формы).
 * По логину/e-mail номер аккаунта находится через /user/<логин>/runic_details.
 *
 * Заполнение (fillRunic, 0.6.0): только во вкладке «Базовая анкета пользователя #N», которую видит оператор:
 * вписывает значения в поля формы физлица и вызывает события страницы (input/change/blur), чтобы она
 * подсветила изменённые поля и пересчитала English name. Кнопку «Сохранить» не нажимает.
 */
(() => {
  const VERSION = '0.6.0';
  if (window.__doctoolManager && window.__doctoolManager.version === VERSION) return;

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
  // Обычная страница manager: «старая» (#content) или новая на Bootstrap (nav.navbar, напр. базовая анкета).
  const isManagerPage = doc => !!doc.querySelector('meta[name="_csrf"]')
    && !!(doc.querySelector('#content') || doc.querySelector('nav.navbar'));
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
    // ссылки со страницы Sd: «Данные персоны услуги» и «Идентификация через Госуслуги» (у физлица — ?user_id=,
    // у персоны/юрлица — ?person_id=; идентификация относится к администратору домена, а не к аккаунту)
    const href = sel => { const a = doc.querySelector(sel); return a ? absUrl(a.getAttribute('href')) : ''; };
    const links = {
      person: href('a[href*="/manager/user_details?"], a[href*="/manager/person?"]'),
      esia: href('a[href*="esia_identifications"]'),
    };
    return { group: pickGroup(groups), fields, trustee: trustee ? trustee[1] : '', status: status ? status[1] : '', links };
  }

  const absUrl = h => { try { return h ? new URL(h, location.origin).href : ''; } catch (e) { return ''; } };

  // ------------------------------------------------------------------ идентификация через Госуслуги (ЕСИА)

  // Таблица /manager/esia_identifications?…: колонки identity_id, object, object_id, state, file, comment,
  // creation_date, processed_date, login_processed, doc_id, esia_state_id, reason, action, params.
  function parseEsiaList(doc) {
    const table = [...doc.querySelectorAll('table')].find(t => [...t.querySelectorAll('th')].some(th => text(th) === 'state'));
    if (!table) {
      if (!isManagerPage(doc)) throw new LookupError('bad_page', 'Страница идентификации через Госуслуги не открылась.');
      return { rows: [], latest: null };
    }
    const heads = [...table.querySelectorAll('th')].map(th => text(th));
    const rows = [];
    for (const tr of table.querySelectorAll('tr')) {
      const cells = [...tr.querySelectorAll('td')];
      if (cells.length < heads.length / 2) continue;
      const r = {};
      heads.forEach((h, i) => { if (cells[i]) r[h] = text(cells[i]); });
      const fileCell = cells[heads.indexOf('file')];
      const a = fileCell && fileCell.querySelector('a[href]');
      r.file_url = a ? absUrl(a.getAttribute('href')) : '';
      delete r.file;
      if (r.state !== undefined) rows.push(r);
    }
    // последняя попытка — по дате создания (формат «ГГГГ-ММ-ДД чч:мм:сс» сортируется как строка)
    const latest = rows.length ? [...rows].sort((a, b) => (b.creation_date || '').localeCompare(a.creation_date || ''))[0] : null;
    return { rows, latest };
  }

  // ------------------------------------------------------------------ аккаунт: данные пользователя и базовая анкета

  // /manager/user_details?user_id=N. Базовая анкета уже есть в HTML (блок .user_contacts_block, скрыт стилем),
  // кнопка «Базовая анкета пользователя» только показывает его.
  function parseUserDetails(doc) {
    const h = text(doc.querySelector('#content h2'));
    const m = /#\s*(\d+)/.exec(h) || /user_id=(\d+)/.exec((doc.querySelector('a[href*="user_details?user_id="]') || {}).href || '');
    const ba = {};
    const block = doc.querySelector('.user_contacts_block');
    if (block) {
      for (const tr of block.querySelectorAll('tr')) {
        const c = tr.querySelectorAll('td');
        if (c.length < 2) continue;
        const k = text(c[0]);
        if (/^[a-z_]+\.[a-z0-9_]+$/i.test(k)) ba[k] = (c[1].textContent || '').replace(/[ \t]+/g, ' ').trim();
      }
    }
    const det = doc.querySelector('#user_details');
    const row = title => {
      if (!det) return null;
      return [...det.querySelectorAll('tr')].find(tr => { const th = tr.querySelector('th'); return th && text(th).startsWith(title); }) || null;
    };
    const first = det && det.querySelector('tr td');
    const login = first ? text(first.querySelector('b')) : '';
    const statuses = first ? [...first.querySelectorAll('span')].map(s => text(s)).filter(Boolean) : [];
    const org = row('Обслуживающая организация');
    const orgText = org ? text(org.querySelector('td')) : '';
    const orgCur = /текущая:\s*(.+)$/i.exec(orgText);
    const companyId = org ? (org.querySelector('input[name="company_id"]') || {}).value || '' : '';
    const contypes = row('contypes');
    const ent = doc.querySelector('#user_details input[name="is_entrepreneur"]');
    const links = {};
    for (const [k, sel] of [['runic', 'a[href*="/runic_details"]'], ['esia', 'a[href*="esia_identifications"]'],
                            ['persons', 'a[href*="/manager/persons?"]'], ['history', 'a[href*="history_changes_of_base_form"]']]) {
      const a = doc.querySelector(sel);
      if (a) links[k] = absUrl(a.getAttribute('href'));
    }
    return {
      user_id: m ? m[1] : '', login, statuses, ba,
      servicing_org: orgCur ? orgCur[1].trim() : orgText, servicing_org_id: companyId,
      contypes: contypes ? text(contypes.querySelector('td')) : '',
      is_entrepreneur: !!(ent && ent.checked), links,
    };
  }

  // Поля формы физлица на странице базовой анкеты (/user/N/runic_details), которые читает и заполняет doctool.
  // Гражданство (country), страна почтового адреса, область, SMS-безопасность, факс, English name
  // и флажок «Внесены данные нового владельца» doctool не заполняет.
  const RUNIC_FIELDS = ['person_r_surname', 'person_r_name', 'person_r_patronimic', 'person', 'passport_number',
    'passport_date', 'passport_place', 'birth_date', 'country', 'p_addr_country', 'p_addr_zip', 'p_addr_area',
    'p_addr_city', 'p_addr_addr', 'p_addr_recipient', 'phone', 'sms_security_number', 'fax', 'e_mail'];
  const RUNIC_FILL = ['person_r_surname', 'person_r_name', 'person_r_patronimic', 'passport_number', 'passport_date',
    'passport_place', 'birth_date', 'p_addr_zip', 'p_addr_city', 'p_addr_addr', 'p_addr_recipient', 'phone', 'e_mail'];
  const TYPE_RU = { pp: 'физлицо', ip: 'ИП', org: 'юрлицо' };

  function parseRunic(doc) {
    const h = text(doc.querySelector('h3'));
    const m = /#\s*(\d+)/.exec(h);
    const checked = doc.querySelector('input[name="type"]:checked') || doc.querySelector('input[name="type"][checked]');
    const form = doc.querySelector('form#ru_pp_contacts');
    const values = {};
    if (form) {
      for (const k of RUNIC_FIELDS) {
        const el = form.querySelector(`[name="${k}"]`);
        if (el) values[k] = (el.value || '').trim();
      }
    }
    return { user_id: m ? m[1] : '', type: checked ? checked.value : '', has_pp_form: !!form, values };
  }

  // Заполнение формы физлица на открытой странице базовой анкеты (вкладка оператора). fields — {имя поля: значение};
  // пустые значения пропускаются (кроме restore — возврата прежних значений). «Сохранить» не нажимается.
  function fillRunic(expectedId, fields, opts) {
    opts = opts || {};
    const info = parseRunic(document);
    if (!info.user_id) return { ok: false, error: { code: 'bad_page', message: 'Во вкладке не страница базовой анкеты.' } };
    if (String(info.user_id) !== String(expectedId)) {
      return { ok: false, error: { code: 'wrong_account', message: `Во вкладке базовая анкета аккаунта #${info.user_id}, а нужен #${expectedId}.` } };
    }
    if (info.type !== 'pp') {
      const who = TYPE_RU[info.type] || (info.type || 'неизвестный тип');
      return { ok: false, error: { code: 'not_person', type: info.type,
        message: `Аккаунт #${expectedId} оформлен на ${who === 'юрлицо' ? 'юрлицо' : who === 'ИП' ? 'ИП' : who}: автозаполнение пока только для физлиц. Вкладка базовой анкеты открыта.` } };
    }
    const form = document.querySelector('form#ru_pp_contacts');
    if (!form) return { ok: false, error: { code: 'bad_page', message: 'На странице нет формы физлица (form#ru_pp_contacts).' } };
    const changed = [], same = [], missing = [];
    const fire = (el, type) => el.dispatchEvent(new Event(type, { bubbles: type !== 'blur' }));
    fields = fields || {};
    for (const name of RUNIC_FILL) {             // в порядке полей на странице
      if (!(name in fields)) continue;
      const value = (fields[name] ?? '').toString();
      if (!value && !opts.restore) continue;
      const el = form.querySelector(`[name="${name}"]`);
      if (!el) { missing.push(name); continue; }
      const old = el.value;
      const label = text((el.previousElementSibling && el.previousElementSibling.matches('.input-group-addon'))
        ? el.previousElementSibling : form.querySelector(`.input-group-addon.${name}`)) || name;
      if (old === value) { same.push({ name, label, value }); continue; }
      try { el.focus({ preventScroll: true }); } catch (e) { /* ничего */ }
      el.value = value;
      fire(el, 'input');
      fire(el, 'change');      // страница помечает поле как изменённое (класс unsaved)
      try { el.blur(); } catch (e) { /* ничего */ }
      fire(el, 'blur');        // у ФИО — пересчёт English name (translit_pers_org)
      el.style.outline = opts.restore ? '' : '2px solid #f0ad4e';
      changed.push({ name, label, old, new: value });
    }
    return { ok: true, data: { user_id: info.user_id, type: info.type, changed, same, missing,
                               values: parseRunic(document).values } };
  }

  async function account(reqId, query) {
    const progress = (step, message) => {
      try { chrome.runtime.sendMessage({ type: 'progress', reqId, step, message }); } catch (e) { /* вне расширения */ }
    };
    try {
      let uid = String((query && query.user_id) || '').trim();
      const login = String((query && query.login) || '').trim();
      if (!uid && login) {
        progress('account', `${login}: поиск аккаунта по логину`);
        const r = parseRunic(await getDoc(`/user/${encodeURIComponent(login)}/runic_details`));
        if (!r.user_id) throw new LookupError('not_found', `Аккаунт с логином «${login}» не найден в manager.`);
        uid = r.user_id;
      }
      if (!/^\d+$/.test(uid)) throw new LookupError('bad_request', 'Не указан номер аккаунта.');
      progress('user_details', `аккаунт #${uid}: данные пользователя`);
      const det = parseUserDetails(await getDoc(`/manager/user_details?user_id=${uid}`));
      if (!det.user_id && !det.login) throw new LookupError('not_found', `Аккаунт #${uid} не найден в manager.`);
      progress('runic', `аккаунт #${uid}: базовая анкета`);
      const runic = parseRunic(await getDoc(`/user/${uid}/runic_details`));
      return {
        ok: true,
        data: {
          ...det, user_id: det.user_id || uid, runic,
          urls: {
            user_details: `${location.origin}/manager/user_details?user_id=${uid}`,
            runic: `${location.origin}/user/${uid}/runic_details`,
            esia: det.links.esia || `${location.origin}/manager/esia_identifications?user_id=${uid}`,
          },
        },
      };
    } catch (e) {
      if (e instanceof LookupError) return { ok: false, error: { code: e.code, message: e.message, ...e.extra } };
      return { ok: false, error: { code: 'error', message: String((e && e.message) || e) } };
    }
  }

  // Страница идентификации общая у доменов одной персоны — в пределах одной загрузки читается один раз.
  const esiaCache = new Map();   // url → {t, value}
  async function esiaFor(url, task) {
    const key = `${task || ''}|${url}`;
    const hit = esiaCache.get(key);
    if (hit && Date.now() - hit.t < 10 * 60 * 1000) return hit.value;
    let value;
    try {
      const { rows, latest } = parseEsiaList(await getDoc(url.replace(location.origin, '')));
      value = latest
        ? { url, count: rows.length, state: latest.state || '', file_url: latest.file_url, latest,
            history: rows.map(r => ({ state: r.state, creation_date: r.creation_date, processed_date: r.processed_date,
                                      action: r.action, reason: r.reason, comment: r.comment })) }
        : { url, count: 0, state: '' };
    } catch (e) {
      if (e instanceof LookupError && e.code === 'not_logged_in') throw e;
      value = { url, error: String((e && e.message) || e) };
    }
    if (esiaCache.size > 500) esiaCache.clear();
    esiaCache.set(key, { t: Date.now(), value });
    return value;
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

  async function lookup(reqId, domain, ascii, opts) {
    opts = opts || {};
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
      // ЕСИА — только для физлиц (ru_pp): по ссылке со страницы Sd этого домена
      let esia = null;
      if (opts.esia !== false && sd.group === 'ru_pp' && sd.links && sd.links.esia) {
        progress('esia', `${domain}: идентификация через Госуслуги`);
        esia = await esiaFor(sd.links.esia, opts.task);
      }
      return {
        ok: true,
        data: {
          domain, service_id: found.service_id, account: s.user_id || found.user_id, bill_owner: found.bill_owner,
          bill_state: found.state, sd, s, esia,
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

  window.__doctoolManager = {
    version: VERSION, lookup, account, fillRunic, parseBills, pickService, parseSd, parseS, parseEsiaList,
    parseUserDetails, parseRunic, isManagerPage,
  };
})();
