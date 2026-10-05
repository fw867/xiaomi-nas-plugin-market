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

  const icons = {"close": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAADNElEQVR4nOyazWsTQRjGn9kk9aZH0RZDa61HKT0Uo43iB0UQb3rw4MmDgn+NF0+C4k1v4qX1A0qkIKWI3lpta9QqetOTyX6M85osttk12czOTCIzPwjsZtjd9/ntzrA7ux4sx4PlOAGwHCcAluMEwHKcAFiOEwDLsV5AERJMzl4YYxHugKHKwBfCX9GtjbeL32GQ8dmz+0tR6TYH5sXqkheEN9deP/2CPpG6AljEHzCGiwzYK9YuF/YUalQQDEHHKvKRGhi7whjbJ36XomLxHiSQE8DY6d1/YIoKMiEhDi/kH9ldAqYhgZQADl7r/I8K0i2hFb70sjN8mxVIICXAb+IaOE/0N50S/p55NploFLUEkX8DEjBIMjFz7lChUFoWOxhFoh5sRo3wuKqB8ej0+YOij9fEuDOROBawHYZ+ZXP12UdIIC2A6CXBC4I5mZF5JxSel4rLYrGcOEbO8EQuAUQ3CYI684OKrATd4YncAggdEkyEJ5TcCVIhVBAVltJcpiAUCBnpFl5QVxWeUHYrHEsQi/WU5swSeoWnq0lVeEJJF9hJlgD/6g55tpVFuQBCJsggwhNaBBD9BBpUeEKbACJLMFoYVHhCqwCi/ei8lHYXJ6JvtcsYT7SIGynWbFTX37zYhka0CyB6XAlpaD/zMUYEEH1IMBaeMCaAyCDBaHhiqOYERb8PYRjXBWCAYR4EtXeBqWNnRmkyA6nh+YfWL0GZtqFtoRl3IwRNWH0rbPXD0P/2OKx0EKSpsTwBqK09LuSaVOkHZQLieUHkPHtZJNCxoAjtk6Ky0+OmJkW1TovnfUEy9NPiJt4O6ZYgPQaYejUWjwm0z842OjbVkGdMkLoCuoYHf98MebW+uvAVCinPzB8Y8Tx6P3g4eUx8jnhY2VhZ/IQ+kboCCl7xfupbII71gPknVYcnaJ+B1zxBgjvbRC1jHgp3IYGaDyQIztd8r1ndevX8GzRB+ybB4oy/62wb7AcSf8L7p3SGj2lJaM6lSDD3gQRn7KoYlJ6IIn6KtUdhI6qaCB8TSxDiH3LOf4jfYxYE1yGB0TnBYcR9KAnLcQJgOU4ALMcJgOU4AbAcJwCW8xsAAP//lTIWqQAAAAZJREFUAwAdzGhsZYRH1AAAAABJRU5ErkJggg==", "folder": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAADj0lEQVR4nOybTUhUURTH/+fNB7UskECi/BqrdX5ABgVGNdEiaFWbTKtVRAUtNc3aFrRsYbmKiIIgUiakVoWj0CYCnVFJymijtAkc9Z7OMz/evDffztSbue+3cMbDuY97/vfc9+65940BzTGgOZ4A0Bw/8iTUdOIsiE4DVAtXwTMg9SIWjbzOpxXl6lhTc3RbsGr7S2kRhqvhV5PR4TO5evtydaxqPHCPCB1wPbR/Z3XINz8Xf5eTdy5Ota3tuwIqOCveQZQDjMSSkdgzMzryM5trTveAgAp024NnRj8RKxSAtN1BRNeSbfxQMmwBBcBMhrTt3jBIX1f7DFzN1jZrBqQafQYGYtGhLhRIw8FwveFD3GpjlaiPjY9Mo0BCzeEBEeHi5gVzy4Ksj0E/B/pswa8w4TZcBhvoMfu2YZA++znYm61dRgEaWsO75UqXrDYCP4mPDn2Dy1jr06DVRozOvzGkJ6MAhkIPWZ4UpsKKqBcuxcxMexaQ4u5MbdIKYConF+y02oj5sRtHfx2zb2aGJhmJujJlQVoBJH36HKNvUB9cjpmh1iwwYzCYe9P5pxRgTbELVpsIMuDm0V9nNQskU602BnWky4KUAsi8uescfdxBmWBmqj0LJKb+VL6OdUCoqb0OFIzJM7WiKkVZfClwImRfaziCJArcrLTgTVZjMgLX7XZHoJI6+1C5NNoNjlqA8iiRyw52Vr9ZiyEGP5WS5xHKEDJwhUDnNg3kKN6yV4OKZuPjQ+9RhoRawsmbN8yOKe9tikJzPAGgOZ4A0Jy8D0byob75+CFiY2s7yQYW49HhjygEWf0nDXFB64ACqGs51egHR+Tr3mKsK2XDc9qglWMT0cgMtgI7e1OSKeBndQtm8EVCCpk6BeMG8sWwB8z/RgAF+oVio+g38sWcAlZSTIGSCOBbXr4vH19RLBiTy77EA2yVFFOgJPeAiU9v5+Sjpmg3wbECb4J2yJYRKPFTYGos8gH/E/s9wCuGnHgCQHM8AaA5ngDQHE8AaI4nADTHEwCa4wkAzfEEgOZ4AkBzUuwJUiLpP8LhxpaTvShHGG1Ju4KERbuLQwDZOP8iaRG2NGqTP20oR+yb4Io+212cr8mpleeoUGRP/Jnd5nhrav7H1Ped1Q0LROTyH0flh4K6LOcLb+z2tEeXDc3hI3KOcF6+1olbAGUJL0mEUzLyg+lOmCv3ncAc8dYB0BztBfgDAAD//6fLaKkAAAAGSURBVAMAC2YQrc+clvkAAAAASUVORK5CYII=", "play": "data:image/png;base64,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", "plus": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAABlklEQVR4nOzau0sDQRDH8d9eRAw2ogjaRZJYWmhibSU5i8TSxz9npZUBReEOFOwEE0S0M74QIV3UJkIeN24QIWizEsh4zHy6WwYu94U7wrIehPMgnAaAcBoAwmkACKcBwCibK2zO5wobYGTAJLvsh/bmq19XdHB7EayDAUuAVH5tZtRQvX+tmYimXs7DBoaM5RUYiWj851qy3ZkAA/0IQjgNAOE0AITTABBOA0A4DQDhNACE0wAQ7k87QnOLhYWEh0kMyN501nhmt3+NItoioI4BdSM0Hi+Da9d5pwCp1MrY6HSybKd9xAARHdYqQcll1ukVsA9fisvD9xhjiplcoegy6xSgi+gdMWNATZc5pwD3lTAgwhFiwv7W/Vo1PHGZ1Y8gGGSW/LSXwF3/GkWtdK16+oAh0z9CEE4DQDgNAOE0AITTABBOA0A4DQDhNACEYwnQanm/DkS2O92hH5LsYQnwfHP8SqC972u7jb3zdHX2BgZsZ4V7Mnl/2+7eftg9/DKYsAb4D/QjCOE0AITTABBOA0C4TwAAAP//94C6LgAAAAZJREFUAwBTwWq6SxHu6gAAAABJRU5ErkJggg==", "reload": "data:image/png;base64,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", "stop": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAABlklEQVR4nOzasUvDQBTH8d+7VkHo4uboYOqgDiopDlKdtFUH9e8UBAcTNxELlhikOlnrX+AiIjhU7hmHQLN41y3xvc/QcvCutN+SkOEMhDMQTgNAOA0A4TQAhKtPMxy09g8ZZtWA51BCFvRlLD8N7+NL3z3kM7QYHizMEJ9nw1uoBL61YzodPURvrkmfS4BmYS+q8+N/0TbVceYz6QywFHbbIApRMURoN8OO83s77wGGsVa8UPiTmVKUEBFvZq+NfM2M9ewt+WuPM4AlbpiJAgw8vyTRLkooCDtp9s9v5Gs2NO/ao88BEE4DQDgNAOE0AITTABBOA0A4DQDhNACE0wAQTgNAOA0A4TQAhNMAEE4DQDgNAOE0AITTABBOA0A4PSrrMfMxuSBgOQi71yghIm4W1pbfXXvc5wQtHlErfGyDCDsopeLJX67RwLXDeQkM07jHQB9Vw+iN+tGda8znHsBjpuMqRWDGjf3Gic+s12nxXNDaO2LUVsp8XJ7ZDl6Tq9h3z1QB/iN9DoBwGgDCaQAIJz7ADwAAAP//m1H40QAAAAZJREFUAwAAtFhxIVWrVAAAAABJRU5ErkJggg==", "transmission": "data:image/png;base64,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", "trash": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAACAUlEQVR4nOzaP2tTURiA8ee9iUIdVBQnK7WaKDgISiN0UlBs4wfo4NRdBBFcnFxUEDr5IUTcmyhBB7c0IsXJRFIsdG5FRCLXezwOFcXppu8tt7zvb0nOhfzh4dxzz3ASjEswzgNgXJUCTM7OTkz8PHibkFyLw/3shDCSwKv+SmuJAhQS4EB66A4ij+Kf1yFcr19q/hh0W09RVsgtEOAsyiSE8xSgkACCtFAWo6p/529ak/Q/9cb8oggnURAyPg567WcUoLAAe4XvAzBu7Mfg9IUbU5VKNk0JZCEdDt911hnDWGvAmZn5JRK5S5mE8Li/0r5PTrkDnL44V6tUkwFlNBpN9ldfb+T5SO41INuXfqWsqtmInCrktLWx9u3o8Vp8J1cokRC4N+h1OuSksg+oN5qbcdNzeHucERY+ddsvUFRrzC0kkjz/69Jmv9s6wg75PgDjPADGeQCM8wAY5wEwzgNgnAfAOA+AcR4A4zwAxnkAjPMAGOcBMM4DYJwHwDgPgHF+TA4FInyJL39OiAjyoN5o3kKTcOzfC2ELBSoBQuBDjDC1PRY4V/whXFlFgcotIJI9CRG7JP5SliIPUaASoN99+VZEFtkFsfL3OLluDrvLPRSoTtT6zNVTSPVyIDmRSFBdYLMgmRDW0zR5s/Z++TNK/Lg8xnkAjPMAGOcBMO4XAAAA//+dUHYFAAAABklEQVQDAIR/eG7GGNrOAAAAAElFTkSuQmCC", "up": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAC/ElEQVR4nOyaz08TQRiG35lNSwUC8UBMxBgiPw6Y6AExwRgFIUATPagHDv5z3jxpTIxpUQgcNCqUaIJyKMU0HDhpFVOEtN0ZZ2o22Rbb7ba7OyYzT8Kh3+x+37xPS9ttS6E5FJpjBEBzjABojhEAzbGgkLHJ5NLZweHLhYO9L1AEgSJGry+mCchC9Qbn6exmOgkFRC5gfHw8Xu65uEwImXbXOefrsaP9hZ2dnRIiJFIBjcI7qJAQmQCv8A5RS4hEwNDQdCI2kEh5hXeIUkLoAmT4+EBiFYTc8HNeVBJCFXB+4l53r1VeFmNuuusc/ES8AiS8aqK6VrRjdw+2XvxGSIT2Rkje8720/Lo+vAhVYJXy/frjOcOSXKutkhnZ48LU1BmERCgCGj3sxcP60GbsNqXI1p9jUbYt1+QxNQuiR3elf0X2RAgELqBZeM7JzF7m1WfO46f+9Rizyd81MvMvCbJnGBICFeAVPpdJffTqIY+JUkJgAs5dme/pNLxDlBICESDD93fRtSDCO0QloWMBrvCT7non4R0cCaJbsWYhQAkdCWgUXm640/AO1R6Mz4YloW0BzcLLDQcR3iGbWd5oJkHuBW3SlgCv8NUNB0wzCXIv7UpoS0BfF31+KjznRwyYbyW8Zdmsvkapxb3Ok73lDDmrZkHspS9Bn6INfAsYvTZ7SVzVzdVWeZFRMpfbSL9DyMgZcla9BPnp0tjVO4PwiW8B3Ip/cw8Xd9uxzchi7kPqfas95Lu+VmqNqM5iWJSzXTsrlg7Zd/jEtwAx/BcjeCgvV8Xfig1yay+TeuunR7kSY63UmpHdSr8hnMtrh5XqXhh7kM+vn8AnSj4UHZlIDlMLOXeNs9Lwbmb1KyLGfDECzTECoDlGADTHCIDmGAHQHCMAmmMEQHOMAGiOEQDNMQKgOUYANEeJgFKJFupr5YpdgAKUCNjffvmDgz9xbosvNh7nP63/hAKU/VpcMjKZfETAj3c308+gCKUC/gfMkyA0xwiA5hgB0BztBfwBAAD//3U1BlAAAAAGSURBVAMA9d6vRa/kgPgAAAAASUVORK5CYII="};
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
