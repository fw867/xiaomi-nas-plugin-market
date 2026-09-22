'use strict';
// Windows 客户端 location 可能带盘符（/D:/plugin/...），相对 fetch 会 400。
// 以当前 script URL 为绝对基址（与 aliyundrive/115 插件一致）。
function pluginAssetBase() {
  const loaded = document.currentScript?.src
    || [...document.scripts].map((s) => s.src).find((src) => /\/app(?:\.bundle)?\.js(?:$|\?)/.test(src));
  if (loaded) return new URL('./', loaded).href;
  const route = window.__MICRO_APP_BASE_ROUTE__;
  if (typeof route === 'string' && route) {
    const cleaned = route.replace(/^\/[A-Za-z]:/, '') || route;
    const normalized = cleaned.endsWith('/') ? cleaned : `${cleaned}/`;
    return new URL(normalized, window.location.origin).href;
  }
  const dir = window.location.pathname.replace(/^\/[A-Za-z]:/, '').replace(/[^/]*$/, '');
  return new URL(dir || '/', window.location.origin).href;
}
const assetUrl = (path) => new URL(path, pluginAssetBase()).href;

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
  const response = await fetch(assetUrl('api' + path), {
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

function toggleLabel(running) {
  return running ? '停止服务' : '启动服务';
}

function render(s) {
  $('serviceState').textContent = stateLabel(s);
  const dot = s.busy ? '' : (s.running ? 'on' : 'off');
  $('statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';

  const info = [];
  // clientVersion 来自 fwclient -v，本身已带 v 前缀，不要再补一次
  if (s.clientVersion) info.push('当前版本 ' + s.clientVersion);
  if (s.preview) info.push('预览模式');
  $('clientInfo').textContent = info.join(' · ');
  $('clientInfo').hidden = !info.length;

  // 配置卡常驻：已配置时也能改服务器域名。令牌不回显，留空表示保持不变。
  $('setup').hidden = s.busy;
  const form = $('setupForm');
  if (s.configured && s.server) {
    if (form.elements.server.value !== s.server) form.elements.server.value = s.server;
    $('confirmCheck').hidden = true;
    $('saveBtn').textContent = '保存并重新启动';
  } else {
    $('confirmCheck').hidden = false;
    $('saveBtn').textContent = '保存并启动';
  }

  $('toggleService').disabled = s.busy;
  $('toggleService').textContent = toggleLabel(s.running);
  $('upgrade').disabled = s.busy;
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
    // 倒序：最新日志在最上面
    const ordered = lines.slice().reverse().join('\n');
    $('logBox').textContent = lines.length ? ordered : '（暂无）';
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
  const configured = !!(state && state.configured);
  const action = configured ? '/service/reconfigure' : '/service/setup';
  const token = form.elements.token.value;
  const payload = {
    server: form.elements.server.value.trim(),
    insecure: form.elements.insecure.checked,
  };
  // 令牌留空 = 保持原有值不变（页面不回显明文）
  if (token) payload.token = token;
  const submit = form.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    await act(action, payload);
    form.elements.token.value = '';
    if (state && state.server) form.elements.server.value = state.server;
    toast(configured ? '已保存并重新启动' : '已保存并启动');
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
  const wasRunning = state.running;
  try {
    await act('/service/' + (wasRunning ? 'stop' : 'start'));
    toast(wasRunning ? '正在停止' : '正在启动');
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
    if (state) state.error = '';
    showError('');
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
