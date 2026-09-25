'use strict';
const $ = (id) => document.getElementById(id);
let state = null;
let logMode = 'events';
let polling = false;
let toastTimer = null;

// Windows 客户端 location 可能带盘符（/D:/plugin/...），相对 fetch 会 400。
// 以当前 script URL 为绝对基址（与仓库其它插件一致）。
function pluginAssetBase() {
  const loaded = document.currentScript?.src
    || [...document.scripts].map((s) => s.src).find((src) => /\/app\.js(?:$|\?)/.test(src));
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
const assetBase = pluginAssetBase();
const assetUrl = (path) => new URL(path, assetBase).href;

function leavePlugin() {
  const host = window.flutter_inappwebview || window.android_webview;
  if (host && host.callHandler) {
    const payload = host === window.android_webview ? '{}' : {};
    host.callHandler('hs_webCallAppHandler', 'normal_goback', payload, '1');
    return;
  }
  if (window.history.length > 1) { window.history.back(); return; }
  window.close();
}

function toast(message) {
  const box = $('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3200);
}

function showError(message) {
  const box = $('error');
  box.textContent = message || '';
  box.hidden = !message;
}

async function call(path, body) {
  const init = { cache: 'no-store' };
  if (body !== undefined) {
    init.method = 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(assetUrl('api/' + path), init);
  let data = {};
  try {
    data = JSON.parse(await response.text());
  } catch {
    throw new Error(`服务返回异常（HTTP ${response.status}）`);
  }
  if (!response.ok || data.ok === false) throw new Error(data.error || '操作失败');
  return data;
}

function formatTime(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const EVENT_LABEL = { standby: '进入休眠', wake: '被唤醒', config: '修改设置', switch: '开关变更' };

function render(current) {
  state = current;
  $('statusDot').className = `status-dot ${current.appSwitch && current.hdidleActive ? 'on' : 'off'}`;
  $('serviceState').textContent = current.appSwitch ? '休眠已开启' : '休眠已关闭';
  const parts = [`系统开关 ${current.appSwitch ? '开' : '关'}`];
  parts.push(current.hdidleActive ? '守护运行中' : '守护未运行');
  if (current.effectiveMinutes) parts.push(`生效 ${current.effectiveMinutes} 分钟`);
  $('serviceInfo').textContent = parts.join(' · ');

  $('toggleSleep').textContent = current.appSwitch ? '关闭休眠' : '开启休眠';

  const managed = current.managed;
  const effective = current.effectiveMinutes;
  $('timeoutState').textContent = managed
    ? (effective ? `插件已接管：${effective} 分钟（官方默认 ${current.officialMinutes}）` : `插件已接管：${current.minutes} 分钟`)
    : `官方默认 ${current.officialMinutes} 分钟，未自定义`;
  $('restoreOfficial').hidden = !managed;

  $('minutes').min = current.minMinutes;
  $('minutes').max = current.maxMinutes;
  if (document.activeElement !== $('minutes')) $('minutes').value = current.minutes;
  $('minutesHint').textContent = `可设 ${current.minMinutes} 到 ${current.maxMinutes} 分钟，保存后立即生效。`;

  $('presets').replaceChildren(...current.presets.map((minutes) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `preset ${minutes === current.minutes ? 'selected' : ''}`;
    button.textContent = minutes >= 60 && minutes % 60 === 0 ? `${minutes / 60} 小时` : `${minutes} 分钟`;
    button.addEventListener('click', () => { $('minutes').value = minutes; saveMinutes(minutes); });
    return button;
  }));

  $('disks').replaceChildren(...(current.disks.length ? current.disks.map((disk) => {
    const article = document.createElement('article');
    article.className = 'disk';
    article.innerHTML = `
      <div class="disk-head">
        <strong>${disk.device.toUpperCase()}</strong>
        <em class="${disk.standby ? 'is-standby' : 'is-active'}">${disk.standby ? '休眠中' : '活动'}</em>
      </div>
      <p class="muted">${disk.model || '型号未知'} · ${disk.state}</p>
      <p class="muted">最近休眠 ${formatTime(disk.lastStandby)} · 最近唤醒 ${formatTime(disk.lastWake)}</p>`;
    return article;
  }) : [Object.assign(document.createElement('p'), { className: 'muted', textContent: '未检测到硬盘' })]));
}

async function refresh() {
  if (polling) return;
  polling = true;
  try {
    render(await call('status'));
    showError('');
  } catch (error) {
    showError(error.message);
  } finally {
    polling = false;
  }
}

async function saveMinutes(minutes) {
  const value = Number(minutes);
  if (!Number.isInteger(value)) { toast('请输入整数分钟'); return; }
  try {
    render(await call('timeout', { minutes: value }));
    toast(`休眠时间已设为 ${value} 分钟`);
  } catch (error) {
    toast(error.message);
  }
}

async function loadLog() {
  $('logBox').textContent = '读取中…';
  try {
    if (logMode === 'events') {
      const data = await call('events?limit=200');
      const lines = data.events.map((item) => {
        const label = EVENT_LABEL[item.kind] || item.kind;
        const who = item.device ? ` ${item.device.toUpperCase()}` : '';
        const detail = item.detail ? ` · ${item.detail}` : '';
        return `${formatTime(item.at)}  ${label}${who}${detail}`;
      });
      $('logBox').textContent = lines.length ? lines.join('\n') : '（暂无事件，插件每 20 秒采样一次）';
    } else {
      const data = await call('hdidle-log?limit=200');
      const lines = data.entries.map((item) => `${formatTime(item.at)}  ${item.message}`);
      $('logBox').textContent = lines.length ? lines.join('\n') : '（hdidle 暂无日志）';
    }
  } catch (error) {
    $('logBox').textContent = error.message;
  }
}

$('back').onclick = leavePlugin;
$('refresh').onclick = () => { refresh(); if ($('logCard').open) loadLog(); };
$('saveMinutes').onclick = () => saveMinutes($('minutes').value);
$('toggleSleep').onclick = async () => {
  const target = !state.appSwitch;
  try {
    render(await call('switch', { enabled: target }));
    toast(target ? '已开启休眠' : '已关闭休眠');
  } catch (error) {
    toast(error.message);
  }
};
$('restoreOfficial').onclick = async () => {
  try {
    render(await call('restore', {}));
    toast('已恢复官方 30 分钟');
  } catch (error) {
    toast(error.message);
  }
};
document.querySelectorAll('.seg').forEach((button) => {
  button.addEventListener('click', () => {
    logMode = button.dataset.log;
    document.querySelectorAll('.seg').forEach((item) => item.classList.toggle('selected', item === button));
    loadLog();
  });
});
$('logCard').addEventListener('toggle', () => { if ($('logCard').open) loadLog(); });
$('copyLog').onclick = async () => {
  const text = $('logBox').textContent;
  if (!text || text === '读取中…') { toast('暂无可复制的日志'); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast('日志已复制');
  } catch {
    toast('当前客户端不支持剪贴板，请长按选择复制');
  }
};

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 10000);
