/* doctool — manager: фоновая часть расширения (service worker).
 *
 * 1) Держит WebSocket-соединение с doctool на этом компьютере: ws://127.0.0.1:<порт>/ext.
 * 2) Получает команды lookup (данные домена) и выполняет их по очереди в отдельной фоновой вкладке manager.
 * 3) Если manager просит вход (окно «Войти»), показывает эту вкладку и сообщает doctool.
 *
 * Расширение только читает страницы manager. Кнопки и формы manager оно не трогает.
 */
const VERSION = chrome.runtime.getManifest().version;
const MANAGER = 'https://manager.reg.ru/';
const DEFAULTS = { port: 8765, authWaitSec: 180, pauseMs: 300, closeTabAfterSec: 60 };

let ws = null;
let reconnectTimer = null;
let backoff = 2000;
let keepalive = null;
let managerTabId = null;
let createdTab = false;
let currentReq = null;
let closeTimer = null;
let queue = Promise.resolve();
const authHooks = new Map();   // tabId → функция «ждать дольше: идёт вход»
const state = { connected: false, port: DEFAULTS.port, busy: false, events: [], lastError: '' };

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

function note(msg) {
  state.events.unshift(`${new Date().toLocaleTimeString()}  ${msg}`);
  state.events.length = Math.min(state.events.length, 40);
}

function badge() {
  const [text, color] = !state.connected ? ['!', '#9b1c1c'] : state.busy ? ['…', '#2b6cb0'] : ['', '#1e6b3a'];
  chrome.action.setBadgeText({ text }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color }).catch(() => {});
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// ------------------------------------------------------------------ связь с doctool

async function connect() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  clearTimeout(reconnectTimer);
  const { port } = await settings();
  state.port = port;
  let sock;
  try {
    sock = new WebSocket(`ws://127.0.0.1:${port}/ext`);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws = sock;
  sock.onopen = () => {
    backoff = 2000;
    state.connected = true;
    state.lastError = '';
    send({ type: 'hello', version: VERSION });
    note(`Подключено к doctool (порт ${port})`);
    clearInterval(keepalive);
    keepalive = setInterval(() => send({ type: 'ping' }), 20000);   // не даёт service worker уснуть
    badge();
  };
  sock.onmessage = e => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === 'request') enqueue(m);
  };
  sock.onclose = () => {
    if (ws === sock) ws = null;
    clearInterval(keepalive);
    if (state.connected) note('Связь с doctool потеряна');
    state.connected = false;
    badge();
    scheduleReconnect();
  };
  sock.onerror = () => { state.lastError = `doctool не отвечает на порту ${port}`; };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, backoff);
  backoff = Math.min(backoff * 2, 15000);
}

// ------------------------------------------------------------------ очередь команд

function enqueue(m) {
  queue = queue.then(() => handle(m)).catch(e => console.error('doctool:', e));
}

async function handle(m) {
  state.busy = true;
  currentReq = m.id;
  clearTimeout(closeTimer);
  badge();
  let reply;
  try {
    if (m.cmd === 'lookup') reply = await lookupDomain(m.id, m.params || {});
    else if (m.cmd === 'check_manager') reply = await checkManager();
    else reply = { ok: false, error: { code: 'unknown_cmd', message: `Неизвестная команда: ${m.cmd}` } };
  } catch (e) {
    reply = { ok: false, error: { code: e.code || 'error', message: e.message || String(e) } };
  }
  send({ type: 'result', id: m.id, ...reply });
  currentReq = null;
  state.busy = false;
  badge();
  scheduleTabClose();
}

function fail(code, message) {
  const e = new Error(message);
  e.code = code;
  return e;
}

async function runLookup(tabId, reqId, domain, ascii) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ['content/manager.js'] });
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (id, d, a) => window.__doctoolManager.lookup(id, d, a),
    args: [reqId, domain, ascii || domain],
  });
  return (res && res.result) || { ok: false, error: { code: 'error', message: 'Скрипт во вкладке manager не вернул результат' } };
}

async function lookupDomain(reqId, { domain, ascii }) {
  if (!domain) throw fail('bad_request', 'Не указан домен');
  note(`Запрос: ${domain}`);
  let tabId = await ensureManagerTab();
  let out = await runLookup(tabId, reqId, domain, ascii);
  if (!out.ok && out.error && out.error.code === 'not_logged_in') {
    // вход истёк посреди работы: обновляем вкладку (появится окно «Войти»), ждём вход и пробуем ещё раз
    await chrome.tabs.reload(tabId).catch(() => {});
    await waitLoaded(tabId, 30000, 30000);
    await waitForLogin(tabId);
    out = await runLookup(tabId, reqId, domain, ascii);
  }
  note(`${domain}: ${out.ok ? 'данные получены' : out.error.message}`);
  const { pauseMs } = await settings();
  await sleep(pauseMs);
  return out;
}

async function checkManager() {
  const tabId = await ensureManagerTab();
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const h = document.querySelector('#content h3');
      return { login: h ? h.textContent.replace(/\s*\[x\]\s*$/, '').replace(/[\[\]]/g, '').trim() : '' };
    },
  });
  return { ok: true, data: { manager: 'ok', login: (res && res.result && res.result.login) || '' } };
}

// ------------------------------------------------------------------ вкладка manager

async function pageOk(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => !!document.querySelector('meta[name="_csrf"]') && !!document.querySelector('#content'),
    });
    return !!(r && r.result);
  } catch {
    return false;   // страница ошибки Chrome, 401 и т. п.
  }
}

