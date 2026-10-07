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

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------

function renderDiscovery(data) {
  const settings = data.settings || {};
  const discovery = data.discovery || {};
  const enabled = settings.discoveryEnabled !== undefined
    ? Boolean(settings.discoveryEnabled) : Boolean(discovery.enabled);
  const running = Boolean(discovery.running);

  $('statusDot').className = `status-dot ${running ? 'on' : 'off'}`;
  if (!enabled) {
    $('serviceState').textContent = '已关闭';
  } else if (running) {
    $('serviceState').textContent = '发现服务运行中';
  } else {
    $('serviceState').textContent = discovery.message || '发现服务未运行';
  }

  $('discoverySwitch').checked = enabled;
}

// SMB 名：**只读展示**服务端自动获取的值（页面不给改，也没有输入框）。
// 系统主机名只在服务端作为「两名是否一致」的判据，页面不展示；
// 不一致时显示黄色警告与「恢复为系统主机名」按钮。
function renderIdentity(data) {
  const identity = data.identity || {};
  $('netbiosName').textContent = identity.netbiosName || '—';
  $('accessHint').textContent = identity.hint || '';

  const mismatch = identity.matched === false;
  const warning = $('nameWarning');
  warning.textContent = mismatch
    ? (identity.warning || 'SMB 名与系统主机名不一致，Windows 可能连不上') : '';
  warning.hidden = !mismatch;
  const restore = $('nameRestore');
  restore.hidden = !mismatch;
  restore.disabled = false;
}

function removeBlockedReason(share) {
  // 为什么这一行的 `-` 是灰的（把原因挂在按钮的 title 上，用户悬停就能看到）
  if (share.deletable) return '';
  if (!share.name) return '配置里有目录，但没有对应的共享段；请到小米 App 里处理';
  if (share.custom) return '插件只允许删除自己添加的共享';
  return '由小米 App 管理的共享，插件不能删除';
}

function shareRow(share) {
  // 已共享目录的一行：显示名 + 共享名（<账号>_nb_<序号>）+ 完整路径 + 行尾 `-` 删除按钮
  const row = element('div', 'share-row');
  const info = element('div', 'share-info');
  info.append(element('div', 'share-name', share.display || share.name || '（未命名）'));
  if (share.display && share.name && share.display !== share.name) {
    info.append(element('div', 'share-sub', `共享名 ${share.name}`));
  }
  if (share.path) info.append(element('div', 'share-sub', share.path));

  const remove = element('button', 'share-remove', '−');
  remove.type = 'button';
  if (share.deletable) {
    remove.dataset.shareName = share.name;
    remove.dataset.display = share.display || share.name;
    remove.title = '删除这个共享';
    remove.setAttribute('aria-label', `删除共享 ${share.display || share.name}`);
  } else {
    // 官方 App 建的共享要显示出来，但删不了：按钮置灰并说明原因
    remove.disabled = true;
    remove.title = removeBlockedReason(share);
    remove.setAttribute('aria-label', remove.title);
  }
  row.append(info, remove);
  return row;
}

function renderShares(accounts) {
  const box = $('shares');
  const line = $('accountLine');
  if (!accounts || !accounts.length) {
    line.textContent = '';
    box.replaceChildren(element('p', 'muted', '没有读到任何 Samba 账号'));
    $('addShare').disabled = true;
    return;
  }
  $('addShare').disabled = false;
  const current = accounts[0];
  line.textContent = current.sambaUser
    ? `账号 ${current.account}（SMB 用户 ${current.sambaUser}）` : `账号 ${current.account}`;
  const shares = current.shares || [];
  if (!shares.length) {
    box.replaceChildren(element('p', 'muted', '还没有共享目录：点上面的「添加共享」选一个目录'));
    return;
  }
  box.replaceChildren(...shares.map((share) => shareRow(share)));
}

function render(data) {
  state = data;
  renderDiscovery(data);
  renderIdentity(data);
  renderShares(data.accounts || []);
  $('version').textContent = data.version || '—';
  if (data.sambaMgrFound === false) {
    showError(`没有找到 smb_mgr.sh（当前按 ${data.sambaMgr} 查找），无法增删共享`);
  }
}

async function refresh() {
  if (busy) return;
  try {
    render(await call('status'));
    if (state && state.sambaMgrFound !== false) showError('');
  } catch (error) {
    showError(error.message);
  }
}

// ---------------------------------------------------------------------------
// 网络发现开关
// ---------------------------------------------------------------------------

