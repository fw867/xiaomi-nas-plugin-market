'use strict';
const $ = (id) => document.getElementById(id);
let state = null;
let logTab = 'events';
let polling = false;
let activityBusy = false;
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

// 同一分钟内只说 HH:MM，跨天补上日期
function formatSince(seconds) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  return sameDay ? clock : `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
}

function formatRate(kbps) {
  const value = Number(kbps) || 0;
  if (value >= 1024) return `${(value / 1024).toFixed(2)} MB/s`;
  return `${value.toFixed(1)} KB/s`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function copyText(text, okMessage) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMessage);
  } catch {
    toast('当前客户端不支持剪贴板，请长按选择复制');
  }
}

const EVENT_LABEL = { standby: '进入休眠', wake: '被唤醒', config: '修改设置', switch: '开关变更' };

function render(current) {
  state = current;
  const active = Boolean(current.active);
  $('statusDot').className = `status-dot ${active ? 'on' : 'off'}`;
  $('serviceState').textContent = active ? '插件接管中' : '未接管';
  const minutes = current.effectiveMinutes || current.minutes || current.officialMinutes;
  $('serviceInfo').textContent = [
    `系统开关 ${current.appSwitch ? '开' : '关'}`,
    current.hdidleActive ? '守护运行中' : '守护未运行',
    `${minutes} 分钟`,
  ].join(' · ');

  $('version').textContent = current.version || '—';

  $('toggleSleep').textContent = active ? '关闭插件接管' : '开启插件接管';

  $('disks').replaceChildren(...(current.disks.length ? current.disks.map((disk) => {
    const row = element('div', 'disk-row');
    row.append(element('span', 'disk-name', disk.device.toUpperCase()));
    const standby = Boolean(disk.standby);
    row.append(element('span', `disk-state${standby ? ' is-standby' : ''}`, standby ? '休眠中' : '活动'));
    const since = formatSince(standby ? disk.lastStandby : disk.lastWake);
    if (since) row.append(element('span', 'disk-since', `${since} 起`));
    return row;
  }) : [element('p', 'muted', '未检测到硬盘')]));

  const dot = $('activityDot');
  const activity = current.activity || {};
  dot.hidden = !(activity.hasWrites || activity.sampling);
  if (!dot.hidden) dot.classList.toggle('quiet', Boolean(activity.sampling) && !activity.hasWrites);

  $('minutes').min = current.minMinutes;
  $('minutes').max = current.maxMinutes;
  if (document.activeElement !== $('minutes')) $('minutes').value = current.minutes;
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

// ---------------------------------------------------------------------------
// 谁在写盘（日志卡片里的第三个标签页）
// ---------------------------------------------------------------------------

function renderNotes(notes) {
  const box = $('activityNotes');
  if (!notes || !notes.length) { box.replaceChildren(); return; }
  box.replaceChildren(...notes.map((text) => {
    const calm = /采样中|本来就应该能休眠|已经过期/.test(text);
    return element('div', calm ? 'note calm' : 'note', text);
  }));
}

function renderMounts(mounts, disks, windowSeconds) {
  const box = $('activityMounts');
  if (!mounts || !mounts.length) {
    box.replaceChildren(element('p', 'muted file-empty', '没有检测到机械盘挂载点'));
    return;
  }
  const rows = mounts.map((mount) => {
    const row = element('article', mount.busy ? 'rate busy' : 'rate');
    const head = element('div', 'rate-head');
    head.append(element('span', 'rate-name', mount.mountpoint));
    if (mount.array) {
      head.append(element('span', 'rate-chip raid', `${mount.array} ${mount.level}：${mount.members.join('+')}`));
    }
    row.append(head);
    row.append(element('span', 'rate-value', formatRate(mount.writeKBps)));
    row.append(element('span', 'rate-meta muted',
      `${mount.device} · 读 ${formatRate(mount.readKBps)}`
      + (mount.mirrored ? ' · RAID1 镜像写入，每块成员盘都要写一次' : '')));
    return row;
  });
  if (disks && disks.length) {
    rows.push(element('p', 'muted file-empty',
      `整盘合计：${disks.map((d) => `${d.device} 写 ${formatRate(d.writeKBps)}`).join(' · ')}`
      + (windowSeconds ? `（采样窗口 ${windowSeconds} 秒）` : '')));
  }
  box.replaceChildren(...rows);
}

function renderFiles(box, items, emptyText, metaOf) {
  if (!items || !items.length) {
    box.replaceChildren(element('p', 'muted file-empty', emptyText));
    return;
  }
  box.replaceChildren(...items.map((item) => {
    const row = element('button', 'file');
    row.type = 'button';
    row.append(element('span', 'file-path', item.path));
    row.append(element('span', 'file-meta', metaOf(item)));
    row.addEventListener('click', () => copyText(item.path, '路径已复制'));
    return row;
  }));
}

function renderActivity(data) {
  renderNotes(data.notes);
  renderMounts(data.mounts, data.disks, data.windowSeconds);
  renderFiles($('activityWriters'), data.writers,
    data.writersAt ? '这段时间没有扫到被改动的文件（写入可能只在文件系统元数据上）' : '尚未扫描',
    (item) => {
      const bits = [];
      if (item.kind === 'new') bits.push('新增');
      else if (item.delta) bits.push(`${item.delta > 0 ? '+' : ''}${item.delta} B`);
      else bits.push('mtime 变化');
      if (item.owner) bits.push(item.owner);
      if (item.heldBy && item.heldBy.length) bits.push(`被 ${item.heldBy.join('、')} 打开`);
      return bits.join(' · ');
    });
  renderFiles($('activityEvents'), data.events,
    data.eventsNote || '系统索引还没有记录到文件改动',
    (item) => {
      const bits = [`最近 300 条记录里出现 ${item.count} 次`];
      if (item.owner) bits.push(item.owner);
      return bits.join(' · ');
    });
}

function activityVisible() {
  return $('logCard').open && logTab === 'activity';
}

async function loadActivity(force) {
  if (activityBusy || !activityVisible()) return;
  activityBusy = true;
  const button = $('refreshActivity');
  button.disabled = true;
  try {
    renderActivity(await call(force ? 'activity?force=1' : 'activity'));
  } catch (error) {
    $('activityNotes').replaceChildren(element('div', 'note', error.message));
  } finally {
    activityBusy = false;
    button.disabled = false;
  }
}

async function loadLog() {
  if (logTab === 'activity') {
    $('logBox').hidden = true;
    $('activityPanel').hidden = false;
    $('copyLog').hidden = true;
    await loadActivity(true);
    return;
  }
  $('activityPanel').hidden = true;
  $('logBox').hidden = false;
  $('copyLog').hidden = false;
  $('logBox').textContent = '读取中…';
  try {
    if (logTab === 'events') {
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

async function saveMinutes(minutes) {
  const value = Number(minutes);
  if (!Number.isInteger(value)) { toast('请输入整数分钟'); return; }
  try {
    const next = await call('timeout', { minutes: value });
    render(next);
    toast(next.active ? `休眠时间已设为 ${value} 分钟` : `已记为 ${value} 分钟，开启接管后生效`);
  } catch (error) {
    toast(error.message);
  }
}

function selectTab(button) {
  logTab = button.dataset.tab;
  document.querySelectorAll('.seg').forEach((item) => item.classList.toggle('selected', item === button));
  if ($('logCard').open) loadLog();
}

$('back').onclick = leavePlugin;
$('refresh').onclick = () => {
  refresh();
  if ($('logCard').open) loadLog();
};
$('saveMinutes').onclick = () => saveMinutes($('minutes').value);
$('toggleSleep').onclick = async () => {
  const target = !state.active;
  try {
    render(await call('takeover', { enabled: target }));
    toast(target ? '已开启插件接管' : '已关闭插件接管，交还官方 30 分钟');
  } catch (error) {
    toast(error.message);
  }
};
document.querySelectorAll('.seg').forEach((button) => {
  button.addEventListener('click', () => selectTab(button));
});
$('logCard').addEventListener('toggle', () => { if ($('logCard').open) loadLog(); });
$('refreshActivity').onclick = () => loadActivity(true);
$('copyLog').onclick = async () => {
  const text = $('logBox').textContent;
  if (!text || text === '读取中…') { toast('暂无可复制的日志'); return; }
  await copyText(text, '日志已复制');
};

refresh();
setInterval(() => {
  if (document.hidden) return;
  refresh();
  if (activityVisible()) loadActivity();
}, 10000);
