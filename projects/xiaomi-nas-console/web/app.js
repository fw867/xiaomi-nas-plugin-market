'use strict';

/* 小米智能存储「控制台」前端：只读。全部使用相对路径，
   因此同一份页面既能挂在 /plugin/<用户>/nasconsole/ 下（小米客户端入口），
   也能挂在浏览器入口的根路径下。 */

const API = 'api';

const POLL = {
  overview: 2000,
  history: 6000,
  storage: 60000,
  docker: 10000,
  services: 30000,
  plugins: 60000,
};

const state = {
  view: null,                 // 当前聚焦的窗口
  open: new Set(),            // 已打开的窗口
  z: 10,
  session: null,
  overview: null,
  storage: null,
  docker: null,
  services: null,
  history: null,
  series: { cpu: true, mem: true, net: false, disk: false },
  files: {
    path: '', data: null, preview: null, filter: '', active: null, loading: false,
    selected: new Set(), mode: 'browse', trash: null,
    clipboard: { mode: '', paths: [] }, task: null, taskTimer: 0,
    pick: { path: '', data: null, onPick: null },
  },
  csrf: '',
  plugins: [],
  lastRun: {},
  lastOk: 0,
  failures: 0,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 工具 */

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'style') applyStyle(node, value);
      else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// 页面带严格 CSP（style-src 'self'），setAttribute('style', …) 会被浏览器拒绝，
// 所以动态样式一律走 CSSOM（CSP 不限制 CSSOM 写入）。
function applyStyle(node, declarations) {
  for (const part of String(declarations).split(';')) {
    const index = part.indexOf(':');
    if (index <= 0) continue;
    const name = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (name && value) node.style.setProperty(name, value);
  }
}

function fmtBytes(bytes, digits = 1) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let value = Number(bytes);
  let index = 0;
  while (Math.abs(value) >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  const text = index === 0 || Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(digits);
  return `${text} ${units[index]}`;
}

function fmtRate(bytesPerSecond) {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return '—';
  return `${fmtBytes(bytesPerSecond)}/s`;
}

function fmtUptime(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function fmtHours(raw) {
  const hours = Number(String(raw).split(' ')[0]);
  if (!Number.isFinite(hours)) return raw === null || raw === undefined ? '—' : String(raw);
  if (hours >= 24) return `${Math.round(hours / 24)} 天`;
  return `${hours} 小时`;
}

function fmtNumber(value) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString('zh-CN');
}

function fmtTime(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function niceMax(value) {
  const target = Math.max(1, Number(value) || 0);
  const power = 10 ** Math.floor(Math.log10(target));
  for (const step of [1, 2, 5, 10]) {
    if (target <= step * power) return step * power;
  }
  return 10 * power;
}

function percentClass(percent) {
  if (percent >= 90) return 'is-bad';
  if (percent >= 75) return 'is-warn';
  return 'is-ok';
}

function badge(text, kind) {
  return h('span', { class: `badge ${kind ? `is-${kind}` : ''}`, text });
}

function dot(kind) {
  return h('span', { class: `dot ${kind ? `is-${kind}` : ''}` });
}

function diskStateLabel(value) {
  const map = {
    standby: ['休眠', 'ok'],
    sleeping: ['休眠', 'ok'],
    'active/idle': ['活动', 'info'],
    idle: ['空闲', 'info'],
    active: ['活动', 'info'],
    unknown: ['未知', ''],
  };
  return map[value] || [value || '未知', ''];
}

/* ------------------------------------------------------------------ 图表 */

function pathFor(values, width, height, scale) {
  const points = [];
  const step = values.length > 1 ? width / (values.length - 1) : width;
  values.forEach((value, index) => {
    const numeric = Number(value);
    const safe = Number.isFinite(numeric) ? Math.max(0, Math.min(scale, numeric)) : 0;
    const x = index * step;
    const y = height - (safe / scale) * height;
    points.push(`${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`);
  });
  return points.join(' ');
}

function chartSpec(history) {
  if (!history || !history.cpu || !history.cpu.length) return [];
  const spec = [];
  const maxOf = (values) => values.reduce((acc, value) => Math.max(acc, Number(value) || 0), 0);
  if (state.series.cpu) {
    spec.push({ key: 'cpu', label: 'CPU', color: 'var(--accent)', values: history.cpu, scale: 100, unit: '%' });
  }
  if (state.series.mem) {
    spec.push({ key: 'mem', label: '内存', color: 'var(--info)', values: history.mem, scale: 100, unit: '%' });
  }
  if (state.series.net) {
    spec.push({ key: 'net', label: '网络', color: 'var(--ok)', values: history.net, scale: niceMax(maxOf(history.net)), unit: 'B/s' });
  }
  if (state.series.disk) {
    spec.push({ key: 'disk', label: '磁盘 I/O', color: 'var(--warn)', values: history.disk, scale: niceMax(maxOf(history.disk)), unit: 'B/s' });
  }
  return spec;
}

function sparkline(values, scale, color) {
  const width = 100;
  const height = 30;
  const safeScale = scale || niceMax(values.reduce((acc, v) => Math.max(acc, Number(v) || 0), 0));
  const path = pathFor(values && values.length ? values : [0], width, height, safeScale);
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  node.setAttribute('viewBox', `0 0 ${width} ${height}`);
  node.setAttribute('preserveAspectRatio', 'none');
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  line.setAttribute('d', path);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', color);
  line.setAttribute('stroke-width', '2');
  line.setAttribute('stroke-linejoin', 'round');
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  node.append(line);
  return node;
}

function renderMainChart() {
  const host = $('mainChart');
  if (!host) return;
  const spec = chartSpec(state.history);
  if (!spec.length) {
    host.replaceChildren(h('div', { class: 'empty', text: '正在采集…' }));
    return;
  }
  const width = Math.max(280, host.clientWidth || 320);
  const height = host.clientHeight || 150;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('preserveAspectRatio', 'none');

  for (let index = 0; index <= 3; index += 1) {
    const y = (height / 3) * index;
    const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    grid.setAttribute('x1', '0');
    grid.setAttribute('x2', String(width));
    grid.setAttribute('y1', y.toFixed(1));
    grid.setAttribute('y2', y.toFixed(1));
    grid.setAttribute('stroke', 'var(--line)');
    grid.setAttribute('stroke-width', '1');
    grid.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.append(grid);
  }

  for (const series of spec) {
    const path = pathFor(series.values, width, height, series.scale);
    const area = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    area.setAttribute('d', `${path} L${width},${height} L0,${height} Z`);
    area.setAttribute('fill', series.color);
    area.setAttribute('opacity', '0.14');
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    line.setAttribute('d', path);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', series.color);
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-linejoin', 'round');
    line.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.append(area, line);
    const last = Number(series.values[series.values.length - 1]);
    if (Number.isFinite(last)) {
      const marker = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      const clamped = Math.max(0, Math.min(series.scale, last));
      marker.setAttribute('cx', String(Math.max(3, width - 3)));
      marker.setAttribute('cy', (height - (clamped / series.scale) * height).toFixed(1));
      marker.setAttribute('r', '3.5');
      marker.setAttribute('fill', series.color);
      svg.append(marker);
    }
  }
  host.replaceChildren(svg, buildChartSummary(spec));
}

function buildChartSummary(spec) {
  const box = h('div', { class: 'legend chart-summary' });
  for (const series of spec) {
    const last = series.values[series.values.length - 1];
    const text = series.unit === '%' ? `${last === null || last === undefined ? '—' : Math.round(last)}%` : fmtRate(last);
    box.append(h('span', { class: 'muted', text: `${series.label} ${text}` }));
  }
  return box;
}

/* ------------------------------------------------------------------ 渲染 */

function ring(value, { color, label }) {
  const size = 76;
  const stroke = 8;
  const center = size / 2;
  const radius = center - stroke / 2 - 1;
  const circumference = 2 * Math.PI * radius;
  const percent = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, Number(value)));
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  const dial = (strokeColor, dash) => {
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', String(center));
    circle.setAttribute('cy', String(center));
    circle.setAttribute('r', String(radius));
    circle.setAttribute('fill', 'none');
    circle.setAttribute('stroke', strokeColor);
    circle.setAttribute('stroke-width', String(stroke));
    if (dash) circle.setAttribute('stroke-dasharray', dash);
    return circle;
  };
  svg.append(dial('rgba(140, 150, 172, .30)'));
  const progress = dial(color, `${((circumference * percent) / 100).toFixed(2)} ${circumference.toFixed(2)}`);
  progress.setAttribute('stroke-linecap', 'round');
  progress.setAttribute('transform', `rotate(-90 ${center} ${center})`);
  svg.append(progress);
  return h('div', { class: 'ring' },
    h('div', { class: 'ring-dial' }, svg,
      h('span', { class: 'ring-value' },
        value === null || value === undefined ? '—' : String(Math.round(percent)),
        h('small', { text: '%' }))),
    h('span', { class: 'ring-label', text: label }));
}

function widgetRow(label, value) {
  return h('div', { class: 'kv' }, h('dt', { text: label }), h('dd', { text: value }));
}

