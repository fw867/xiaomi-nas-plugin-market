'use strict';

/* 注入到软件中心页面里的浮动按钮：点它打开插件的设置页（连接参数、令牌、状态）。
   之所以要它：软件中心是插件根路径的页面，设置页在 panel.html，App 里需要一个入口。
   按钮用 DOM + CSSOM 创建，不用内联样式，也不依赖任何框架。 */

(function () {
  if (window.__unifiOverlayLoaded) return;
  window.__unifiOverlayLoaded = true;

  const STYLE = `
.unifi-fab{position:fixed;right:12px;top:calc(60px + env(safe-area-inset-top,0px));z-index:2147483000;
  width:38px;height:38px;border-radius:50%;border:1px solid rgba(255,255,255,.5);
  background:rgba(28,32,44,.66);color:#fff;font-size:17px;line-height:1;cursor:pointer;
  display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px);
  box-shadow:0 6px 18px rgba(0,0,0,.22);opacity:.5;transition:opacity .18s ease,transform .18s ease}
.unifi-fab:hover{opacity:1;transform:scale(1.06)}
.unifi-fab:active{transform:scale(.94)}
@media (max-width:560px){.unifi-fab{width:34px;height:34px;font-size:15px;opacity:.72;top:calc(56px + env(safe-area-inset-top,0px))}}
`;

  function mount() {
    if (document.querySelector('.unifi-fab')) return;
    const style = document.createElement('style');
    style.textContent = STYLE;
    document.head.append(style);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'unifi-fab';
    button.title = 'Unifi 设置';
    button.setAttribute('aria-label', 'Unifi 设置');
    button.textContent = '⚙';
    button.addEventListener('click', () => {
      // 相对当前页面目录：/plugin/<用户名>/rtrcenter/ → /plugin/<用户名>/rtrcenter/panel.html
      window.location.href = 'panel.html';
    });
    document.body.append(button);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
