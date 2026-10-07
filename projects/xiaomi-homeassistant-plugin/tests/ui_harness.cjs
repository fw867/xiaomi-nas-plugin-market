'use strict';
/*
 * 前端行为校验（Node，无依赖）：用最小 DOM 桩加载 web/app.js，验证
 *   - 多存储位置选择器、绝对路径显示（状态卡片、弹窗顶部）
 *   - 「修改目录」弹窗：只改配置目录，保持当前/私有目录/新位置三种语义
 *   - 提交给 /service/reconfigure 的载荷与确认框文案
 *   - 「重新初始化」的确认与复位
 *   - Home Assistant 版本显示（.HA_VERSION → clientVersion）
 * 用法：node tests/ui_harness.cjs [插件目录]   （退出码非 0 表示有用例失败）
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const pluginDir = process.argv[2] || path.join(__dirname, '..');
const code = fs.readFileSync(path.join(pluginDir, 'web', 'app.js'), 'utf8');

const elements = new Map();
function makeElement(id) {
  return {
    id, textContent: '', title: '', className: '', hidden: false, value: '',
    disabled: false, isConnected: true, children: [], dataset: {}, onclick: null,
    type: '', classList: { contains: () => false },
    replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; },
    append(node) { this.children.push(node); },
    close() { this.closed = true; },
    showModal() { this.opened = true; },
    // 表单需要这几个方法：提交处理里会 reset() 并临时禁用提交按钮
    reset() { this.resetCalled = true; },
    querySelector: () => ({ disabled: false }),
  };
}
const calls = [];
const confirms = [];
let confirmAnswer = true;

const state = {
  ok: true, configured: true, running: true, ready: true, busy: false, error: '',
  port: 8123, config_abs: '/nas/pool0/u1/data/ha', config_private: false,
  clientVersion: '2025.1.2', imageVersion: 'stable（滚动标签）', roots: [
    { index: 0, label: '存储池', path: '/nas/pool0/u1/data', exists: true },
    { index: 1, label: '外接设备', path: '/nas/mnt/usb', exists: true },
  ],
};

const sandbox = {
  console,
  URL, Promise, JSON, Number, String, Error, setTimeout: () => 0, clearTimeout: () => {},
  navigator: { clipboard: { writeText: async () => {} } },
  document: {
    currentScript: null,
    scripts: [],
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    querySelector: () => ({ content: 'token' }),
    querySelectorAll: () => [],
    createElement: (tag) => makeElement(tag),
  },
  fetch: async (url, init) => {
    const method = (init && init.method) || 'GET';
    calls.push({ method, url, body: init && init.body ? JSON.parse(init.body) : null });
    const payload = url.includes('/browse')
      ? { ok: true, items: [{ name: '照片', path: '照片' }] }
      : JSON.parse(JSON.stringify(state));
    return { ok: true, status: 200, json: async () => payload };
  },
};
sandbox.window = {
  location: { origin: 'http://127.0.0.1:19387', pathname: '/' },
  confirm: (text) => { confirms.push(text); return confirmAnswer; },
};
sandbox.globalThis = sandbox;
sandbox.__MICRO_APP_BASE_ROUTE__ = undefined;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'app.js' });

const el = (id) => sandbox.document.getElementById(id);
const peek = (expr) => vm.runInContext(expr, sandbox);
const flush = () => new Promise((r) => setImmediate(r));
const posts = () => calls.filter((c) => c.method === 'POST');
const lastPost = () => posts().slice(-1)[0];
const lastConfirm = () => confirms[confirms.length - 1];

const failures = [];
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures.push(`${name}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name} -> ${JSON.stringify(got)}`);
}

// 在目录选择器里切到某个存储位置、回到该位置根部（可选再进一层），然后确认选择
async function pickFolder(rootIndex, intoFolder) {
  if (peek('browseRoot') !== rootIndex) {
    el('roots').children[rootIndex].onclick();
    await flush();
  }
  for (let step = 0; step < 4 && peek('browsePath'); step += 1) {
    el('up').onclick();
    await flush();
  }
  if (intoFolder) {
    el('folders').children[0].onclick();
    await flush();
  }
  el('selectFolder').onclick();
  await flush();
  await flush();
}

(async () => {
  await sandbox.refresh();
  await flush();
  check('状态卡片显示配置目录绝对路径', el('configDirectory').textContent, '/nas/pool0/u1/data/ha');
  check('已配置时显示目录设置块', el('directorySettings').hidden, false);
  check('状态栏显示 .HA_VERSION 里的版本',
    el('serverInfo').textContent.includes('Home Assistant 2025.1.2'), true);
  check('状态栏同时给出镜像口径',
    el('serverInfo').textContent.includes('镜像 stable（滚动标签）'), true);
  check('入口地址来自状态接口', el('address').textContent,
    '（请通过小米客户端打开插件以获取地址）');
  check('端口显示 8123', el('panelPort').textContent, '8123');

  // —— 多存储位置选择器 ——
  sandbox.openBrowser('t', '/nas/mnt/usb/下载/MT', () => {});
  check('定位到外接设备', peek('browseRoot'), 1);
  check('根内相对路径', peek('browsePath'), '下载/MT');
  check('弹窗顶部完整绝对路径', el('browsePath').textContent, '/nas/mnt/usb/下载/MT');
  check('位置按钮数量', el('roots').children.length, 2);
  el('roots').children[0].onclick();
  await flush();
  check('切换位置后请求带 root=0', calls[calls.length - 1].url.includes('root=0&path='), true);
  check('位置根部显示根的绝对路径', el('browsePath').textContent, '/nas/pool0/u1/data');

  // —— 修改目录：默认「保持当前」 ——
  calls.length = 0; confirms.length = 0; confirmAnswer = true;
  el('reconfigure').onclick();
  check('修改目录打开专用弹窗', el('reconfigureDialog').opened, true);
  check('配置目录默认保持当前', el('reconfigureConfig').value, '/nas/pool0/u1/data/ha（保持不变）');
  el('reconfigureSubmit').onclick();
  await flush(); await flush();
  check('保持配置目录时不带 configPath',
    Object.prototype.hasOwnProperty.call(lastPost().body, 'configPath'), false);
  check('确认框写明配置目录保持不变',
    lastConfirm().includes('配置目录：/nas/pool0/u1/data/ha（保持不变）'), true);
  check('确认框写明数据不会被删', lastConfirm().includes('不会被删除'), true);
  check('提交后关闭弹窗', el('reconfigureDialog').closed, true);

  // —— 配置目录换到外接设备 ——
  calls.length = 0; confirms.length = 0;
  el('reconfigure').onclick();
  el('reconfigureConfigChoose').onclick();
  check('选择器从当前目录开始', [peek('browseRoot'), peek('browsePath')], [0, 'ha']);
  await pickFolder(1, true);                       // 外接设备/照片
  check('回填新配置目录', el('reconfigureConfig').value, '/nas/mnt/usb/照片');
  check('配置目录提示说明新位置后果',
    el('reconfigureConfigHint').textContent.includes('全新状态启动'), true);
  el('reconfigureSubmit').onclick();
  await flush(); await flush();
  check('提交新的配置目录路径', lastPost().body.configPath, '/nas/mnt/usb/照片');
  check('确认框动态描述配置目录变化',
    lastConfirm().includes('配置目录：/nas/pool0/u1/data/ha → /nas/mnt/usb/照片'), true);

  // —— 配置目录改回插件私有目录（空串） ——
  calls.length = 0; confirms.length = 0;
  el('reconfigure').onclick();
  el('reconfigureConfigPrivate').onclick();
  check('私有目录显示在弹窗里', el('reconfigureConfig').value, '插件私有目录（外部不可见）');
  check('私有目录有说明', el('reconfigureConfigHint').textContent.includes('插件私有目录'), true);
  el('reconfigureSubmit').onclick();
  await flush(); await flush();
  check('选私有目录时提交空串', lastPost().body.configPath, '');
  check('确认框写明改为私有目录', lastConfirm().includes('→ 插件私有目录（外部不可见）'), true);

  // —— 取消确认不发请求 ——
  calls.length = 0; confirmAnswer = false;
  el('reconfigure').onclick();
  el('reconfigureSubmit').onclick();
  await flush(); await flush();
  check('取消确认后不发请求', posts().length, 0);

  // —— 配置目录当前就在私有目录时的显示 ——
  confirmAnswer = true;
  state.config_private = true;
  state.config_abs = '';
  await sandbox.refresh();
  check('私有配置目录显示在状态卡片里',
    el('configDirectory').textContent, '—（插件私有目录，外部不可见）');
  el('reconfigure').onclick();
  check('私有配置目录显示为保持', el('reconfigureConfig').value, '插件私有目录（保持不变）');
  await sandbox.refresh();
  state.config_private = false;
  state.config_abs = '/nas/pool0/u1/data/ha';
  await sandbox.refresh();

  // —— 首次初始化：配置目录留空也可提交（＝插件私有目录） ——
  state.configured = false;
  await sandbox.refresh();
  await flush();
  calls.length = 0;
  el('setupForm').onsubmit({ preventDefault: () => {}, target: el('setupForm') });
  await flush(); await flush();
  check('未配置时隐藏目录设置', el('directorySettings').hidden, true);
  check('未配置时显示初始化表单', el('setup').hidden, false);
  check('初始化提交到 /setup', lastPost().url.includes('/setup'), true);
  check('未选目录时提交空串', lastPost().body.path, '');

  // —— 初始化时选了用户存储里的目录 ——
  state.configured = false;
  await sandbox.refresh();
  calls.length = 0;
  el('chooseConfig').onclick();
  await pickFolder(1, true);
  check('回填初始化用的配置目录', el('configPath').value, '/nas/mnt/usb/照片');
  el('setupForm').onsubmit({ preventDefault: () => {}, target: el('setupForm') });
  await flush(); await flush();
  check('初始化提交绝对路径', lastPost().body.path, '/nas/mnt/usb/照片');

  // —— 重新初始化：确认 → service/reset → 表单复位 ——
  state.configured = true;
  await sandbox.refresh();
  calls.length = 0; confirms.length = 0;
  el('configPath').value = '/nas/mnt/usb/照片';
  el('reset').onclick();
  await flush(); await flush();
  check('确认框说明了用户数据不受影响', lastConfirm().includes('文件不受影响'), true);
  check('提交到 reset 且带 confirm', lastPost().body, { confirm: true });
  check('reset 后清掉旧的目录选择', el('configPath').value, '');

  // —— 就绪提示只在容器起来但还没就绪时出现 ——
  state.ready = false;
  await sandbox.refresh();
  check('未就绪时显示初始化提示', el('wizardHint').hidden, false);
  state.ready = true;
  await sandbox.refresh();
  check('就绪后隐藏初始化提示', el('wizardHint').hidden, true);

  if (failures.length) {
    console.log('\nFAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('\n全部通过');
})();