// 右侧组件栏：一眼可见的运行状态、网络、存储读写与容量。
function renderWidgets() {
  const data = state.overview;
  if (!data) return;
  const host = data.host || {};
  const history = state.history || {};
  const memPercent = data.mem && data.mem.total ? Math.round((data.mem.used * 100) / data.mem.total) : null;
  const diskTemps = (state.storage?.drives || [])
    .filter((drive) => drive.temperature !== null && drive.temperature !== undefined)
    .map((drive) => `${drive.dev} ${drive.temperature}°C`);

  $('widgetStatus').replaceChildren(
    h('div', { class: 'widget-head' }, h('h3', { text: '运行状态' }), h('span', { text: host.hostname || '' })),
    h('div', { class: 'rings' },
      ring(data.cpu, { color: 'var(--accent)', label: 'CPU' }),
      ring(memPercent, { color: 'var(--info)', label: '内存' })),
    h('dl', { class: 'kv-list' },
      widgetRow('本次运行', fmtUptime(host.uptime)),
      widgetRow('CPU 温度', host.cpu_temp === null || host.cpu_temp === undefined ? '—' : `${host.cpu_temp} °C`),
      widgetRow('硬盘温度', diskTemps.length ? diskTemps.join(' · ') : '休眠中未读取'),
      widgetRow('系统负载', (host.loads || []).map((value) => Number(value).toFixed(2)).join(' / '))),
  );

  const net = data.net || {};
  const primary = (net.ifaces || []).find((item) => item.name === net.primary) || { in: 0, out: 0 };
  $('widgetNetwork').replaceChildren(
    h('div', { class: 'widget-head' }, h('h3', { text: '网络' }), h('span', { text: net.primary || '—' })),
    h('div', { class: 'rate-row' },
      h('span', { class: 'muted', text: '实时' }),
      h('span', null,
        h('b', { class: 'down', text: `↓ ${fmtRate(primary.in)}` }), ' ',
        h('b', { class: 'up', text: `↑ ${fmtRate(primary.out)}` }))),
    h('div', { class: 'spark' }, sparkline(history.net || [0], null, 'var(--info)')),
  );

  const io = data.io || {};
  let read = 0;
  let write = 0;
  let active = 0;
  for (const value of Object.values(io)) {
    read += value.in || 0;
    write += value.out || 0;
    if ((value.in || 0) + (value.out || 0) > 0) active += 1;
  }
  $('widgetStorage').replaceChildren(
    h('div', { class: 'widget-head' }, h('h3', { text: '存储读写' }),
      h('span', { text: active ? `${active} 个设备活动` : '当前空闲' })),
    h('div', { class: 'rate-row' },
      h('span', { class: 'muted', text: '实时' }),
      h('span', null,
        h('b', { class: 'down', text: `R ${fmtRate(read)}` }), ' ',
        h('b', { class: 'up', text: `W ${fmtRate(write)}` }))),
    h('div', { class: 'spark' }, sparkline(history.disk || [0], null, 'var(--accent)')),
  );

  // 容量优先显示 NAS 自己的存储：pool → 数据盘 → 系统区 → 内置盘
  const priority = ['/nas/pool0', '/nas/mnt/pa0', '/nas/mnt/pa1', '/nas/sys', '/data', '/log'];
  const rank = (point) => {
    const index = priority.findIndex((prefix) => point === prefix || point.startsWith(`${prefix}/`));
    return index === -1 ? priority.length : index;
  };
  const allMounts = state.storage?.mounts || [];
  const mounts = allMounts
    .filter((mount) => mount.total >= 512 * 1024 * 1024)
    .sort((a, b) => rank(a.point) - rank(b.point) || b.percent - a.percent)
    .slice(0, 5);
  $('widgetSpace').replaceChildren(
    h('div', { class: 'widget-head' }, h('h3', { text: '存储空间' }),
      h('span', { text: `${allMounts.length} 个挂载点` })),
    h('div', null, ...(mounts.length ? mounts.map((mount) => h('div', { class: 'space-row' },
      h('div', { class: 'space-top' },
        h('span', { text: mount.point }),
        h('b', { text: `${fmtBytes(mount.used)} / ${fmtBytes(mount.total)}` })),
      h('div', { class: `bar ${percentClass(mount.percent)}` }, h('i', { style: `width:${mount.percent}%` }))),
    ) : [h('div', { class: 'empty', text: '正在读取容量…' })])),
  );
}

function renderOverview() {
  const data = state.overview;
  if (!data) return;
  const host = data.host || {};
  $('hostName').textContent = [host.hostname, data.version ? `v${data.version}` : ''].filter(Boolean).join(' · ');


  $('diskSleepRows').replaceChildren(...Object.entries(data.spin || {}).map(([dev, spinState]) => {
    const [label, kind] = diskStateLabel(spinState);
    const drive = (state.storage?.drives || []).find((item) => item.dev === dev);
    const temperature = drive && drive.temperature !== null && drive.temperature !== undefined
      ? `温度 ${drive.temperature}°C`
      : '温度待唤醒后读取';
    return h('div', { class: 'row' },
      h('div', { class: 'row-main' },
        h('div', { class: 'row-title' }, dot(kind || 'ok'), dev, drive ? h('span', { class: 'muted', text: drive.model || '' }) : null),
        h('div', { class: 'row-sub', text: temperature })),
      h('div', { class: 'row-side' }, badge(label, kind)));
  }));

  $('systemRows').replaceChildren(
    row('主机名', host.hostname || '—'),
    row('型号', host.model || '—'),
    row('内核', host.kernel || '—'),
    row('CPU', `${host.cores || '—'} 核 · ${host.freq_mhz ? `${(host.freq_mhz / 1000).toFixed(2)} GHz` : '—'}${host.governor ? ` · ${host.governor}` : ''}`),
    row('采集间隔', '2 秒 · 只读 /proc'),
  );
  renderWidgets();
}

function row(label, value) {
  return h('div', { class: 'row' },
    h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: label })),
    h('div', { class: 'row-side', text: value }));
}

function renderStorage() {
  const data = state.storage;
  if (!data) return;
  const host = $('driveCards');
  const drives = data.drives || [];
  host.replaceChildren(...(drives.length ? drives.map((drive) => {
    const [label, kind] = diskStateLabel(drive.state);
    const attrs = [
      ['温度', drive.temperature !== null && drive.temperature !== undefined ? `${drive.temperature} °C` : '（休眠）'],
      ['通电时间', fmtHours(drive.power_on_hours)],
      ['重映射扇区', fmtNumber(drive.reallocated)],
      ['待映射扇区', fmtNumber(drive.pending)],
      ['起停次数', fmtNumber(drive.start_stop)],
      ['SMART', drive.health || (drive.skipped ? '待唤醒' : '—')],
    ];
    return h('div', { class: 'card' },
      h('div', { class: 'card-head' },
        h('h2', { text: `${drive.dev} · ${drive.model || '未知型号'}` }),
        badge(drive.rotational === 1 ? '机械盘' : '固态', 'info')),
      h('div', { class: 'rows' },
        row('容量', fmtBytes(drive.bytes)),
        h('div', { class: 'row' },
          h('div', { class: 'row-main' }, h('div', { class: 'row-title', text: '电源状态' })),
          h('div', { class: 'row-side' }, badge(label, kind))),
        row('序列号', drive.serial || '—')),
      h('div', { class: 'attrs' }, ...attrs.map(([name, value]) => h('div', { class: 'attr' },
        h('span', { text: name }), h('strong', { text: String(value) })))),
      drive.skipped ? h('p', { class: 'muted', style: 'margin:8px 0 0', text: '硬盘处于休眠（standby），控制台不会为了读 SMART 把它唤醒。' }) : null,
    );
  }) : [h('div', { class: 'card' }, h('div', { class: 'empty', text: '未发现物理硬盘' }))]));

  $('mountNote').textContent = `更新于 ${fmtTime(data.at)}`;
  $('mountRows').replaceChildren(...(data.mounts || []).map((mount) => h('div', { class: 'row row-block' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title' }, mount.point, h('span', { class: 'muted', text: mount.fstype })),
      h('div', { class: 'row-sub', text: `${mount.source} · 已用 ${fmtBytes(mount.used)} / ${fmtBytes(mount.total)} · 可用 ${fmtBytes(mount.free)}` })),
    h('div', { class: 'bar row-bar ' + percentClass(mount.percent) }, h('i', { style: `width:${mount.percent}%` })))));

  const arrays = data.arrays || [];
  $('arrayRows').replaceChildren(...(arrays.length ? arrays.map((array) => h('div', { class: 'row' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title', text: `${array.name} · ${array.level}` }, badge(array.state, 'ok')),
      h('div', { class: 'row-sub', text: `成员 ${(array.members || []).join(', ') || '—'} · ${array.health || ''} ${array.sync || ''}` })),
    h('div', { class: 'row-side', text: array.blocks ? fmtBytes(array.blocks * 1024) : '' }),
  )) : [h('div', { class: 'empty', text: '没有 md 阵列' })]));
}

function renderDocker() {
  const data = state.docker;
  if (!data) return;
  if (!data.active) {
    $('dockerCards').replaceChildren();
    $('dockerRows').replaceChildren(h('div', { class: 'empty', text: data.error || 'Docker 未运行' }));
    $('dockerNote').textContent = '';
    return;
  }
  $('dockerNote').textContent = data.error ? data.error : `更新于 ${fmtTime(data.at)}`;
  $('dockerCards').replaceChildren(
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', { text: 'Docker' })),
      h('div', { class: 'rows' },
        row('版本', data.version || '—'),
        row('API', data.api_version || '—'),
        row('镜像数', fmtNumber(data.images)),
        row('容器', `${fmtNumber(data.containers_running)} 运行 / ${fmtNumber(data.containers_total)} 总计`))),
  );
  const containers = (data.containers || []).slice().sort((a, b) => {
    if (a.state !== b.state) return a.state === 'running' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  $('dockerRows').replaceChildren(...(containers.length ? containers.map((item) => h('div', { class: 'row' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title' }, dot(item.state === 'running' ? 'ok' : ''), item.name,
        item.system ? badge('系统', 'warn') : null),
      h('div', { class: 'row-sub', text: `${item.image} · ${item.status || item.state}${item.ports && item.ports.length ? ` · ${item.ports.join(' ')}` : ''}` })),
    h('div', { class: 'row-side' },
      item.state === 'running'
        ? [h('strong', { text: item.cpu === null || item.cpu === undefined ? '—' : `${item.cpu}%` }),
           h('br'),
           h('span', { class: 'muted', text: `${fmtBytes(item.mem)}${item.net_rx || item.net_tx ? ` · ↓${fmtBytes(item.net_rx)} ↑${fmtBytes(item.net_tx)}` : ''}` })]
        : badge(item.state === 'exited' ? '已停止' : item.state, ''))
  )) : [h('div', { class: 'empty', text: '没有容器' })]));
}

function renderServices() {
  const data = state.services;
  if (!data) return;
  const settings = data.settings || {};
  $('settingCards').replaceChildren(
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', { text: '硬盘休眠' })),
      h('div', { class: 'rows' },
        row('系统开关', settings.hibernate ? '已开启' : '已关闭'),
        row('空闲时长', settings.hibernate_timeout_minutes ? `${settings.hibernate_timeout_minutes} 分钟` : '—'),
        row('时长来源', settings.hibernate_source === 'drop-in' ? '硬盘休眠插件 drop-in' : '系统默认（30 分钟）'))),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', { text: '设备' })),
      h('div', { class: 'rows' },
        row('型号', settings.model || '—'),
        row('主机名', settings.hostname || '—'),
        row('内核', settings.kernel || '—'),
        row('风扇', `${settings.fan_enable ? '已开启' : '已关闭'} · ${settings.fan_mode || '—'}`))),
  );

  const plugins = data.plugins || [];
  $('pluginNote').textContent = `${plugins.length} 个应用`;
  $('pluginRows').replaceChildren(...(plugins.length ? plugins.map((plugin) => h('div', { class: 'row' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title' }, plugin.name || plugin.key,
        plugin.unit || plugin.unit_active
          ? (plugin.unit_active === 'active' ? badge('运行中', 'ok') : badge(plugin.unit_active || '未运行', ''))
          : badge('无独立服务', '')),
      h('div', { class: 'row-sub', text: `${plugin.key} · v${plugin.version || '?'}${plugin.ui_version ? `（界面 ${plugin.ui_version}）` : ''} · ID ${plugin.id ?? '—'}${plugin.port ? ` · 端口 ${plugin.port}` : ''}` })),
    h('div', { class: 'row-side muted', text: plugin.users.join(' ') })
  )) : [h('div', { class: 'empty', text: '注册表里没有插件' })]));

  $('serviceChips').replaceChildren(...(data.watched || []).map((item) =>
    h('span', { class: `chip ${item.active === 'active' ? 'is-ok' : 'is-bad'}`, title: item.unit,
      text: `${item.label} ${item.active === 'active' ? '正常' : item.active}` })));

  const cron = data.crontab || [];
  $('cronRows').replaceChildren(...(cron.length ? cron.map((item) => h('div', { class: 'row' },
    h('div', { class: 'row-main' },
      h('div', { class: 'row-title', text: item.command }),
      h('div', { class: 'row-sub', text: `${item.schedule}${item.tag ? ` · ${item.tag}` : ''}` })),
  )) : [h('div', { class: 'empty', text: '没有匹配到存储相关的定时任务' })]));
}

