const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
const statusHint = document.getElementById('statusHint');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const restartBtn = document.getElementById('restartBtn');
const saveBtn = document.getElementById('saveBtn');
const reloadBtn = document.getElementById('reloadBtn');
const groupsBox = document.getElementById('groups');
const form = document.getElementById('settingsForm');
const note = document.getElementById('note');
const toast = document.getElementById('toast');
const webControlCard = document.getElementById('webControlCard');
const webControlLink = document.getElementById('webControlLink');
const missingCard = document.getElementById('missingCard');
const missingHint = document.getElementById('missingHint');
const autostartHint = document.getElementById('autostartHint');

const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64];

let schema = null;
let busy = false;
const controls = new Map();

function showToast(message, isError) {
  toast.textContent = message;
  toast.classList.toggle('error', Boolean(isError));
  toast.classList.add('visible');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('visible'), 3600);
}

/* ---------- 数值与控件值互转 ---------- */

function minutesToTime(minutes) {
  const safe = Number.isInteger(minutes) ? Math.min(Math.max(minutes, 0), 1439) : 0;
  return `${String(Math.floor(safe / 60)).padStart(2, '0')}:${String(safe % 60).padStart(2, '0')}`;
}

function timeToMinutes(text) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(text || '').trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const mins = Number(match[2]);
  if (hours > 23 || mins > 59) return null;
  return hours * 60 + mins;
}

function readControl(control) {
  if (control.kind === 'bool') return control.input.checked;
  if (control.kind === 'path') return control.input.value.trim();
  if (control.kind === 'clock') {
    const minutes = timeToMinutes(control.input.value);
    if (minutes === null) throw new Error(`${control.field.label}请填写有效时间`);
    return minutes;
  }
  if (control.kind === 'weekdays') {
    let bits = 0;
    control.inputs.forEach((input, index) => {
      if (input.checked) bits += WEEKDAY_BITS[index];
    });
    return bits;
  }
  const raw = control.input.value.trim();
  if (raw === '') throw new Error(`${control.field.label}不能为空`);
  const value = Number(raw);
  if (!Number.isInteger(value)) throw new Error(`${control.field.label}必须是整数`);
  const { minimum, maximum } = control.field;
  if (value < minimum || value > maximum) {
    throw new Error(`${control.field.label}必须在 ${minimum} 到 ${maximum} 之间`);
  }
  return value;
}

/* ---------- 渲染 ---------- */

function renderStatus(data) {
  const running = Boolean(data.daemonRunning);
  statusDot.className = `status-dot ${running ? 'on' : 'off'}`;
  statusText.textContent = running ? 'transmission-daemon 正在运行' : 'transmission-daemon 已停止';

  const parts = [];
  if (data.version) parts.push(`版本 ${data.version}`);
  if (data.pid) parts.push(`PID ${data.pid}`);
  parts.push(`RPC 127.0.0.1:${data.rpcPort}`);
  if (data.session && data.session.torrentCount !== null && data.session.torrentCount !== undefined) {
    parts.push(`任务 ${data.session.torrentCount}（活动中 ${data.session.activeTorrentCount ?? 0}）`);
    const down = Number(data.session.downloadSpeed || 0) / 1024;
    const up = Number(data.session.uploadSpeed || 0) / 1024;
    parts.push(`↓ ${down.toFixed(1)} KB/s ↑ ${up.toFixed(1)} KB/s`);
  }
  statusHint.textContent = parts.join('　·　');

  startBtn.disabled = busy || running;
  restartBtn.disabled = busy || !running;
  stopBtn.disabled = busy || !running;

  webControlCard.hidden = !data.webControlInstalled;
  if (data.webControlInstalled) webControlLink.href = data.webControlPath;

  const missing = !data.runtimeInstalled;
  missingCard.hidden = !missing;
  if (missing) {
    missingHint.textContent = '未找到运行文件，请先在仓库中执行 scripts/fetch_runtime.py 后重新打包安装。';
  }
  autostartHint.textContent = data.autostart
    ? '已开启开机自启：设备或插件服务重启后会重新拉起下载服务。'
    : '开机自启未开启：下次开机后需要手动点「启动」。';
}

