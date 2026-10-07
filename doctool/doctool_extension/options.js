const DEFAULTS = { port: 8765, authWaitSec: 180, pauseMs: 300, closeTabAfterSec: 60 };
const keys = Object.keys(DEFAULTS);

chrome.storage.local.get(keys).then(v => {
  for (const k of keys) document.getElementById(k).value = v[k] ?? DEFAULTS[k];
});
document.getElementById('save').addEventListener('click', async () => {
  const out = {};
  for (const k of keys) {
    const n = parseInt(document.getElementById(k).value, 10);
    out[k] = Number.isFinite(n) ? n : DEFAULTS[k];
  }
  await chrome.storage.local.set(out);
  document.getElementById('saved').textContent = 'Сохранено';
  setTimeout(() => { document.getElementById('saved').textContent = ''; }, 2000);
});