/* ------------------------------------------------------------------ 数据 */

async function api(path, options) {
  const response = await fetch(`${API}/${path}`, { credentials: 'same-origin', ...options });
  if (response.status === 401) {
    showLogin('需要先登录控制台');
    throw new Error('unauthorized');
  }
  if (!response.ok) throw new Error(`${path} -> ${response.status}`);
  return response.json();
}

function markOk() {
  state.lastOk = Date.now();
  state.failures = 0;
  $('liveDot').classList.remove('is-stale');
}

function markFail(error) {
  state.failures += 1;
  if (state.failures >= 3) $('liveDot').classList.add('is-stale');
  if (state.failures === 1) console.warn('控制台请求失败', error);
}

async function loadSession() {
  state.session = await api('session');
  state.csrf = state.session.csrf || '';
  const needsLogin = state.session.needsLogin && !state.session.authed;
  $('logoutBtn').classList.toggle('hidden', !(state.session.entry === 'lan' && state.session.authed));
  if (needsLogin) showLogin('');
  else hideLogin();
  return state.session;
}

async function loadOverview() {
  state.overview = await api('overview');
  renderWidgets();
  if (state.open.has('overview')) renderOverview();
}

async function loadHistory() {
  state.history = await api('history');
  renderWidgets();
  if (state.open.has('overview')) { renderMainChart(); renderOverview(); }
}

async function loadStorage(force) {
  if (!force && state.storage && Date.now() - (state.storageLoadedAt || 0) < POLL.storage) return;
  state.storage = await api('storage');
  state.storageLoadedAt = Date.now();
  renderWidgets();
  if (state.open.has('storage')) renderStorage();
}

async function loadDocker(force) {
  if (!force && state.docker && Date.now() - (state.dockerLoadedAt || 0) < POLL.docker) return;
  state.docker = await api('docker?stats=1');
  state.dockerLoadedAt = Date.now();
  if (state.open.has('docker')) renderDocker();
}

async function loadServices(force) {
  if (!force && state.services && Date.now() - (state.servicesLoadedAt || 0) < POLL.services) return;
  state.services = await api('services');
  state.servicesLoadedAt = Date.now();
  if (state.open.has('services')) renderServices();
}

async function tick() {
  if (document.hidden) return;
  // 没登录（浏览器入口）时不要打接口，否则控制台会刷一串 401
  if (!state.session) return;
  if (state.session.needsLogin && !state.session.authed) return;
  const now = Date.now();
  const due = (key, interval) => {
    if (now - (state.lastRun[key] || 0) < interval) return false;
    state.lastRun[key] = now;
    return true;
  };
  try {
    if (due('overview', POLL.overview)) await loadOverview();
    if (due('history', POLL.history)) await loadHistory();
    if (due('plugins', POLL.plugins)) await loadPlugins();
    if (state.open.has('storage') && due('storage', POLL.storage)) await loadStorage();
    if (state.open.has('docker') && due('docker', POLL.docker)) await loadDocker();
    if (state.open.has('services') && due('services', POLL.services)) await loadServices();
    markOk();
  } catch (error) {
    markFail(error);
  }
}

/* ------------------------------------------------------------------ 桌面窗口管理 */

// 窗口默认几何：宽高与位置都是「桌面区域的比例」，因此在不同分辨率下都会
// 自动放大/缩小（高清屏默认就占大半屏），另有最小尺寸兜底。
const WINDOW_LAYOUT = {
  overview: { size: [0.74, 0.88], pos: [0.02, 0.03], min: [660, 470] },
  storage: { size: [0.78, 0.90], pos: [0.05, 0.05], min: [700, 480] },
  files: { size: [0.84, 0.93], pos: [0.04, 0.04], min: [680, 450] },
  docker: { size: [0.70, 0.82], pos: [0.08, 0.07], min: [620, 440] },
  services: { size: [0.76, 0.90], pos: [0.03, 0.05], min: [640, 460] },
};
const WINDOW_ORDER = ['overview', 'storage', 'files', 'docker', 'services'];
const CASCADE = 26;

// 点击可能落在 <svg>/<path> 上：不用 Element.closest（老 WebView 对 SVG 支持不一），
// 手动向上找，稳妥。
function closestWithClass(node, className) {
  let element = node instanceof Element ? node : (node && node.parentElement);
  while (element) {
    if (element.classList && element.classList.contains(className)) return element;
    element = element.parentElement;
  }
  return null;
}

function windowNode(view) {
  return $(`view-${view}`);
}

function layerBounds() {
  const rect = $('windowLayer').getBoundingClientRect();
  return { width: Math.max(340, rect.width), height: Math.max(260, rect.height) };
}

function applyGeometry(node, left, top, width, height) {
  const bounds = layerBounds();
  const boxWidth = Math.min(width, bounds.width - 8);
  const boxHeight = Math.min(height, bounds.height - 8);
  const x = Math.max(0, Math.min(left, bounds.width - boxWidth - 4));
  const y = Math.max(0, Math.min(top, bounds.height - boxHeight - 4));
  node.style.setProperty('left', `${Math.round(x)}px`);
  node.style.setProperty('top', `${Math.round(y)}px`);
  node.style.setProperty('width', `${Math.round(boxWidth)}px`);
  node.style.setProperty('height', `${Math.round(boxHeight)}px`);
}

function defaultGeometry(view, cascadeIndex) {
  const bounds = layerBounds();
  const layout = WINDOW_LAYOUT[view] || { size: [0.6, 0.76], pos: [0.05, 0.06], min: [520, 400] };
  const width = Math.max(layout.min[0], Math.round(bounds.width * layout.size[0]));
  const height = Math.max(layout.min[1], Math.round(bounds.height * layout.size[1]));
  const offset = (cascadeIndex % 5) * CASCADE;
  return {
    width,
    height,
    left: Math.round(bounds.width * layout.pos[0]) + offset,
    top: Math.round(bounds.height * layout.pos[1]) + offset,
  };
}

function focusWindow(view) {
  const node = windowNode(view);
  if (!node || !state.open.has(view)) return;
  state.z += 1;
  node.style.setProperty('z-index', String(state.z));
  state.view = view;
  renderWindowTabs();
}

function updateIconState() {
  for (const icon of document.querySelectorAll('.desk-icon')) {
    if (icon.dataset.view) icon.classList.toggle('is-open', state.open.has(icon.dataset.view));
  }
}

function renderWindowTabs() {
  const order = [...WINDOW_ORDER, ...[...state.open].filter((view) => view.startsWith('plugin-'))];
  const tabs = order.filter((view) => state.open.has(view)).map((view) => {
    const node = windowNode(view);
    const title = node && node.querySelector('.win-title') ? node.querySelector('.win-title').textContent : view;
    const minimized = node ? node.classList.contains('is-min') : false;
    return h('button', {
      class: `win-tab${state.view === view && !minimized ? ' is-active' : ''}`,
      'data-view': view,
      type: 'button',
      text: minimized ? `${title} ▪` : title,
    });
  });
  $('windowTabs').replaceChildren(...tabs);
}

function renderView(view) {
  if (view === 'overview') { renderOverview(); renderMainChart(); }
  if (view === 'storage') renderStorage();
  if (view === 'files') renderFileView();
  if (view === 'docker') renderDocker();
  if (view === 'services') renderServices();
}

function ensureData(view) {
  if (view === 'storage') loadStorage().catch(markFail);
  if (view === 'files' && !state.files.data) loadFiles(state.files.path || '').catch(markFail);
  if (view === 'docker') loadDocker().catch(markFail);
  if (view === 'services') loadServices().catch(markFail);
}

function openWindow(view) {
  const node = windowNode(view);
  if (!node) return;
  if (!state.open.has(view)) {
    state.open.add(view);
    node.classList.add('is-open');
    node.classList.remove('is-min');
    const geometry = defaultGeometry(view, state.open.size - 1);
    applyGeometry(node, geometry.left, geometry.top, geometry.width, geometry.height);
  } else {
    node.classList.remove('is-min');
  }
  focusWindow(view);
  updateIconState();
  renderView(view);
  ensureData(view);
}

function closeWindow(view) {
  const node = windowNode(view);
  if (!node) return;
  state.open.delete(view);
  node.classList.remove('is-open', 'is-min', 'is-max');
  if (state.view === view) {
    const remaining = [...WINDOW_ORDER, ...state.open].filter((item) => state.open.has(item));
    state.view = remaining.length ? remaining[remaining.length - 1] : null;
  }
  updateIconState();
  renderWindowTabs();
}

function minimizeWindow(view) {
  const node = windowNode(view);
  if (!node) return;
  node.classList.add('is-min');
  if (state.view === view) state.view = null;
  renderWindowTabs();
}

function beginDrag(event, node) {
  if (typeof event.button === 'number' && event.button !== 0) return;
  if (node.classList.contains('is-max')) return;
  const layerRect = $('windowLayer').getBoundingClientRect();
  const rect = node.getBoundingClientRect();
  const offsetX = event.clientX - rect.left;
  const offsetY = event.clientY - rect.top;
  const move = (moveEvent) => {
    const bounds = layerBounds();
    const left = Math.max(0, Math.min(moveEvent.clientX - layerRect.left - offsetX, bounds.width - rect.width - 4));
    const top = Math.max(0, Math.min(moveEvent.clientY - layerRect.top - offsetY, bounds.height - rect.height - 4));
    node.style.setProperty('left', `${Math.round(left)}px`);
    node.style.setProperty('top', `${Math.round(top)}px`);
  };
  const stop = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', stop);
    window.removeEventListener('pointercancel', stop);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', stop);
  window.addEventListener('pointercancel', stop);
}