async function toggleDiscovery(enabled) {
  const toggle = $('discoverySwitch');
  toggle.disabled = true;
  busy = true;
  try {
    const data = await call('discovery', { enabled });
    render(data.status);
    toast(enabled ? '已打开网络发现' : '已关闭网络发现');
    if (!enabled) showError('');
  } catch (error) {
    showError(error.message);
    toast('设置失败');
    await refresh();                       // 失败时把开关拨回真实状态
  } finally {
    busy = false;
    toggle.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 主机名：只读展示 + 「恢复为系统主机名」
// ---------------------------------------------------------------------------

// 页面不给改名字（写入口也已被服务端拒绝）：只在两个名字不一致时提供一键恢复，
// 由服务端写 option name = 系统主机名 → init_config → restart smb nmb wsdd → 回读校验。
async function restoreHostname() {
  const button = $('nameRestore');
  button.disabled = true;
  busy = true;
  try {
    const data = await call('hostname/restore', {});
    if (data.status) render(data.status);
    const result = data.result || {};
    if (result.verified) {
      toast(`已恢复为系统主机名 ${result.name}`);
      showError('');
    } else {
      // 回读校验没过：把服务端的原始输出贴出来，别让用户以为改好了
      const notes = (result.errors || []).join('；') || '回读校验未通过';
      showError(`恢复后 smb.conf 里的 netbios name 仍是 ${result.netbiosName || '（空）'}：${notes}`
        + (result.output ? `\n${result.output}` : ''));
      toast('恢复后校验未通过');
    }
  } catch (error) {
    showError(error.message);
    toast('恢复失败');
  } finally {
    busy = false;
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 共享目录：添加（目录浏览器）/ 删除（行尾 `-`）
// ---------------------------------------------------------------------------

function currentAccount() {
  const accounts = (state && state.accounts) || [];
  return accounts.length ? accounts[0].account : '';
}

// 目录浏览器状态：当前位置序号、当前位置内的相对路径、位置列表（含 exists 标记）
let browseRoot = 0;
let browsePath = '';
let browseAbsolute = '';
let browseLocations = [];

function currentLocation() {
  return browseLocations.find((item) => Number(item.index) === browseRoot) || null;
}

function setBrowseError(message) {
  const box = $('browseError');
  if (box) box.textContent = message || '';
}

// 位置切换按钮（存储池 / 外接存储）：只有一个位置时不显示；未接入的置灰
function renderBrowseRoots() {
  const box = $('browseRoots');
  if (!box) return;
  if (browseLocations.length < 2) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  box.hidden = false;
  box.replaceChildren(...browseLocations.map((entry) => {
    const active = Number(entry.index) === browseRoot;
    const button = element('button', active ? 'active' : '',
                           entry.exists ? entry.label : `${entry.label}（未接入）`);
    button.type = 'button';
    button.dataset.root = String(entry.index);
    if (!entry.exists) {
      button.disabled = true;                 // 拔盘/不存在：不给点，避免白报错
      button.title = `这个位置当前不可用：${entry.path}`;
    } else {
      button.title = entry.path;
    }
    return button;
  }));
}

function folderRow(item) {
  // 一层子目录：点整行进入；`path` 是相对路径（服务端算好的）
  const row = element('button', 'folder-row');
  row.type = 'button';
  row.dataset.folder = item.path || '';
  row.append(element('span', 'folder-name', item.name));
  if (item.shared) row.append(element('span', 'chip', '已共享'));
  row.append(element('span', 'folder-go', '›'));
  return row;
}

// 逐层浏览：root 是位置序号，path 是相对该位置的路径（空串 = 位置根目录）
async function browse(path) {
  const account = currentAccount();
  if (!account) { toast('没有读到账号'); return; }
  browsePath = String(path || '').replace(/^\/+|\/+$/g, '');
  setBrowseError('');
  $('browseUp').disabled = !browsePath;
  $('browseList').replaceChildren(element('p', 'muted', '读取中…'));
  try {
    const data = await call(
      `browse?account=${encodeURIComponent(account)}&root=${browseRoot}`
      + `&path=${encodeURIComponent(browsePath)}`);
    browseLocations = data.locations || [];
    browseAbsolute = data.absolute || '';
    // 弹窗顶部显示**当前完整绝对路径**：选到的到底是哪块盘上一眼可见
    $('browsePath').textContent = browseAbsolute || '—';
    renderBrowseRoots();
    const items = data.items || [];
    if (!items.length) {
      $('browseList').replaceChildren(element('p', 'muted', '这个目录下没有子目录'));
    } else {
      $('browseList').replaceChildren(...items.map((item) => folderRow(item)));
    }
    $('selectFolder').disabled = !browseAbsolute;
  } catch (error) {
    browseAbsolute = '';
    $('browseList').replaceChildren();
    $('browsePath').textContent = '—';
    $('selectFolder').disabled = true;
    renderBrowseRoots();
    setBrowseError(error.message);            // 后端错误（越界/不存在/位置无效）原样显示
  }
}

async function openBrowse() {
  const account = currentAccount();
  if (!account) { toast('没有读到账号'); return; }
  browseRoot = 0;
  browsePath = '';
  browseAbsolute = '';
  browseLocations = [];
  $('manualPath').value = '';
  setBrowseError('');
  $('browsePath').textContent = '—';
  $('browseList').replaceChildren(element('p', 'muted', '读取中…'));
  $('browseDialog').showModal();
  await browse('');
}

// 添加共享：走现有的 /api/share/add（提交**绝对路径**），成功后刷新下半部列表
async function addShareAt(path, note) {
  const account = currentAccount();
  const value = String(path || '').trim();
  if (!account) { toast('没有读到账号'); return; }
  if (!value) { toast('请选择或填写目录的绝对路径'); return; }
  const button = $('selectFolder');
  button.disabled = true;
  busy = true;
  try {
    const data = await call('share/add', { account, path: value });
    if (data.status) render(data.status);
    $('browseDialog').close();
    toast(note || `已添加共享：${value}`);
    showError('');
  } catch (error) {
    // 校验失败（绝对路径 / 根白名单 / 越界 / 不存在）与 smb_mgr.sh 的失败都原样显示
    setBrowseError(error.message);
    showError(error.message);
  } finally {
    busy = false;
    button.disabled = false;
  }
}

// 行尾 `-`：二次确认后按共享段 id 删除，再刷新列表
async function removeShare(button) {
  const name = button.dataset.shareName || '';
  const label = button.dataset.display || name;
  if (!name) return;
  if (!window.confirm(`删除共享「${label}」？\n删除后 Windows「网络」里也会消失。`)) return;
  button.disabled = true;
  busy = true;
  try {
    const data = await call('share/delete', { shareName: name });
    if (data.status) render(data.status);     // 下半部列表按最新状态重画
    toast(`已删除共享「${label}」`);
    showError('');
  } catch (error) {
    showError(error.message);                 // 后端错误原样显示
  } finally {
    busy = false;
  }
}

// ---------------------------------------------------------------------------
// 事件绑定
// ---------------------------------------------------------------------------

$('back').onclick = leavePlugin;
$('refresh').onclick = () => { refresh(); };
$('discoverySwitch').addEventListener('change', (event) => {
  toggleDiscovery(event.target.checked);
});
// 名字不一致时的「恢复为系统主机名」（一致时按钮是 hidden 的，点不到）
$('nameRestore').onclick = restoreHostname;
$('addShare').onclick = openBrowse;
$('browseCancel').onclick = () => { $('browseDialog').close(); };
$('browseUp').onclick = () => {
  const parts = browsePath.split('/').filter(Boolean);
  parts.pop();
  browse(parts.join('/'));
};
$('selectFolder').onclick = () => addShareAt(browseAbsolute);
// 位置切换：浏览路径回到该位置的根部
$('browseRoots').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-root]');
  if (!button || button.disabled) return;
  browseRoot = Number(button.dataset.root) || 0;
  browse('');
});
// 点文件夹整行进入下一层
$('browseList').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-folder]');
  if (button) browse(button.dataset.folder);
});
// 手填绝对路径：回车或「直接添加」都直接提交（服务端照旧校验）
$('manualAdd').onclick = () => addShareAt($('manualPath').value);
$('manualPath').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); addShareAt($('manualPath').value); }
});
// 行尾 `-`：事件委托（列表每次都会被重画）
$('shares').addEventListener('click', (event) => {
  const button = event.target.closest('.share-remove');
  if (!button || button.disabled) return;
  removeShare(button);
});
// 点遮罩关闭（点 dialog 自身而不是内容区）
$('browseDialog').addEventListener('click', (event) => {
  if (event.target === $('browseDialog')) $('browseDialog').close();
});

refresh();
setInterval(() => {
  if (document.hidden) return;
  if ($('browseDialog').open) return;         // 弹窗开着时不要重排列表
  refresh();
}, 15000);
