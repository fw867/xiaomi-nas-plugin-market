(() => {
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

  const icons = __ICON_ASSETS__;
  const root = document.querySelector('#qb-app');
  const $ = (s) => root.querySelector(s);
  const session = document.querySelector('meta[name="qb-session"]').content;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = (name) => `<img src="${icons[name]}" alt="">`;
  root.querySelectorAll('[data-icon]').forEach(el => el.src = icons[el.dataset.icon]);
  let state, items = [], browseRoot = 0, browsePath = '', pickInput = null, removeHash, toastTimer, polling = false, generation = 0;
  const bytes = (n) => { n = Number(n || 0); const u = ['B','KiB','MiB','GiB','TiB']; let i = 0; while (n >= 1024 && i < 4) {n /= 1024;i++;} return n.toFixed(i ? 1 : 0) + ' ' + u[i]; };
  const stopped = item => /stopped|paused|error|missingFiles/i.test(item.state);
  function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 6500); }
  function showError(message) { const box = $('#error'); box.textContent = message || ''; box.hidden = !message; }
  async function api(route, data) {
    const response = await fetch(assetUrl('api/' + route), {method:data === undefined ? 'GET':'POST', cache:'no-store', headers:{'X-QB-Session':session,'X-CSRF-Token':csrf,...(data === undefined ? {} : {'Content-Type':'application/json'})}, body:data === undefined ? undefined : JSON.stringify(data)});
    const result = await response.json().catch(() => ({})); if (!response.ok || !result.ok) throw new Error(result.error || '请求失败'); return result;
  }
  async function busy(button, fn) { button.disabled = true; try {await fn();} catch(e) {toast(e.message);showError(e.message);} finally {button.disabled = false;} }
  function stateLabel(current) {
    if (current.busy) return '正在处理，请稍候';
    if (!current.configured) return '未初始化';
    if (!current.running) return '已停止';
    return current.ready ? '服务运行中' : '容器已启动，等待 qBittorrent 就绪';
  }
  // 状态页与控制台是两个视图；用 hash 记住位置，刷新后仍停在控制台。
  function showConsole(on) {
    $('#statusView').hidden = on;
    $('#consoleView').hidden = !on;
    if (on) {
      if (location.hash !== '#console') location.hash = 'console';
    } else if (location.hash === '#console') {
      history.replaceState(null, '', location.pathname + location.search);
    }
  }
  function render(current) {
    $('#serviceState').textContent = stateLabel(current);
    // 圆点和文案同源，避免出现「绿点 + 未就绪」这种自相矛盾的画面
    const dot = current.busy ? '' : (current.running && current.ready ? 'on' : 'off');
    $('#statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
    const info = [];
    if (current.imageVersion) info.push('镜像 ' + current.imageVersion);
    if (current.preview) info.push('预览模式，不会操作 Docker');
    $('#serviceInfo').textContent = info.join(' · ');
    $('#serviceInfo').hidden = !info.length;

    $('#setup').hidden = current.configured || current.busy;
    $('#serviceActions').hidden = !current.configured;
    // 入口地址与控制台按钮只在服务真正跑起来之后才有意义
    const live = current.configured && current.running;
    $('#access').hidden = !live;
    $('#consoleEntry').hidden = !live;
    $('#address').textContent = current.address || '（请从设备所有者的小米客户端打开插件以获取地址）';
    $('#toggleService').disabled = current.busy;
    $('#toggleService').textContent = current.running ? '停止服务' : '启动服务';
    // 状态卡片显示完整绝对路径：存储位置多于一个时，相对路径看不出在哪
    const absolute = current.download_abs || (current.directory ? '/' + current.directory : '');
    $('#directory').textContent = current.configured ? (absolute || '—') : '—';
    $('#directory').title = current.configured ? absolute : '';
    $('#reconfigure').disabled = current.busy;
    $('#reset').disabled = current.busy;

    $('#login').hidden = current.loggedIn || current.busy;
    $('#consoleBody').hidden = !current.loggedIn;
    renderForward(current);
    if (!current.busy) showError(current.error);
  }
  function renderForward(current) {
    // 容器里的 qB 自己不做 UPnP，端口转发由插件在宿主机侧要
    const box = $('#forwardState');
    if (!box) return;
    const forward = current.forward || {};
    const port = forward.externalPort || 36754;
    if (forward.ok) {
      const lease = Number(forward.lease) || 0;
      const renew = lease ? `，${Math.round(lease / 60)} 分钟自动续期` : '';
      box.textContent = `路由器已转发 ${port}（${forward.method}${renew}）`;
    } else if (forward.removed) {
      box.textContent = `路由器映射已移除（${port} TCP+UDP），启动服务时会重新映射`;
    } else if (forward.at) {
      box.textContent = `路由器未转发 ${port}：${forward.detail || '原因未知'}`;
    } else {
      box.textContent = `路由器端口映射未尝试（${port} TCP+UDP）`;
    }
    $('#forwardPort').disabled = current.busy || !current.running;
  }
  function renderTasks() {
    const search = $('#search').value.toLowerCase(), filter = $('#filter').value;
    const visible = items.filter(item => item.name.toLowerCase().includes(search) && (filter === 'all' || filter === 'completed' && item.progress >= 1 || filter === 'stopped' && stopped(item) || filter === 'downloading' && item.progress < 1 && !stopped(item)));
    $('#count').textContent = `${visible.length} 个任务 · 最多显示最近 500 个`;
    $('#empty').hidden = visible.length > 0;
    $('#tasks').innerHTML = visible.map(item => `<article class="task"><div class="task-body"><button class="task-name" data-action="detail" data-hash="${escape(item.hash)}">${escape(item.name)}</button><progress max="1" value="${Math.max(0,Math.min(1,Number(item.progress)||0))}" aria-label="下载进度"></progress><small>${(item.progress*100).toFixed(1)}% · ${bytes(item.size)} · ${escape(item.state)} · ↓ ${bytes(item.dlspeed)}/s · ↑ ${bytes(item.upspeed)}/s</small></div><div class="task-actions"><button title="${stopped(item)?'继续':'暂停'}" aria-label="${stopped(item)?'继续':'暂停'}" data-action="${stopped(item)?'start':'stop'}" data-hash="${escape(item.hash)}">${icon(stopped(item)?'play':'stop')}</button><button title="移除任务，保留文件" aria-label="移除任务，保留文件" data-action="remove" data-hash="${escape(item.hash)}">${icon('trash')}</button></div></article>`).join('');
  }
  async function refresh() {
    if (polling) return; polling = true;
    try {
      const current = await api('status');
      state = current;
      render(current);
      if (current.running && current.loggedIn) {
        const result = await api('torrents'); items = result.items;
        $('#downSpeed').textContent = bytes(result.transfer.dl_info_speed) + '/s';
        $('#upSpeed').textContent = bytes(result.transfer.up_info_speed) + '/s';
        $('#downloaded').textContent = bytes(result.transfer.dl_info_data); renderTasks();
      }
    } catch(e) { showError(e.message); }
    finally {polling = false;}
  }
  // 当前「存储位置」（内置存储池 / 外接设备），由状态接口的 roots 提供
  function currentRoot() {
    const roots = (state && state.roots) || [];
    return roots[browseRoot] || roots[0] || null;
  }
  // 根内相对路径 → 完整绝对路径（位置根部就显示根的绝对路径）
  function absoluteBrowsePath() {
    const root = currentRoot(), base = root ? String(root.path || '').replace(/\/+$/, '') : '';
    if (!browsePath) return base;
    return base ? base + '/' + browsePath : browsePath;
  }
  // 绝对路径 →（位置序号，根内相对路径）：与后端一致做最长前缀匹配，找不到就退回根部
  function locateRoot(value) {
    const roots = (state && state.roots) || [], text = String(value || '').replace(/\/+$/, '');
    let index = -1, length = -1;
    roots.forEach((root, position) => {
      const base = String(root.path || '').replace(/\/+$/, '');
      if (base && (text === base || text.startsWith(base + '/')) && base.length > length) {index = position;length = base.length;}
    });
    if (index < 0) return {root: 0, path: ''};
    const base = String(roots[index].path || '').replace(/\/+$/, '');
    return {root: index, path: text === base ? '' : text.slice(base.length + 1)};
  }
  // 「位置」切换按钮：只有一个存储位置时不显示
  function renderRoots() {
    const roots = (state && state.roots) || [], box = $('#roots');
    box.hidden = roots.length < 2;
    box.innerHTML = roots.length < 2 ? '' : roots.map((root, position) => {
      const index = Number.isInteger(root.index) ? root.index : position;
      return `<button type="button" class="btn small${index === browseRoot ? ' active' : ''}" data-root="${index}">${escape(root.label || root.path)}${root.exists ? '' : '（不可用）'}</button>`;
    }).join('');
  }
  async function browse(path) {
    browsePath = path; const current = ++generation;
    const shown = absoluteBrowsePath();
    $('#browsePath').textContent = shown || '用户存储根目录';
    $('#browsePath').title = shown;
    $('#folders').textContent = '正在读取'; $('#selectFolder').disabled = true; $('#up').disabled = !path;
    try { const result = await api('browse?root=' + encodeURIComponent(browseRoot) + '&path=' + encodeURIComponent(path)); if(current !== generation) return;
      $('#folders').innerHTML = result.items.map(item => `<button type="button" data-folder="${escape(item.path)}">${icon('folder')}${escape(item.name)}</button>`).join('') || '<p class="muted">此目录下没有子文件夹</p>';
      $('#selectFolder').disabled = !path;
    } catch(e) { if(current === generation) $('#folders').textContent = e.message; }
  }
  // 打开目录选择弹窗：从输入框现有值定位到所属位置，没有就停在该位置根部
  function openBrowser(input) {
    const located = locateRoot(input ? input.value : '');
    pickInput = input; browseRoot = located.root; browsePath = located.path;
    renderRoots(); $('#browse').showModal(); browse(browsePath);
  }
  root.addEventListener('click', e => {
    const button = e.target.closest('button'); if (!button) return;
    if (button.dataset.close) {$('#' + button.dataset.close).close();return;}
    if (button.dataset.root !== undefined) {const index = Number(button.dataset.root);if (index !== browseRoot) {browseRoot = index;renderRoots();browse('');}return;}
    if (button.dataset.folder !== undefined) {browse(button.dataset.folder);return;}
    if (!button.dataset.action) return;
    busy(button, async () => {
      const hash = button.dataset.hash, action = button.dataset.action;
      if (action === 'remove') {removeHash = hash; $('#removeDialog').showModal();return;}
      if (action === 'detail') {
        const result = await api('detail?hash=' + encodeURIComponent(hash));
        $('#detailTitle').textContent = items.find(x=>x.hash === hash)?.name || '任务详情';
        $('#detailStats').textContent = `已下载 ${bytes(result.properties.total_downloaded)} · 已上传 ${bytes(result.properties.total_uploaded)} · 分享率 ${Number(result.properties.share_ratio || 0).toFixed(2)}`;
        $('#files').innerHTML = result.files.map(file=>`<div class="file">${escape(file.name)}<small>${bytes(file.size)} · ${(file.progress*100).toFixed(1)}%</small></div>`).join('');
        $('#detailDialog').showModal(); return;
      }
      await api(action,{hash}); await refresh();
    });
  });
  $('#refresh').onclick = () => busy($('#refresh'),refresh);
  $('#search').oninput = renderTasks; $('#filter').onchange = renderTasks;
  $('#openConsole').onclick = () => { showConsole(true); busy($('#openConsole'), refresh); };
  $('#backToStatus').onclick = () => showConsole(false);
  $('#copyAddress').onclick = () => busy($('#copyAddress'), async () => {
    const text = $('#address').textContent;
    try { await navigator.clipboard.writeText(text); toast('已复制地址'); }
    catch (e) { toast('复制失败，请手动记录：' + text); }
  });
  window.addEventListener('hashchange', () => showConsole(location.hash === '#console'));
  $('#choose').onclick = () => openBrowser($('#downloadPath'));
  $('#up').onclick = () => browse(browsePath.split('/').slice(0,-1).join('/'));
  $('#selectFolder').onclick = () => {
    // 写回输入框的是完整绝对路径：服务端据此反查它属于哪个存储位置
    if (!browsePath) return;
    if (pickInput) {pickInput.value = absoluteBrowsePath();pickInput.title = pickInput.value;}
    $('#browse').close();
  };
  $('#setupForm').onsubmit = e => {e.preventDefault();const f=e.target;if(!f.elements.path.value){showError('请先选择下载目录');return;}busy(f.querySelector('[type=submit]'),async()=>{await api('service/setup',{path:f.elements.path.value,password:f.elements.password.value}); f.elements.password.value='';await refresh();});};
  $('#reconfigure').onclick = () => {
    const form = $('#reconfigureForm'); form.reset();
    $('#reconfigurePath').value = (state && state.download_abs) || '';
    $('#currentDirectory').textContent = '当前目录：' + ((state && state.download_abs) || '—');
    $('#reconfigureDialog').showModal();
  };
  $('#chooseNew').onclick = () => openBrowser($('#reconfigurePath'));
  $('#reconfigureForm').onsubmit = e => {
    e.preventDefault(); const form = e.target;
    if (!form.elements.path.value) {showError('请先选择新的下载目录');return;}
    busy(form.querySelector('[type=submit]'),async()=>{
      const result = await api('service/reconfigure',{path:form.elements.path.value,password:form.elements.password.value});
      form.elements.password.value = ''; $('#reconfigureDialog').close();
      if (result.state) {state = result.state;render(state);}
      await refresh(); toast('下载目录已更新，容器已按新位置重建');
    });
  };
  $('#reset').onclick = () => $('#resetDialog').showModal();
  $('#confirmReset').onclick = () => busy($('#confirmReset'),async()=>{
    await api('service/reset',{confirm:true});
    $('#resetDialog').close(); await refresh();
    toast('已重新初始化：插件配置已清空，下载目录里的文件未受影响');
  });
  $('#loginForm').onsubmit = e => {e.preventDefault();const f=e.target;busy(f.querySelector('[type=submit]'),async()=>{await api('login',{password:f.elements.password.value});f.reset();await refresh();});};
  $('#toggleService').onclick = () => busy($('#toggleService'),async()=>{await api('service/' + (state.running?'stop':'start'),{});await refresh();});
  $('#forwardPort').onclick = () => busy($('#forwardPort'),async()=>{
    const result = await api('forward',{}), forward = result.forward || {};
    toast(forward.ok ? '路由器已转发 BT 端口' : '未能转发：' + (forward.detail || '原因未知'));
    await refresh();
  });
  $('#add').onclick = () => {$('#addForm').reset();$('#addDialog').showModal();};
  $('#addForm').onsubmit = e => {e.preventDefault();const f=e.target;busy(f.querySelector('[type=submit]'),async()=>{
    const file=f.elements.torrent.files[0],magnet=f.elements.magnet.value.trim();
    if(!!file === !!magnet)throw new Error('请选择种子文件，或填写一条磁力链接');
    if(file){if(file.size>2*1024*1024)throw new Error('种子文件不能超过 2 MiB');const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('读取文件失败'));reader.readAsDataURL(file);});await api('torrent',{content});}
    else await api('magnet',{url:magnet});$('#addDialog').close();await refresh();toast('任务已添加');
  });};
  $('#limits').onclick = () => busy($('#limits'),async()=>{const limits=await api('limits');for(const k of ['download','upload','active'])$('#limitForm').elements[k].value=limits[k];$('#limitDialog').showModal();});
  $('#limitForm').onsubmit = e=>{e.preventDefault();busy(e.target.querySelector('[type=submit]'),async()=>{const f=e.target.elements;await api('limits',{download:Number(f.download.value),upload:Number(f.upload.value),active:Number(f.active.value)});$('#limitDialog').close();toast('已保存');});};
  $('#confirmRemove').onclick=()=>busy($('#confirmRemove'),async()=>{await api('remove',{hash:removeHash});$('#removeDialog').close();await refresh();});
  $('#logout').onclick=()=>busy($('#logout'),async()=>{await api('logout',{});await refresh();});
  if (location.hash === '#console') showConsole(true);
  async function tick(){if(!root.isConnected)return;if(!document.hidden)await refresh();setTimeout(tick,3000);}tick();
})();
