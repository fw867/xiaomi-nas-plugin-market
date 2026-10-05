(() => {
  'use strict';
// Windows 客户端 location 可能带盘符（/D:/plugin/...），相对 fetch 会 400。
// 以当前 script URL 为绝对基址（与 aliyundrive/115 插件一致）。
  function pluginAssetBase() {
    const loaded = document.currentScript?.src
      || [...document.scripts].map((s) => s.src).find((src) => /\/app(?:\.bundle)?\.js(?:$|\?)/.test(src));
    if (loaded) {
      const url = new URL(loaded);
      const cleanPath = url.pathname.replace(/^\/[A-Za-z]:/, '');
      return new URL(cleanPath.replace(/[^/]*$/, ''), url.origin).href;
    }
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
  const root = document.querySelector('#tr-app');
  const $ = (s) => root.querySelector(s);
  const session = document.querySelector('meta[name="tr-session"]').content;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = (name) => `<img src="${icons[name]}" alt="">`;
  root.querySelectorAll('[data-icon]').forEach(el => el.src = icons[el.dataset.icon]);
  let state, items = [], browsePath = '', browseRoot = 0, browseTarget = '', toastTimer, polling = false, generation = 0, consoleOpen = false;
  // 目录选择弹窗写回的目标：初始化表单与「修改目录」弹窗共用同一个选择器
  const fieldIds = {
    download: '#downloadPath', config: '#configPath', watch: '#watchPath',
    reDownload: '#reDownloadPath', reConfig: '#reConfigPath', reWatch: '#reWatchPath',
  };
  const bytes = (n) => {
    n = Number(n || 0);
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0;
    while (n >= 1024 && i < 4) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + u[i];
  };
  const TR_STATUS_LABEL = {
    0: '已停止', 1: '等待校验', 2: '校验中', 3: '等待下载', 4: '等待做种',
    5: '下载中', 6: '下载中', 7: '做种中', 8: '做种中',
  };
  const isStopped = (s) => s === 0;
  // 活动中的任务（没暂停）排到列表最前面；暂停/停止的留在后面
  const isActive = (item) => !isStopped(Number(item.status));
  // 进度条配色：报错红 > 暂停灰 > 完成绿 > 下载中蓝。
  // 暂停排在完成前面：下完再被停止的任务显示灰色（用户要求），只有仍在做种/运行的
  // 完成态才是绿色。
  function progressState(item) {
    if (item.error) return 'error';
    if (isStopped(Number(item.status))) return 'paused';
    if (Number(item.progress || 0) >= 1) return 'done';
    return 'active';
  }

  function toast(message) {
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6500);
  }
  function showError(message) {
    const box = $('#error');
    box.textContent = message || '';
    box.hidden = !message;
  }
  async function api(route, data) {
    const response = await fetch(assetUrl('api/' + route), {
      method: data === undefined ? 'GET' : 'POST',
      cache: 'no-store',
      headers: {
        'X-TR-Session': session,
        'X-CSRF-Token': csrf,
        ...(data === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) throw new Error(result.error || '请求失败');
    return result;
  }
  async function busy(button, fn) {
    button.disabled = true;
    try { await fn(); } catch (e) { toast(e.message); } finally { button.disabled = false; }
  }
  function showConsole(on) {
    consoleOpen = !!on;
    const status = $('#statusView');
    const view = $('#consoleView');
    if (status) status.hidden = !!on;
    if (view) view.hidden = !on;
    if (on) {
      if (location.hash !== '#console') location.hash = 'console';
    } else if (location.hash === '#console') {
      history.replaceState(null, '', location.pathname + location.search);
    }
  }
  function stateLabel(current) {
    if (current.busy) return '正在处理，请稍候';
    if (!current.configured) return '未初始化';
    if (!current.running) return '已停止';
    return current.ready ? '服务运行中' : '容器已启动，等待 Transmission 就绪';
  }
  // 状态卡片里的目录：显示后端给的绝对路径，长路径靠 CSS 换行 + title 悬停查看
  function setPathText(selector, value) {
    const node = $(selector);
    if (!node) return;
    node.textContent = value || '—';
    node.title = value || '';
  }
  function render(current) {
    $('#serviceState').textContent = stateLabel(current);
    const dot = current.busy ? '' : (current.running && current.ready ? 'on' : 'off');
    $('#statusDot').className = dot ? 'status-dot ' + dot : 'status-dot';
    const info = [];
    if (current.imageVersion) info.push('镜像 ' + current.imageVersion);
    if (current.preview) info.push('预览模式，不会操作 Docker');
    $('#serviceInfo').textContent = info.join(' · ');
    $('#serviceInfo').hidden = !info.length;

    $('#setup').hidden = current.configured || current.busy;
    $('#serviceActions').hidden = !current.configured;
    const live = current.configured && current.running;
    $('#access').hidden = !live;
    // 「打开控制台」现在和端口按钮同排（那张卡片常显），服务没跑时要禁掉
    const consoleEntry = $('#openConsole');
    if (consoleEntry) consoleEntry.disabled = !live;
    $('#address').textContent = current.address || '（请从设备所有者的小米客户端打开插件以获取地址）';
    $('#toggleService').disabled = current.busy;
    $('#toggleService').textContent = current.running ? '停止服务' : '启动服务';
    // 三个目录显示完整绝对路径（后端给的 *_abs），长路径悬停看 title
    setPathText('#downloadDir', current.download_abs || (current.download ? '/' + current.download : ''));
    setPathText('#configDir', current.config_abs || (current.config ? '/' + current.config : ''));
    setPathText('#watchDir', current.watch_abs || (current.watch ? '/' + current.watch : ''));
    $('#username').textContent = current.username || '—';
    $('#settingsFile').textContent = current.settingsFile || '—';
    const legacy = $('#legacyHint');
    if (legacy) {
      legacy.hidden = !current.legacySettings;
      if (current.legacySettings) $('#legacyPath').textContent = current.legacyFile || '';
    }
    renderPortState(current);
    renderPortPublish(current);
    renderForward(current);
    renderStats(current);
    renderSchedule(current);
    renderRoots();
    renderCredentialWarning(current);
    if (!current.busy) showError(current.error);
  }
  // 配置还在、凭据文件却丢了：明确告诉用户去哪个入口重设密码，别让人卡在
  // 「容器起不来、又因为配置已存在没法重新初始化」的状态里
  function renderCredentialWarning(current) {
    const box = $('#credentialWarning');
    if (!box) return;
    box.hidden = !current.credentialMissing;
    if (current.credentialMissing) {
      box.textContent = 'WebUI 凭据文件缺失（数据目录里的 credential.json 不在了）：'
        + '点「修改目录」填写新的 WebUI 密码即可恢复，或点「重新初始化」重新设置目录与账号密码。';
    }
  }
  function renderStats(current) {
    const box = $('#statusStats');
    if (!box) return;
    const t = current.transfer || {};
    const has = t.dlspeed !== null && t.dlspeed !== undefined;
    box.hidden = !has;
    if (!has) return;
    $('#statsDown').textContent = bytes(t.dlspeed) + '/s';
    $('#statsUp').textContent = bytes(t.upspeed) + '/s';
    $('#statsSeeding').textContent = t.seeding;
    $('#statsDownloading').textContent = t.downloading;
  }
  // 状态用圆点表示（绿=正常，红=异常，灰=未知/未测），完整说明放在 title 里
  function setDot(selector, state, title) {
    const dot = $(selector);
    if (!dot) return;
    dot.className = state ? 'status-dot ' + state : 'status-dot';
    if (title) dot.title = title;
  }
  function renderForward(current) {
    const forward = current.forward || {};
    const port = forward.externalPort || 51413;
    let text = `路由器端口映射未尝试（${port} TCP+UDP）`, state = '';
    if (forward.ok) {
      const lease = Number(forward.lease) || 0;
      const renew = lease ? `，${Math.round(lease / 60)} 分钟自动续期` : '';
      text = `路由器已转发 ${port}（${forward.method}${renew}）`;
      state = 'on';
    } else if (forward.removed) {
      text = `路由器映射已移除（${port} TCP+UDP），启动服务时会重新映射`;
    } else if (forward.at) {
      text = `路由器未转发 ${port}：${forward.detail || '原因未知'}`;
      state = 'bad';
    }
    setDot('#forwardDot', current.busy ? '' : state, text);
    $('#forwardPort').disabled = current.busy || !current.running;
  }
  function forwardText(current) {
    const forward = current.forward || {};
    const port = forward.externalPort || 51413;
    if (forward.ok) return `路由器已转发 ${port}（${forward.method || 'UPnP'}）`;
    if (forward.removed) return `路由器映射已移除（${port}）`;
    if (forward.at) return `未能转发 ${port}：${forward.detail || '原因未知'}`;
    return `路由器端口映射未尝试（${port}）`;
  }
  function renderPortState(current) {
    const port = current.port || {};
    const value = port.peerPort || 51413;
    let text = `BT 端口 ${value} 状态未测试`, state = '';
    if (port.testedAt) {
      if (port.open) {
        text = `BT 端口 ${value} 公网可达`;
        state = 'on';
      } else {
        text = `BT 端口 ${value} 仅局域网可达 · 需在路由器转发 TCP+UDP`;
        state = 'bad';
      }
    }
    setDot('#portDot', current.busy ? '' : state, text);
    $('#testPort').disabled = current.busy || !current.configured || !current.running;
  }
  function portText(current) {
    const port = (current || {}).port || {};
    const value = port.peerPort || 51413;
    if (port.testedAt && port.open) return `BT 端口 ${value} 公网可达`;
    if (port.testedAt) return `BT 端口 ${value} 仅局域网可达 · 需在路由器转发 TCP+UDP`;
    return `BT 端口 ${value} 测试超时，请稍后再试`;
  }
  function stamp(epoch) {
    const d = new Date(Number(epoch) * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function renderSchedule(current) {
    const schedule = current.schedule || {};
    const targets = [['start', '#scheduleStart', '开启'], ['stop', '#scheduleStop', '关闭']];
    for (const [kind, selector] of targets) {
      const select = $(selector);
      if (!select) continue;
      fillScheduleOptions(select);
      const entry = schedule[kind] || {};
      const value = optionValue(entry.value);
      if (select.value !== value) select.value = value;
    }
    const hint = $('#scheduleHint');
    if (!hint) return;
    const parts = [];
    for (const [kind, , label] of targets) {
      const entry = schedule[kind] || {};
      if (optionValue(entry.value) !== 'off' && entry.next) parts.push(`${label} ${stamp(entry.next)}`);
    }
    hint.hidden = !parts.length;
    hint.textContent = parts.length ? `下次：${parts.join(' · ')}` : '';
  }
  // 选项 = 关闭 + 0 点…23 点（共 25 个），按钟点每天执行一次
  function optionValue(value) {
    const text = String(value ?? '');
    if (text === 'off') return 'off';
    return /^([0-9]|1[0-9]|2[0-3])$/.test(text) ? text : 'off';
  }
  function fillScheduleOptions(select) {
    if (select.dataset.filled) return;
    const off = document.createElement('option');
    off.value = 'off';
    off.textContent = '关闭';
    select.append(off);
    for (let hour = 0; hour < 24; hour += 1) {
      const option = document.createElement('option');
      option.value = String(hour);
      option.textContent = `${hour} 点`;
      select.append(option);
    }
    select.dataset.filled = '1';
  }
  function renderPortPublish(current) {
    // Docker 会说端口都发布了，但 docker-proxy 掉线后宿主上其实没人监听
    const box = $('#portPublish');
    if (!box) return;
    const ports = current.ports || {};
    const missing = ports.missing || [];
    box.hidden = !missing.length;
    if (!missing.length) return;
    const tried = ports.repaired
      ? '插件已重启容器重试，仍未成功，请停止后重新启动服务'
      : '插件每分钟巡检一次，会自动重启容器修复';
    box.textContent = `入站端口未发布：${missing.join('、')}（${tried}）`;
  }
  function renderTasks() {
    const search = $('#search').value.toLowerCase();
    const filter = $('#filter').value;
    const visible = items.filter(item => {
      const name = String(item.name || '').toLowerCase();
      if (search && !name.includes(search)) return false;
      if (filter === 'downloading') return !isStopped(item.status) && item.progress < 1;
      if (filter === 'completed') return item.progress >= 1;
      if (filter === 'stopped') return isStopped(item.status);
      return true;
    });
    $('#count').textContent = `${visible.length} 个任务`;
    $('#empty').hidden = visible.length > 0;
    // 活动中的排最前面，其余保持服务端给的顺序（分两段拼接，稳定且不依赖 sort 的稳定性）
    const ordered = visible.filter(item => isActive(item))
      .concat(visible.filter(item => !isActive(item)));
    $('#tasks').innerHTML = ordered.map(item => {
      const stopped = isStopped(item.status);
      const label = TR_STATUS_LABEL[item.status] || ('状态 ' + item.status);
      return `<article class="task"><div class="task-body">
        <strong class="task-name">${escape(item.name)}</strong>
        <progress class="p-${progressState(item)}" max="1" value="${Math.max(0, Math.min(1, Number(item.progress) || 0))}" aria-label="下载进度"></progress>
        <small>${(Number(item.progress || 0) * 100).toFixed(1)}% · ${bytes(item.size)} · ${escape(label)} · ↓ ${bytes(item.dlspeed)}/s · ↑ ${bytes(item.upspeed)}/s${item.error ? ' · ' + escape(item.error) : ''}</small>
      </div><div class="task-actions">
        <button title="${stopped ? '继续' : '暂停'}" aria-label="${stopped ? '继续' : '暂停'}" data-action="${stopped ? 'start' : 'stop'}" data-id="${item.id}">${icon(stopped ? 'play' : 'stop')}</button>
        <button title="移除任务，保留文件" aria-label="移除任务，保留文件" data-action="remove" data-id="${item.id}">${icon('trash')}</button>
      </div></article>`;
    }).join('');
  }
  // 存储位置（存储池 / 外接设备）：state.roots 由后端按 LOCAL_ROOTS 顺序给出
  function rootEntry(index) {
    const roots = (state || {}).roots || [];
    return roots.find((entry) => Number(entry.index) === Number(index)) || null;
  }
  // 弹窗顶部与表单里都用完整绝对路径，一眼能看出目录落在哪块盘上
  function absolutePath(path) {
    const entry = rootEntry(browseRoot);
    const base = entry ? String(entry.path || '').replace(/\/+$/, '') : '';
    if (!path) return base || '存储位置根目录';
    if (!base) return '/' + path;
    return base + '/' + path;
  }
  // 只有一个存储位置时不显示切换，避免多一行无用按钮
  function renderRoots() {
    const box = $('#browseRoots');
    if (!box) return;
    const roots = (state || {}).roots || [];
    if (roots.length < 2) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.hidden = false;
    box.innerHTML = roots.map((entry) => {
      const index = Number(entry.index) || 0;
      const active = index === browseRoot ? ' active' : '';
      const disabled = entry.exists ? '' : ' disabled';
      return `<button type="button" class="root-chip${active}" data-root="${index}"${disabled}>${escape(entry.label || entry.path)}</button>`;
    }).join('');
  }
  // 表单里存的是绝对路径：反查它属于哪个存储位置，供再次打开弹窗时定位
  function locateValue(value) {
    const text = String(value || '');
    const roots = (state || {}).roots || [];
    let found = null;
    for (const entry of roots) {
      const base = String(entry.path || '').replace(/\/+$/, '');
      if (!base) continue;
      if (text === base) return { root: Number(entry.index) || 0, relative: '' };
      if (text.startsWith(base + '/') && (!found || base.length > found.base.length)) {
        found = { base, root: Number(entry.index) || 0, relative: text.slice(base.length + 1) };
      }
    }
    if (found) return { root: found.root, relative: found.relative };
    // 认不出来（旧值，或后端没给位置列表）时按第 0 个位置、原样当相对路径处理
    return { root: 0, relative: text.startsWith('/') ? '' : text };
  }
  async function browse(path) {
    browsePath = path;
    const current = ++generation;
    renderRoots();
    $('#browsePath').textContent = absolutePath(path);
    $('#folders').textContent = '正在读取';
    $('#selectFolder').disabled = true;
    $('#up').disabled = !path;
    try {
      const result = await api('browse?root=' + browseRoot + '&path=' + encodeURIComponent(path));
      if (current !== generation) return;
      $('#folders').innerHTML = result.items.map(item =>
        `<button type="button" data-folder="${escape(item.path)}">${icon('folder')}${escape(item.name)}</button>`
      ).join('') || '<p class="muted">此目录下没有子文件夹</p>';
      $('#selectFolder').disabled = !path;
    } catch (e) {
      if (current === generation) $('#folders').textContent = e.message;
    }
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      const current = await api('status');
      state = current;
      render(current);
      if (consoleOpen || location.hash === '#console') {
        if (!current.running) {
          items = [];
          renderTasks();
          showError('Transmission 未运行，请先启动服务');
        } else {
          try {
            const result = await api('torrents');
            items = result.items || [];
            const t = result.transfer || {};
            $('#downSpeed').textContent = bytes(t.dlspeed) + '/s';
            $('#upSpeed').textContent = bytes(t.upspeed) + '/s';
            $('#downloaded').textContent = bytes(t.downloaded);
            $('#uploaded').textContent = bytes(t.uploaded);
            showError('');
            renderTasks();
          } catch (e) {
            items = [];
            renderTasks();
            showError(e.message);
          }
        }
      }
    } catch (e) {
      showError(e.message);
    } finally {
      polling = false;
    }
  }
  root.addEventListener('click', e => {
    const button = e.target.closest('button');
    if (!button) return;
    if (button.dataset.close) { $('#' + button.dataset.close).close(); return; }
    if (button.dataset.root !== undefined) {
      // 切换存储位置：浏览路径回到该位置的根部
      browseRoot = Number(button.dataset.root) || 0;
      browse('');
      return;
    }
    if (button.dataset.folder !== undefined) { browse(button.dataset.folder); return; }
    if (button.classList.contains('choose')) {
      browseTarget = button.dataset.target;
      const located = locateValue($(fieldIds[browseTarget]).value);
      browseRoot = located.root;
      $('#browse').showModal();
      browse(located.relative);
      return;
    }
    if (button.dataset.action && button.dataset.id) {
      busy(button, async () => {
        await api(button.dataset.action, { id: Number(button.dataset.id) });
        await refresh();
      });
    }
  });
  $('#refresh').onclick = () => busy($('#refresh'), refresh);
  $('#up').onclick = () => browse(browsePath.split('/').slice(0, -1).join('/'));
  $('#selectFolder').onclick = () => {
    // 表单里写完整绝对路径：服务端两种写法都接受，绝对路径更不容易选错盘
    if (browseTarget && fieldIds[browseTarget]) $(fieldIds[browseTarget]).value = absolutePath(browsePath);
    $('#browse').close();
  };
  $('#copyAddress').onclick = () => busy($('#copyAddress'), async () => {
    const text = $('#address').textContent;
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制地址');
    } catch (e) {
      toast('复制失败，请手动记录：' + text);
    }
  });
  $('#testPort').onclick = () => busy($('#testPort'), async () => {
    const before = Number(((state || {}).port || {}).testedAt || 0);
    await api('service/port-test', {});
    // 端口测试在插件后台线程里跑，接口只回 202：轮询到 testedAt 变了再报结果，
    // 否则刷新的还是上一次的状态，弹窗就会说"未测试"。
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      await refresh();
      if (Number(((state || {}).port || {}).testedAt || 0) > before) break;
    }
    toast(portText(state));
  });
  $('#forwardPort').onclick = () => busy($('#forwardPort'), async () => {
    const result = await api('forward', {});
    await refresh();
    toast(result.forward && result.forward.ok
      ? forwardText(state)
      : '未能转发：' + ((result.forward || {}).detail || '原因未知'));
  });
  function saveSchedule(kind, value, select) {
    select.disabled = true;
    api('schedule', { kind, value })
      .then(() => {
        toast(value === 'off' ? '已关闭该定时' : `已设为每天 ${value} 点执行`);
        return refresh();
      })
      .catch(async (e) => { toast(e.message); await refresh(); })
      .finally(() => { select.disabled = false; });
  }
  const scheduleStart = $('#scheduleStart');
  if (scheduleStart) scheduleStart.onchange = (e) => saveSchedule('start', e.target.value, e.target);
  const scheduleStop = $('#scheduleStop');
  if (scheduleStop) scheduleStop.onchange = (e) => saveSchedule('stop', e.target.value, e.target);
  $('#setupForm').onsubmit = e => {
    e.preventDefault();
    const form = e.target;
    const payload = {
      download: form.elements.download.value,
      config: form.elements.config.value,
      watch: form.elements.watch.value,
      username: form.elements.username.value.trim(),
      password: form.elements.password.value,
    };
    if (!payload.download || !payload.config || !payload.watch) {
      showError('请分别选择下载目录、配置文件夹目录和监控目录');
      return;
    }
    busy(form.querySelector('[type=submit]'), async () => {
      await api('service/setup', payload);
      form.elements.password.value = '';
      await refresh();
    });
  };
  $('#toggleService').onclick = () => busy($('#toggleService'), async () => {
    await api('service/' + (state && state.running ? 'stop' : 'start'), {});
    await refresh();
  });
  // 「修改目录」：只换位置，不动用户目录里的文件；容器会按新宿主路径重建
  const reconfigureBtn = $('#reconfigure');
  if (reconfigureBtn) reconfigureBtn.onclick = () => {
    const form = $('#reconfigureForm');
    form.reset();
    for (const [target, key] of [['reDownload', 'download_abs'], ['reConfig', 'config_abs'],
      ['reWatch', 'watch_abs']]) {
      const box = $(fieldIds[target]);
      box.value = (state || {})[key] || '';
      box.title = box.value;
    }
    const current = ['download_abs', 'config_abs', 'watch_abs']
      .map((key) => (state || {})[key]).filter(Boolean);
    $('#currentDirectories').textContent = current.length ? '当前目录：' + current.join(' · ') : '';
    const password = form.elements.password;
    if (password) {
      // 凭据文件丢了时必须设新密码：否则重建容器时没有密码可用，用户会被卡住
      const missing = !!(state || {}).credentialMissing;
      password.required = missing;
      password.placeholder = missing ? '凭据缺失，必须设置新的 WebUI 密码' : '留空表示不修改';
    }
    $('#reconfigureDialog').showModal();
  };
  const reconfigureForm = $('#reconfigureForm');
  if (reconfigureForm) reconfigureForm.onsubmit = e => {
    e.preventDefault();
    const form = e.target;
    const payload = {
      download: form.elements.download.value,
      config: form.elements.config.value,
      watch: form.elements.watch.value,
      password: form.elements.password.value,
    };
    if (!payload.download || !payload.config || !payload.watch) {
      showError('请分别选择下载目录、配置文件夹目录和监控目录');
      return;
    }
    busy(form.querySelector('[type=submit]'), async () => {
      const result = await api('service/reconfigure', payload);
      form.elements.password.value = '';
      $('#reconfigureDialog').close();
      if (result.state) { state = result.state; render(state); }
      await refresh();
      toast('目录已更新，容器已按新位置重建；原目录里的文件不会被删除');
    });
  };
  // 「重新初始化」：清空插件配置并移除容器，用户目录里的文件不受影响
  const resetBtn = $('#reset');
  if (resetBtn) resetBtn.onclick = () => $('#resetDialog').showModal();
  const confirmResetBtn = $('#confirmReset');
  if (confirmResetBtn) confirmResetBtn.onclick = () => busy(confirmResetBtn, async () => {
    const result = await api('service/reset', { confirm: true });
    $('#resetDialog').close();
    if (result.state) { state = result.state; render(state); }
    await refresh();
    toast('已重新初始化：插件配置已清空，下载、配置、监控目录里的文件未受影响');
  });
  const openBtn = $('#openConsole');
  if (openBtn) openBtn.onclick = () => { showConsole(true); busy(openBtn, refresh); };
  const backBtn = $('#backToStatus');
  if (backBtn) backBtn.onclick = () => showConsole(false);
  // 控制台里的「全部开始 / 全部暂停」：不带 ids 就是全部任务
  const startAllBtn = $('#startAll');
  if (startAllBtn) startAllBtn.onclick = () => busy(startAllBtn, async () => {
    await api('all-start', {});
    await refresh();
    toast('已开始全部任务');
  });
  const pauseAllBtn = $('#pauseAll');
  if (pauseAllBtn) pauseAllBtn.onclick = () => busy(pauseAllBtn, async () => {
    await api('all-stop', {});
    await refresh();
    toast('已暂停全部任务');
  });
  const searchInput = $('#search');
  if (searchInput) searchInput.oninput = renderTasks;
  const filterEl = $('#filter');
  if (filterEl) filterEl.onchange = renderTasks;
  const addBtn = $('#add');
  if (addBtn) addBtn.onclick = () => { $('#addForm').reset(); $('#addDialog').showModal(); };
  const addForm = $('#addForm');
  if (addForm) addForm.onsubmit = e => {
    e.preventDefault();
    const form = e.target;
    const file = form.elements.file.files[0];
    const url = (form.elements.url.value || '').trim();
    if (!!file === !!url) { toast('请选择种子文件，或填写种子/磁力地址'); return; }
    busy(form.querySelector('[type=submit]'), async () => {
      if (file) {
        if (file.size > 4 * 1024 * 1024) throw new Error('种子文件不能超过 4 MiB');
        const content = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
          reader.onerror = () => reject(new Error('读取种子文件失败'));
          reader.readAsDataURL(file);
        });
        await api('add', { content });
      } else {
        await api('add', { url });
      }
      $('#addDialog').close();
      await refresh();
      toast('任务已添加');
    });
  };
  window.addEventListener('hashchange', () => showConsole(location.hash === '#console'));
  if (location.hash === '#console') showConsole(true);
  async function tick() {
    if (!root.isConnected) return;
    if (!document.hidden) await refresh();
    setTimeout(tick, 3000);
  }
  tick();
})();