function beginResize(event, node, direction) {
  if (node.classList.contains('is-max')) return;
  const layerRect = $('windowLayer').getBoundingClientRect();
  const rect = node.getBoundingClientRect();
  const startX = event.clientX;
  const startY = event.clientY;
  const layout = WINDOW_LAYOUT[node.dataset.view] || { min: [520, 400] };
  const minWidth = layout.min[0];
  const minHeight = layout.min[1];
  const move = (moveEvent) => {
    const bounds = layerBounds();
    let width = rect.width;
    let height = rect.height;
    if (direction.includes('e')) {
      width = Math.max(minWidth, Math.min(rect.width + (moveEvent.clientX - startX), bounds.width - rect.left - 4));
    }
    if (direction.includes('s')) {
      height = Math.max(minHeight, Math.min(rect.height + (moveEvent.clientY - startY), bounds.height - rect.top - 4));
    }
    node.style.setProperty('width', `${Math.round(width)}px`);
    node.style.setProperty('height', `${Math.round(height)}px`);
  };
  const stop = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', stop);
    window.removeEventListener('pointercancel', stop);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', stop);
  window.addEventListener('pointercancel', stop);
}

/* ------------------------------------------------------------------ 已安装应用的桌面图标与窗口 */

async function loadPlugins() {
  const data = await api('plugins');
  state.plugins = data.plugins || [];
  renderPluginIcons();
}

function renderPluginIcons() {
  // 插件图标和系统图标排在同一列流里（一列放不下自动换到下一列），
  // 所以这里只是把上一次渲染的插件图标删掉再按顺序追加。
  for (const node of $('icons').querySelectorAll('.desk-icon.is-plugin')) node.remove();
  const host = $('icons');
  const icons = (state.plugins || []).map((plugin) => h('button', {
    class: 'desk-icon is-plugin',
    'data-view': `plugin-${plugin.key}`,
    'data-key': plugin.key,
    type: 'button',
    title: `${plugin.name} · 点击打开（v${plugin.version}）`,
  },
    h('span', { class: 'icon-tile' },
      plugin.icon
        ? h('img', { src: `plugin-icons/${plugin.icon}`, alt: plugin.name, loading: 'lazy' })
        : h('span', { class: 'icon-fallback', text: (plugin.name || plugin.key).slice(0, 1) }),
      h('span', { class: 'icon-dot', 'aria-hidden': 'true' })),
    h('span', { class: 'desk-label', text: plugin.name }),
  ));
  host.append(...icons);
  updateIconState();
}

function buildPluginWindow(plugin) {
  const view = `plugin-${plugin.key}`;
  const body = [];
  if (plugin.web_ready) {
    body.push(h('div', { class: 'plugin-toolbar' },
      h('span', { class: 'muted', text: `${plugin.key} · v${plugin.version} · ${plugin.web_path}` }),
      h('button', { class: 'mini2', 'data-act': 'reload', type: 'button', text: '重新加载' }),
      h('a', { class: 'mini2', href: plugin.web_path, target: '_blank', rel: 'noreferrer', text: '新标签打开' })));
    body.push(h('div', { class: 'plugin-frame-wrap' },
      h('iframe', {
        class: 'plugin-frame',
        src: plugin.web_path,
        title: plugin.name,
        referrerpolicy: 'no-referrer',
      })));
  } else {
    body.push(h('div', { class: 'plugin-note' },
      h('h3', { text: `${plugin.name} 没有网页界面` }),
      h('p', { class: 'muted', text: plugin.web_path
        ? '这个应用的页面由小米客户端提供（没有挂在 nginx 的 /plugin/ 路由上），浏览器里打不开'
        : '这个应用只有小米客户端里有界面，桌面端拿不到它的页面'}),
      h('p', { class: 'muted' }, '请在小米智能存储客户端的「全部应用」里打开它；桌面端仍会显示它的图标和版本信息。'),
      plugin.client_url
        ? h('a', { class: 'mini2', href: plugin.client_url, target: '_blank', rel: 'noreferrer', text: '仍要在浏览器里试试（443 客户端入口）' })
        : null));
  }
  return h('section', { class: 'window', id: `view-${view}`, 'data-view': view },
    h('header', { class: 'window-bar' },
      h('span', { class: 'win-title', text: plugin.name }),
      h('span', { class: 'win-sub', text: plugin.web_ready
        ? `${plugin.key} · v${plugin.version}`
        : `${plugin.key} · 客户端内应用` }),
      h('span', { class: 'win-actions' },
        h('button', { class: 'win-btn', 'data-act': 'min', type: 'button', title: '最小化', 'aria-label': '最小化', text: '–' }),
        h('button', { class: 'win-btn', 'data-act': 'max', type: 'button', title: '最大化', 'aria-label': '最大化', text: '□' }),
        h('button', { class: 'win-btn is-close', 'data-act': 'close', type: 'button', title: '关闭', 'aria-label': '关闭', text: '×' }))),
    h('div', { class: 'window-body is-flat' }, ...body.filter(Boolean)),
    h('span', { class: 'win-resize', 'data-dir': 'e' }),
    h('span', { class: 'win-resize', 'data-dir': 's' }),
    h('span', { class: 'win-resize', 'data-dir': 'se' }));
}

function openPluginWindow(plugin) {
  const view = `plugin-${plugin.key}`;
  let node = windowNode(view);
  if (!node) {
    node = buildPluginWindow(plugin);
    $('windowLayer').append(node);
  }
  openWindow(view);
}

/* ------------------------------------------------------------------ 文件浏览（只读） */

function rawUrl(path) {
  return `api/files/raw?path=${encodeURIComponent(path)}`;
}

function downloadUrl(path) {
  return `api/files/download?path=${encodeURIComponent(path)}`;
}

function joinPath(base, name) {
  const trimmed = String(base || '').replace(/\/+$/, '');
  return `${trimmed}/${name}` || `/${name}`;
}

function fmtDate(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (value) => String(value).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function typeIcon(shape) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  const shapes = {
    dir: 'M3 6.4A1.6 1.6 0 0 1 4.6 4.8h4.3c.5 0 .9.2 1.2.6l1 1.5h8.3A1.6 1.6 0 0 1 21 8.5v9.1a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3 17.6z',
    image: 'M4 5h16v14H4zm3.6 3.4a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2zM6.2 17.4l4.1-4.1 2.9 2.9 2.8-2.8 4 4z',
    text: 'M6 3h8l4 4v14H6zm2.6 8.6h6.8v1.6H8.6zm0 3.4h6.8v1.6H8.6z',
    media: 'M4 5h16v14H4zm6 3.6v6.8l5.8-3.4z',
    archive: 'M4 5h16v4H4zm1.6 5.4h12.8V19H5.6z',
    file: 'M6 3h8l4 4v14H6z',
  };
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', shapes[shape] || shapes.file);
  path.setAttribute('fill', '#fff');
  svg.append(path);
  return svg;
}

function renderFileView() {
  renderFileCrumbs();
  renderFileList();
  renderFileAside();
  renderFileActions();
}

function renderFileCrumbs() {
  const host = $('fileCrumbs');
  const data = state.files.data;
  if (state.files.mode === 'trash') {
    host.replaceChildren(h('span', { class: 'crumb is-current', text: '回收站' }));
    $('fileUp').disabled = true;
    return;
  }
  if (!data || !data.path) {
    host.replaceChildren(h('span', { class: 'muted', text: '选择位置' }));
    $('fileUp').disabled = true;
    return;
  }
  $('fileUp').disabled = !data.parent;
  const segments = data.path.split('/').filter(Boolean);
  const crumbs = [h('button', { class: 'crumb', type: 'button', 'data-path': '/', text: '根' })];
  let accumulated = '';
  segments.forEach((segment, index) => {
    accumulated += `/${segment}`;
    crumbs.push(h('span', { class: 'crumb-sep', text: '›' }));
    crumbs.push(h('button', {
      class: `crumb${index === segments.length - 1 ? ' is-current' : ''}`,
      type: 'button',
      'data-path': accumulated,
      text: segment,
    }));
  });
  host.replaceChildren(...crumbs);
  host.scrollLeft = host.scrollWidth;
}

function shortcutRow(root) {
  return h('div', { class: 'shortcut', 'data-path': root.path },
    h('span', { class: 'ficon dir' }, typeIcon('dir')),
    h('span', null, h('b', { text: root.label }), h('br'), h('span', { text: root.path })));
}

function renderFileList() {
  const host = $('fileList');
  if (state.files.mode === 'trash') return renderTrashList();
  const data = state.files.data;
  if (state.files.loading) {
    host.replaceChildren(h('div', { class: 'overlay-loading', text: '读取目录…' }));
    return;
  }
  if (!data || !data.path) {
    const roots = ((data && data.roots) || []).filter((root) => root.exists);
    host.replaceChildren(
      h('div', { class: 'fhead' }, h('span', { text: '允许浏览的位置（只读浏览 / 可上传与整理）' })),
      ...(roots.length ? roots.map(shortcutRow) : [h('div', { class: 'empty', text: '没有可浏览的位置' })]));
    return;
  }
  const filter = state.files.filter.trim().toLowerCase();
  const all = data.entries || [];
  const entries = filter ? all.filter((entry) => entry.name.toLowerCase().includes(filter)) : all;
  const rows = entries.map((entry) => {
    const path = joinPath(data.path, entry.name);
    const selected = state.files.selected.has(path);
    return h('div', {
      class: `frow${selected ? ' is-selected' : ''}${state.files.active === path ? ' is-active' : ''}`,
      'data-name': entry.name,
    },
      h('input', {
        class: 'fcheck', type: 'checkbox', 'data-name': entry.name, 'aria-label': `选择 ${entry.name}`,
        ...(selected ? { checked: true } : {}),
      }),
      h('span', { class: `ficon ${entry.kind === 'dir' ? 'dir' : entry.type}` },
        typeIcon(entry.kind === 'dir' ? 'dir' : entry.type)),
      h('span', { class: `fname${entry.hidden ? ' is-hidden' : ''}`, text: entry.name }),
      h('span', { class: 'fmeta', text: entry.kind === 'dir' ? '目录' : fmtBytes(entry.size) }),
      h('span', { class: 'fmeta', text: entry.mtime ? fmtDate(entry.mtime) : '' }),
    );
  });
  host.replaceChildren(
    h('div', { class: 'fhead' },
      h('span', { text: filter ? `${entries.length} / ${all.length} 项` : `${all.length} 项` }),
      h('span', { text: data.truncated ? `目录过大，只显示前 ${data.limit} 项` : '可上传 / 重命名 / 删除（删除进回收站）' })),
    ...(rows.length ? rows : [h('div', { class: 'empty', text: filter ? '没有匹配的项目' : '空目录（可以把文件拖进来上传）' })]));
}

