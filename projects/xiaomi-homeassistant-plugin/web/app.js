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
const session = document.querySelector('meta[name="homeassistant-session"]').content;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let state = null;
let configSelection = '';                               // 初始化表单：选中的配置目录（绝对路径）
let browseRoot = 0;       // 当前浏览的「存储位置」序号（state.roots 的下标）
let browsePath = '';      // 该位置内的相对路径，空串表示位置根部
let browseOnPick = null;
let reconfigureConfig = { mode: 'keep', path: '' };    // 修改目录：配置目录 keep / private / path
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
  const headers = { 'X-Homeassistant-Session': session };
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
  return s.ready ? '服务运行中' : '容器已启动，等待 Home Assistant 就绪';
}

function render(s) {
  $('serviceState').textContent = stateLabel(s);
  const dot = s.busy ? '' : (s.running && s.ready ? 'on' : 'off');
  $('statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
  $('setup').hidden = s.configured || s.busy;
  $('serviceActions').hidden = !s.configured;
  $('directorySettings').hidden = !s.configured;
  $('reconfigure').disabled = s.busy;
  $('reset').disabled = s.busy;
  $('toggleService').hidden = !s.configured || s.busy;
  $('access').hidden = !(s.configured && s.running);
  // 状态卡片显示完整绝对路径：存储位置多于一个时，相对路径看不出在哪
  const configAbs = s.config_abs || '';
  // 配置目录留在插件私有目录（不属于任何存储位置）时补一句说明
  const configNote = s.configured && s.config_private ? '（插件私有目录，外部不可见）' : '';
  $('configDirectory').textContent = s.configured ? (configAbs || '—') + configNote : '—';
  $('configDirectory').title = s.configured ? configAbs + configNote : '';
  $('panelPort').textContent = String(s.port || 8123);
  $('address').textContent = s.address || '（请通过小米客户端打开插件以获取地址）';
  $('wizardHint').hidden = !(s.configured && s.running && !s.ready);
  $('toggleService').textContent = s.running ? '停止服务' : '启动服务';
  $('toggleService').disabled = s.busy;
  const info = [];
  // 版本优先显示 Home Assistant 自己写在 /config/.HA_VERSION 里的号，
  // 读不到（首次启动、目录被清）就退回镜像口径
  if (s.clientVersion) info.push('Home Assistant ' + s.clientVersion);
  if (s.imageVersion) info.push('镜像 ' + s.imageVersion);
  if (s.preview) info.push('预览模式，不会操作 Docker');
  $('serverInfo').textContent = info.join(' · ');
  $('serverInfo').hidden = !info.length;
  // 实际使用的镜像与「为什么是它」：厂商 Docker（20.10）解不开 HA 从 2026.3.0 起的
  // zstd 层，插件会自动固定到 2026.2.0——把原因写清楚，免得用户以为装错了版本。
  const imageNote = [];
  if (s.image) imageNote.push('当前使用镜像：' + s.image);
  if (s.imageReason) imageNote.push(s.imageReason);
  $('imageInfo').textContent = imageNote.join(' · ');
  $('imageInfo').hidden = !imageNote.length;
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

// 当前「存储位置」（内置存储池 / 外接设备），由 state.roots 提供
function currentRoot() {
  const roots = (state && state.roots) || [];
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
  const roots = (state && state.roots) || [];
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
  const roots = (state && state.roots) || [];
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
    button.onclick = () => {
      if (index === browseRoot) return;
      browseRoot = index;
      browsePath = '';                 // 切换位置后从该位置根部重新浏览
      renderRoots();
      loadFolders();
    };
    box.append(button);
  });
}

async function loadFolders() {
  // 先显示当前位置的完整绝对路径（位置根部就显示根自身），再读子目录
  const shown = absoluteBrowsePath();
  $('browsePath').textContent = shown || '用户存储根目录';
  $('browsePath').title = shown;
  $('up').disabled = !browsePath;
  $('folders').textContent = '正在读取';
  let data;
  try {
    data = await call('/browse?root=' + encodeURIComponent(browseRoot)
      + '&path=' + encodeURIComponent(browsePath));
  } catch (e) {
    $('folders').textContent = e.message;
    return;
  }
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
  const located = locateRoot(startPath);
  browseRoot = located.root;
  browsePath = located.path;
  $('browseTitle').textContent = title;
  renderRoots();
  $('browse').showModal();
  loadFolders();
}