function waitLoaded(tabId, ms, authMs) {
  return new Promise(resolve => {
    let timer;
    const done = v => {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpd);
      chrome.tabs.onRemoved.removeListener(onRem);
      authHooks.delete(tabId);
      resolve(v);
    };
    const onUpd = (id, info) => { if (id === tabId && info.status === 'complete') done(true); };
    const onRem = id => { if (id === tabId) done(false); };
    chrome.tabs.onUpdated.addListener(onUpd);
    chrome.tabs.onRemoved.addListener(onRem);
    authHooks.set(tabId, () => { clearTimeout(timer); timer = setTimeout(() => done(false), authMs); });
    timer = setTimeout(() => done(false), ms);
    chrome.tabs.get(tabId).then(t => {
      if (t.status === 'complete' && t.url && t.url.startsWith(MANAGER)) done(true);
    }).catch(() => done(false));
  });
}

async function ensureManagerTab() {
  const cfg = await settings();
  if (managerTabId === null) {
    const saved = await chrome.storage.session.get(['managerTabId', 'createdTab']);
    managerTabId = saved.managerTabId ?? null;
    createdTab = !!saved.createdTab;
  }
  if (managerTabId !== null) {
    try {
      const t = await chrome.tabs.get(managerTabId);
      if (t.url && t.url.startsWith(MANAGER)) {
        if (t.status !== 'complete') await waitLoaded(t.id, 30000, cfg.authWaitSec * 1000);
        await waitForLogin(t.id);
        return t.id;
      }
    } catch { /* вкладку закрыли */ }
    managerTabId = null;
  }
  note('Открываю вкладку manager');
  const tab = await chrome.tabs.create({ url: MANAGER, active: false });
  managerTabId = tab.id;
  createdTab = true;
  await chrome.storage.session.set({ managerTabId, createdTab });
  await waitLoaded(tab.id, 30000, cfg.authWaitSec * 1000);
  await waitForLogin(tab.id);
  return tab.id;
}

// Страница manager не открылась (окно «Войти», 401, страница входа): показываем вкладку оператору
// и ждём, пока в ней появится обычная страница manager.
async function waitForLogin(tabId) {
  if (await pageOk(tabId)) return;
  const { authWaitSec } = await settings();
  const msg = 'manager просит вход: введите логин и пароль во вкладке manager в Chrome '
    + '(если окна «Войти» нет — обновите вкладку, F5)';
  note(msg);
  if (currentReq) send({ type: 'progress', id: currentReq, step: 'auth_required', message: msg });
  try {
    const t = await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(t.windowId, { focused: true });
  } catch { /* вкладку закрыли */ }
  const until = Date.now() + authWaitSec * 1000;
  while (Date.now() < until) {
    await sleep(1000);
    try { await chrome.tabs.get(tabId); } catch { break; }
    if (await pageOk(tabId)) {
      note('Вход в manager выполнен');
      if (currentReq) send({ type: 'progress', id: currentReq, step: 'auth_ok', message: 'вход в manager выполнен, продолжаю' });
      return;
    }
  }
  throw fail('not_logged_in', `Вход в manager не выполнен за ${authWaitSec} с. Войдите в manager в Chrome и повторите.`);
}

async function scheduleTabClose() {
  clearTimeout(closeTimer);
  const { closeTabAfterSec } = await settings();
  if (!closeTabAfterSec || !createdTab || managerTabId === null) return;
  closeTimer = setTimeout(async () => {
    if (state.busy || managerTabId === null) return;
    try {
      const t = await chrome.tabs.get(managerTabId);
      if (!t.active) await chrome.tabs.remove(managerTabId);   // вкладку, которую смотрит оператор, не закрываем
    } catch { /* уже закрыта */ }
    managerTabId = null;
    createdTab = false;
    chrome.storage.session.set({ managerTabId: null, createdTab: false }).catch(() => {});
  }, closeTabAfterSec * 1000);
}

// Окно «Войти» (HTTP-авторизация manager): показываем вкладку и сообщаем doctool.
chrome.webRequest.onAuthRequired.addListener(details => {
  if (details.tabId < 0 || details.tabId !== managerTabId) return;
  note('manager показал окно «Войти»');
  chrome.tabs.update(details.tabId, { active: true })
    .then(t => chrome.windows.update(t.windowId, { focused: true }))
    .catch(() => {});
  const hook = authHooks.get(details.tabId);
  if (hook) hook();
}, { urls: ['https://manager.reg.ru/*'] });

// ------------------------------------------------------------------ сообщения от вкладки и окна расширения

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'progress') {
    send({ type: 'progress', id: msg.reqId, step: msg.step, message: msg.message });
    return;
  }
  if (msg && msg.type === 'state') {
    reply({ ...state, version: VERSION, managerTab: managerTabId !== null });
    return;
  }
  if (msg && msg.type === 'reconnect') {
    if (ws) { try { ws.close(); } catch { /* ignore */ } }
    ws = null;
    backoff = 2000;
    connect().then(() => reply({ ok: true }));
    return true;
  }
  if (msg && msg.type === 'check_manager') {
    checkManager()
      .then(r => reply({ ok: true, login: r.data.login }))
      .catch(e => reply({ ok: false, message: e.message }));
    return true;
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.port) {
    if (ws) { try { ws.close(); } catch { /* ignore */ } }
    ws = null;
    backoff = 2000;
    connect();
  }
});

chrome.alarms.create('doctool-reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'doctool-reconnect') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
badge();
connect();
