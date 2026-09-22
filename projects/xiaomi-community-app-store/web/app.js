let csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const sessionToken = document.querySelector('meta[name="session-token"]').content;
const packageList = document.getElementById('packageList');
const installedList = document.getElementById('installedList');
const toast = document.getElementById('toast');
let packages = [];
let preview = false;

// 小米智能存储客户端在 WebView 里注入 flutter_inappwebview / android_webview，
// 官方插件靠自带的 js-bridge.js 调宿主方法（浏览器里没有这两个对象）。
// 只实现我们用到的那一小部分：调宿主方法并忽略返回结果。
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

// 在客户端里让宿主关掉当前插件页面；不在客户端时退回浏览器历史
function leavePlugin() {
  if (callClient('normal_goback', {})) return;
  if (history.length > 1) {
    history.back();
    return;
  }
  window.close();
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('visible');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('visible'), 3200);
}

function makeIcon(item) {
  const icon = document.createElement('img');
  icon.className = 'package-icon';
  icon.src = item.iconUrl || '';
  icon.alt = '';
  icon.loading = 'lazy';
  icon.onerror = () => { icon.style.visibility = 'hidden'; };
  return icon;
}

function makeCopy(item, installed = false) {
  const copy = document.createElement('div');
  copy.className = 'package-copy';
  const name = document.createElement('strong');
  name.textContent = item.name;
  const summary = document.createElement('p');
  summary.textContent = (item.channel === 'candidate' ? '测试版 · ' : '') + item.summary;
  const version = document.createElement('small');
  if (installed) {
    version.textContent = item.installedVersion === item.version
      ? `已是最新 ${item.version}`
      : `已安装 ${item.installedVersion} · 可更新到 ${item.version}`;
    if (item.installedVersion !== item.version) version.classList.add('has-update');
  } else {
    version.textContent = `版本 ${item.version}`;
  }
  copy.append(name, summary, version);
  return copy;
}

function makeButton(label, variant, handler, action) {
  const button = document.createElement('button');
  button.className = `action-button${variant ? ` ${variant}` : ''}`;
  button.textContent = label;
  if (action) button.dataset.action = action;
  if (handler) button.addEventListener('click', handler);
  else button.disabled = true;
  return button;
}

function emptyState(text) {
  return Object.assign(document.createElement('div'), { className: 'empty', textContent: text });
}

// 精选：只展示还没装的插件
function packageCard(item) {
  const article = document.createElement('article');
  article.className = 'package-item';
  const actions = document.createElement('div');
  actions.className = 'package-actions';
  actions.append(makeButton('安装', '', () => mutate('install', item, actions), 'install'));
  article.append(makeIcon(item), makeCopy(item), actions);
  return article;
}

// 已安装：可更新时给「更新」，托管插件给「卸载」
function installedCard(item) {
  const article = document.createElement('article');
  article.className = 'package-item';
  const actions = document.createElement('div');
  actions.className = 'package-actions';
  if (item.installedVersion !== item.version) {
    actions.append(makeButton('更新', '', () => mutate('install', item, actions), 'install'));
  }
  if (item.managed) {
    actions.append(makeButton('卸载', 'remove', () => mutate('uninstall', item, actions), 'uninstall'));
  } else {
    actions.append(makeButton('外部安装', '', null));
  }
  article.append(makeIcon(item), makeCopy(item, true), actions);
  return article;
}

function render() {
  const available = packages.filter(item => !item.installedVersion);
  packageList.replaceChildren(...(available.length
    ? available.map(packageCard)
    : [emptyState('全部插件都已安装')]));

  const installed = packages.filter(item => item.installedVersion);
  installedList.replaceChildren(...(installed.length
    ? installed.map(installedCard)
    : [emptyState('还没有通过插件市场安装插件')]));

  const updates = installed.filter(item => item.installedVersion !== item.version).length;
  const parts = [`${available.length} 个可安装`];
  if (installed.length) parts.push(`${installed.length} 个已安装`);
  if (updates) parts.push(`${updates} 个可更新`);
  const counter = document.getElementById('updatedAt');
  if (counter) counter.textContent = preview ? '本地预览' : parts.join(' · ');
}