function renderTrashList() {
  const host = $('fileList');
  const data = state.files.trash;
  if (!data) {
    host.replaceChildren(h('div', { class: 'overlay-loading', text: '读取回收站…' }));
    return;
  }
  const items = data.items || [];
  const rows = items.map((item) => {
    const selected = state.files.selected.has(String(item.id));
    return h('div', { class: `frow${selected ? ' is-selected' : ''}`, 'data-id': item.id, 'data-trash': '1' },
      h('input', {
        class: 'fcheck', type: 'checkbox', 'data-id': item.id, 'aria-label': `选择 ${item.name}`,
        ...(selected ? { checked: true } : {}),
      }),
      h('span', { class: `ficon ${item.kind === 'dir' ? 'dir' : 'file'}` },
        typeIcon(item.kind === 'dir' ? 'dir' : 'file')),
      h('span', { class: 'fname', text: item.name }),
      h('span', { class: 'fmeta', text: item.kind === 'dir' ? '目录' : fmtBytes(item.size) }),
      h('span', { class: 'fmeta', text: `${fmtDate(item.at)} 删除` }),
    );
  });
  host.replaceChildren(
    h('div', { class: 'trash-head' },
      h('span', { class: 'muted', text: `回收站 · ${items.length} 项（还在盘上，可以恢复）` }),
      h('span', { class: 'muted', text: '位置：各存储根的 .console-trash/' })),
    ...(rows.length ? rows : [h('div', { class: 'empty', text: '回收站是空的' })]));
}

function renderFileActions() {
  const trashMode = state.files.mode === 'trash';
  const count = state.files.selected.size;
  const clip = state.files.clipboard;
  const clipText = clip.paths.length ? ` · 剪贴板：已${clip.mode === 'cut' ? '剪切' : '复制'} ${clip.paths.length} 项` : '';
  $('fileSelection').textContent = (trashMode
    ? (count ? `回收站已选 ${count} 项` : '回收站：选中后可恢复或彻底删除')
    : (count ? `已选 ${count} 项` : '未选择任何项目')) + clipText;
  for (const id of ['fileRename', 'fileDelete', 'fileClear', 'fileCopyTo', 'fileMoveTo', 'filePaste']) {
    $(id).classList.toggle('hidden', trashMode);
  }
  for (const id of ['fileRestore', 'filePurge', 'fileEmptyTrash']) $(id).classList.toggle('hidden', !trashMode);
  $('fileRename').disabled = count !== 1;
  $('fileDelete').disabled = count === 0;
  $('fileClear').disabled = count === 0;
  $('fileCopyTo').disabled = count === 0;
  $('fileMoveTo').disabled = count === 0;
  $('filePaste').disabled = !clip.paths.length || !state.files.path;
  $('fileRestore').disabled = count === 0;
  $('filePurge').disabled = count === 0;
  $('fileEmptyTrash').disabled = !((state.files.trash && state.files.trash.items) || []).length;
  $('fileTrash').classList.toggle('is-on', trashMode);
  $('fileUpload').disabled = trashMode;
  $('fileMkdir').disabled = trashMode;
  $('fileFilter').disabled = trashMode;
  $('fileUp').disabled = trashMode || !(state.files.data && state.files.data.parent);
  $('fileHome').disabled = false;
}

/* ------------------------------------------------- 右键菜单 */

// 菜单图标用内联 SVG：不依赖字体（之前用 ␡ 这类字符，某些字体会渲染成 "DEL" 字样）
const CTX_ICON_PATHS = {
  open: ['M6 3.5h6.5v6.5', 'M12.5 3.5 5.5 10.5', 'M11 9v3.5H3.5V5H7'],
  eye: ['M1.8 8S4.4 4 8 4s6.2 4 6.2 4-2.6 4-6.2 4S1.8 8 1.8 8z', 'M8 6.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z'],
  download: ['M8 2.5v7', 'M4.9 6.6 8 9.7l3.1-3.1', 'M3 13h10'],
  copy: ['M5.5 2.5h6a1.5 1.5 0 0 1 1.5 1.5v6', 'M4.5 5.5h6A1.5 1.5 0 0 1 12 7v5.5a1.5 1.5 0 0 1-1.5 1.5h-6A1.5 1.5 0 0 1 3 12.5V7a1.5 1.5 0 0 1 1.5-1.5z'],
  cut: ['M4.2 3 11 12.2', 'M11.8 3 5 12.2', 'M4.6 13.4a1.9 1.9 0 1 0 0-3.8 1.9 1.9 0 0 0 0 3.8z', 'M11.4 13.4a1.9 1.9 0 1 0 0-3.8 1.9 1.9 0 0 0 0 3.8z'],
  paste: ['M6 2.8h4v1.6H6z', 'M4.5 4.4H3.5A1.5 1.5 0 0 0 2 5.9v6.6A1.5 1.5 0 0 0 3.5 14h9a1.5 1.5 0 0 0 1.5-1.5V5.9a1.5 1.5 0 0 0-1.5-1.5h-1'],
  rename: ['M2.5 13.5h3.2L13.4 5.8a1.4 1.4 0 0 0 0-2L12.2 2.6a1.4 1.4 0 0 0-2 0L2.5 10.3v3.2z', 'M9.6 3.4l3 3'],
  copyto: ['M2 6h4l1.4 1.6H14v5.6H2z', 'M7.4 10.4h5', 'M10.6 8.6l1.8 1.8-1.8 1.8'],
  moveto: ['M2.5 8h10', 'M9 4.5 12.5 8 9 11.5'],
  trash: ['M3 4.6h10', 'M6.2 4.6V3h3.6v1.6', 'M4.6 4.6 5.3 13h5.4l.7-8.4'],
  link: ['M6.6 9.4a2.6 2.6 0 0 0 3.7 0l2-2a2.6 2.6 0 1 0-3.7-3.7l-.7.7', 'M9.4 6.6a2.6 2.6 0 0 0-3.7 0l-2 2a2.6 2.6 0 1 0 3.7 3.7l.7-.7'],
  check: ['M2.5 8.4 6 11.9l7.5-7.5'],
  newfolder: ['M2 5.4h4.2L7.6 7H14v6.6H2z', 'M8 9.6v2.4', 'M6.8 10.8h2.4'],
  upload: ['M8 13.5V6.4', 'M4.9 9.5 8 6.4l3.1 3.1', 'M3 3.5h10'],
  refresh: ['M13 8a5 5 0 1 1-1.6-3.7', 'M13 2.6V5h-2.4'],
  selectall: ['M2.5 8.4 5.4 11.3 13.5 3.2'],
};

function ctxIcon(name) {
  const paths = CTX_ICON_PATHS[name];
  if (!paths) return h('span', { class: 'ctx-icon' });
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'ctx-svg');
  for (const data of paths) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', data);
    svg.append(path);
  }
  return h('span', { class: 'ctx-icon' }, svg);
}

function closeContextMenu() {
  $('ctxMenu').classList.add('hidden');
  $('ctxMenu').replaceChildren();
}

function openContextMenu(x, y, items) {
  const menu = $('ctxMenu');
  const nodes = [];
  for (const item of items) {
    if (!item) continue;
    if (item.sep) { nodes.push(h('div', { class: 'ctx-sep' })); continue; }
    const button = h('button', { class: 'ctx-item' + (item.danger ? ' is-danger' : ''), type: 'button', role: 'menuitem' },
      ctxIcon(item.icon),
      h('span', { text: item.label }),
      item.key ? h('span', { class: 'ctx-key', text: item.key }) : null);
    if (item.disabled) button.disabled = true;
    else button.addEventListener('click', () => { closeContextMenu(); item.run(); });
    nodes.push(button);
  }
  menu.replaceChildren(...nodes);
  menu.classList.remove('hidden');
  menu.style.setProperty('left', '0px');
  menu.style.setProperty('top', '0px');
  const box = menu.getBoundingClientRect();
  menu.style.setProperty('left', `${Math.max(4, Math.min(x, window.innerWidth - box.width - 8))}px`);
  menu.style.setProperty('top', `${Math.max(4, Math.min(y, window.innerHeight - box.height - 8))}px`);
}

function rowPath(row) {
  if (!row || state.files.mode === 'trash') return '';
  const data = state.files.data;
  if (!data || !data.path || !row.dataset.name) return '';
  return joinPath(data.path, row.dataset.name);
}

function rowEntry(row) {
  const data = state.files.data;
  if (!data || !row || !row.dataset.name) return null;
  return (data.entries || []).find((item) => item.name === row.dataset.name) || null;
}

function selectRow(row, additive) {
  const path = rowPath(row);
  if (!path) return;
  if (!additive) state.files.selected = new Set([path]);
  else if (state.files.selected.has(path)) state.files.selected.delete(path);
  else state.files.selected.add(path);
  renderFileList();
  renderFileActions();
}

function selectAll() {
  if (state.files.mode === 'trash') {
    const items = (state.files.trash && state.files.trash.items) || [];
    state.files.selected = new Set(items.map((item) => String(item.id)));
    renderTrashList();
  } else {
    const data = state.files.data;
    if (!data || !data.path) return;
    state.files.selected = new Set((data.entries || []).map((entry) => joinPath(data.path, entry.name)));
    renderFileList();
  }
  renderFileActions();
}

function contextMenuForRow(event, row) {
  const trashMode = state.files.mode === 'trash';
  const path = trashMode ? '' : rowPath(row);
  const entry = rowEntry(row);
  const items = [];
  if (trashMode) {
    const ids = selectedPaths();
    items.push(
      { label: '恢复到原位置', icon: 'refresh', run: doRestore, disabled: !ids.length },
      { sep: true },
      { label: '彻底删除…', icon: 'trash', key: 'Del', danger: true, run: () => doPurge(ids, `彻底删除 ${ids.length} 项`) },
      { sep: true },
      { label: '全选', icon: 'check', key: 'Ctrl+A', run: selectAll },
    );
    openContextMenu(event.clientX, event.clientY, items);
    return;
  }
  const isDir = entry && entry.kind === 'dir';
  const count = state.files.selected.size;
  items.push(
    isDir
      ? { label: '打开', icon: 'open', run: () => loadFiles(path).catch(markFail) }
      : { label: '预览', icon: 'eye', run: () => { if (entry) selectFile(entry, path).catch(markFail); } },
    !isDir && entry ? { label: '下载', icon: 'download', run: () => downloadPath(path) } : null,
    { sep: true },
    { label: '复制', icon: 'copy', key: 'Ctrl+C', run: () => clipboardSet('copy') },
    { label: '剪切', icon: 'cut', key: 'Ctrl+X', run: () => clipboardSet('cut') },
    { label: '粘贴到此处', icon: 'paste', key: 'Ctrl+V', disabled: !state.files.clipboard.paths.length, run: () => pasteInto(state.files.path) },
    { sep: true },
    { label: '重命名…', icon: 'rename', key: 'F2', disabled: count !== 1, run: doRename },
    { label: '复制到…', icon: 'copyto', run: () => copyMoveTo('copy') },
    { label: '移动到…', icon: 'moveto', run: () => copyMoveTo('move') },
    { label: '删除（进回收站）', icon: 'trash', key: 'Del', danger: true, run: doDelete },
    { sep: true },
    { label: '复制完整路径', icon: 'link', run: async () => {
      const ok = await copyText(selectedPaths().join('\n'));
      toast(ok ? '路径已复制' : '复制失败，请长按选择', ok ? '' : 'error');
    } },
    { label: '全选', icon: 'check', key: 'Ctrl+A', run: selectAll },
  );
  openContextMenu(event.clientX, event.clientY, items);
}

