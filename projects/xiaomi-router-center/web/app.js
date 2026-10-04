'use strict';

/* 路由器软件中心插件的壳页面：
   顶栏显示路由器状态；下面用同源 iframe 承载真实的软件中心页面。
   两个关键点：
   1) 被嵌入的页面与本页同源，所以本页可以先把 AdminToken 写进 localStorage，
      软件中心一加载就自动带上令牌 —— 手机上不用每次重新输入；
   2) 所有请求都走相对路径，浏览器入口 / 客户端入口 / 任意反代前缀下都能用。 */

const API = 'api';
const $ = (id) => document.getElementById(id);
const state = { settings: null, status: null };

async function api(path, options) {
  const response = await fetch(`${API}/${path}`, { credentials: 'same-origin', ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message, kind) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('is-error', kind === 'error');
  node.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add('hidden'), 3200);
}

function fmtLatency(value) {
  return typeof value === 'number' ? `${value} ms` : '—';
}

function renderBar() {
  const status = state.status;
  const router = (status && status.router) || {};
  const dot = $('statusDot');
  dot.classList.toggle('is-ok', Boolean(router.reachable));
  dot.classList.toggle('is-bad', status !== null && !router.reachable);

  $('brandTitle').textContent = '路由器软件中心';
  if (!status) {
    $('brandSub').textContent = '正在探测…';
  } else if (router.reachable) {
    const title = router.title ? router.title.replace(/\s*-\s*安全管理中心$/, '') : 'UniFi SoftCenter';
    $('brandSub').textContent = `${title} · ${fmtLatency(router.latency_ms)}`;
  } else {
    $('brandSub').textContent = `连不上目标：${router.error || '未知错误'}`;
  }
  const bits = [];
  if (status && status.target) bits.push(status.target.replace(/^https?:\/\//, '').replace(/\/$/, ''));
  if (router.device) bits.push(router.device);
  if (router.version) bits.push(`v${router.version}`);
  if (status && status.token_set) bits.push(router.authenticated ? '令牌有效' : '令牌已存');
  $('routerMeta').textContent = bits.join(' · ');
}

function frameUrl() {
  return 'site/?embed=1&_t=' + Date.now();
}

function loadFrame(force) {
  const frame = $('siteFrame');
  const router = (state.status && state.status.router) || {};
  if (!router.reachable) {
    frame.classList.add('hidden');
    $('framePlaceholder').classList.remove('hidden');
    $('frameHint').textContent = '连不上路由器软件中心';
    $('frameDetail').textContent = `${(state.settings && state.settings.target) || ''} — ${router.error || ''}`;
    return;
  }
  $('framePlaceholder').classList.add('hidden');
  frame.classList.remove('hidden');
  if (force || !frame.getAttribute('src')) frame.setAttribute('src', frameUrl());
}

async function loadStatus() {
  try {
    state.status = await api('status');
  } catch (error) {
    state.status = null;
    $('frameDetail').textContent = error.message;
  }
  renderBar();
  loadFrame(false);
}

async function loadSettings() {
  try {
    state.settings = await api('settings');
  } catch (error) {
    state.settings = null;
    return;
  }
  // 把存在 NAS 上的令牌注入给同源的软件中心页面，手机端就不用每次手输
  if (state.settings.token) {
    try {
      localStorage.setItem('sc_token', state.settings.token);
    } catch (error) {
      /* 隐私模式下写不了 localStorage，忽略 */
    }
  }
  $('targetInput').value = state.settings.target || '';
  $('tokenInput').value = '';
  $('tokenInput').placeholder = state.settings.token_set ? '已保存（留空则保持不变）' : '留空表示不保存';
}

function openSettings() {
  $('settingsError').classList.add('hidden');
  $('settingsDrawer').classList.remove('hidden');
  $('targetInput').focus();
}

function closeSettings() {
  $('settingsDrawer').classList.add('hidden');
}

async function saveSettings() {
  const button = $('saveSettings');
  const error = $('settingsError');
  error.classList.add('hidden');
  button.disabled = true;
  const payload = { target: $('targetInput').value.trim() };
  if ($('tokenInput').value.trim()) payload.token = $('tokenInput').value.trim();
  try {
    state.status = await api('settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.settings = await api('settings');
    if (state.settings.token) localStorage.setItem('sc_token', state.settings.token);
    closeSettings();
    toast('已保存');
    renderBar();
    loadFrame(true);
  } catch (failure) {
    error.textContent = failure.message;
    error.classList.remove('hidden');
    if (failure.payload && failure.payload.router) {
      state.status = failure.payload;
      renderBar();
    }
  } finally {
    button.disabled = false;
  }
}

async function init() {
  await loadSettings();
  await loadStatus();
  $('reloadFrame').addEventListener('click', () => { loadFrame(true); toast('已重新加载'); });
  $('openSettings').addEventListener('click', openSettings);
  $('closeSettings').addEventListener('click', closeSettings);
  $('saveSettings').addEventListener('click', saveSettings);
  $('settingsDrawer').addEventListener('click', (event) => {
    if (event.target === $('settingsDrawer')) closeSettings();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeSettings();
  });
  setInterval(loadStatus, 20000);       // 只探测本机服务，不频繁打扰路由器页面
}

document.addEventListener('DOMContentLoaded', init);
