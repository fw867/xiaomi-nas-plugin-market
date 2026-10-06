'use strict';

/* Unifi 插件的设置/状态页（panel.html）。
   软件中心本身由插件服务直接提供在插件根路径上，这里只管连接参数与状态。
   接口前缀用相对路径 ctl/ → 解析为 /plugin/<用户名>/rtrcenter/ctl/...（用户名带不带 u 都行）。 */

const API = 'ctl';
const $ = (id) => document.getElementById(id);

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

function addRow(list, label, value, kind) {
  const dt = document.createElement('dt');
  dt.textContent = label;
  const dd = document.createElement('dd');
  dd.textContent = value;
  if (kind) dd.classList.add(`is-${kind}`);
  list.append(dt, dd);
}

function renderStatus(panel) {
  const router = (panel && panel.router) || {};
  const dot = $('statusDot');
  dot.classList.toggle('is-ok', Boolean(router.reachable));
  dot.classList.toggle('is-bad', Boolean(panel) && !router.reachable);

  $('brandSub').textContent = router.reachable
    ? `${(router.title || 'UniFi SoftCenter').replace(/\s*-\s*安全管理中心$/, '')} · ${typeof router.latency_ms === 'number' ? `${router.latency_ms} ms` : ''}`
    : (panel ? `连不上目标：${router.error || '未知错误'}` : '正在探测…');
  const bits = [];
  if (panel && panel.target) bits.push(panel.target.replace(/^https?:\/\//, '').replace(/\/$/, ''));
  if (router.device) bits.push(router.device);
  if (router.version) bits.push(`v${router.version}`);
  $('routerMeta').textContent = bits.join(' · ');

  const list = $('statusList');
  list.replaceChildren();
  if (!panel) {
    addRow(list, '插件服务', '无法读取状态', 'bad');
    return;
  }
  addRow(list, '插件版本', panel.version || '—');
  addRow(list, '目标地址', panel.target || '—');
  addRow(list, '路由器', router.reachable ? `可达（HTTP ${router.status}）` : `不可达：${router.error || ''}`,
    router.reachable ? 'ok' : 'bad');
  addRow(list, '响应延迟', typeof router.latency_ms === 'number' ? `${router.latency_ms} ms` : '—');
  addRow(list, '软件中心', router.title || '—');
  addRow(list, 'AdminToken', panel.token_set ? (router.authenticated ? '已保存且有效' : '已保存（未验证）') : '未保存',
    panel.token_set ? 'ok' : '');
  const assets = panel.assets || {};
  const missing = Object.entries(assets).filter(([, present]) => !present).map(([name]) => name);
  addRow(list, '本地资源', missing.length ? `缺失：${missing.join('、')}` : 'Vue / Tailwind / Lucide 都已本地化',
    missing.length ? 'bad' : 'ok');
  addRow(list, 'nginx 入口', panel.nginx_conf_ready ? '已渲染' : '未渲染', panel.nginx_conf_ready ? 'ok' : 'bad');
}

async function loadStatus() {
  try {
    renderStatus(await api('status'));
  } catch (error) {
    renderStatus(null);
  }
}

/* 令牌明文只有"设备所有者的小米客户端"能拿到：其它入口（控制台电脑端、浏览器直连）
   /api/settings 只回 token_hint（例如 a1b2…z9）。这里绝不把提示当令牌显示 ——
   输入框只显示"已保存（留空则保持不变）"，提示另起一行说明去哪看明文。 */
let tokenVisible = false;
let tokenHint = '';

async function loadSettings() {
  try {
    const settings = await api('settings');
    tokenVisible = Boolean(settings.token_visible);
    tokenHint = settings.token_hint || '';
    $('targetInput').value = settings.target || '';
    $('tokenInput').value = '';                       // 明文令牌从不预填（免得被误当提示显示/回存）
    $('tokenInput').placeholder = settings.token_set ? '已保存（留空则保持不变）' : '留空表示不保存';
    const hint = $('tokenHint');
    hint.textContent = settings.token_set
      ? (tokenVisible
        ? `已保存：${tokenHint}（明文只在小mi App 或 NAS 本地可见）`
        : `已保存：${tokenHint} —— 明文只在小mi App 或 NAS 本地能看到；这里只能替换，看不到原值`)
      : '';
    hint.classList.toggle('hidden', !settings.token_set);
  } catch (error) {
    $('settingsError').textContent = error.message;
    $('settingsError').classList.remove('hidden');
  }
}

async function saveSettings() {
  const button = $('saveSettings');
  const error = $('settingsError');
  error.classList.add('hidden');
  button.disabled = true;
  const payload = { target: $('targetInput').value.trim() };
  // 输入框为空就不回存：不能把 token_hint 当作令牌存回去
  if ($('tokenInput').value.trim()) payload.token = $('tokenInput').value.trim();
  try {
    renderStatus(await api('settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }));
    await loadSettings();
    toast('已保存');
  } catch (failure) {
    error.textContent = failure.message;
    error.classList.remove('hidden');
    if (failure.payload && failure.payload.router) renderStatus(failure.payload);
  } finally {
    button.disabled = false;
  }
}

async function init() {
  await loadSettings();
  await loadStatus();
  $('saveSettings').addEventListener('click', saveSettings);
  $('refreshStatus').addEventListener('click', async () => { await loadStatus(); toast('已重新探测'); });
  setInterval(loadStatus, 20000);
}

document.addEventListener('DOMContentLoaded', init);