function contextMenuForEmpty(event) {
  const trashMode = state.files.mode === 'trash';
  openContextMenu(event.clientX, event.clientY, [
    { label: '新建文件夹…', icon: 'newfolder', disabled: trashMode, run: doMkdir },
    { label: '上传文件…', icon: 'upload', disabled: trashMode, run: () => $('fileInput').click() },
    { label: '粘贴到当前目录', icon: 'paste', key: 'Ctrl+V', disabled: trashMode || !state.files.clipboard.paths.length, run: () => pasteInto(state.files.path) },
    { sep: true },
    { label: '刷新', icon: 'refresh', run: () => (trashMode ? loadTrash() : loadFiles(state.files.path || '')).catch(markFail) },
    { label: '全选', icon: 'check', key: 'Ctrl+A', run: selectAll },
  ]);
}

/* ------------------------------------------------- 剪贴板与复制/移动 */

function clipboardSet(mode) {
  const paths = selectedPaths();
  if (!paths.length) { toast('先选中要处理的项目', 'error'); return; }
  state.files.clipboard = { mode, paths };
  renderFileActions();
  toast(`已${mode === 'cut' ? '剪切' : '复制'} ${paths.length} 项：到目标目录右键「粘贴到当前目录」`);
}

async function pasteInto(directory) {
  const clip = state.files.clipboard;
  if (!clip.paths.length) { toast('剪贴板是空的', 'error'); return; }
  if (!directory) { toast('请先进入一个目录', 'error'); return; }
  if (clip.paths.includes(directory)) { toast('不能粘贴到源目录本身', 'error'); return; }
  await runFileOp(clip.mode === 'cut' ? 'move' : 'copy', clip.paths, directory);
}

async function runFileOp(mode, paths, target) {
  if (!paths.length) { toast('先选中要处理的项目', 'error'); return; }
  try {
    const result = await writeApi('files/op', { mode, paths, target, conflict: 'rename' });
    watchTask(result.task);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function copyMoveTo(mode) {
  const paths = selectedPaths();
  if (!paths.length) { toast('先选中要处理的项目', 'error'); return; }
  const label = mode === 'move' ? '移动到' : '复制到';
  pickDirectory({
    title: `${label}哪里？（已选 ${paths.length} 项）`,
    onPick: (target) => runFileOp(mode, paths, target),
  });
}

function watchTask(task) {
  state.files.task = task;
  renderTaskBar();
  clearInterval(state.files.taskTimer);
  state.files.taskTimer = setInterval(pollTask, 1000);
}

async function pollTask() {
  const task = state.files.task;
  if (!task) { clearInterval(state.files.taskTimer); state.files.taskTimer = 0; return; }
  try {
    const info = await api(`files/op/${task.id}`);
    state.files.task = info;
    renderTaskBar();
    if (info.state === 'running') return;
    clearInterval(state.files.taskTimer);
    state.files.taskTimer = 0;
    state.files.clipboard = { mode: '', paths: [] };
    toast(info.message || '完成', info.state === 'failed' ? 'error' : '');
    await loadFiles(state.files.path || '');
    setTimeout(() => {
      if (state.files.task && state.files.task.state !== 'running') {
        state.files.task = null;
        renderTaskBar();
      }
    }, 5000);
  } catch (error) {
    /* 网关抖动之类的瞬时错误忽略，下一次轮询继续 */
  }
}

function renderTaskBar() {
  const task = state.files.task;
  const bar = $('taskBar');
  if (!task) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  const percent = task.state === 'running' ? (task.percent || 0) : 100;
  $('taskFill').style.setProperty('width', `${percent}%`);
  const action = task.mode === 'move' ? '移动' : '复制';
  const parts = [];
  if (task.state === 'running') {
    parts.push(`${action}中 ${task.percent}%`);
    parts.push(`${fmtBytes(task.done)} / ${fmtBytes(task.total)}`);
    if (task.speed) parts.push(`${fmtBytes(task.speed)}/s`);
    if (task.eta) parts.push(`剩约 ${task.eta}`);
    if (task.current) parts.push(task.current.split('/').pop());
  } else {
    parts.push(task.message || action);
    if (task.error_count) parts.push(`错误 ${task.error_count} 项`);
  }
  $('taskText').textContent = parts.join(' · ');
  $('taskCancel').classList.toggle('hidden', task.state !== 'running');
}

/* ------------------------------------------------- 目标目录选择器 */

function pickDirectory(options) {
  state.files.pick = { path: '', data: null, onPick: options.onPick, title: options.title };
  $('pickTitle').textContent = options.title || '选择目标文件夹';
  $('pickError').classList.add('hidden');
  $('pickDialog').classList.remove('hidden');
  loadPick('');
}

async function loadPick(path) {
  const pick = state.files.pick;
  pick.path = path;
  try {
    pick.data = await api(`files?path=${encodeURIComponent(path)}`);
  } catch (error) {
    $('pickError').textContent = error.message;
    $('pickError').classList.remove('hidden');
    return;
  }
  renderPick();
}

function renderPick() {
  const pick = state.files.pick;
  const data = pick.data;
  const roots = data && data.path ? [] : ((data && data.roots) || []).filter((root) => root.exists);
  const crumbs = $('pickCrumbs');
  const nodes = [h('button', {
    class: 'crumb' + (data && data.path ? '' : ' is-current'), type: 'button', 'data-path': '', text: '根目录',
  })];
  if (data && data.path) {
    const segments = data.path.split('/').filter(Boolean);
    let accumulated = '';
    segments.forEach((segment, index) => {
      accumulated += `/${segment}`;
      nodes.push(h('span', { class: 'crumb-sep', text: '›' }));
      nodes.push(h('button', {
        class: `crumb${index === segments.length - 1 ? ' is-current' : ''}`,
        type: 'button', 'data-path': accumulated, text: segment,
      }));
    });
  }
  crumbs.replaceChildren(...nodes);
  $('pickUp').disabled = !(data && data.parent);

  const rows = [];
  if (roots.length) {
    for (const root of roots) rows.push(pickRow(root.label, root.path, true));
  } else if (data && data.path) {
    for (const entry of (data.entries || [])) {
      if (entry.kind !== 'dir') continue;
      rows.push(pickRow(entry.name, joinPath(data.path, entry.name), true));
    }
  }
  $('pickList').replaceChildren(...(rows.length ? rows : [h('div', { class: 'pick-empty', text: '这一层没有子文件夹（可以直接选当前文件夹）' })]));
}

function pickRow(label, path, canEnter) {
  const button = h('button', { class: 'pick-row', type: 'button' },
    ctxIcon('open'),
    h('span', { class: 'pick-name', text: label }));
  button.addEventListener('click', () => loadPick(canEnter ? path : ''));
  return button;
}

function downloadPath(path) {
  const link = document.createElement('a');
  link.href = downloadUrl(path);
  link.rel = 'noreferrer';
  link.download = '';
  document.body.append(link);
  link.click();
  link.remove();
}

function closePick() {
  $('pickDialog').classList.add('hidden');
  state.files.pick = { path: '', data: null, onPick: null };
}

/* ------------------------------------------------- 行内/空处右键绑定 */

function renderFileAside() {
  const host = $('fileAside');
  const preview = state.files.preview;
  if (!preview) {
    host.replaceChildren(h('div', { class: 'empty', text: '点一个文件看预览；点目录进去。删除会先移到回收站，可以恢复。' }));
    return;
  }
  const rows = [
    ['类型', preview.kind === 'dir' ? '目录' : (preview.type || 'file')],
    ['大小', preview.kind === 'dir' ? '—' : fmtBytes(preview.size)],
    ['修改时间', preview.mtime ? new Date(preview.mtime * 1000).toLocaleString('zh-CN') : '—'],
  ];
  const body = [h('h3', { text: preview.name })];
  // 注意：用 type（image/text/media…）判断渲染方式，kind 只区分目录/文件
  if (preview.type === 'image') {
    body.push(h('img', { class: 'preview-image', src: rawUrl(preview.path), alt: preview.name }));
  } else if (preview.type === 'text') {
    body.push(preview.loading
      ? h('div', { class: 'overlay-loading', text: '读取中…' })
      : h('pre', { class: 'preview-text', text: preview.text || '' }));
    if (preview.truncated) body.push(h('p', { class: 'muted', text: '文件较大，只显示开头部分。' }));
  } else if (preview.kind === 'dir') {
    body.push(h('p', { class: 'muted', text: '目录 — 在左侧点进去。' }));
  } else {
    body.push(h('p', { class: 'muted', text: '这个类型不支持在线预览，可下载后打开。' }));
  }
  body.push(h('dl', { class: 'kv-list' },
    ...rows.map(([label, value]) => h('div', { class: 'kv' }, h('dt', { text: label }), h('dd', { text: String(value) }))),
    h('div', { class: 'kv' }, h('dt', { text: '路径' }), h('dd', { text: preview.path }))));
  body.push(h('div', { class: 'preview-actions' },
    preview.kind === 'dir' ? null : h('a', { class: 'mini2', href: downloadUrl(preview.path), text: '下载' }),
    h('button', { class: 'mini2', 'data-copy': preview.path, type: 'button', text: '复制路径' })));
  host.replaceChildren(...body.filter(Boolean));
}

async function loadFiles(path) {
  state.files.loading = true;
  renderFileList();
  try {
    const data = await api(path ? `files?path=${encodeURIComponent(path)}` : 'files');
    state.files.data = data;
    state.files.path = data.path || '';
    state.files.active = null;
    state.files.preview = null;
    state.files.filter = '';
    state.files.selected = new Set();
    state.files.mode = 'browse';
    $('fileFilter').value = '';
  } finally {
    state.files.loading = false;
  }
  renderFileView();
}

async function selectFile(entry, path) {
  state.files.active = path;
  state.files.preview = {
    name: entry.name,
    path,
    kind: entry.kind,
    type: entry.type,
    size: entry.size,
    mtime: entry.mtime,
  };
  renderFileList();
  renderFileAside();
  if (entry.kind !== 'file' || entry.type !== 'text') return;
  state.files.preview.loading = true;
  renderFileAside();
  try {
    const payload = await api(`files/text?path=${encodeURIComponent(path)}`);
    if (state.files.preview && state.files.preview.path === path) {
      state.files.preview.text = payload.text;
      state.files.preview.truncated = payload.truncated;
      state.files.preview.loading = false;
      renderFileAside();
    }
  } catch (error) {
    if (state.files.preview && state.files.preview.path === path) {
      state.files.preview.text = `读取失败：${error.message}`;
      state.files.preview.loading = false;
      renderFileAside();
    }
  }
}

/* ------------------------------------------------------------------ 文件写操作 */

function toast(message, kind) {
  const node = $('toast');
  node.textContent = message;
  node.classList.toggle('is-error', kind === 'error');
  node.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add('hidden'), 2800);
}

async function writeApi(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf || '' },
    body: JSON.stringify(body),
  });
}

