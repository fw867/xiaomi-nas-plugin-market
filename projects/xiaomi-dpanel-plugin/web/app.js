'use strict';
const $ = (id) => document.getElementById(id);
const session = document.querySelector('meta[name="dpanel-session"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let state = null;
let configSelection = '';
let browsePath = '';
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
  const headers = { 'X-DPanel-Session': session };
  if (body !== undefined) {
    headers['X-CSRF-Token'] = csrf;
    headers['Content-Type'] = 'application/json';
  }
  // 相对路径：插件页挂在 /plugin/<用户>/dpanel/ 下
  const response = await fetch('api' + path, {
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
  return s.ready ? '服务运行中' : '容器已启动，等待 DPanel 就绪';
}

function render(s) {
  $('serviceState').textContent = stateLabel(s);
  const dot = s.busy ? '' : (s.running && s.ready ? 'on' : 'off');
  $('statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
  $('setup').hidden = s.configured || s.busy;
  $('serviceActions').hidden = !s.configured;
  $('access').hidden = !(s.configured && s.running);
  $('configDirectory').textContent = s.configured
    ? (s.configDirectory || '插件私有目录（外部不可见）')
    : '—';
  $('panelPort').textContent = String(s.port || 8807);
  $('address').textContent = s.address || '（请通过小米客户端打开插件以获取地址）';
  $('toggleService').textContent = s.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = s.busy;
  const info = [];
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

async function browse(path) {
  browsePath = path;
  $('browsePath').textContent = path ? '/' + path : '用户存储根目录';
  $('folders').textContent = '正在读取';
  $('selectFolder').disabled = true;
  $('up').disabled = !path;
  try {
    const result = await call('/browse?path=' + encodeURIComponent(path));
    $('folders').innerHTML = result.items.map(item =>
      `<button type="button" data-folder="${item.path}">${item.name}</button>`
    ).join('') || '<p class="muted">此目录下没有子文件夹</p>';
    $('selectFolder').disabled = !path;
  } catch (e) {
    $('folders').textContent = e.message;
  }
}

$('refresh').onclick = () => refresh();
$('chooseConfig').onclick = () => { $('browse').showModal(); browse(configSelection || ''); };
$('clearConfig').onclick = () => { configSelection = ''; $('configPath').value = ''; };
$('up').onclick = () => browse(browsePath.split('/').slice(0, -1).join('/'));
$('selectFolder').onclick = () => {
  configSelection = browsePath;
  $('configPath').value = browsePath ? '/' + browsePath : '';
  $('browse').close();
};
$('folders').onclick = (e) => {
  const button = e.target.closest('button[data-folder]');
  if (button) browse(button.dataset.folder);
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
  const form = e.target;
  const submit = form.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    await call('/service/setup', { configPath: configSelection });
    form.reset();
    configSelection = '';
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
  if (!$('dpanel-app').isConnected) return;
  if (!document.hidden) await refresh();
  setTimeout(tick, 3000);
}
tick();
