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
        self.assertIn('↩', text)

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

    def test_former_sources_content_lives_in_about(self) -> None:
        for text in (
            "Kingwell Community",
            "内置可信源 · ECDSA P-256",
            "已启用",
            "添加签名源",
            "自定义源会在公钥指纹确认流程完成后开放。",
        ):
            self.assertIn(text, self.about, text)
        # 原有信息没被删掉：禁用按钮依然是禁用的
        self.assertIn('class="add-source" disabled', self.about)

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
        for rule in (".source-band {", ".source-icon {", ".source-copy {", ".source-state {", ".source-note {"):
            self.assertIn(rule, STYLES, rule)
        self.assertIn(".about-card .source-band", STYLES)


if __name__ == "__main__":
    unittest.main()
