#!/usr/bin/env python3
"""极简自检：`web/app.js` 里用到的每个 id 都必须在 `web/index.html` 里存在。

前端不写自动化测试（与仓库其它插件一致），但「JS 引用的 id 拼错」是这套页面里
唯一容易静默出错的地方（点了按钮没反应），所以用一个独立脚本兜住。

    python3 scripts/check_web_ids.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / 'web'


def html_ids(text: str) -> set:
    return set(re.findall(r'\bid="([^"]+)"', text))


def js_ids(text: str) -> set:
    """取 `$('x')` / `getElementById('x')` 里的 id（不含 `$(` 变量/index 参数）。"""
    found = set(re.findall(r"\$\(\s*'([A-Za-z][\w-]*)'\s*\)", text))
    found |= set(re.findall(r"getElementById\(\s*'([A-Za-z][\w-]*)'\s*\)", text))
    # 选择器里出现的 #id（如 `#dirList .dir-box`）
    found |= set(re.findall(r"querySelectorAll?\(\s*'#([A-Za-z][\w-]*)", text))
    return found


def main() -> int:
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    js = (WEB / 'app.js').read_text(encoding='utf-8')
    css = (WEB / 'styles.css').read_text(encoding='utf-8')
    present = html_ids(html)
    used = js_ids(js)
    missing = sorted(used - present)
    if missing:
        print('app.js 引用了 index.html 里不存在的 id：%s' % '、'.join(missing))
        return 1
    print('index.html 提供 %d 个 id，app.js 使用 %d 个，全部存在（OK）'
          % (len(present), len(used)))
    # 顺带核对：模板里引用的静态资源文件真的在
    for name in ('styles.css', 'app.js'):
        if not (WEB / name).is_file():
            print('缺少前端文件：%s' % name)
            return 1
        if 'id="%s"' % name in html:                       # pragma: no cover 只是防御
            print('index.html 里的 id 不应与文件名冲突：%s' % name)
            return 1
    if 'discoverySwitch' not in css and 'switch' not in css:
        print('styles.css 里没有开关样式')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
