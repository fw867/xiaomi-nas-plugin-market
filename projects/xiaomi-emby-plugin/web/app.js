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
const session = document.querySelector('meta[name="emby-session"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let current = null;
let mediaSelection = null;
let configSelection = '';
let browseRoot = 0;       // 当前浏览的「存储位置」序号（state.roots 的下标）
let browsePath = '';      // 该位置内的相对路径，空串表示位置根部
let toastTimer = null;
let polling = false;      // 防止 5 秒轮询叠加请求

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
  const headers = { 'X-Emby-Session': session };
  if (body !== undefined) {
    headers['X-CSRF-Token'] = csrf;
    headers['Content-Type'] = 'application/json';
  }
  // 必须用相对路径：插件页挂在 /plugin/<用户>/emby/ 下，
  // 写成 /api/... 会打到站点根，被 nginx 判成 404。
  const response = await fetch(assetUrl('api' + path), {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || '请求失败（HTTP ' + response.status + '）');
  }
  return data;
}

function stateLabel(state) {
  if (state.busy) return '正在处理，请稍候';
  if (!state.configured) return '未初始化';
  if (!state.running) return '已停止';
  return state.ready ? '运行中' : '容器已启动，等待 Emby 就绪';
}

function render(state) {
  $('serviceState').textContent = stateLabel(state);
  // 圆点和文案同源，避免出现「绿点 + 已停止」这种自相矛盾的画面
  const dot = state.busy ? '' : (state.running && state.ready ? 'on' : 'off');
  $('statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
  // 忙的时候也保持显示：下面有目录设置与进度提示，不该整块消失
  $('setup').hidden = state.configured;
  $('serviceActions').hidden = !state.configured;
  $('access').hidden = !state.configured;
  $('directorySettings').hidden = !state.configured;
  // 状态卡片显示完整绝对路径：存储位置多于一个时，相对路径看不出在哪
  const mediaAbs = state.media_abs || state.directory || '';
  const configAbs = state.config_abs || state.configDirectory || '';
  const configNote = state.configured && state.config_private ? '（插件私有目录，外部不可见）' : '';
  $('directory').textContent = state.configured ? (mediaAbs || '—') : '—';
  $('directory').title = state.configured ? mediaAbs : '';
  $('configDirectory').textContent = state.configured ? (configAbs || '—') + configNote : '—';
  $('configDirectory').title = state.configured ? configAbs + configNote : '';
  $('address').textContent = state.address || '（请通过小米客户端打开插件以获取地址）';
  $('wizardHint').hidden = state.wizardCompleted !== false;
  $('toggleService').textContent = state.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = state.busy;
  $('setupForm').querySelector('button[type=submit]').disabled = state.busy;
  $('reconfigure').disabled = state.busy;
  $('reset').disabled = state.busy;
  const info = [];
  if (state.serverVersion) info.push('Emby ' + state.serverVersion);
  if (state.imageVersion) info.push('镜像 ' + state.imageVersion);
  if (state.healthcheckOff) info.push('健康检查已关，不会定时唤醒硬盘');
  if (state.preview) info.push('预览模式，不会操作 Docker');
  $('serverInfo').textContent = info.join(' · ');
  $('serverInfo').hidden = !info.length;
  if (!state.busy) showError(state.error);
}

async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const state = await call('/status');
    current = state;
    render(state);
  } catch (error) {
    showError(error.message);
  } finally {
    polling = false;
  }
}

