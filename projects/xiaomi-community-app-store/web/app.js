let csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const sessionToken = document.querySelector('meta[name="session-token"]').content;
const packageList = document.getElementById('packageList');
const installedList = document.getElementById('installedList');
const toast = document.getElementById('toast');
let packages = [];
let preview = false;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('visible');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('visible'), 3200);
}

function packageCard(item, installedView = false) {
  const article = document.createElement('article');
  article.className = 'package-item';

  const icon = document.createElement('img');
  icon.className = 'package-icon';
  icon.src = item.iconUrl || '';
  icon.alt = '';
  icon.loading = 'lazy';
  icon.onerror = () => { icon.style.visibility = 'hidden'; };

  const copy = document.createElement('div');
  copy.className = 'package-copy';
  const name = document.createElement('strong');
  name.textContent = item.name;
  const summary = document.createElement('p');
  summary.textContent = (item.channel === 'candidate' ? '测试版 · ' : '') + item.summary;
  const version = document.createElement('small');
  version.textContent = item.installedVersion ? `已安装 ${item.installedVersion} · 最新 ${item.version}` : `版本 ${item.version}`;
  copy.append(name, summary, version);

  const button = document.createElement('button');
  button.className = `action-button${installedView ? ' remove' : ''}`;
  if (installedView && item.managed) {
    button.textContent = '卸载';
    button.addEventListener('click', () => mutate('uninstall', item, button));
  } else if (installedView) {
    button.textContent = '外部安装';
    button.disabled = true;
  } else if (item.installedVersion === item.version) {
    button.textContent = '已安装';
    button.disabled = true;
  } else {
    button.textContent = item.installedVersion ? '更新' : '安装';
    button.addEventListener('click', () => mutate('install', item, button));
  }
  article.append(icon, copy, button);
  return article;
}

function render() {
  packageList.replaceChildren(...packages.map(item => packageCard(item)));
  const installed = packages.filter(item => item.installedVersion);
  installedList.replaceChildren(...(installed.length
    ? installed.map(item => packageCard(item, true))
    : [Object.assign(document.createElement('div'), { className: 'empty', textContent: '还没有通过插件市场安装插件' })]));
}

async function loadCatalog() {
  try {
    const response = await fetch('api/catalog', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Community-Session': sessionToken },
    });
    const payload = await response.json();
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
    document.getElementById('updatedAt').textContent = preview ? '本地预览' : `${packages.length} 个应用`;
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
  const releaseLink = document.getElementById('releaseLink');

  try {
    const res = await fetch('api/status', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Community-Session': sessionToken },
    });
    const data = await res.json();
    if (data.ok) localEl.textContent = `v${data.version}`;
    else localEl.textContent = '未知';
  } catch { localEl.textContent = '未知'; }

  checkBtn.disabled = true;
  checkBtn.textContent = '检测中…';
  remoteEl.textContent = '检测中…';
  statusEl.textContent = '—';
  noteEl.textContent = '';
  releaseLink.hidden = true;

  try {
    const res = await fetch('api/update-check', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Community-Session': sessionToken },
    });
    const data = await res.json();
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
      noteEl.textContent = `商店 v${data.current} → v${data.latest}。请在 NAS 上重新运行一键安装脚本更新。`;
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

document.getElementById('checkUpdateButton').addEventListener('click', loadStoreStatus);

async function mutate(action, item, button) {
  if (preview) {
    showToast('本地预览不会修改 NAS');
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = action === 'install' ? '安装中' : '卸载中';
  try {
    const response = await fetch(`api/${action}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken,
        'X-Community-Session': sessionToken,
      },
      body: JSON.stringify({ id: item.id }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || '操作失败');
    showToast(action === 'install' ? `${item.name} 已安装` : `${item.name} 已卸载，数据已保留`);
    await loadCatalog();
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
    button.textContent = original;
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
document.getElementById('refreshButton').addEventListener('click', loadCatalog);
document.getElementById('backButton').addEventListener('click', () => history.length > 1 ? history.back() : window.close());
loadCatalog();