function fieldNode(field) {
  const wrapper = document.createElement('div');
  wrapper.className = 'field';

  const head = document.createElement('div');
  head.className = 'field-head';
  const label = document.createElement('label');
  label.textContent = field.label;
  label.htmlFor = `field-${field.id}`;
  const unit = document.createElement('span');
  unit.className = 'unit';
  unit.textContent = field.unit ? `单位：${field.unit}` : '';
  head.append(label, unit);
  wrapper.append(head);

  const help = document.createElement('p');
  help.className = 'field-help';
  help.textContent = field.help;

  const controlBox = document.createElement('div');
  controlBox.className = 'field-control';
  const control = { field, kind: field.kind };

  if (field.kind === 'bool') {
    // 开关和标题同一行，说明文字在下：wrapper → [row(head + switch), help]
    wrapper.textContent = '';
    const toggle = document.createElement('label');
    toggle.className = 'switch';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.id = `field-${field.id}`;
    const slider = document.createElement('span');
    slider.className = 'slider';
    toggle.append(input, slider);
    const row = document.createElement('div');
    row.className = 'row';
    row.append(head, toggle);
    wrapper.append(row, help);
    control.input = input;
  } else {
    wrapper.append(head, help);
    if (field.kind === 'path') {
      const input = document.createElement('input');
      input.type = 'text';
      input.id = `field-${field.id}`;
      input.spellcheck = false;
      controlBox.append(input);
      control.input = input;
    } else if (field.kind === 'clock') {
      const input = document.createElement('input');
      input.type = 'time';
      input.id = `field-${field.id}`;
      controlBox.append(input);
      control.input = input;
    } else if (field.kind === 'weekdays') {
      const box = document.createElement('div');
      box.className = 'weekdays';
      control.inputs = WEEKDAY_LABELS.map((text, index) => {
        const item = document.createElement('label');
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.id = `field-${field.id}-${index}`;
        item.append(input, document.createTextNode(text));
        input.addEventListener('change', () => item.classList.toggle('checked', input.checked));
        box.append(item);
        return input;
      });
      controlBox.append(box);
    } else {
      const input = document.createElement('input');
      input.type = 'number';
      input.id = `field-${field.id}`;
      input.min = String(field.minimum);
      input.max = String(field.maximum);
      input.step = '1';
      input.inputMode = 'numeric';
      controlBox.append(input);
      control.input = input;
    }
    wrapper.append(controlBox);
  }

  const error = document.createElement('p');
  error.className = 'field-error';
  error.hidden = true;
  wrapper.append(error);
  control.error = error;
  control.wrapper = wrapper;

  controls.set(field.id, control);
  return wrapper;
}

function setControlValue(control, value) {
  if (control.kind === 'bool') {
    control.input.checked = Boolean(value);
  } else if (control.kind === 'path') {
    control.input.value = value == null ? '' : String(value);
  } else if (control.kind === 'clock') {
    control.input.value = minutesToTime(Number(value));
  } else if (control.kind === 'weekdays') {
    const bits = Number.isInteger(Number(value)) ? Number(value) : 0;
    control.inputs.forEach((input, index) => {
      input.checked = Boolean(bits & WEEKDAY_BITS[index]);
      input.parentElement.classList.toggle('checked', input.checked);
    });
  } else {
    control.input.value = value == null ? '' : String(value);
  }
}