let dialogHandler = null;
let dialogCancel = null;

function openDialog(options) {
  $('dialogTitle').textContent = options.title || '';
  const text = $('dialogText');
  text.textContent = options.text || '';
  text.classList.toggle('hidden', !options.text);
  const input = $('dialogInput');
  input.classList.toggle('hidden', !options.input);
  if (options.input) {
    input.value = options.input.value || '';
    input.placeholder = options.input.placeholder || '';
  }
  const wrap = $('dialogCheckWrap');
  wrap.classList.toggle('hidden', !options.check);
  if (options.check) {
    $('dialogCheck').checked = false;
    $('dialogCheckText').textContent = options.check;
  }
  $('dialogError').classList.add('hidden');
  const ok = $('dialogOk');
  ok.textContent = options.okText || '确定';
  ok.classList.toggle('is-danger', options.danger !== false);
  dialogHandler = options.onOk || null;
  dialogCancel = options.onCancel || null;
  $('fileDialog').classList.remove('hidden');
  if (options.input) setTimeout(() => { input.focus(); input.select(); }, 30);
  else setTimeout(() => ok.focus(), 30);
}

function closeDialog(runCancel) {
  $('fileDialog').classList.add('hidden');
  const cancel = dialogCancel;
  dialogHandler = null;
  dialogCancel = null;
  if (runCancel && cancel) cancel();
}

async function submitDialog() {
  if (!dialogHandler) return;
  const error = $('dialogError');
  error.classList.add('hidden');
  $('dialogOk').disabled = true;
  try {
    await dialogHandler($('dialogInput').value.trim(), $('dialogCheck').checked);
    closeDialog(false);
  } catch (failure) {
    error.textContent = failure.message || String(failure);
    error.classList.remove('hidden');
  } finally {
    $('dialogOk').disabled = false;
  }
}

function askOverwrite(name) {
  return new Promise((resolve) => {
    let answered = false;
    openDialog({
      title: '同名文件已存在',
      text: `${name} 已经存在，要覆盖它吗？（覆盖后原文件无法恢复）`,
      okText: '覆盖',
      onOk: async () => { answered = true; resolve(true); },
      onCancel: () => { if (!answered) resolve(false); },
    });
  });
}

function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length || state.files.mode !== 'browse') return;
  const directory = state.files.path;
  if (!directory) {
    toast('请先进入一个目录再上传', 'error');
    return;
  }
  const bar = $('uploadBar');
  const fill = $('uploadFill');
  const label = $('uploadText');
  bar.classList.remove('hidden');
  let index = 0;
  let done = 0;

  const finish = () => {
    bar.classList.add('hidden');
    fill.style.setProperty('width', '0%');
    if (done) toast(`已上传 ${done} 个文件`);
    loadFiles(directory).catch(markFail);
  };

  const send = (file, overwrite) => {
    const request = new XMLHttpRequest();
    const query = `path=${encodeURIComponent(directory)}&name=${encodeURIComponent(file.name)}`
      + (overwrite ? '&overwrite=1' : '');
    request.open('POST', `api/files/upload?${query}`, true);
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.setRequestHeader('X-CSRF-Token', state.csrf || '');
    request.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      fill.style.setProperty('width', `${percent}%`);
      label.textContent = `${file.name} ${percent}%（${index}/${files.length}）`;
    };
    request.onload = async () => {
      if (request.status === 409) {
        const overwriteNow = await askOverwrite(file.name);
        if (overwriteNow) { send(file, true); return; }
        next();
        return;
      }
      if (request.status >= 200 && request.status < 300) {
        done += 1;
        next();
        return;
      }
      let message = `上传 ${file.name} 失败`;
      try {
        message = JSON.parse(request.responseText).error || message;
      } catch (error) { /* 保持默认文案 */ }
      bar.classList.add('hidden');
      toast(message, 'error');
      loadFiles(directory).catch(markFail);
    };
    request.onerror = () => {
      bar.classList.add('hidden');
      toast(`上传 ${file.name} 失败（连接中断）`, 'error');
      loadFiles(directory).catch(markFail);
    };
    request.send(file);
  };

  const next = () => {
    if (index >= files.length) { finish(); return; }
    const file = files[index];
    index += 1;
    send(file, false);
  };
  next();
}

async function loadTrash() {
  state.files.mode = 'trash';
  state.files.selected = new Set();
  renderFileView();
  state.files.trash = await api('files/trash');
  renderFileView();
  // 回收站列表也要能预览属性：这里不预览，保持简单
  state.files.preview = null;
  renderFileAside();
}

function selectedPaths() {
  return [...state.files.selected];
}

function doMkdir() {
  const directory = state.files.path;
  openDialog({
    title: '新建文件夹',
    text: directory,
    input: { placeholder: '文件夹名' },
    okText: '创建',
    onOk: async (value) => {
      if (!value) throw new Error('请输入文件夹名');
      await writeApi('files/mkdir', { path: directory, name: value });
      toast(`已创建 ${value}`);
      await loadFiles(directory);
    },
  });
}

function doRename() {
  const paths = selectedPaths();
  if (paths.length !== 1) return;
  const current = paths[0];
  const name = current.split('/').pop();
  openDialog({
    title: '重命名',
    text: current,
    input: { value: name },
    okText: '重命名',
    onOk: async (value) => {
      if (!value) throw new Error('请输入新名字');
      if (value === name) return;
      await writeApi('files/rename', { path: current, name: value });
      toast(`已重命名为 ${value}`);
      await loadFiles(state.files.path);
    },
  });
}

function doDelete() {
  const paths = selectedPaths();
  if (!paths.length) return;
  openDialog({
    title: `删除 ${paths.length} 项`,
    text: paths.slice(0, 6).join('\n') + (paths.length > 6 ? `\n…以及另外 ${paths.length - 6} 项` : ''),
    check: '我确认删除（会移到 .console-trash 回收站，可恢复）',
    okText: '删除',
    onOk: async (value, checked) => {
      if (!checked) throw new Error('请先勾选确认');
      const result = await writeApi('files/delete', { paths });
      const failed = result.failed || [];
      state.files.selected = new Set();
      await loadFiles(state.files.path);
      toast(failed.length ? `已删除 ${result.moved.length} 项，${failed.length} 项失败：${failed[0].error}` : `已移到回收站：${result.moved.length} 项`,
        failed.length ? 'error' : undefined);
    },
  });
}

function doRestore() {
  const ids = selectedPaths();
  if (!ids.length) return;
  openDialog({
    title: `恢复 ${ids.length} 项`,
    text: '恢复到原来的位置；如果原位置已有同名文件会失败。',
    okText: '恢复',
    danger: false,
    onOk: async () => {
      const result = await writeApi('files/restore', { ids });
      state.files.selected = new Set();
      await loadTrash();
      toast(`已恢复 ${result.restored} 项`);
    },
  });
}

function doPurge(ids, title) {
  openDialog({
    title,
    text: '彻底删除后无法恢复，文件会从磁盘上移除。',
    check: '我确认彻底删除，无法恢复',
    okText: '彻底删除',
    onOk: async (value, checked) => {
      if (!checked) throw new Error('请先勾选确认');
      const result = await writeApi('files/trash/empty', ids ? { ids } : {});
      state.files.selected = new Set();
      await loadTrash();
      toast(`已彻底删除 ${result.removed.length} 项，释放 ${fmtBytes(result.freed)}`);
    },
  });
}

