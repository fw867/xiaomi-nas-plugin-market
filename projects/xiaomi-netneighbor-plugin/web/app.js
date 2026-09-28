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
  // 每行：目录名（+ Windows 里显示的共享名），行尾一个 X 删除按钮。
  // 只有插件自建（deletable）的行才有 X；官方 App 建的那条不显示。
  const row = element('div', 'share-row');
  const info = element('div', 'share-info');
  info.append(element('div', 'share-name', share.display || share.name || '（未命名）'));
  if (share.display && share.name && share.display !== share.name) {
    info.append(element('div', 'share-sub', `共享名 ${share.name}`));
  }
  row.append(info);
  if (share.deletable) {
    const remove = element('button', 'share-remove', '\u00d7');
    remove.type = 'button';
    remove.title = `删除共享 ${share.display || share.name}`;
    remove.setAttribute('aria-label', `删除共享 ${share.display || share.name}`);
    remove.addEventListener('click', () => deleteShare(share));
    row.append(remove);
  }
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

async function deleteShare(share) {
  const label = share.display || share.name;
  if (!window.confirm(`确定删除共享「${label}」？\n只删除共享，不会删除目录里的文件。`)) return;
  busy = true;
  try {
    const data = await call('share/delete', { shareName: share.name });
    render(data.status);
    toast(`已删除共享「${label}」`);
    if (data.result && data.result.errors && data.result.errors.length) {
      showError(`已完成，但收尾有提示：${data.result.errors.join('；')}`);
    } else {
      showError('');
    }
  } catch (error) {
    showError(error.message);
    toast('删除失败');
  } finally {
    busy = false;
  }
}

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
  if (item.shared) {
    // 已经共享的目录：默认选中且禁用（删除走列表行尾的 X）
    box.checked = true;
    box.disabled = true;
    row.classList.add('shared');
  }
  row.append(box);
  row.append(element('span', 'dir-name', item.name));
  if (item.shared) row.append(element('span', 'chip', '已共享'));
  return row;
}

async function openDirDialog() {
  const account = currentAccount();
  if (!account) { toast('没有读到账号'); return; }
  const dialog = $('dirDialog');
  $('dirRoot').textContent = '';
  $('dirError').textContent = '';
  $('dirList').replaceChildren(element('p', 'muted', '读取中…'));
  dialog.showModal();
  try {
    const data = await call(`dirs?account=${encodeURIComponent(account)}`);
    const dirs = data.dirs || [];
    $('dirRoot').textContent = `数据目录：${data.root || '—'}`;
    if (!dirs.length) {
      $('dirList').replaceChildren(element('p', 'muted', '这个目录下没有子目录'));
      return;
    }
    $('dirList').replaceChildren(...dirs.map((item) => dirRow(item)));
  } catch (error) {
    $('dirList').replaceChildren();
    $('dirError').textContent = error.message;
  }
}

async function confirmAddShares() {
  const account = currentAccount();
  const boxes = [...document.querySelectorAll('#dirList .dir-box')]
    .filter((box) => box.checked && !box.disabled);
  if (!boxes.length) { toast('请勾选要共享的目录'); return; }
  const button = $('dirConfirm');
  button.disabled = true;
  busy = true;
  try {
    const data = await call('share/add', {
      account,
      paths: boxes.map((box) => box.value),
    });
    render(data.status);
    $('dirDialog').close();
    const result = data.result || {};
    const added = (result.added || []).length;
    const errors = (result.errors || []).map((item) => item.error || String(item));
    if (added) {
      // SMB 客户端会缓存共享列表：刚加完立刻双击可能报错，提示一句省得以为是没生效
      toast(`已添加 ${added} 个共享（Windows 里可能要重开资源管理器才看得到）`);
    }
    showError(errors.length ? `已完成，但有提示：${errors.join('；')}` : '');
  } catch (error) {
    $('dirError').textContent = error.message;
    showError(error.message);
  } finally {
    busy = false;
    button.disabled = false;
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
$('addShare').onclick = openDirDialog;
$('dirCancel').onclick = () => { $('dirDialog').close(); };
$('dirConfirm').onclick = confirmAddShares;
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
