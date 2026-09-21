(() => {
  'use strict';
  const icons = __ICON_ASSETS__;
  const root = document.querySelector('#tr-app');
  const $ = (s) => root.querySelector(s);
  const session = document.querySelector('meta[name="tr-session"]').content;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = (name) => `<img src="${icons[name]}" alt="">`;
  root.querySelectorAll('[data-icon]').forEach(el => el.src = icons[el.dataset.icon]);
  let state, browsePath = '', browseTarget = '', toastTimer, polling = false, generation = 0;
  const fieldIds = { download: '#downloadPath', config: '#configPath', watch: '#watchPath' };

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
    const response = await fetch('api/' + route, {
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
  function stateLabel(current) {
    if (current.busy) return '正在处理，请稍候';
    if (!current.configured) return '未初始化';
    if (!current.running) return '已停止';
    return current.ready ? '服务运行中' : '容器已启动，等待 Transmission 就绪';
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
    $('#address').textContent = current.address || '（请从设备所有者的小米客户端打开插件以获取地址）';
    $('#toggleService').disabled = current.busy;
    $('#toggleService').textContent = current.running ? '停止服务' : '启动服务';
    $('#downloadDir').textContent = current.download ? '/' + current.download : '—';
    $('#configDir').textContent = current.config ? '/' + current.config : '—';
    $('#watchDir').textContent = current.watch ? '/' + current.watch : '—';
    $('#username').textContent = current.username || '—';
    if (!current.busy) showError(current.error);
  }
  async function browse(path) {
    browsePath = path;
    const current = ++generation;
    $('#browsePath').textContent = path ? '/' + path : '用户存储根目录';
    $('#folders').textContent = '正在读取';
    $('#selectFolder').disabled = true;
    $('#up').disabled = !path;
    try {
      const result = await api('browse?path=' + encodeURIComponent(path));
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
    if (button.dataset.folder !== undefined) { browse(button.dataset.folder); return; }
    if (button.classList.contains('choose')) {
      browseTarget = button.dataset.target;
      $('#browse').showModal();
      browse($(fieldIds[browseTarget]).value);
    }
  });
  $('#refresh').onclick = () => busy($('#refresh'), refresh);
  $('#up').onclick = () => browse(browsePath.split('/').slice(0, -1).join('/'));
  $('#selectFolder').onclick = () => {
    if (browseTarget && fieldIds[browseTarget]) $(fieldIds[browseTarget]).value = browsePath;
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
  async function tick() {
    if (!root.isConnected) return;
    if (!document.hidden) await refresh();
    setTimeout(tick, 3000);
  }
  tick();
})();