function setupFileBrowser() {
  $('fileList').addEventListener('click', (event) => {
    const shortcut = closestWithClass(event.target, 'shortcut');
    if (shortcut) {
      loadFiles(shortcut.dataset.path).catch(markFail);
      return;
    }
    const row = closestWithClass(event.target, 'frow');
    if (!row) return;

    if (state.files.mode === 'trash') {
      const id = String(row.dataset.id);
      if (state.files.selected.has(id)) state.files.selected.delete(id);
      else state.files.selected.add(id);
      renderTrashList();
      renderFileActions();
      return;
    }

    const data = state.files.data;
    if (!data || !data.path) return;
    const entry = (data.entries || []).find((item) => item.name === row.dataset.name);
    if (!entry) return;
    const path = joinPath(data.path, entry.name);

    if (event.target.classList && event.target.classList.contains('fcheck')) {
      if (state.files.selected.has(path)) state.files.selected.delete(path);
      else state.files.selected.add(path);
      row.classList.toggle('is-selected', state.files.selected.has(path));
      renderFileActions();
      return;
    }
    if (entry.kind === 'dir') loadFiles(path).catch(markFail);
    else selectFile(entry, path).catch(markFail);
  });

  $('fileCrumbs').addEventListener('click', (event) => {
    const crumb = closestWithClass(event.target, 'crumb');
    if (!crumb || !crumb.dataset.path) return;
    loadFiles(crumb.dataset.path === '/' ? '' : crumb.dataset.path).catch(markFail);
  });

  // 右键：行上出行的菜单，空白处出目录级菜单
  $('fileList').addEventListener('contextmenu', (event) => {
    event.preventDefault();
    const row = closestWithClass(event.target, 'frow');
    if (row) {
      const path = rowPath(row);
      const additive = event.ctrlKey || event.metaKey;
      if (path && !state.files.selected.has(path)) selectRow(row, false);
      else if (path && additive) selectRow(row, true);
      contextMenuForRow(event, row);
      return;
    }
    contextMenuForEmpty(event);
  });
  // 点其他地方关掉菜单
  document.addEventListener('click', (event) => {
    if (!event.target.closest || !event.target.closest('#ctxMenu')) closeContextMenu();
  });
  document.addEventListener('scroll', closeContextMenu, true);
  window.addEventListener('resize', closeContextMenu);
  window.addEventListener('blur', closeContextMenu);

  // 目标目录选择器
  $('pickCrumbs').addEventListener('click', (event) => {
    const crumb = closestWithClass(event.target, 'crumb');
    if (!crumb) return;
    loadPick(crumb.dataset.path === '/' ? '' : crumb.dataset.path);
  });
  $('pickUp').addEventListener('click', () => {
    const data = state.files.pick.data;
    if (data && data.parent) loadPick(data.parent);
  });
  $('pickCancel').addEventListener('click', closePick);
  $('pickDialog').addEventListener('click', (event) => {
    if (event.target === $('pickDialog')) closePick();
  });
  $('pickOk').addEventListener('click', () => {
    const pick = state.files.pick;
    const target = pick.data && pick.data.path;
    if (!target) { $('pickError').textContent = '请先进入要作为目标的文件夹'; $('pickError').classList.remove('hidden'); return; }
    const callback = pick.onPick;
    closePick();
    if (callback) callback(target);
  });

  // 任务进度条
  $('taskCancel').addEventListener('click', async () => {
    const task = state.files.task;
    if (!task || task.state !== 'running') return;
    try {
      const info = await writeApi('files/op/cancel', { id: task.id });
      state.files.task = info.task || task;
      renderTaskBar();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  // 工具栏的复制到 / 移动到 / 粘贴
  $('fileCopyTo').addEventListener('click', () => copyMoveTo('copy'));
  $('fileMoveTo').addEventListener('click', () => copyMoveTo('move'));
  $('filePaste').addEventListener('click', () => pasteInto(state.files.path));

  // 快捷键（只在文件窗口打开、且没有弹窗时生效）
  document.addEventListener('keydown', (event) => {
    if (!state.open.has('files')) return;
    // 正在输入框里打字时不抢快捷键；但复选框/按钮获得焦点时快捷键仍要能用
    const target = event.target || {};
    const tag = target.tagName || '';
    const type = target.type || '';
    const typing = tag === 'TEXTAREA' || (tag === 'INPUT' && !['checkbox', 'radio', 'button', 'file', 'submit'].includes(type));
    if (typing) return;
    if (!$('fileDialog').classList.contains('hidden') || !$('pickDialog').classList.contains('hidden')) return;
    const key = event.key.toLowerCase();
    const modify = event.ctrlKey || event.metaKey;
    if (event.key === 'F2') { event.preventDefault(); doRename(); return; }
    if (event.key === 'Delete') {
      event.preventDefault();
      if (state.files.mode === 'trash') {
        const ids = selectedPaths();
        if (ids.length) doPurge(ids, `彻底删除 ${ids.length} 项`);
      } else {
        doDelete();
      }
      return;
    }
    if (event.key === 'Escape') { closeContextMenu(); return; }
    if (!modify) return;
    if (key === 'c') { event.preventDefault(); clipboardSet('copy'); }
    else if (key === 'x') { event.preventDefault(); clipboardSet('cut'); }
    else if (key === 'v') { event.preventDefault(); pasteInto(state.files.path); }
    else if (key === 'a') { event.preventDefault(); selectAll(); }
  });

  $('fileUp').addEventListener('click', () => {
    if (state.files.mode === 'trash') { loadFiles(state.files.path || '').catch(markFail); return; }
    const data = state.files.data;
    if (data && data.parent) loadFiles(data.parent).catch(markFail);
  });
  $('fileHome').addEventListener('click', () => loadFiles('').catch(markFail));
  $('fileRefresh').addEventListener('click', () => {
    if (state.files.mode === 'trash') loadTrash().catch(markFail);
    else loadFiles(state.files.path || '').catch(markFail);
  });
  $('fileFilter').addEventListener('input', (event) => {
    state.files.filter = event.target.value;
    renderFileList();
  });

  $('fileUpload').addEventListener('click', () => $('fileInput').click());
  $('fileInput').addEventListener('change', (event) => {
    uploadFiles(event.target.files);
    event.target.value = '';
  });
  $('fileMkdir').addEventListener('click', doMkdir);
  $('fileRename').addEventListener('click', doRename);
  $('fileDelete').addEventListener('click', doDelete);
  $('fileClear').addEventListener('click', () => {
    state.files.selected = new Set();
    renderFileView();
  });
  $('fileTrash').addEventListener('click', () => {
    if (state.files.mode === 'trash') loadFiles(state.files.path || '').catch(markFail);
    else loadTrash().catch((error) => toast(error.message, 'error'));
  });
  $('fileRestore').addEventListener('click', doRestore);
  $('filePurge').addEventListener('click', () => {
    const ids = selectedPaths();
    if (ids.length) doPurge(ids, `彻底删除 ${ids.length} 项`);
  });
  $('fileEmptyTrash').addEventListener('click', () => doPurge(null, '清空回收站'));

  $('dialogOk').addEventListener('click', submitDialog);
  $('dialogCancel').addEventListener('click', () => closeDialog(true));
  $('dialogInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); submitDialog(); }
    if (event.key === 'Escape') { event.preventDefault(); closeDialog(true); }
  });
  $('fileDialog').addEventListener('click', (event) => {
    if (event.target === $('fileDialog')) closeDialog(true);
  });

  const dropZone = $('fileList');
  dropZone.addEventListener('dragover', (event) => {
    if (state.files.mode !== 'browse') return;
    event.preventDefault();
    dropZone.classList.add('is-drop');
  });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('is-drop'));
  dropZone.addEventListener('drop', (event) => {
    dropZone.classList.remove('is-drop');
    if (state.files.mode !== 'browse') return;
    event.preventDefault();
    const files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) uploadFiles(files);
  });

  $('fileAside').addEventListener('click', async (event) => {
    const button = closestWithClass(event.target, 'mini2');
    if (!button || !button.dataset.copy) return;
    const ok = await copyText(button.dataset.copy);
    button.textContent = ok ? '已复制' : '复制失败';
    setTimeout(() => { button.textContent = '复制路径'; }, 1400);
  });
}

/* ------------------------------------------------------------------ 登录与启动 */

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (inner) {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

function setupNarrowHint() {
  const address = location.host || '';
  $('narrowUrl').textContent = address ? `http://${address}/` : '—';
}

function showLogin(message) {
  $('loginOverlay').classList.remove('hidden');
  if (message) {
    $('loginError').textContent = message;
    $('loginError').classList.remove('hidden');
  }
}

function hideLogin() {
  $('loginOverlay').classList.add('hidden');
  $('loginError').classList.add('hidden');
}

async function submitLogin(event) {
  event.preventDefault();
  const token = $('loginToken').value.trim();
  if (!token) return;
  $('loginSubmit').disabled = true;
  try {
    const response = await fetch(`${API}/login`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      showLogin(payload.error || '登录失败');
      return;
    }
    $('loginToken').value = '';
    hideLogin();
    await bootstrap();
  } catch (error) {
    showLogin(`登录失败：${error.message}`);
  } finally {
    $('loginSubmit').disabled = false;
  }
}

async function logout() {
  await fetch(`${API}/logout`, { method: 'POST', credentials: 'same-origin' }).catch(() => {});
  state.session = null;
  showLogin('已退出登录');
}

async function bootstrap() {
  await loadSession();
  if (state.session.needsLogin && !state.session.authed) return;
  try {
    await Promise.all([loadOverview(), loadHistory()]);
    await Promise.all([loadStorage(true), loadDocker(true), loadServices(true)]);
    await loadPlugins();
    markOk();
  } catch (error) {
    markFail(error);
  }
  if (state.open.size) for (const view of state.open) renderView(view);
}

function updateClock() {
  const node = $('clock');
  if (!node) return;
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  node.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function init() {
  setupNarrowHint();
  setupFileBrowser();

  $('icons').addEventListener('click', (event) => {
    const icon = closestWithClass(event.target, 'desk-icon');
    if (!icon || !icon.dataset.view) return;        // 「入口设置」是链接，走默认行为
    const view = icon.dataset.view;
    if (view.startsWith('plugin-')) {
      const plugin = (state.plugins || []).find((item) => `plugin-${item.key}` === view);
      if (plugin) openPluginWindow(plugin);
      return;
    }
    openWindow(view);
  });

  $('windowTabs').addEventListener('click', (event) => {
    const tab = closestWithClass(event.target, 'win-tab');
    if (!tab) return;
    const node = windowNode(tab.dataset.view);
    if (node) node.classList.remove('is-min');
    focusWindow(tab.dataset.view);
    renderWindowTabs();
  });

  $('windowLayer').addEventListener('click', (event) => {
    const reload = closestWithClass(event.target, 'mini2');
    if (reload && reload.dataset.act === 'reload') {
      const frame = closestWithClass(reload, 'window')?.querySelector('iframe');
      if (frame) {
        try {
          frame.contentWindow.location.reload();
        } catch (error) {
          frame.src = frame.src;
        }
      }
      return;
    }
    const button = closestWithClass(event.target, 'win-btn');
    if (button) {
      const node = closestWithClass(button, 'window');
      if (!node) return;
      const view = node.dataset.view;
      const action = button.dataset.act;
      if (action === 'close') closeWindow(view);
      else if (action === 'min') minimizeWindow(view);
      else if (action === 'max') { node.classList.toggle('is-max'); focusWindow(view); }
      return;
    }
    const node = closestWithClass(event.target, 'window');
    if (node) focusWindow(node.dataset.view);
  });

  $('windowLayer').addEventListener('pointerdown', (event) => {
    const handle = closestWithClass(event.target, 'win-resize');
    if (handle) {
      const node = closestWithClass(handle, 'window');
      if (node) {
        focusWindow(node.dataset.view);
        beginResize(event, node, handle.dataset.dir || 'se');
      }
      return;
    }
    if (closestWithClass(event.target, 'win-btn')) return;
    const bar = closestWithClass(event.target, 'window-bar');
    if (!bar) return;
    const node = closestWithClass(bar, 'window');
    if (!node) return;
    focusWindow(node.dataset.view);
    beginDrag(event, node);
  });

  $('refreshBtn').addEventListener('click', () => {
    state.lastRun = {};
    tick();
  });
  $('logoutBtn').addEventListener('click', logout);
  $('loginForm').addEventListener('submit', submitLogin);
  $('chartLegend').addEventListener('click', (event) => {
    const chip = closestWithClass(event.target, 'chip');
    if (!chip) return;
    const key = chip.dataset.series;
    state.series[key] = !state.series[key];
    chip.classList.toggle('is-on', state.series[key]);
    renderMainChart();
  });
  window.addEventListener('resize', () => {
    for (const view of state.open) {
      const node = windowNode(view);
      if (!node || node.classList.contains('is-max')) continue;
      const rect = node.getBoundingClientRect();
      const layerRect = $('windowLayer').getBoundingClientRect();
      applyGeometry(node, rect.left - layerRect.left, rect.top - layerRect.top, rect.width, rect.height);
    }
    if (state.open.has('overview')) renderMainChart();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  updateClock();
  setInterval(updateClock, 15000);
  bootstrap().catch(markFail);
  setInterval(tick, 1000);
}

document.addEventListener('DOMContentLoaded', init);
