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

// 每 15 秒会重新渲染一次：正在编辑的输入框不能被后台刷新覆盖
function setValue(id, value) {
  const node = $(id);
  if (!node || document.activeElement === node) return;
  node.value = value === undefined || value === null ? '' : String(value);
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

  const toggle = $('discoverySwitch');
  toggle.checked = enabled;
  setValue('hostnameInput', settings.hostname || discovery.hostname || '');
}

function shareRow(share) {
  // 只读的一行：目录名（+ Windows 里显示的共享名）。
  // 增删都在「编辑共享」弹窗里用勾选/取消勾选完成，这里不再有行尾的 X 按钮。
  const row = element('div', 'share-row');
  const info = element('div', 'share-info');
  info.append(element('div', 'share-name', share.display || share.name || '（未命名）'));
  if (share.display && share.name && share.display !== share.name) {
    info.append(element('div', 'share-sub', `共享名 ${share.name}`));
  }
  row.append(info);
  return row;
}

function renderAccounts(accounts) {
  const box = $('accounts');
  const line = $('accountLine');
  if (!accounts || !accounts.length) {
    line.textContent = '';
    box.replaceChildren(element('p', 'muted', '没有读到任何 Samba 账号'));
    return;
  }
  const current = accounts[0];
  line.textContent = current.sambaUser
    ? `账号 ${current.account}（SMB 用户 ${current.sambaUser}）` : `账号 ${current.account}`;
  const shares = current.shares || [];
  if (!shares.length) {
    box.replaceChildren(element('p', 'muted', '还没有共享目录'));
    return;
  }
  box.replaceChildren(...shares.map((share) => shareRow(share)));
}