async function readJson(response) {
  // Windows 本地代理偶发把 401/SPA 回退/网关错误包成 HTML；先读文本再解析，
  // 避免 "Unexpected token '<'"。
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (error) {
    if (response.status === 401) return { ok: false, error: '请从小米智能存储客户端重新打开插件市场' };
    const type = (response.headers.get('content-type') || '').split(';')[0].trim();
    const snippet = text.slice(0, 80).replace(/\s+/g, ' ').trim();
    return {
      ok: false,
      error: `服务返回了非 JSON 响应（HTTP ${response.status} ${type || 'unknown'}）：${snippet || '（空）'}`,
    };
  }
}

function sessionHeaders(extra, includeSession) {
  // 空值头会被部分代理/nginx 判成 400 Bad Request，一律不要发。
  const headers = {};
  for (const [key, value] of Object.entries(extra || {})) {
    if (value !== undefined && value !== null && value !== '') headers[key] = value;
  }
  if (includeSession !== false && sessionToken) headers['X-Community-Session'] = sessionToken;
  return headers;
}

async function fetchApi(path, init) {
  // 本地代理可能把 /api/* 当成前端路由回退到 index.html。
  // 依次尝试：标准 API → 静态扩展名 → 页面路径 + ?api=；并区分是否带会话头。
  const [name, qs] = String(path).split('?');
  const query = qs ? `?${qs}` : '';
  const dir = location.pathname.replace(/[^/]*$/, '');
  const urls = [
    `api/${name}${query}`,
    `${dir}catalog.json${query}`,
    `${dir}index.html?api=${encodeURIComponent(name)}${query ? `&${qs}` : ''}`,
    `${dir}catalog.js${query}`,
  ];
  let lastError = new Error('请求失败');
  for (const url of urls) {
    for (const withSession of (sessionToken ? [true, false] : [false])) {
      try {
        const base = Object.assign({}, init || {});
        base.headers = sessionHeaders(withSession ? (init && init.headers) : undefined, withSession);
        const response = await fetch(url, base);
        const payload = await readJson(response);
        if (response.status === 401) return { response, payload, url };
        if (payload && typeof payload === 'object' && ('ok' in payload)) {
          return { response, payload, url };
        }
        lastError = new Error(payload && payload.error ? payload.error : '服务返回了非 JSON 响应');
      } catch (error) {
        lastError = error;
      }
    }
  }
  // 最后用 script 标签拉 catalog.js（静态资源通道，避开自定义请求头）
  try {
    const payload = await loadCatalogScript();
    if (payload && typeof payload === 'object') {
      return { response: { status: 200, ok: true }, payload, url: 'catalog.js' };
    }
  } catch (error) {
    lastError = error;
  }
  throw lastError;
}

function loadCatalogScript() {
  return new Promise((resolve, reject) => {
    const dir = location.pathname.replace(/[^/]*$/, '');
    const script = document.createElement('script');
    const timeout = setTimeout(() => {
      script.remove();
      reject(new Error('catalog.js 加载超时'));
    }, 8000);
    script.src = dir + 'catalog.js?ts=' + Date.now();
    script.onload = () => {
      clearTimeout(timeout);
      script.remove();
      resolve(window.__BOOTSTRAP_CATALOG__ || null);
    };
    script.onerror = () => {
      clearTimeout(timeout);
      script.remove();
      reject(new Error('catalog.js 加载失败'));
    };
    document.head.appendChild(script);
  });
}