async function act(path, body, message) {
  showError('');
  try {
    await call(path, body);
    toast(message);
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

// 后台操作是异步的（换目录要重建容器、等 Emby 就绪，可能好几分钟）：
// 轮询状态直到 busy 结束，再刷新状态卡片；失败原因由后端写进 error，原样显示。
async function waitForOperation() {
  for (let i = 0; i < 900; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    await refresh();
    if (current && !current.busy) return;
  }
}

// 当前「存储位置」（内置存储池 / 外接设备），由 state.roots 提供
function currentRoot() {
  const roots = (current && current.roots) || [];
  return roots[browseRoot] || roots[0] || null;
}

// 根内相对路径 → 完整绝对路径（位置根部就显示根的绝对路径）
function absoluteBrowsePath() {
  const root = currentRoot();
  const base = root ? String(root.path || '').replace(/\/+$/, '') : '';
  if (!browsePath) return base;
  return base ? base + '/' + browsePath : browsePath;
}

// 绝对路径 →（位置序号，根内相对路径）：与后端一致做最长前缀匹配，
// 找不到就退回第 0 个位置的根部（用户重新选一个即可）。
function locateRoot(value) {
  const roots = (current && current.roots) || [];
  const text = String(value || '').replace(/\\/g, '/').replace(/\/+$/, '');
  let index = -1;
  let length = -1;
  roots.forEach((root, position) => {
    const base = String(root.path || '').replace(/\\/g, '/').replace(/\/+$/, '');
    if (base && (text === base || text.startsWith(base + '/')) && base.length > length) {
      index = position;
      length = base.length;
    }
  });
  if (index < 0) return { root: 0, path: '' };
  const base = String(roots[index].path || '').replace(/\\/g, '/').replace(/\/+$/, '');
  return { root: index, path: text === base ? '' : text.slice(base.length + 1) };
}

// 「位置」切换按钮：只有一个存储位置时不显示
function renderRoots() {
  const roots = (current && current.roots) || [];
  const box = $('roots');
  box.hidden = roots.length < 2;
  box.replaceChildren();
  if (roots.length < 2) return;
  roots.forEach((root, position) => {
    const index = Number.isInteger(root.index) ? root.index : position;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn small' + (index === browseRoot ? ' active' : '');
    button.textContent = (root.label || root.path) + (root.exists ? '' : '（不可用）');
    button.addEventListener('click', () => {
      if (index === browseRoot) return;
      browseRoot = index;
      browsePath = '';                 // 切换位置后从该位置根部重新浏览
      renderRoots();
      loadFolders();
    });
    box.append(button);
  });
}

async function loadFolders() {
  let data;
  try {
    data = await call('/browse?root=' + encodeURIComponent(browseRoot)
      + '&path=' + encodeURIComponent(browsePath));
  } catch (error) {
    showError(error.message);
    $('folders').replaceChildren();
    return;
  }
  const shown = absoluteBrowsePath();
  $('browsePath').textContent = shown || '用户存储根目录';
  $('browsePath').title = shown;
  $('up').disabled = !browsePath;
  const list = $('folders');
  list.replaceChildren();
  for (const item of data.items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = item.name;
    button.addEventListener('click', () => { browsePath = item.path; loadFolders(); });
    list.append(button);
  }
  if (!data.items.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '此目录下没有子目录';
    list.append(empty);
  }
}

$('setupForm').addEventListener('submit', (event) => {
  event.preventDefault();
  if (mediaSelection === null) {
    showError('请选择媒体目录');
    return;
  }
  act('/setup', { path: mediaSelection, configPath: configSelection },
      '正在初始化并启动，首次需要拉取镜像');
});

$('toggleService').addEventListener('click', () => {
  const action = current && current.running ? 'stop' : 'start';
  act('/service/' + action, {}, action === 'start' ? '正在启动服务' : '正在停止服务');
});

// 目录选择弹窗：初始化表单的媒体/配置目录、以及「修改目录」都复用它。
let browseOnPick = null;
let pickTarget = '';      // 'media' 时在弹窗底部提示已选的媒体目录
function openBrowser(title, startPath, onPick) {
  browseOnPick = onPick;
  const located = locateRoot(startPath);
  browseRoot = located.root;
  browsePath = located.path;
  $('browseTitle').textContent = title;
  $('pickHint').hidden = pickTarget !== 'media' || !mediaSelection;
  $('pickedPaths').textContent = mediaSelection || '';
  renderRoots();
  $('browse').showModal();
  loadFolders();
}

// 选中的目录写回初始化表单：表单里显示的始终是完整绝对路径
function syncSelections() {
  $('mediaPath').value = mediaSelection || '';
  $('configPath').value = configSelection || '';
}

function chooseInto(target, title) {
  pickTarget = target;
  const start = target === 'media' ? mediaSelection : configSelection;
  try {
    openBrowser(title, start || '', (path) => {
      $('pickHint').hidden = true;
      if (target === 'media') {
        mediaSelection = path || null;
      } else {
        configSelection = path || '';
      }
      syncSelections();
    });
  } catch (error) {
    showError(error.message);
  }
}

$('choose').addEventListener('click', () => chooseInto('media', '选择媒体目录'));
$('chooseConfig').addEventListener('click', () => chooseInto('config', '选择配置目录'));

$('clearConfig').addEventListener('click', () => {
  configSelection = '';
  syncSelections();
});

$('up').addEventListener('click', () => {
  browsePath = browsePath.split('/').slice(0, -1).join('/');
  loadFolders();
});

$('selectFolder').addEventListener('click', () => {
  // 提交绝对路径；服务端两种（绝对 / 根内相对）都接受
  const picked = absoluteBrowsePath();
  const onPick = browseOnPick;
  browseOnPick = null;
  $('browse').close();
  if (onPick) onPick(picked);
});

$('reconfigure').addEventListener('click', async () => {
  // 没重新选就沿用当前值（修改目录只换位置，不迁数据）
  const media = mediaSelection || (current && current.media_abs) || '';
  const config = configSelection || (current && current.config_abs) || '';
  if (!media) {
    showError('请先选择媒体目录');
    return;
  }
  const confirmed = confirm(
    '修改 Emby 的目录设置？\n\n' +
    '媒体目录：' + media + '\n' +
    '配置目录：' + config + '\n\n' +
    '只会换位置、不会删除已有文件，但会重建容器（挂载只在创建容器时生效）：旧容器会被停掉并删除，再按新目录创建，Emby 会中断一会儿。\n' +
    '新位置若没有原 Emby 配置，应用会以全新状态启动，需要重新跑一遍初始化向导。');
  if (!confirmed) return;
  try {
    await call('/service/reconfigure', { path: media, configPath: config });
    toast('正在换目录并重建容器，请稍候');
    mediaSelection = null;
    configSelection = '';
    syncSelections();
    await waitForOperation();
  } catch (error) {
    showError(error.message);
  }
});

$('reset').addEventListener('click', async () => {
  const confirmed = confirm(
    '确定要重新初始化 Emby 插件？\n\n' +
    '· 会停止并移除本插件的容器；\n' +
    '· 会清空插件配置（原设置文件会备份保留），页面回到初始化表单；\n' +
    '· 用户数据目录不受影响：媒体目录与配置目录里的文件一个都不会删。\n\n' +
    '之后需要重新选择目录并初始化。');
  if (!confirmed) return;
  try {
    await call('/service/reset', { confirm: true });
    toast('已重新初始化，请重新选择目录');
    mediaSelection = null;
    configSelection = '';
    syncSelections();
    await waitForOperation();
  } catch (error) {
    showError(error.message);
  }
});

$('copyAddress').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('address').textContent);
    toast('已复制 Emby 地址');
  } catch (error) {
    toast('复制失败，请手动记录地址');
  }
});

$('refresh').addEventListener('click', refresh);

document.querySelectorAll('[data-close]').forEach((button) => {
  button.addEventListener('click', () => $(button.dataset.close).close());
});

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
