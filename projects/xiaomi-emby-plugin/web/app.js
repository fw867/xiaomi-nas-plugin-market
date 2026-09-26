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
let browsePath = '';
let toastTimer = null;

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
  $('setup').hidden = state.configured;
  $('serviceActions').hidden = !state.configured;
  $('access').hidden = !state.configured;
  $('directory').textContent = state.configured ? state.directory : '—';
  $('configDirectory').textContent = state.configured
      ? (state.configDirectory || '插件私有目录（外部不可见）')
      : '—';
  $('address').textContent = state.address || '（请通过小米客户端打开插件以获取地址）';
  $('wizardHint').hidden = state.wizardCompleted !== false;
  $('toggleService').textContent = state.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = state.busy;
  $('setupForm').querySelector('button[type=submit]').disabled = state.busy;
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
  try {
    const state = await call('/status');
    current = state;
    render(state);
  } catch (error) {
    showError(error.message);
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

async function loadFolders() {
  let data;
  try {
    data = await call('/browse?path=' + encodeURIComponent(browsePath));
  } catch (error) {
    showError(error.message);
    return;
  }
  $('browsePath').textContent = browsePath || '用户存储根目录';
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

// 媒体目录、初始化时的配置目录、以及给已运行实例换配置目录，共用这个选择框。
let browseOnPick = null;
function openBrowser(title, startPath, onPick) {
  browseOnPick = onPick;
  browsePath = startPath || '';
  $('browseTitle').textContent = title;
  $('browse').showModal();
  loadFolders();
}

async function relocateConfig(path) {
  const confirmed = confirm(
    '把 Emby 的配置目录迁到「' + path + '」？\n\n' +
    '插件会先停容器，把现有配置复制到新目录，核对文件数无误后删除旧目录，再重建容器。' +
    '过程中 Emby 会短暂中断，媒体文件不受影响。');
  if (!confirmed) return;
  await act('/service/relocate', { configPath: path }, '正在迁移配置目录，请稍候');
}

$('choose').addEventListener('click', () => openBrowser('选择媒体目录', mediaSelection, (path) => {
  mediaSelection = path;
  $('mediaPath').value = path || '用户存储根目录';
}));

$('chooseConfig').addEventListener('click', () => openBrowser('选择配置目录', configSelection, (path) => {
  configSelection = path;
  $('configPath').value = path;
}));

$('moveConfig').addEventListener('click', () => openBrowser('选择新的配置目录', '', (path) => {
  if (!path) {
    showError('请选择存储里的一个目录（不能是存储根目录本身）');
    return;
  }
  relocateConfig(path);
}));

$('clearConfig').addEventListener('click', () => {
  configSelection = '';
  $('configPath').value = '';
});

$('up').addEventListener('click', () => {
  browsePath = browsePath.split('/').slice(0, -1).join('/');
  loadFolders();
});

$('selectFolder').addEventListener('click', () => {
  const picked = browsePath;
  const onPick = browseOnPick;
  browseOnPick = null;
  $('browse').close();
  if (onPick) onPick(picked);
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
