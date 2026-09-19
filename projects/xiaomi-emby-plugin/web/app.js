'use strict';
const $ = (id) => document.getElementById(id);
const session = document.querySelector('meta[name="emby-session"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let current = null;
let mediaSelection = null;
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
  const response = await fetch(path, {
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
  $('setup').hidden = state.configured;
  $('serviceActions').hidden = !state.configured;
  $('access').hidden = !state.configured;
  $('directory').textContent = state.configured ? '媒体目录 ' + state.directory : '';
  $('address').textContent = state.address || '（请通过小米客户端打开插件以获取地址）';
  $('wizardHint').hidden = state.wizardCompleted !== false;
  $('toggleService').textContent = state.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = state.busy;
  $('setupForm').querySelector('button[type=submit]').disabled = state.busy;
  const info = [];
  if (state.serverVersion) info.push('Emby ' + state.serverVersion);
  if (state.imageVersion) info.push('镜像 ' + state.imageVersion);
  if (state.preview) info.push('预览模式，不会操作 Docker');
  $('serverInfo').textContent = info.join(' · ');
  $('serverInfo').hidden = !info.length;
  if (!state.busy) showError(state.error);
}

async function refresh() {
  try {
    const state = await call('/api/status');
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
    data = await call('/api/browse?path=' + encodeURIComponent(browsePath));
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
  act('/api/setup', { path: mediaSelection }, '正在初始化并启动，首次需要拉取镜像');
});

$('toggleService').addEventListener('click', () => {
  const action = current && current.running ? 'stop' : 'start';
  act('/api/service/' + action, {}, action === 'start' ? '正在启动服务' : '正在停止服务');
});

$('choose').addEventListener('click', () => {
  browsePath = mediaSelection || '';
  $('browse').showModal();
  loadFolders();
});

$('up').addEventListener('click', () => {
  browsePath = browsePath.split('/').slice(0, -1).join('/');
  loadFolders();
});

$('selectFolder').addEventListener('click', () => {
  mediaSelection = browsePath;
  $('mediaPath').value = browsePath || '用户存储根目录';
  $('browse').close();
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
