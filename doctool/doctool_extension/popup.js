const $ = s => document.querySelector(s);

function render(st) {
  $('#ver').textContent = 'v' + st.version;
  $('#conn').className = 'st ' + (st.connected ? 'ok' : 'bad');
  $('#connText').textContent = st.connected
    ? `doctool подключён (порт ${st.port})${st.busy ? ' — идёт запрос…' : ''}`
    : `нет связи с doctool (порт ${st.port}) — запустите doctool_web.bat`;
  $('#events').textContent = (st.events || []).join('\n') || 'пока ничего';
}

function refresh() {
  chrome.runtime.sendMessage({ type: 'state' }, st => { if (st) render(st); });
}

$('#reconnect').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'reconnect' }, () => setTimeout(refresh, 800)));
$('#check').addEventListener('click', () => {
  $('#mgr').className = 'st';
  $('#mgrText').textContent = 'manager: проверяю… (если откроется окно «Войти» — войдите)';
  chrome.runtime.sendMessage({ type: 'check_manager' }, r => {
    $('#mgr').className = 'st ' + (r && r.ok ? 'ok' : 'bad');
    $('#mgrText').textContent = r && r.ok ? `manager: вход выполнен${r.login ? ' (' + r.login + ')' : ''}` : `manager: ${r ? r.message : 'нет ответа'}`;
    refresh();
  });
});
refresh();
setInterval(refresh, 1500);
