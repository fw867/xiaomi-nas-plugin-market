'use strict';

/* 移动端控制页：桌面 Web 入口的开关、访问地址与令牌。
   全部走相对路径，因此既能挂在 /plugin/<用户>/nasconsole/ 下（小米客户端入口），
   也能从桌面入口的 /control.html 打开。 */

const API = 'api';

const $ = (id) => document.getElementById(id);
const state = { data: null, revealed: false, busy: false, confirmRegen: false, csrf: '' };

async function api(path, options) {
  const response = await fetch(`${API}/${path}`, { credentials: 'same-origin', ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

// 浏览器入口的写操作要带 CSRF 令牌（客户端入口/回环直连不需要，带上也无害）
function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf || '' },
    body: JSON.stringify(body || {}),
  });
}

function toast(message, kind) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('is-error', kind === 'error');
  node.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add('hidden'), 2600);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    // 老 WebView / 非安全上下文没有 clipboard API，退回 selection + execCommand
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (inner) {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

function render() {
  const data = state.data;
  if (!data) return;
  const chip = $('stateChip');
  chip.textContent = data.enabled ? '已启用' : '已停用';
  chip.classList.toggle('is-on', !!data.enabled);
  chip.classList.toggle('is-off', !data.enabled);

  const button = $('switchBtn');
  button.setAttribute('aria-checked', data.enabled ? 'true' : 'false');
  button.disabled = state.busy;
  $('switchHint').textContent = data.enabled
    ? '已开启：电脑浏览器可直接打开下面的地址'
    : '已关闭：端口不再监听，电脑端打不开（手机端不受影响）';

  $('urlValue').textContent = data.url || '—';
  $('openUrl').setAttribute('href', data.url || '#');
  $('openUrl').classList.toggle('hidden', !data.enabled);
  $('portHint').textContent = data.lan_port ? `端口 ${data.lan_port}` : '';

  const portInput = $('portInput');
  if (document.activeElement !== portInput) portInput.value = data.lan_port || '';
  // 模板缺失是硬故障（没法启用/改端口），直接顶掉端口卡片的状态文字
  $('portState').textContent = data.template_ready === false
    ? '缺少入口模板，无法启用或改端口'
    : (data.enabled ? '生效中' : '入口已停用（保存后下次启用生效）');
  $('savePort').disabled = state.busy || data.template_ready === false;

  const tokenInput = $('tokenInput');
  if (state.data === null || document.activeElement !== tokenInput) tokenInput.value = data.token || '';
  tokenInput.type = state.revealed ? 'text' : 'password';
  const eye = $('revealToken');
  eye.classList.toggle('is-revealed', state.revealed);
  eye.setAttribute('aria-pressed', state.revealed ? 'true' : 'false');
  eye.setAttribute('aria-label', state.revealed ? '隐藏令牌' : '显示令牌');
  $('versionText').textContent = data.version ? `v${data.version}` : '';
}

async function load() {
  try {
    const session = await api('session');
    state.csrf = session.csrf || '';
    state.data = await api('control');
    render();
  } catch (error) {
    $('stateChip').textContent = '读取失败';
    if (error.status === 401) {
      toast('请先在电脑浏览器登录后再刷新本页', 'error');
    } else {
      toast(error.message, 'error');
    }
  }
}

async function toggle() {
  if (!state.data || state.busy) return;
  const next = !state.data.enabled;
  state.busy = true;
  $('switchBtn').disabled = true;
  try {
    state.data = await post('control', { enabled: next });
    toast(next ? '已启用桌面 Web 控制台' : '已停用桌面 Web 控制台');
  } catch (error) {
    if (error.payload && typeof error.payload.enabled === 'boolean') state.data = error.payload;
    toast(error.message, 'error');
  } finally {
    state.busy = false;
    render();
  }
}

async function savePort() {
  if (!state.data || state.busy) return;
  const port = Number($('portInput').value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    toast('端口范围是 1024–65535', 'error');
    return;
  }
  if (port === state.data.lan_port) {
    toast('端口没有变化');
    return;
  }
  state.busy = true;
  render();
  try {
    state.data = await post('control/port', { port });
    toast(`端口已改为 ${state.data.lan_port}，新地址 ${state.data.url}`);
  } catch (error) {
    if (error.payload && typeof error.payload.lan_port === 'number') state.data = error.payload;
    toast(error.message, 'error');
  } finally {
    state.busy = false;
    render();
  }
}

function regenToken() {
  const button = $('regenToken');
  if (!state.confirmRegen) {
    state.confirmRegen = true;
    button.classList.add('is-confirm');
    button.textContent = '再点一次确认';
    clearTimeout(regenToken.timer);
    regenToken.timer = setTimeout(() => {
      state.confirmRegen = false;
      button.classList.remove('is-confirm');
      button.textContent = '重新生成';
    }, 4000);
    return;
  }
  clearTimeout(regenToken.timer);
  state.confirmRegen = false;
  button.classList.remove('is-confirm');
  button.textContent = '重新生成';
  rotateToken();
}

async function rotateToken() {
  if (state.busy) return;
  state.busy = true;
  render();
  try {
    state.data = await post('control/token');
    state.revealed = true;
    toast('已生成新令牌，电脑端需要用新令牌重新登录');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    state.busy = false;
    render();
  }
}

function init() {
  $('switchBtn').addEventListener('click', toggle);
  $('savePort').addEventListener('click', savePort);
  $('portInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); savePort(); }
  });
  $('regenToken').addEventListener('click', regenToken);
  $('copyUrl').addEventListener('click', async () => {
    const ok = await copyText($('urlValue').textContent.trim());
    toast(ok ? '地址已复制' : '复制失败，请长按选择', ok ? '' : 'error');
  });
  $('copyToken').addEventListener('click', async () => {
    const token = state.data ? state.data.token : '';
    if (!token) return;
    const ok = await copyText(token);
    toast(ok ? '令牌已复制' : '复制失败，请长按选择', ok ? '' : 'error');
  });
  $('revealToken').addEventListener('click', () => {
    state.revealed = !state.revealed;
    render();
    // 展开后把光标放到末尾，方便长按选择
    if (state.revealed) {
      const input = $('tokenInput');
      input.focus({ preventScroll: true });
      input.setSelectionRange(input.value.length, input.value.length);
    }
  });
  load();
}

document.addEventListener('DOMContentLoaded', init);
