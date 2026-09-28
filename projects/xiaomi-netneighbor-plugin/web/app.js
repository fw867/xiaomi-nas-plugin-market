'use strict';
// 网络邻居插件前端：原生 JS，无外部依赖。
// 令牌来自 index.html 里的两个 meta：nginx 把页面代理给插件服务，由服务端把
// __SESSION_TOKEN__ / __CSRF_TOKEN__ 替换掉（直接 alias 静态页会拿到空令牌）。

const $ = (id) => document.getElementById(id);
const meta = (name) => {
  const node = document.querySelector(`meta[name="${name}"]`);
  return node ? (node.getAttribute('content') || '') : '';
};
const SESSION = meta('nn-session');
const CSRF = meta('csrf-token');

let state = null;
let toastTimer = null;
let busy = false;

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
  toastTimer = setTimeout(() => { box.hidden = true; }, 4000);
}

function showError(message) {
  const box = $('error');
  box.textContent = message || '';
  box.hidden = !message;
}

async function call(path, body) {
  const init = {
    cache: 'no-store',
    headers: { 'X-NN-Session': SESSION },
  };
  if (body !== undefined) {
    init.method = 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.headers['X-CSRF-Token'] = CSRF;
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

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function formatTime(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatSince(seconds) {
  if (!seconds) return '还没有 Windows 来取过';
  const date = new Date(seconds * 1000);
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  return sameDay ? `今天 ${clock}` : `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
}

async function copyText(text, okMessage) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMessage);
  } catch {
    toast('当前客户端不支持剪贴板，请长按选择复制');
  }
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

function renderDiscovery(discovery, version) {
  const running = Boolean(discovery.running);
  $('statusDot').className = `status-dot ${running ? 'on' : 'off'}`;
  $('serviceState').textContent = running ? '发现服务运行中' : '发现服务未运行';
  const bits = [discovery.message || ''];
  if (discovery.helloInterval) bits.push(`Hello 每 ${Math.round(discovery.helloInterval / 60)} 分钟一次`);
  if (discovery.metadataPosts) bits.push(`已响应 ${discovery.metadataPosts} 次元数据请求`);
  $('serviceInfo').textContent = bits.filter(Boolean).join(' · ');

  $('hostname').textContent = discovery.hostname || '—';
  $('workgroup').textContent = discovery.workgroup || '—';
  $('xaddrs').textContent = (discovery.xaddrs && discovery.xaddrs.length)
    ? discovery.xaddrs.join('  ') : '—';
  if (discovery.wsddDropin) {
    $('wsddState').textContent = '已接管（官方 wsdd 已 stop，并用 systemd drop-in 置为空操作）';
  } else if (discovery.managed) {
    $('wsddState').textContent = '已尝试接管（drop-in 未确认写入，最近日志里有原因）';
  } else {
    $('wsddState').textContent = '未接管（官方 wsdd 自由运行）';
  }
  $('lastMetadata').textContent = formatSince(discovery.lastMetadataAt);
  $('version').textContent = version || '—';
}

function shareRow(account, share) {
  const row = element('div', 'share-row');
  const head = element('div', 'share-head');
  head.append(element('span', 'share-name', share.name));
  if (share.custom) head.append(element('span', 'chip', '插件添加'));
  else head.append(element('span', 'chip system', '系统/App'));
  if (share.missing) head.append(element('span', 'chip warn', '配置不完整'));
  row.append(head);
  row.append(element('div', 'share-path', share.path || '（配置里没有路径）'));
  const metaBits = [];
  if (share.users && share.users.length) metaBits.push(`可访问：${share.users.join('、')}`);
  if (share.forceUser) metaBits.push(`force_user：${share.forceUser}`);
  if (share.status && share.status !== '1') metaBits.push(`status：${share.status}`);
  row.append(element('div', 'share-meta', metaBits.join(' · ') || '—'));
  if (share.deletable) {
    const remove = element('button', 'btn danger small', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => deleteShare(share));
    row.append(remove);
  }
  return row;
}

function renderAccounts(accounts) {
  const box = $('accounts');
  if (!accounts || !accounts.length) {
    box.replaceChildren(element('p', 'muted', '没有读到任何 Samba 账号（/etc/config/sambauser）'));
    return;
  }
  box.replaceChildren(...accounts.map((account) => {
    const card = element('article', 'account');
    const head = element('div', 'account-head');
    head.append(element('strong', 'account-name', account.account));
    head.append(element('span', 'chip', account.sambaUser || '—'));
    if (account.customCount) {
      head.append(element('span', 'chip', `插件添加 ${account.customCount} 个`));
    }
    card.append(head);
    if (!account.shares.length) {
      card.append(element('p', 'muted', '这个账号还没有共享目录'));
      return card;
    }
    card.append(...account.shares.map((share) => shareRow(account.account, share)));
    return card;
  }));
}

function renderAccountOptions(accounts) {
  const select = $('account');
  const previous = select.value;
  select.replaceChildren(...accounts.map((account) => {
    const option = document.createElement('option');
    option.value = account.account;
    option.textContent = `${account.account}（${account.sambaUser || '—'}，已有 ${account.shares.length} 个共享）`;
    return option;
  }));
  if (previous && accounts.some((item) => item.account === previous)) select.value = previous;
}

function render(data) {
  state = data;
  renderDiscovery(data.discovery || {}, data.version);
  renderAccounts(data.accounts || []);
  renderAccountOptions(data.accounts || []);
  const roots = data.allowedRoots || [];
  $('rootsHint').textContent = roots.length
    ? `允许的根目录：${roots.join('、')}`
    : '没有配置允许的根目录（请检查 ALLOWED_ROOTS）';
  if (data.sambaMgrFound === false) {
    showError(`没有找到 smb_mgr.sh（当前按 ${data.sambaMgr} 查找），无法增删共享`);
  }
}

async function refresh() {
  if (busy) return;
  try {
    render(await call('status'));
    if (state && state.sambaMgrFound !== false) showError('');
    await loadLog();
  } catch (error) {
    showError(error.message);
  }
}

async function loadLog() {
  try {
    const data = await call('log');
    const lines = data.lines || [];
    $('logBox').textContent = lines.length ? lines.join('\n') : '（暂无日志）';
  } catch (error) {
    $('logBox').textContent = error.message;
  }
}

// ---------------------------------------------------------------------------
// 操作
// ---------------------------------------------------------------------------

async function addShare() {
  const account = $('account').value;
  const path = $('path').value.trim();
  if (!account) { toast('请选择账号'); return; }
  if (!path) { toast('请填写目录路径'); return; }
  const body = {
    account,
    path,
    sharePoint: $('sharePoint').value.trim(),
  };
  if ($('useForce').checked) body.forceUser = 'auto';
  const button = $('addShare');
  button.disabled = true;
  button.textContent = '正在添加…';
  try {
    const data = await call('share/add', body);
    render(data.status);
    const result = data.result || {};
    $('path').value = '';
    $('sharePoint').value = '';
    toast(`已添加共享 ${result.shareName}（${result.sharePoint}）`);
    if (result.errors && result.errors.length) {
      showError(`已完成，但收尾有提示：${result.errors.join('；')}`);
    } else {
      showError('');
    }
  } catch (error) {
    showError(error.message);
    toast('添加失败');
  } finally {
    button.disabled = false;
    button.textContent = '添加共享目录';
  }
}

async function deleteShare(share) {
  const who = share.users && share.users.length ? `，可访问 ${share.users.join('、')}` : '';
  if (!window.confirm(`确定删除共享 ${share.name}（${share.path}${who}）？\n`
    + '只删除共享，不会删除目录里的文件。')) return;
  try {
    const data = await call('share/delete', { shareName: share.name });
    render(data.status);
    toast(`已删除共享 ${share.name}`);
    if (data.result && data.result.errors && data.result.errors.length) {
      showError(`已完成，但收尾有提示：${data.result.errors.join('；')}`);
    } else {
      showError('');
    }
  } catch (error) {
    showError(error.message);
    toast('删除失败');
  }
}

async function announce() {
  const button = $('announce');
  button.disabled = true;
  button.textContent = '正在宣告…';
  try {
    const data = await call('detect/restart', {});
    render(data.status);
    toast('已重新宣告，等 Windows 刷新「网络」');
    showError('');
  } catch (error) {
    showError(error.message);
    toast('重新宣告失败');
  } finally {
    button.disabled = false;
    button.textContent = '重新宣告';
  }
}

$('back').onclick = leavePlugin;
$('refresh').onclick = () => { refresh(); };
$('announce').onclick = announce;
$('addShare').onclick = addShare;
$('copyLog').onclick = async () => {
  const text = $('logBox').textContent;
  if (!text || text === '读取中…') { toast('暂无可复制的日志'); return; }
  await copyText(text, '日志已复制');
};

refresh();
setInterval(() => {
  if (document.hidden) return;
  refresh();
}, 15000);
