const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
const statusHint = document.getElementById('statusHint');
const statusMeta = document.getElementById('statusMeta');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const restartBtn = document.getElementById('restartBtn');
const saveBtn = document.getElementById('saveBtn');
const reloadBtn = document.getElementById('reloadBtn');
const groupsBox = document.getElementById('groups');
const form = document.getElementById('settingsForm');
const toast = document.getElementById('toast');
const webControlCard = document.getElementById('webControlCard');
const webControlLink = document.getElementById('webControlLink');
const missingCard = document.getElementById('missingCard');
const missingHint = document.getElementById('missingHint');

const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64];
// 界面上只有两个页签；这个分组进「常用」，其余全部进「高级」
const COMMON_GROUP = 'basic';

let schema = null;
let busy = false;
let activeGroup = null;
// 这些字段是派生值，界面只展示不允许直接编辑
const DERIVED_FIELDS = new Set(['rpc-authentication-required']);
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
  // 文本类：不做整数解析。密码保留原样（可含空格），
  // 留空表示不修改现有密码，由后端跳过。
  if (control.kind === 'choice') return control.input.value;
  if (control.kind === 'text') return control.input.value.trim();
  if (control.kind === 'password') return control.input.value;
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

  // 第二行只放日常真正会看的：任务数与实时速度
  const live = [];
  if (data.session && data.session.torrentCount !== null && data.session.torrentCount !== undefined) {
    live.push(`任务 ${data.session.torrentCount}（活动中 ${data.session.activeTorrentCount ?? 0}）`);
    const down = Number(data.session.downloadSpeed || 0) / 1024;
    const up = Number(data.session.uploadSpeed || 0) / 1024;
    live.push(`↓ ${down.toFixed(1)} KB/s　↑ ${up.toFixed(1)} KB/s`);
  }
  statusHint.textContent = live.join('　·　') || '暂无下载任务';

  // 版本 / PID / RPC 地址只在排障时有用，压成一行小字放第三行
  const meta = [];
  if (data.version) meta.push(`版本 ${data.version}`);
  if (data.pid) meta.push(`PID ${data.pid}`);
  // 反映 daemon 实际绑定、插件实际连接的地址，别写死 127.0.0.1
  const bind = data.rpcBind || '127.0.0.1';
  const target = data.rpcTarget || `${bind}:${data.rpcPort}`;
  meta.push(bind === '127.0.0.1' || bind === '::1'
    ? `RPC 仅本机 ${target}`
    : `RPC 对外监听 ${target}${data.rpcAuthRequired ? '（需登录）' : ''}`);
  statusMeta.textContent = meta.join('　·　');

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
  // 「开机自启」开关的状态跟着 status 走：它存在 plugin-state 里，不是 settings.json 的字段
  if (autostartToggle) autostartToggle.checked = Boolean(data.autostart);
}

let autostartToggle = null;

/* 「开机自启」不是 settings.json 的字段：它存在 plugin-state.json 里，由插件服务
   启动时读取。所以单独做一行开关，放在「常用」页最上面，也不跟着表单提交。 */
