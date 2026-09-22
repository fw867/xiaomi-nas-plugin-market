'use strict';
const $ = (id) => document.getElementById(id);
const session = document.querySelector('meta[name="fw-session"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let state = null;
let polling = false;
let toastTimer = null;

function toast(message) {
  const box = $('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 4000);
}

function showError(message) {
  const box = $('error');
  box.textContent = message || '';
  box.hidden = !message;
}

async function call(path, body) {
  const headers = {};
  if (session) headers['X-Fw-Session'] = session;
  if (body !== undefined) {
    if (csrf) headers['X-CSRF-Token'] = csrf;
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch('api' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data = {};
  try { data = JSON.parse(text); } catch (e) {
    throw new Error(response.status === 401 ? '请从小米智能存储客户端重新打开插件' : '服务返回了非 JSON 响应');
  }
  if (!response.ok || data.ok === false) throw new Error(data.error || '请求失败');
  return data;
}

function stateLabel(s) {
  if (s.busy) return '正在处理，请稍候';
  if (!s.configured) return '未配置';
  return s.running ? '穿透运行中' : '已停止';
}

function render(s) {
  $('serviceState').textContent = stateLabel(s);
  const dot = s.busy ? '' : (s.running ? 'on' : 'off');
  $('statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
  $('setup').hidden = s.configured || s.busy;
  $('serviceActions').hidden = !s.configured;
  $('serverName').textContent = s.server || '—';
  $('tokenState').textContent = s.hasToken ? '已保存' : '未设置';
  $('clientVersion').textContent = s.clientVersion || '—';
  $('toggleService').textContent = s.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = s.busy;
  $('upgrade').disabled = s.busy;
  const info = [];
  if (s.clientVersion) info.push('fwclient ' + s.clientVersion);
  if (s.preview) info.push('预览模式');
  $('clientInfo').textContent = info.join(' · ');
  $('clientInfo').hidden = !info.length;
  if (!s.busy && s.error) showError(s.error);
  else if (!s.busy) showError('');
}

async function refresh() {
  if (polling) return;
  polling = true;
  try {
    state = await call('/status');
    render(state);
    const log = await call('/log');
    const text = log.log || '';
    const lines = text.split('\n').filter((line) => line.trim());
    $('logBox').textContent = lines.length ? text : '（暂无）';
    $('logCount').textContent = lines.length ? lines.length + ' 行' : '暂无日志';
  } catch (e) {
    showError(e.message);
  } finally {
    polling = false;
  }
}

async function act(path, body) {
  await call(path, body || {});
  await refresh();
}

$('refresh').onclick = () => refresh();
$('setupForm').onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  const submit = form.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    await act('/service/setup', {
      server: form.elements.server.value.trim(),
      token: form.elements.token.value,
      insecure: form.elements.insecure.checked,
    });
    form.reset();
    toast('已保存并启动');
  } catch (err) {
    toast(err.message);
  } finally {
    submit.disabled = false;
  }
};
$('toggleService').onclick = async () => {
  if (!state) return;
  const button = $('toggleService');
  button.disabled = true;
  try {
    await act('/service/' + (state.running ? 'stop' : 'start'));
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
};
$('upgrade').onclick = async () => {
  const button = $('upgrade');
  button.disabled = true;
  button.textContent = '升级中…';
  try {
    await act('/service/upgrade');
    toast('已检查并升级');
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
    button.textContent = '检查升级';
    await refresh();
  }
};

// 一键复制整份日志；剪贴板不可用时退回到全选，让用户手动复制。
$('copyLog').onclick = async () => {
  const box = $('logBox');
  const text = box.textContent || '';
  if (!text || text === '（暂无）') {
    toast('暂无可复制的日志');
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    toast('日志已复制（' + text.split('\n').filter((l) => l.trim()).length + ' 行）');
  } catch (e) {
    const range = document.createRange();
    range.selectNodeContents(box);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast('剪贴板不可用，已全选，请按 Ctrl+C 复制');
  }
};

async function tick() {
  if (!$('fw-app').isConnected) return;
  if (!document.hidden) await refresh();
  setTimeout(tick, 4000);
}
tick();