function render(data) {
  state = data;
  renderDiscovery(data);
  renderAccounts(data.accounts || []);
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
// 主机名
// ---------------------------------------------------------------------------

async function saveHostname() {
  const button = $('hostnameSave');
  const hostname = $('hostnameInput').value.trim();
  if (!hostname) { toast('请填写主机名'); return; }
  button.disabled = true;
  busy = true;
  try {
    const data = await call('hostname', { hostname });
    render(data.status);
    toast(`主机名已改为 ${hostname}`);
    showError('');
  } catch (error) {
    showError(error.message);
    toast('保存失败');
  } finally {
    busy = false;
    button.disabled = false;
  }
}

// 恢复默认：清掉插件里存的名字，回到系统配置（/etc/config/samba 的 option name）里的名字
async function resetHostname() {
  const button = $('hostnameReset');
  button.disabled = true;
  busy = true;
  try {
    const data = await call('hostname', { reset: true });
    render(data.status);
    const settings = data.status.settings || {};
    const discovery = data.status.discovery || {};
    toast(`已恢复默认主机名 ${settings.hostname || discovery.hostname || ''}`.trim());
    showError('');
  } catch (error) {
    showError(error.message);
    toast('恢复失败');
  } finally {
    busy = false;
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 共享目录
// ---------------------------------------------------------------------------

function currentAccount() {
  const accounts = (state && state.accounts) || [];
  return accounts.length ? accounts[0].account : '';
}

function dirRow(item) {
  const row = element('label', 'dir-item');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.value = item.path;
  box.className = 'dir-box';
  // 记住原始状态：确定时用它算差异（勾上的是新增、取消勾选的是删除）
  box.dataset.shared = item.shared ? '1' : '0';
  box.dataset.shareName = item.shareName || '';
  const locked = Boolean(item.shared) && !item.deletable;
  if (item.shared) {
    box.checked = true;
    row.classList.add('shared');
  }
  if (locked) {
    // 官方 App 建的共享（deletable === false）：插件删不了，锁死别让用户白点
    box.disabled = true;
    box.title = '由小米 App 管理的共享，插件不能删除';
  }
  row.append(box);
  row.append(element('span', 'dir-name', item.name));
  if (item.shared) row.append(element('span', 'chip', '已共享'));
  return row;
}

function setDialogError(message) {
  const box = $('dirError');
  if (box) box.textContent = message || '';
  showError(message || '');
}

// 一个「位置」（存储池 / 外接设备 / 其它挂载点）：标题 + 根路径 + 该根下的一层子目录。
// 根不存在（外接设备拔了）时 available === false：只提示「未接入」，不报错。
function dirGroup(location) {
  const group = element('section', 'dir-group');
  const head = element('div', 'dir-group-head');
  head.append(element('span', 'dir-group-label', location.label || '目录'));
  head.append(element('span', 'dir-group-root', location.root || ''));
  group.append(head);
  if (location.available === false) {
    head.append(element('span', 'chip', '未接入'));
    group.classList.add('off');
    group.append(element('p', 'muted', '这个位置当前不可用，插入设备后再刷新'));
    return group;
  }
  const dirs = location.dirs || [];
  if (!dirs.length) {
    group.append(element('p', 'muted', '这个位置下没有子目录'));
    return group;
  }
  group.append(...dirs.map((item) => dirRow(item)));
  return group;
}

// 手填的绝对路径也算一个已勾选的条目（与勾选项合并提交，服务端照旧做
// 绝对路径 / 根白名单 / 越界与软链真实路径校验，错误原样显示）。
function addManualPath() {
  const input = $('dirPathInput');
  const value = (input.value || '').trim().replace(/\/+$/, '');
  if (!value) { toast('请填写目录的绝对路径'); return; }
  if (!value.startsWith('/')) { toast('要填绝对路径（以 / 开头）'); return; }
  const boxes = [...document.querySelectorAll('#dirDialog .dir-box')];
  if (boxes.some((box) => String(box.value).replace(/\/+$/, '') === value)) {
    toast('这个目录已经在列表里了');
    input.value = '';
    return;
  }
  const row = dirRow({
    name: value, path: value, shared: false, shareName: '', display: '', deletable: false,
  });
  row.querySelector('.dir-box').checked = true;
  $('dirList').append(row);
  input.value = '';
}

async function openDirDialog() {
  const account = currentAccount();
  if (!account) { toast('没有读到账号'); return; }
  const dialog = $('dirDialog');
  $('dirRoot').textContent = '';
  setDialogError('');
  $('dirList').replaceChildren(element('p', 'muted', '读取中…'));
  $('dirPathInput').value = '';
  dialog.showModal();
  try {
    const data = await call(`dirs?account=${encodeURIComponent(account)}`);
    const dirs = data.dirs || [];
    const locations = Array.isArray(data.locations) && data.locations.length
      ? data.locations : null;
    $('dirRoot').textContent = `数据目录：${data.root || '—'}`;
    if (locations) {
      // 新后端：按「位置」分组（存储池 / 外接设备），外接设备没插上时标成「未接入」
      $('dirList').replaceChildren(...locations.map((item) => dirGroup(item)));
    } else if (!dirs.length) {
      // 老后端（没有 locations）：退回原来的一层列表渲染
      $('dirList').replaceChildren(element('p', 'muted', '这个目录下没有子目录'));
    } else {
      $('dirList').replaceChildren(...dirs.map((item) => dirRow(item)));
    }
  } catch (error) {
    $('dirList').replaceChildren();
    $('dirError').textContent = error.message;
  }
}

// 「编辑共享」的确定：只按**差异**发请求（先加后删），一次请求处理一类改动。
async function confirmEditShares() {
  const account = currentAccount();
  // 勾选项与手填的路径都在弹窗里（手填的会加成一个已勾选条目），一起提交
  const boxes = [...document.querySelectorAll('#dirDialog .dir-box')];
  const toAdd = boxes.filter((box) => box.checked && box.dataset.shared === '0');
  const toRemove = boxes.filter(
    (box) => !box.checked && box.dataset.shared === '1' && !box.disabled);
  if (!toAdd.length && !toRemove.length) {
    toast('没有改动');
    $('dirDialog').close();
    return;
  }
  const button = $('dirConfirm');
  button.disabled = true;
  busy = true;
  let error = '';
  try {
    let status = null;
    let added = 0;
    let removed = 0;
    let notes = [];
    if (toAdd.length) {
      const data = await call('share/add', { account, paths: toAdd.map((box) => box.value) });
      status = data.status || status;
      const result = data.result || {};
      added = (result.added || []).length;
      notes = notes.concat(
        (result.errors || []).map((item) => item.error || String(item)));
    }
    if (toRemove.length) {
      // 一次请求删掉所有取消勾选的（不要循环发单条删除）
      const data = await call('share/delete', {
        shareNames: toRemove.map((box) => box.dataset.shareName).filter(Boolean),
      });
      status = data.status || status;
      const result = data.result || {};
      removed = (result.removed || []).length;
      notes = notes.concat(result.errors || []);
    }
    if (status) render(status);
    $('dirDialog').close();
    if (added && removed) {
      toast(`已添加 ${added} 个、移除 ${removed} 个共享（Windows 里可能要重开资源管理器才看得到）`);
    } else if (added) {
      // SMB 客户端会缓存共享列表：刚加完立刻双击可能报错，提示一句省得以为是没生效
      toast(`已添加 ${added} 个共享（Windows 里可能要重开资源管理器才看得到）`);
    } else if (removed) {
      toast(`已移除 ${removed} 个共享（Windows 里可能要重开资源管理器才看得到）`);
    } else {
      // 手填的路径没通过校验（绝对路径/白名单/不存在）时走到这里：原因看下面的提示
      toast('没有共享被添加或移除，请看页面上的提示');
    }
    showError(notes.length ? `已完成，但有提示：${notes.join('；')}` : '');
  } catch (err) {
    error = err.message || '操作失败';
  } finally {
    busy = false;
    button.disabled = false;
  }
  if (error) {
    // 弹窗已经关掉（请求中途被关）时错误只能给全局提示，避免提示丢失
    if ($('dirDialog').open) setDialogError(error);
    else showError(error);
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
$('hostnameSave').onclick = saveHostname;
$('hostnameReset').onclick = resetHostname;
$('hostnameInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); saveHostname(); }
});
$('editShare').onclick = openDirDialog;
$('dirCancel').onclick = () => { $('dirDialog').close(); };
$('dirConfirm').onclick = confirmEditShares;
// 手填绝对路径（/nas/mnt 下的目录，如 /nas/mnt/usb）：回车或「添加」都算加一个待共享目录
$('dirPathAdd').onclick = addManualPath;
$('dirPathInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); addManualPath(); }
});
// 点遮罩关闭（点 dialog 自身而不是内容区）
$('dirDialog').addEventListener('click', (event) => {
  if (event.target === $('dirDialog')) $('dirDialog').close();
});

refresh();
setInterval(() => {
  if (document.hidden) return;
  if ($('dirDialog').open) return;          // 弹窗开着时不要重排列表
  refresh();
}, 15000);
