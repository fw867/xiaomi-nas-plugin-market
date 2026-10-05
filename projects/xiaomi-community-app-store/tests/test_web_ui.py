"""前端静态断言：底部 tab 第 3 个换成「回滚」、来源信息挪进「关于」、
回滚按钮与二次确认文案、清理旧版本按钮。

项目没有前端构建链（纯原生 JS），这里只做源码级断言；真实交互另由
`server.py --dev` + DOM 桩验证。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
INDEX = (PROJECT / "web" / "index.html").read_text(encoding="utf-8")
SCRIPT = (PROJECT / "web" / "app.js").read_text(encoding="utf-8")
STYLES = (PROJECT / "web" / "styles.css").read_text(encoding="utf-8")


def section(view_id: str) -> str:
    match = re.search(rf'<section id="{view_id}".*?</section>', INDEX, re.S)
    if match is None:
        raise AssertionError(f"index.html 里找不到 {view_id} 视图")
    return match.group(0)


class TabbarTests(unittest.TestCase):
    def test_third_tab_is_rollback(self) -> None:
        views = re.findall(r'<button class="tab[^"]*" data-view="([^"]+)"', INDEX)
        self.assertEqual(["featured", "installed", "rollback", "about"], views)
        tab = re.search(r'<button class="tab[^"]*" data-view="rollback".*?</button>', INDEX, re.S)
        self.assertIsNotNone(tab)
        text = tab.group(0)
        self.assertIn('<span class="tab-label">回滚</span>', text)
        self.assertIn('class="tab-icon"', text)
        self.assertNotIn('☁', text)
        # 换掉偏细偏小的 ↩，用光学大小更接近 ▦/✓/ⓘ 的 ⟲（U+27F2）
        self.assertNotIn('↩', text)
        self.assertIn('<span class="tab-icon" aria-hidden="true">⟲</span>', text)

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

    def test_sources_styles_are_reused_not_orphaned(self) -> None:
        for rule in (".source-band {", ".source-icon {", ".source-copy {", ".source-state {"):
            self.assertIn(rule, STYLES, rule)
        self.assertIn(".about-card .source-band", STYLES)


class TabConsistencyTests(unittest.TestCase):
    """四个 tab 必须共用同一套 class/尺寸，不能只给回滚 tab 单独写样式。"""

    def test_tabs_share_the_same_markup(self) -> None:
        tabs = re.findall(r'<button class="([^"]*)" data-view="([^"]+)">(.*?)</button>', INDEX, re.S)
        self.assertEqual(4, len(tabs))
        self.assertEqual(
            [("tab", "featured"), ("tab", "installed"), ("tab", "rollback"), ("tab", "about")],
            [(classes.replace(" active", ""), view) for classes, view, _ in tabs],
        )
        for _, _, body in tabs:
            self.assertRegex(
                body.strip(),
                r'^<span class="tab-icon" aria-hidden="true">.</span><span class="tab-label">.+</span>$',
            )

    def test_no_per_tab_size_overrides(self) -> None:
        # 只允许一个 .tabbar .tab-icon 规则，且没有任何针对回滚 tab 的单独尺寸
        self.assertEqual(1, len(re.findall(r'\.tabbar \.tab-icon \{', STYLES)))
        self.assertNotIn('data-view="rollback"', STYLES)
        self.assertNotIn("tab-icon.rollback", STYLES)
        self.assertNotIn("font-size: 19px", INDEX)


if __name__ == "__main__":
    unittest.main()