async function loadCatalog(force) {
  try {
    const name = force ? 'catalog?refresh=1' : 'catalog';
    const { response, payload } = await fetchApi(name, {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (response.status === 401) {
      packageList.replaceChildren(Object.assign(document.createElement('div'), {
        className: 'empty',
        textContent: '请从小米智能存储客户端重新打开插件市场',
      }));
      return;
    }
    if (!response.ok || !payload.ok) throw new Error(payload.error || '仓库读取失败');
    packages = payload.catalog.packages;
    preview = payload.preview;
    render();
  } catch (error) {
    packageList.replaceChildren(Object.assign(document.createElement('div'), { className: 'empty', textContent: error.message }));
  }
}

// ---------- 版本与更新 ----------
async function loadStoreStatus() {
  const localEl = document.getElementById('localVersion');
  const remoteEl = document.getElementById('remoteVersion');
  const statusEl = document.getElementById('updateStatus');
  const noteEl = document.getElementById('updateNote');
  const checkBtn = document.getElementById('checkUpdateButton');
  const updateBtn = document.getElementById('selfUpdateButton');
  const releaseLink = document.getElementById('releaseLink');

  try {
    const res = await fetch('api/status', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: sessionHeaders(),
    });
    const data = await readJson(res);
    if (data.ok) localEl.textContent = `v${data.version}`;
    else localEl.textContent = '未知';
  } catch { localEl.textContent = '未知'; }

  checkBtn.disabled = true;
  checkBtn.textContent = '检测中…';
  remoteEl.textContent = '检测中…';
  statusEl.textContent = '—';
  noteEl.textContent = '';
  updateBtn.hidden = true;
  updateBtn.disabled = false;
  updateBtn.textContent = '立即更新';
  releaseLink.hidden = true;

  try {
    const res = await fetch('api/update-check', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: sessionHeaders(),
    });
    const data = await readJson(res);
    checkBtn.disabled = false;
    checkBtn.textContent = '检查更新';

    if (!data.ok) {
      remoteEl.textContent = '检测失败';
      statusEl.textContent = '—';
      noteEl.textContent = data.error || '无法连接 GitHub';
      return;
    }

    remoteEl.textContent = `v${data.latest}`;
    if (data.hasUpdate) {
      statusEl.innerHTML = '<span class="update-badge">有新版本</span>';
      noteEl.textContent = `商店 v${data.current} → v${data.latest}`;
      updateBtn.hidden = false;
      if (data.url) {
        releaseLink.href = data.url;
        releaseLink.hidden = false;
      }
    } else {
      statusEl.textContent = '已是最新';
      noteEl.textContent = '';
    }
  } catch {
    checkBtn.disabled = false;
    checkBtn.textContent = '检查更新';
    remoteEl.textContent = '检测失败';
    statusEl.textContent = '—';
    noteEl.textContent = '网络错误，无法检测更新';
  }
}

async function selfUpdate() {
  const btn = document.getElementById('selfUpdateButton');
  const noteEl = document.getElementById('updateNote');
  btn.disabled = true;
  btn.textContent = '更新中…';
  noteEl.textContent = '正在下载并安装新版本，请勿关闭页面…';
  try {
    const res = await fetch('api/self-update', {
      method: 'POST',
      credentials: 'same-origin',
      headers: sessionHeaders({
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken || undefined,
      }),
      body: '{}',
    });
    const data = await readJson(res);
    if (!res.ok || !data.ok) throw new Error(data.error || '更新失败');
    if (data.updated) {
      noteEl.textContent = `已更新到 v${data.version}，页面即将刷新…`;
      showToast(`商店已更新到 v${data.version}`);
      setTimeout(() => location.reload(), 2000);
    } else {
      noteEl.textContent = data.message || '已是最新版本';
      btn.textContent = '已是最新';
      btn.disabled = true;
    }
  } catch (error) {
    btn.disabled = false;
    btn.textContent = '立即更新';
    noteEl.textContent = error.message;
    showToast(error.message);
  }
}

document.getElementById('checkUpdateButton').addEventListener('click', loadStoreStatus);
document.getElementById('selfUpdateButton').addEventListener('click', selfUpdate);

async function mutate(action, item, actions) {
  if (preview) {
    showToast('本地预览不会修改 NAS');
    return;
  }
  const buttons = [...actions.querySelectorAll('button')];
  const clicked = actions.querySelector(`button[data-action="${action}"]`) || buttons[0];
  const original = clicked ? clicked.textContent : '';
  buttons.forEach((button) => { button.disabled = true; });
  if (clicked) clicked.textContent = action === 'install' ? '处理中…' : '卸载中…';
  try {
    const response = await fetch(`api/${action}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: sessionHeaders({
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken || undefined,
      }),
      body: JSON.stringify({ id: item.id }),
    });
    const payload = await readJson(response);
    if (!response.ok || !payload.ok) throw new Error(payload.error || '操作失败');
    showToast(action === 'install' ? `${item.name} 已安装` : `${item.name} 已卸载，数据已保留`);
    await loadCatalog();
  } catch (error) {
    showToast(error.message);
    buttons.forEach((button) => { button.disabled = false; });
    if (clicked) clicked.textContent = original;
  }
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item === tab));
    document.querySelectorAll('.view').forEach(view => view.classList.remove('active'));
    document.getElementById(`${tab.dataset.view}View`).classList.add('active');
    if (tab.dataset.view === 'about') loadStoreStatus();
  });
});
document.getElementById('refreshButton').addEventListener('click', () => loadCatalog(true));
document.getElementById('backButton').addEventListener('click', leavePlugin);
loadCatalog();