function renderSettings(data) {
  schema = data;
  controls.clear();
  groupsBox.textContent = '';
  const byGroup = new Map();
  for (const group of data.groups) byGroup.set(group.id, []);
  for (const field of data.fields) {
    if (!byGroup.has(field.group)) byGroup.set(field.group, []);
    byGroup.get(field.group).push(field);
  }
  for (const group of data.groups) {
    const fields = byGroup.get(group.id) || [];
    if (!fields.length) continue;
    const section = document.createElement('section');
    section.className = 'group';
    const title = document.createElement('h2');
    title.textContent = group.label;
    const body = document.createElement('div');
    body.className = 'group-body';
    for (const field of fields) {
      body.append(fieldNode(field));
      setControlValue(controls.get(field.id), field.value);
    }
    section.append(title, body);
    groupsBox.append(section);
  }
  note.innerHTML =
    `设置写入 <code>${data.settingsPath}</code>（当前键名风格：${data.settingsKeyStyle}）。` +
    '保存时会先停止 transmission-daemon、写入文件、再重新启动，改动立即生效。<br />' +
    '时段开始/结束按设备本地时间，以零点起的分钟数存储；生效星期按 7 位位图存储' +
    '（周日 1、周一 2、周二 4、周三 8、周四 16、周五 32、周六 64）。';
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
  if (!response.ok || !payload.ok) {
    const error = new Error(payload.error || '操作失败');
    error.fields = payload.fields || null;
    throw error;
  }
  return payload;
}

async function refreshStatus() {
  try {
    renderStatus(await request('/status'));
  } catch (error) {
    statusDot.className = 'status-dot off';
    statusText.textContent = '无法读取状态';
    statusHint.textContent = error.message;
    startBtn.disabled = true;
    stopBtn.disabled = true;
    restartBtn.disabled = true;
  }
}

async function loadSettings() {
  try {
    renderSettings(await request('/settings'));
  } catch (error) {
    showToast(error.message, true);
  }
}

async function act(fn) {
  if (busy) return;
  busy = true;
  try {
    renderStatus(await fn());
  } catch (error) {
    showToast(error.message, true);
    await refreshStatus();
  } finally {
    busy = false;
    await refreshStatus();
  }
}

function clearErrors() {
  for (const control of controls.values()) {
    control.wrapper.classList.remove('invalid');
    control.error.hidden = true;
  }
}

function showFieldErrors(fields) {
  for (const [id, message] of Object.entries(fields || {})) {
    const control = controls.get(id);
    if (!control) continue;
    control.wrapper.classList.add('invalid');
    control.error.textContent = message;
    control.error.hidden = false;
  }
}

startBtn.addEventListener('click', () => act(() => request('/action', { action: 'start' })
  .then(() => { showToast('已启动'); return request('/status'); })));

stopBtn.addEventListener('click', () => act(() => request('/action', { action: 'stop' })
  .then(() => { showToast('已停止'); return request('/status'); })));

restartBtn.addEventListener('click', () => act(() => request('/action', { action: 'restart' })
  .then(() => { showToast('已重启'); return request('/status'); })));

document.getElementById('refreshButton').addEventListener('click', () => {
  refreshStatus();
  loadSettings();
});
document.getElementById('backButton').addEventListener('click', () =>
  (history.length > 1 ? history.back() : window.close()));
reloadBtn.addEventListener('click', loadSettings);

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  clearErrors();
  const values = {};
  try {
    for (const [id, control] of controls.entries()) values[id] = readControl(control);
  } catch (error) {
    showToast(error.message, true);
    return;
  }
  busy = true;
  saveBtn.disabled = true;
  try {
    const result = await request('/settings', { values });
    renderSettings({ ...schema, values: result.values, fields: schema.fields.map((field) => ({
      ...field, value: result.values[field.id],
    })) });
    showToast(result.restarted ? '已保存并重启 daemon' : '已保存（daemon 未运行，下次启动生效）');
  } catch (error) {
    if (error.fields) showFieldErrors(error.fields);
    showToast(error.message, true);
  } finally {
    busy = false;
    saveBtn.disabled = false;
    await refreshStatus();
  }
});

refreshStatus();
loadSettings();
