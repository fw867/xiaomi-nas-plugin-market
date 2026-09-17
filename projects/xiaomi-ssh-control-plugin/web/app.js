const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
const statusHint = document.getElementById('statusHint');
const autostartToggle = document.getElementById('autostartToggle');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const note = document.getElementById('note');
const toast = document.getElementById('toast');

let busy = false;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('visible');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('visible'), 3200);
}

function render(data) {
  const running = Boolean(data.sshRunning);
  statusDot.className = `status-dot ${running ? 'on' : 'off'}`;
  statusText.textContent = running ? 'SSH 正在运行' : 'SSH 已停止';
  statusHint.textContent = running
    ? '可以从局域网用 SSH 登录此设备'
    : 'SSH 端口未监听，远程登录不可用';

  autostartToggle.checked = Boolean(data.autostart);
  startBtn.disabled = busy || running;
  stopBtn.disabled = busy || !running;

  note.textContent = data.autostart
    ? '开机自启已开启：系统每次启动后会在一分钟内自动拉起 SSH。'
    : '开机自启已关闭：小 Mi 系统会在每次开机时关闭 SSH，手动启动只在本次开机内有效。';
}

async function request(path, body) {
  const init = { method: body ? 'POST' : 'GET', cache: 'no-store' };
  if (body) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`api${path}`, init);
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务返回异常（HTTP ${response.status}）`);
  }
  if (!response.ok || !payload.ok) throw new Error(payload.error || '操作失败');
  return payload;
}

async function refresh() {
  try {
    render(await request('/status'));
  } catch (error) {
    statusDot.className = 'status-dot off';
    statusText.textContent = '无法读取状态';
    statusHint.textContent = error.message;
    startBtn.disabled = true;
    stopBtn.disabled = true;
  }
}

async function act(fn, successMessage) {
  if (busy) return;
  busy = true;
  try {
    const data = await fn();
    render(data);
    if (successMessage) showToast(successMessage);
  } catch (error) {
    showToast(error.message);
    await refresh();
  } finally {
    busy = false;
  }
}

startBtn.addEventListener('click', () =>
  act(() => request('/action', { action: 'start' }), 'SSH 已启动'));

stopBtn.addEventListener('click', () =>
  act(() => request('/action', { action: 'stop' }), 'SSH 已停止，开机自启同时关闭'));

autostartToggle.addEventListener('change', () => {
  const enabled = autostartToggle.checked;
  autostartToggle.disabled = true;
  act(
    () => request('/autostart', { enabled }),
    enabled ? '已开启开机自启' : '已关闭开机自启',
  ).finally(() => { autostartToggle.disabled = false; });
});

document.getElementById('refreshButton').addEventListener('click', refresh);
document.getElementById('backButton').addEventListener('click', () =>
  (history.length > 1 ? history.back() : window.close()));

refresh();