$('refresh').onclick = () => refresh();
$('chooseConfig').onclick = () => openBrowser('选择配置目录', configSelection, (path) => {
  configSelection = path;
  $('configPath').value = path;
});
$('clearConfig').onclick = () => { configSelection = ''; $('configPath').value = ''; };
$('up').onclick = () => {
  browsePath = browsePath.split('/').slice(0, -1).join('/');
  loadFolders();
};
$('selectFolder').onclick = () => {
  const picked = absoluteBrowsePath();      // 提交绝对路径；服务端两种都接受
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
  const submit = e.target.querySelector('button[type=submit]');
  submit.disabled = true;
  try {
    // 配置目录留空＝放在插件私有目录，所以这里不强制选择
    await call('/setup', { path: configSelection });
    toast('正在初始化并启动，首次需要拉取镜像');
    e.target.reset();
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

// 修改目录：只改配置目录，复用与初始化同一套多位置选择器。
// 三种语义：保持当前（不带 configPath）/ 插件私有目录（空串）/ 新位置（路径）。
function currentConfigText() {
  if (!state) return '';
  return state.config_private ? '插件私有目录' : (state.config_abs || '');
}

function reconfigurePayload(choice) {
  if (choice.mode === 'private') return { configPath: '' };        // 空串＝插件私有目录
  if (choice.mode === 'path') return { configPath: choice.path };  // 路径＝新的配置位置
  return {};                                                       // keep：省略，后端沿用现有设置
}

function reconfigureQuestion(choice) {
  let configLine;
  if (choice.mode === 'keep') {
    configLine = '配置目录：' + (currentConfigText() || '—') + '（保持不变）';
  } else if (choice.mode === 'private') {
    configLine = '配置目录：' + (currentConfigText() || '—') + ' → 插件私有目录（外部不可见）';
  } else {
    configLine = '配置目录：' + (currentConfigText() || '—') + ' → ' + choice.path;
  }
  return '修改目录会按新位置重建 Home Assistant 容器：\n\n'
    + '· 只更换存储位置，原目录里的文件不会被删除；\n'
    + '· 新位置若没有原来的 Home Assistant 配置，应用会以全新状态启动；\n'
    + '· 重建期间服务会短暂中断，失败会自动回滚到原目录。\n\n'
    + configLine + '\n\n确定继续？';
}

function renderReconfigure() {
  if (reconfigureConfig.mode === 'keep') {
    $('reconfigureConfig').value = (currentConfigText() || '—') + '（保持不变）';
    $('reconfigureConfigHint').textContent =
      '保持当前配置目录不变（Home Assistant 的数据库、集成与自动化都在这里）。';
  } else if (reconfigureConfig.mode === 'private') {
    $('reconfigureConfig').value = '插件私有目录（外部不可见）';
    $('reconfigureConfigHint').textContent =
      '配置改放在插件私有目录：外部看不到、也不占用用户存储；'
      + '原配置目录里的文件不会被删除，但新位置没有原来的配置，应用会以全新状态启动。';
  } else {
    $('reconfigureConfig').value = reconfigureConfig.path;
    $('reconfigureConfigHint').textContent =
      '配置目录改到新位置；新位置若没有原来的 Home Assistant 配置，应用会以全新状态启动。';
  }
}

function openReconfigure() {
  reconfigureConfig = { mode: 'keep', path: '' };
  renderReconfigure();
  $('reconfigureDialog').showModal();
}

$('reconfigure').onclick = () => openReconfigure();
// 「选择目录」开多位置选择器，选完只回填弹窗、不立即提交
$('reconfigureConfigChoose').onclick = () => openBrowser('选择新的配置目录',
  reconfigureConfig.mode === 'path' ? reconfigureConfig.path : ((state && state.config_abs) || ''),
  (path) => {
    reconfigureConfig = { mode: 'path', path };
    renderReconfigure();
  });
$('reconfigureConfigPrivate').onclick = () => {
  reconfigureConfig = { mode: 'private', path: '' };
  renderReconfigure();
};
$('reconfigureSubmit').onclick = async () => {
  const payload = reconfigurePayload(reconfigureConfig);
  if (!window.confirm(reconfigureQuestion(reconfigureConfig))) return;
  const submit = $('reconfigureSubmit');
  submit.disabled = true;
  try {
    const result = await call('/service/reconfigure', payload);
    $('reconfigureDialog').close();
    toast(result.busy ? '正在按新目录重建容器…' : '已按新目录重建容器');
    await refresh();
  } catch (e) {
    toast('修改目录失败：' + e.message);
    showError(e.message);
  } finally {
    submit.disabled = false;
  }
};

// 重新初始化：清空插件配置并移除容器，用户目录里的文件不受影响
$('reset').onclick = async () => {
  const question = '重新初始化会：\n\n'
    + '· 停止并移除本插件的 Home Assistant 容器（配置目录里的文件不受影响）；\n'
    + '· 清空插件设置，旧设置会另存为带时间戳的备份；\n'
    + '· 之后需要重新初始化才能再次启动服务。\n\n确定继续？';
  if (!window.confirm(question)) return;
  $('reset').disabled = true;
  try {
    const result = await call('/service/reset', { confirm: true });
    if (!result.busy) {
      // 旧的选择已经失效，回到初始化表单让用户重选
      configSelection = '';
      $('configPath').value = '';
    }
    toast(result.busy ? '正在清理容器与配置…' : '已重新初始化，请重新选择目录');
    await refresh();
  } catch (e) {
    toast('重新初始化失败：' + e.message);
    showError(e.message);
  } finally {
    $('reset').disabled = false;
  }
};

async function tick() {
  if (!$('homeassistant-app').isConnected) return;
  if (!document.hidden) await refresh();
  setTimeout(tick, 5000);
}
tick();
