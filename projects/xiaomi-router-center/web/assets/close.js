'use strict';

/* 插件端的关闭按钮：贴在屏幕左侧中间，点它退出当前插件页面。
 *
 * 关闭方式照抄官方插件（应用商店/硬盘休眠等）的做法：
 *   1) 客户端（小米智能存储 App）在 WebView 里注入 flutter_inappwebview / android_webview，
 *      官方插件用宿主方法 `normal_goback` 关掉当前插件页 —— 这里调用同一个方法；
 *   2) 不在客户端（或宿主没响应）时退回浏览器历史；再不行 window.close()；
 *   3) 都不行才提示用手机返回键。
 *
 * 设置页（panel.html）用 <script src="assets/close.js" data-mode="back"> 引入，
 * 点关闭只回上一页（软件中心），不会把整个插件页关掉。
 */

(function () {
  if (window.__unifiCloseLoaded) return;
  window.__unifiCloseLoaded = true;

  const script = document.currentScript;
  const mode = (script && script.dataset && script.dataset.mode) || 'close';

  const STYLE = `
.unifi-close{position:fixed;left:0;top:50%;transform:translateY(-50%);z-index:2147483600;
  width:30px;height:56px;border-radius:0 14px 14px 0;border:1px solid rgba(255,255,255,.4);border-left:0;
  background:rgba(28,32,44,.62);color:#fff;font-size:15px;line-height:1;cursor:pointer;padding:0;
  display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px);
  box-shadow:4px 0 16px rgba(0,0,0,.2);opacity:.42;transition:opacity .18s ease,transform .18s ease}
.unifi-close:hover{opacity:1}
.unifi-close:active{transform:translateY(-50%) scale(.94)}
.unifi-close svg{width:14px;height:14px;stroke:currentColor;stroke-width:2.2;fill:none;
  stroke-linecap:round;stroke-linejoin:round}
.unifi-close-toast{position:fixed;left:44px;top:50%;transform:translateY(-50%);z-index:2147483600;
  background:rgba(16,20,32,.92);color:#fff;font:13px/1.4 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  padding:8px 12px;border-radius:10px;box-shadow:0 10px 26px rgba(0,0,0,.3);max-width:60vw}
@media (max-width:560px){.unifi-close{width:26px;height:50px;opacity:.55}}
`;

  // 小米智能存储客户端在 WebView 里注入 flutter_inappwebview / android_webview，
  // 官方插件靠这套桥调宿主方法；浏览器里没有这两个对象，返回 false 表示"不在客户端"。
  function callClient(method, params) {
    const host = window.flutter_inappwebview || window.android_webview;
    if (!host) return false;
    if (!host.callHandler) {
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
  // 宿主拿结果时会回调这个全局函数，留个空实现免得它报错
  window.hs_appCallBackToWeb = window.hs_appCallBackToWeb || function () {};

  function toast(message) {
    const node = document.createElement('div');
    node.className = 'unifi-close-toast';
    node.textContent = message;
    document.body.append(node);
    setTimeout(() => node.remove(), 2800);
  }

  function goBack() {
    if (window.history.length > 1) {
      window.history.back();
      return true;
    }
    try { window.close(); } catch (error) { /* 继续兜底 */ }
    return false;
  }

  function closePage() {
    if (mode === 'back') {                       // 设置页：只回上一页
      if (!goBack()) toast('请用手机的返回键退出插件');
      return;
    }
    // 插件页：先请宿主关闭（官方插件同款方法 normal_goback），失败再兜底
    if (callClient('normal_goback', {})) return;
    if (!goBack()) toast('请用手机的返回键退出插件');
  }

  function mount() {
    if (document.querySelector('.unifi-close')) return;
    const style = document.createElement('style');
    style.textContent = STYLE;
    document.head.append(style);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'unifi-close';
    button.title = '关闭';
    button.setAttribute('aria-label', '关闭');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    for (const data of ['M4 4l8 8', 'M12 4l-8 8']) {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', data);
      svg.append(path);
    }
    button.append(svg);
    button.addEventListener('click', closePage);
    document.body.append(button);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