function autostartNode() {
  const row = document.createElement('div');
  row.className = 'row autostart-row';

  const copy = document.createElement('div');
  copy.className = 'row-copy';
  const title = document.createElement('strong');
  title.textContent = '开机自启';
  const hint = document.createElement('small');
  hint.textContent = '开启后，设备或插件服务重启时会自动拉起下载服务。';
  copy.append(title, hint);

  const toggle = document.createElement('label');
  toggle.className = 'switch';
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.id = 'autostartToggle';
  input.addEventListener('change', async () => {
    input.disabled = true;
    try {
      renderStatus(await request('/autostart', { enabled: input.checked }));
      showToast(input.checked ? '已开启开机自启' : '已关闭开机自启');
    } catch (error) {
      input.checked = !input.checked;
      showToast(error.message, true);
    } finally {
      input.disabled = false;
    }
  });
  const slider = document.createElement('span');
  slider.className = 'slider';
  toggle.append(input, slider);
  autostartToggle = input;

  row.append(copy, toggle);
  return row;
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
    // 这一项由「监听地址 + 是否配齐凭据」派生，不让用户直接改
    if (DERIVED_FIELDS.has(field.id)) {
      input.disabled = true;
      wrapper.classList.add('is-derived');
    }
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
    } else if (field.kind === 'choice') {
      const select = document.createElement('select');
      select.id = `field-${field.id}`;
      for (const option of field.choices || []) {
        const node = document.createElement('option');
        node.value = option;
        node.textContent = option === '0.0.0.0' ? '0.0.0.0（所有网卡）' : `${option}（仅本机）`;
        select.append(node);
      }
      controlBox.append(select);
      control.input = select;
    } else if (field.kind === 'text' || field.kind === 'password') {
      const input = document.createElement('input');
      input.type = field.kind === 'password' ? 'password' : 'text';
      input.id = `field-${field.id}`;
      input.spellcheck = false;
      input.autocomplete = field.kind === 'password' ? 'new-password' : 'off';
      if (field.kind === 'password') input.placeholder = '留空则不修改';
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
  const visible = data.groups.filter((group) => (byGroup.get(group.id) || []).length);

  // 只留两个页签：常用（basic）直接可见，其余全部收进「高级」。
  // 字段归属仍然由后端的 group 决定，这里只做归并。
  const panes = [
    { id: 'common', label: '常用', groups: visible.filter((group) => group.id === COMMON_GROUP) },
    { id: 'advanced', label: '高级', groups: visible.filter((group) => group.id !== COMMON_GROUP) },
  ].filter((pane) => pane.groups.length);

  if (!panes.some((pane) => pane.id === activeGroup)) {
    activeGroup = panes.length ? panes[0].id : null;
  }

  // 先把所有页签的面板都建出来：切换只是显示/隐藏，
  // 这样来回切不会丢掉已经填了一半的输入。
  const built = new Map();
  for (const pane of panes) {
    const section = document.createElement('section');
    section.className = 'group';
    // 用 fieldset/legend 会在手机上有默认边框，这里用普通容器
    const body = document.createElement('div');
    body.className = 'group-body';
    // 「常用」页最上面是开机自启开关；它不在 form 里，不会跟着「保存」提交
    if (pane.id === 'common') body.append(autostartNode());
    for (const group of pane.groups) {
      // 高级页里合了好几组，用标题分隔；只有一组时不重复标题
      if (pane.groups.length > 1) {
        const title = document.createElement('h3');
        title.className = 'group-title';
        title.textContent = group.label;
        body.append(title);
      }
      for (const field of byGroup.get(group.id)) {
        body.append(fieldNode(field));
        setControlValue(controls.get(field.id), field.value);
      }
    }
    section.append(body);
    built.set(pane.id, section);
  }

  const nav = document.createElement('nav');
  nav.className = 'tabs';
  nav.setAttribute('aria-label', '设置分组');
  for (const pane of panes) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = `tab${pane.id === activeGroup ? ' active' : ''}`;
    tab.textContent = pane.label;
    tab.addEventListener('click', () => {
      activeGroup = pane.id;
      for (const [id, section] of built) section.classList.toggle('active', id === activeGroup);
      for (const node of nav.children) node.classList.toggle('active', node === tab);
      // 面板高度差别大，切完把视线带回页签处
      nav.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    nav.append(tab);
  }
  for (const [id, section] of built) section.classList.toggle('active', id === activeGroup);

  groupsBox.append(nav);
  for (const pane of panes) groupsBox.append(built.get(pane.id));
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
/* 小米客户端在 WebView 里注入 flutter_inappwebview / android_webview，官方插件靠自带的
   js-bridge.js 调宿主方法。这里只实现我们用到的那一小部分：调宿主方法并忽略返回结果。 */
function callClient(method, params) {
  const host = window.flutter_inappwebview || window.android_webview;
  if (!host) return false;
  if (!host.callHandler) {
    // 部分客户端版本只暴露 _callHandler，照 js-bridge.js 的方式补一层
    host.callHandler = function () {
      const id = window.setTimeout(() => {});
      host._callHandler(arguments[0], id, JSON.stringify([].slice.call(arguments, 1)));
      return new Promise((resolve) => { host[id] = resolve; });
    };
  }
  const payload = host === window.android_webview ? JSON.stringify(params || {}) : (params || {});
  host.callHandler('hs_webCallAppHandler', method, payload, String(++callClient.seq));
  return true;
}
callClient.seq = 0;
// 宿主拿结果时会回调这个全局函数；我们不等结果，留个空实现免得它报错
window.hs_appCallBackToWeb = window.hs_appCallBackToWeb || function () {};

document.getElementById('backButton').addEventListener('click', () => {
  // 客户端里没有页面历史，window.close() 也会被 WebView 忽略，
  // 只能请宿主关掉当前插件页面
  if (callClient('normal_goback', {})) return;
  if (history.length > 1) {
    history.back();
    return;
  }
  window.close();
});
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
