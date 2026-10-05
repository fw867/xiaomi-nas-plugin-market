"""前端静态断言：底部 tab 第 3 个换成「回滚」、来源信息挪进「关于」、
回滚按钮与二次确认文案、清理旧版本按钮，以及「所有图标都是内联 SVG」。

项目没有前端构建链（纯原生 JS），这里只做源码级断言；真实交互另由
`server.py --dev` + DOM 桩验证，渲染尺寸由 Playwright 真渲染量测。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
INDEX = (PROJECT / "web" / "index.html").read_text(encoding="utf-8")
SCRIPT = (PROJECT / "web" / "app.js").read_text(encoding="utf-8")
STYLES = (PROJECT / "web" / "styles.css").read_text(encoding="utf-8")

# 全部改成内联 SVG 后，index.html 里不该再出现的「当图标用的生僻字形」
BANNED_ICON_GLYPHS = ("▦", "✓", "ⓘ", "⟲", "↩", "↺", "↻", "⤺", "⇦", "⭯", "☁", "‹", "＋")
# 正文/排版用途，允许保留
ALLOWED_TEXT_GLYPHS = ("—", "…")


def section(view_id: str) -> str:
    match = re.search(rf'<section id="{view_id}".*?</section>', INDEX, re.S)
    if match is None:
        raise AssertionError(f"index.html 里找不到 {view_id} 视图")
    return match.group(0)


def tab_markup(view: str) -> str:
    match = re.search(rf'<button class="tab[^"]*" data-view="{view}">.*?</button>', INDEX, re.S)
    if match is None:
        raise AssertionError(f"index.html 里找不到 {view} tab")
    return match.group(0)


def icon_button_markup(button_id: str) -> str:
    match = re.search(rf'<button class="icon-button[^"]*" id="{button_id}".*?</button>', INDEX, re.S)
    if match is None:
        raise AssertionError(f"index.html 里找不到 {button_id}")
    return match.group(0)


def all_svgs() -> list[str]:
    return re.findall(r"<svg .*?</svg>", INDEX, re.S)


class TabbarTests(unittest.TestCase):
    def test_third_tab_is_rollback(self) -> None:
        views = re.findall(r'<button class="tab[^"]*" data-view="([^"]+)"', INDEX)
        self.assertEqual(["featured", "installed", "rollback", "about"], views)
        text = tab_markup("rollback")
        self.assertIn('<span class="tab-label">回滚</span>', text)
        self.assertIn('class="tab-icon"', text)
        self.assertNotIn('☁', text)
        # 回滚图标是内联 SVG：Android WebView 缺 U+27F2/↩ 这类箭头字形，
        # 回退字体会渲染得又小又细，改用 SVG 才与字体无关。
        self.assertIn("<svg ", text)
        self.assertNotIn('⟲', text)
        self.assertNotIn('↩', text)

    def test_all_four_tabs_use_inline_svg(self) -> None:
        for view in ("featured", "installed", "rollback", "about"):
            body = tab_markup(view)
            self.assertIn("<svg ", body, view)
            # .tab-icon 里只剩 SVG，没有任何文本字形
            icon = re.search(r'<span class="tab-icon" aria-hidden="true">(.*?)</span>', body, re.S).group(1)
            self.assertTrue(icon.strip().startswith("<svg"), view)
            self.assertTrue(icon.strip().endswith("</svg>"), view)

    def test_no_icon_glyph_left_in_index(self) -> None:
        for glyph in BANNED_ICON_GLYPHS:
            self.assertNotIn(glyph, INDEX, f"index.html 里还有当图标用的字形 {glyph!r}")
        # 正文/排版用的破折号与省略号保留（不是图标）
        for glyph in ALLOWED_TEXT_GLYPHS:
            self.assertIn(glyph, INDEX)

    def test_icon_svgs_share_one_visual_style(self) -> None:
        svgs = all_svgs()
        self.assertEqual(6, len(svgs), "应为 顶栏 2 + tab 4 共 6 个内联 SVG")
        for svg in svgs:
            self.assertIn('width="1em"', svg)
            self.assertIn('height="1em"', svg)
            self.assertIn('fill="none"', svg)
            self.assertIn('stroke="currentColor"', svg)
            self.assertIn('stroke-linecap="round"', svg)
            self.assertIn('stroke-linejoin="round"', svg)
            self.assertEqual("1.8", re.search(r'stroke-width="([0-9.]+)"', svg).group(1))
            self.assertIn('focusable="false"', svg)
        # 四个 tab 共用 20×20 画布；顶栏按各自的 em（30px / 25px）取 1:1 画布，
        # 这样 stroke-width=1.8 在任何位置都渲染成约 1.8px，描边光学等重
        self.assertEqual(
            ["0 0 30 30", "0 0 25 25", "0 0 20 20", "0 0 20 20", "0 0 20 20", "0 0 20 20"],
            [re.search(r'viewBox="([^"]+)"', svg).group(1) for svg in svgs],
        )
        for view in ("featured", "installed", "rollback", "about"):
            self.assertIn('viewBox="0 0 20 20"', tab_markup(view), view)

    def test_topbar_icons_use_inline_svg(self) -> None:
        for button_id in ("backButton", "refreshButton"):
            body = icon_button_markup(button_id)
            self.assertIn("<svg ", body, button_id)
            self.assertIn('aria-hidden="true"', body, button_id)

    def test_icon_svgs_follow_the_shared_boxes(self) -> None:
        # SVG 只继承容器的 1em 尺寸，不给某个图标单独写尺寸
        self.assertIn(".tabbar .tab-icon svg {", STYLES)
        self.assertIn(".icon-button svg {", STYLES)
        self.assertIn("width: 1em; height: 1em;", STYLES)
        # 顶栏按钮靠 flex 居中图标，点击区仍是 36×36（≥34px 触控约定）
        self.assertIn("align-items: center;", STYLES)
        self.assertIn("justify-content: center;", STYLES)
        self.assertIn("width: 36px;\n  height: 36px;", STYLES)

    def test_sources_tab_and_view_are_gone(self) -> None:
        self.assertNotIn("sourcesView", INDEX)
        self.assertNotIn('data-view="sources"', INDEX)
        self.assertNotIn('<span class="tab-label">来源</span>', INDEX)
        self.assertNotIn("#sourcesView", SCRIPT)
        self.assertNotIn("sourcesView", SCRIPT)

    def test_rollback_view_has_a_list_container(self) -> None:
        body = section("rollbackView")
        self.assertIn('id="rollbackList"', body)
        self.assertIn("回滚", body)
        self.assertIn("用户数据保留", body)
        self.assertIn("document.getElementById('rollbackList')", SCRIPT)


class RollbackFrontendTests(unittest.TestCase):
    def test_rollback_uses_confirm_and_new_endpoint(self) -> None:
        self.assertIn("api/rollback", SCRIPT)
        self.assertIn("window.confirm(", SCRIPT)
        self.assertIn("只会把插件切回上一版本，用户数据保留；之后仍可再更新回来", SCRIPT)
        self.assertIn("已回滚到", SCRIPT)
        self.assertIn("makeButton('回滚'", SCRIPT)
        self.assertIn("'rollback'", SCRIPT)

    def test_rollback_lists_only_rollbackable_plugins(self) -> None:
        self.assertIn("item.canRollback", SCRIPT)
        self.assertIn("item.previousVersion", SCRIPT)
        self.assertIn("暂无可回滚的插件；某插件更新过一次后，这里就会保留它的上一版本", SCRIPT)

    def test_rollback_refreshes_the_list(self) -> None:
        body = SCRIPT.split("async function rollbackPackage", 1)[1].split("async function pruneReleases", 1)[0]
        self.assertIn("await loadCatalog()", body)
        self.assertIn("preview", body)

    def test_backend_errors_are_shown_verbatim(self) -> None:
        body = SCRIPT.split("async function rollbackPackage", 1)[1].split("async function pruneReleases", 1)[0]
        self.assertIn("throw new Error(payload.error || '回滚失败')", body)


class AboutViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.about = section("aboutView")
        self.cards = re.findall(r'<div class="about-card">.*?\n        </div>', self.about, re.S)

    def test_trusted_source_card_is_first(self) -> None:
        self.assertEqual(4, len(self.cards), self.cards)
        first = self.cards[0]
        for text in ("Kingwell Community", "内置可信源 · ECDSA P-256", "已启用"):
            self.assertIn(text, first, text)
        self.assertIn('class="source-band"', first)

    def test_add_source_button_and_note_are_removed(self) -> None:
        """按用户要求删掉禁用的「添加签名源」按钮与下面那句说明。"""
        self.assertNotIn("添加签名源", INDEX)
        self.assertNotIn("自定义源会在公钥指纹确认流程完成后开放", INDEX)
        self.assertNotIn("add-source", INDEX)
        self.assertNotIn("source-note", INDEX)
        # 样式里也不再留这两条的孤儿规则
        self.assertNotIn(".add-source", STYLES)
        self.assertNotIn(".source-note", STYLES)

    def test_card_order_source_update_prune_repo(self) -> None:
        """可信源在最上面；清理卡片紧跟更新卡片；仓库/声明在最后。"""
        self.assertIn("当前版本", self.cards[1])
        self.assertIn('id="pruneButton"', self.cards[2])
        self.assertIn("旧版本", self.cards[2])
        self.assertIn("仓库", self.cards[3])
        self.assertIn("声明", self.cards[3])
        # 更新卡片后面必须直接是清理卡片（两者相邻且顺序固定）
        self.assertLess(self.about.index('id="checkUpdateButton"'), self.about.index('id="pruneButton"'))
        self.assertLess(self.about.index('id="pruneButton"'), self.about.index('id="repoLink"'))

    def test_prune_button_in_about(self) -> None:
        self.assertIn('id="pruneButton"', self.about)
        self.assertIn("清理旧版本", self.about)
        self.assertIn("每个插件保留当前与上一版本", self.about)
        self.assertIn("document.getElementById('pruneButton')", SCRIPT)
        self.assertIn("api/prune", SCRIPT)
        self.assertIn("共清理", SCRIPT)
        self.assertIn("释放约", SCRIPT)
        self.assertIn("formatSize", SCRIPT)
        self.assertIn("项失败", SCRIPT)
        # 清理结果里要体现「注册表备份」这一类（toast 与关于页说明都要提到）
        self.assertIn("注册表备份", SCRIPT)
        self.assertIn("payload.registryRemoved", SCRIPT)
        self.assertIn("注册表备份", self.about)

    def test_prune_toast_merges_both_counts(self) -> None:
        body = SCRIPT.split("async function pruneReleases", 1)[1].split(
            "document.getElementById('pruneButton').addEventListener", 1
        )[0]
        # 旧版本与注册表备份两个数量都要汇总，缺一时不显示那一段
        self.assertIn("payload.removedCount", body)
        self.assertIn("payload.registryRemoved", body)
        self.assertIn("${releasedCount} 个旧版本", body)
        self.assertIn("${registryCount} 个注册表备份", body)
        self.assertIn("parts.join('、')", body)
        self.assertIn("没有需要清理的旧版本或注册表备份", body)

    def test_sources_styles_are_reused_not_orphaned(self) -> None:
        for rule in (".source-band {", ".source-icon {", ".source-copy {", ".source-state {"):
            self.assertIn(rule, STYLES, rule)
        self.assertIn(".about-card .source-band", STYLES)


class TabConsistencyTests(unittest.TestCase):
    """四个 tab 必须共用同一套 class/尺寸，不能给某个 tab 单独写样式。"""

    def test_tabs_share_the_same_markup(self) -> None:
        tabs = re.findall(r'<button class="([^"]*)" data-view="([^"]+)">(.*?)</button>', INDEX, re.S)
        self.assertEqual(4, len(tabs))
        self.assertEqual(
            [("tab", "featured"), ("tab", "installed"), ("tab", "rollback"), ("tab", "about")],
            [(classes.replace(" active", ""), view) for classes, view, _ in tabs],
        )
        for _, view, body in tabs:
            # 四个 tab 都是「一个 .tab-icon（内联 SVG）+ 一个 .tab-label」，写法完全一致
            self.assertRegex(body.strip(), r'^<span class="tab-icon" aria-hidden="true"><svg ')
            self.assertRegex(body.strip(), r'</svg></span><span class="tab-label">.+</span>$')
            self.assertEqual(1, body.count("<svg "), view)
            self.assertEqual(1, body.count("viewBox=\"0 0 20 20\""), view)

    def test_no_per_tab_size_overrides(self) -> None:
        # 只允许一个 .tabbar .tab-icon 规则（外加一条给内部 svg 的通用规则），
        # 且没有任何针对某个 tab 的单独尺寸
        self.assertEqual(1, len(re.findall(r'\.tabbar \.tab-icon \{', STYLES)))
        self.assertEqual(1, len(re.findall(r'\.tabbar \.tab-icon svg \{', STYLES)))
        self.assertNotIn('data-view="rollback"', STYLES)
        self.assertNotIn("tab-icon.rollback", STYLES)
        self.assertNotIn("font-size: 19px", INDEX)
        # 四个 tab 的图标盒都还是同一个 font-size（19px），SVG 用 1em 跟随
        self.assertIn(".tabbar .tab-icon { font-size: 19px; line-height: 1; }", STYLES)


if __name__ == "__main__":
    unittest.main()
