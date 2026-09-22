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
const session = document.querySelector('meta[name="jellyfin-session"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let state = null;
let mediaSelection = null;
let configSelection = '';
let browsePath = '';
let browseOnPick = null;
let toastTimer = null;
let polling = false;

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
  const headers = { 'X-Jellyfin-Session': session };
  if (body !== undefined) {
    headers['X-CSRF-Token'] = csrf;
    headers['Content-Type'] = 'application/json';
  }
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

function stateLabel(s) {
  if (s.busy) return '正在处理，请稍候';
  if (!s.configured) return '未初始化';
  if (!s.running) return '已停止';
  return s.ready ? '服务运行中' : '容器已启动，等待 Jellyfin 就绪';
}

function render(s) {
  $('serviceState').textContent = stateLabel(s);
  const dot = s.busy ? '' : (s.running && s.ready ? 'on' : 'off');
  $('statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
  $('setup').hidden = s.configured || s.busy;
  $('serviceActions').hidden = !s.configured;
  $('access').hidden = !(s.configured && s.running);
  $('directory').textContent = s.configured ? ('/' + (s.directory || '—')) : '—';
  $('configDirectory').textContent = s.configured
    ? (s.configDirectory ? '/' + s.configDirectory : '插件私有目录（外部不可见）')
    : '—';
  $('panelPort').textContent = String(s.port || 8097);
  $('address').textContent = s.address || '（请通过小米客户端打开插件以获取地址）';
  $('wizardHint').hidden = s.wizardCompleted !== false;
  $('toggleService').textContent = s.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = s.busy;
  const info = [];
  if (s.serverVersion) info.push('Jellyfin ' + s.serverVersion);
  if (s.imageVersion) info.push('镜像 ' + s.imageVersion);
  if (s.preview) info.push('预览模式，不会操作 Docker');
  $('serverInfo').textContent = info.join(' · ');
  $('serverInfo').hidden = !info.length;
  if (!s.busy) showError(s.error);
}

async function refresh() {
  if (polling) return;
  polling = true;
  try {
    state = await call('/status');
    render(state);
  } catch (e) {
    showError(e.message);
  } finally {
    polling = false;
  }
}

async function loadFolders() {
  let data;
  try {
    data = await call('/browse?path=' + encodeURIComponent(browsePath));
  } catch (e) {
    $('folders').textContent = e.message;
    return;
  }
  $('browsePath').textContent = browsePath ? '/' + browsePath : '用户存储根目录';
  $('up').disabled = !browsePath;
  const list = $('folders');
  list.replaceChildren();
  for (const item of data.items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = item.name;
    button.onclick = () => { browsePath = item.path; loadFolders(); };
    list.append(button);
  }
  if (!data.items.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '此目录下没有子目录';
    list.append(empty);
  }
}

function openBrowser(title, startPath, onPick) {
  browseOnPick = onPick;
  browsePath = startPath || '';
  $('browseTitle').textContent = title;
  $('browse').showModal();
  loadFolders();
}

$('refresh').onclick = () => refresh();
$('choose').onclick = () => openBrowser('选择媒体目录', mediaSelection, (path) => {
  mediaSelection = path;
  $('mediaPath').value = path ? '/' + path : '';
});
$('chooseConfig').onclick = () => openBrowser('选择配置目录', configSelection, (path) => {
  configSelection = path;
  $('configPath').value = path ? '/' + path : '';
});
$('clearConfig').onclick = () => { configSelection = ''; $('configPath').value = ''; };
$('up').onclick = () => {
  browsePath = browsePath.split('/').slice(0, -1).join('/');
  loadFolders();
};
$('selectFolder').onclick = () => {
  const picked = browsePath;
  const onPick = browseOnPick;
  browseOnPick = null;
  $('browse').close();
  if (onPick) onPick(picked);
};
document.querySelectorAll('[data-close]').forEach((el) => {
  el.onclick = () => $(el.dataset.close).close();
});
$('copyAddress').onclick = async () => {
  const text = $('address').textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制地址');
  } catch (e) {
    toast('复制失败，请手动记录：' + text);
  }
};
$('setupForm').onsubmit = async (e) => {
  e.preventDefault();
  if (mediaSelection === null) {
    showError('请选择媒体目录');
    return;
  }
  const submit = e.target.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    await call('/setup', { path: mediaSelection, configPath: configSelection });
    toast('正在初始化并启动，首次需要拉取镜像');
    e.target.reset();
    mediaSelection = null;
    configSelection = '';
    $('mediaPath').value = '';
    $('configPath').value = '';
    await refresh();
  } catch (err) {
    toast(err.message);
  } finally {
    submit.disabled = false;
  }
};
$('toggleService').onclick = async () => {
  if (!state) return;
  const button = $('toggleService');
  button.disabled = true;
  try {
    await call('/service/' + (state.running ? 'stop' : 'start'), {});
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
};

async function tick() {
  if (!$('jellyfin-app').isConnected) return;
  if (!document.hidden) await refresh();
  setTimeout(tick, 5000);
}
tick();
