"""前端静态自检：`web/` 里的元素与交互钩子必须都在。

页面不写自动化测试（与仓库其它插件一致），但「JS 引用的 id 拼错」「交互钩子丢了」
是这套页面里最容易静默出错的地方，所以用静态断言兜住：只查元素/类名/调用是否存在，
不查渲染结果（渲染逻辑靠 `scripts/check_web_ids.py` 与 `node --check` 兜底）。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / 'web'
HTML = (WEB / 'index.html').read_text(encoding='utf-8')
JS = (WEB / 'app.js').read_text(encoding='utf-8')
CSS = (WEB / 'styles.css').read_text(encoding='utf-8')


def load_id_checker():
    """加载 `scripts/check_web_ids.py`（不在 sys.path 上，按路径加载）。"""
    path = Path(__file__).resolve().parent.parent / 'scripts' / 'check_web_ids.py'
    spec = importlib.util.spec_from_file_location('check_web_ids', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WebIdConsistencyTests(unittest.TestCase):
    """`app.js` 里 `$('x')` / `#x` 用到的 id 必须在 `index.html` 里存在。"""

    def test_every_id_used_by_app_js_exists_in_index_html(self):
        checker = load_id_checker()
        missing = sorted(checker.js_ids(JS) - checker.html_ids(HTML))
        self.assertEqual(missing, [], 'app.js 引用了 index.html 里不存在的 id：%s' % missing)


class HostnameReadOnlyTests(unittest.TestCase):
    """只读展示 SMB 名（自动获取）+ 不一致时的警告与恢复按钮。"""

    def test_there_is_no_hostname_input_anywhere(self):
        """旧的输入框 / 保存 / 恢复默认按钮要彻底删掉。"""
        for leftover in ('hostnameInput', 'hostnameSave', 'hostnameReset',
                         '保存主机名', 'saveHostname', 'resetHostname', 'setValue'):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, HTML)
                self.assertNotIn(leftover, JS)
        self.assertNotIn('.host-row', CSS)

    def test_only_the_smb_name_is_shown(self):
        """页面上只有「SMB 名」一行；系统主机名不再展示（仍由服务端当判据）。"""
        self.assertIn('id="netbiosName"', HTML)
        self.assertIn('SMB 名', HTML)
        self.assertNotIn('systemName', HTML)                 # 标签与 id 都删掉了
        self.assertNotIn('systemName', JS)
        self.assertNotIn('>主机名<', HTML)
        # 标签是 span，不是 input（不给改）
        self.assertIn('<span id="netbiosName" class="identity-value">', HTML)
        self.assertIn('.identity-value', CSS)
        self.assertIn("$('netbiosName').textContent = identity.netbiosName", JS)

    def test_access_hint_comes_from_the_server(self):
        """提示必须用服务端自动获取的 SMB 名/IP 渲染，页面不拼字符串、也不带括号说明。"""
        self.assertIn('id="accessHint"', HTML)
        self.assertIn("$('accessHint').textContent = identity.hint", JS)
        self.assertIn('.access-hint', CSS)
        self.assertNotIn('\\\\minasc71ab1', HTML + JS)       # 不写死任何具体名字
        for dropped in ('主机名每台设备不同', '此处为自动获取'):
            with self.subTest(dropped=dropped):              # 括号里的说明整段删掉了
                self.assertNotIn(dropped, HTML)
                self.assertNotIn(dropped, JS)

    def test_mismatch_shows_warning_and_restore_button(self):
        self.assertIn('id="nameWarning"', HTML)
        self.assertIn('id="nameRestore"', HTML)
        self.assertIn('class="name-warning"', HTML)
        self.assertIn('.name-warning', CSS)                  # 黄色警告样式
        self.assertIn('identity.matched === false', JS)
        self.assertIn('warning.hidden = !mismatch', JS)
        self.assertIn('restore.hidden = !mismatch', JS)
        self.assertIn('恢复为系统主机名', HTML)

    def test_restore_button_calls_the_read_only_endpoint(self):
        self.assertIn("$('nameRestore').onclick = restoreHostname", JS)
        self.assertIn("call('hostname/restore', {})", JS)
        self.assertIn("result.verified", JS)                 # 回读校验结果要提示
        self.assertIn('showError(error.message)', JS)

    def test_hidden_attribute_wins_over_button_display(self):
        """`hidden` 必须能藏住 `.btn.block`：否则名字一致后恢复按钮还在（实测踩过）。"""
        self.assertIn('[hidden] { display: none !important; }', CSS)
        self.assertIn('.btn.block', CSS)                     # 确实有会压过 hidden 的规则


class ShareCardLayoutTests(unittest.TestCase):
    """共享目录卡片：上部「添加共享」按钮 + 下部「已共享的目录」列表。"""

    def test_add_share_button_is_on_top(self):
        self.assertIn('id="addShare"', HTML)
        self.assertIn('添加共享', HTML)
        self.assertIn("$('addShare')", JS)                 # 绑定了点击

    def test_shared_list_container_below(self):
        self.assertIn('id="shares"', HTML)
        self.assertIn('class="shares"', HTML)
        self.assertIn('.shares', CSS)
        self.assertIn("const box = $('shares')", JS)       # renderShares 往这里画
        self.assertIn('已共享的目录', HTML)

    def test_row_shows_display_name_share_name_and_full_path(self):
        self.assertIn("element('div', 'share-name'", JS)
        self.assertIn('共享名 ${share.name}', JS)           # <账号>_nb_<序号> 这段
        self.assertIn("element('div', 'share-sub', share.path)", JS)   # 完整路径

    def test_row_remove_button_is_a_minus_sign(self):
        self.assertIn("element('button', 'share-remove', '−')", JS)
        self.assertIn('.share-remove', CSS)
        self.assertIn('share-remove:disabled', CSS)         # 禁用态样式
        self.assertIn('.share-remove {', CSS)

    def test_non_deletable_share_is_disabled_with_a_reason(self):
        # 官方 App 建的共享要显示出来，但按钮置灰 + 说明原因（悬停可见）
        self.assertIn('remove.disabled = true', JS)
        self.assertIn('removeBlockedReason', JS)
        self.assertIn('由小米 App 管理的共享，插件不能删除', JS)
        self.assertIn('remove.title = removeBlockedReason(share)', JS)
        # 事件委托里也要跳过禁用的按钮，别让用户白点
        self.assertIn("event.target.closest('.share-remove')", JS)
        self.assertIn('button.disabled) return', JS)

    def test_empty_list_has_guidance(self):
        self.assertIn('还没有共享目录', JS)

    def test_delete_flow_confirms_then_uses_the_share_id(self):
        self.assertIn('window.confirm', JS)                 # 二次确认
        self.assertIn('remove.dataset.shareName = share.name', JS)
        self.assertIn("call('share/delete', { shareName: name })", JS)
        self.assertIn('showError(error.message)', JS)       # 失败原样显示后端错误


class BrowseDialogTests(unittest.TestCase):
    """目录浏览器弹窗：位置切换 + 当前绝对路径 + 逐层进入 + 上一级 + 选择此目录。"""

    DIALOG_IDS = ('browseDialog', 'browseRoots', 'browsePath', 'browseUp', 'browseList',
                  'selectFolder', 'browseCancel', 'browseError', 'manualPath', 'manualAdd')

    def test_dialog_elements_exist_in_html_and_js(self):
        for name in self.DIALOG_IDS:
            with self.subTest(id=name):
                self.assertIn('id="%s"' % name, HTML)
                self.assertIn("$('%s')" % name, JS)

    def test_browses_layer_by_layer(self):
        self.assertIn('browse?account=', JS)                # /api/browse
        self.assertIn("row.dataset.folder = item.path", JS)  # 点整行进入下一层
        self.assertIn("button.dataset.folder", JS)
        self.assertIn("$('browseList').addEventListener", JS)

    def test_up_button_returns_to_the_parent_directory(self):
        self.assertIn("$('browseUp').onclick", JS)
        self.assertIn('parts.pop()', JS)
        self.assertIn('id="browseUp"', HTML)

    def test_shows_the_current_absolute_path(self):
        self.assertIn('browseAbsolute', JS)
        self.assertIn("$('browsePath').textContent = browseAbsolute", JS)

    def test_location_switch_marks_missing_devices(self):
        self.assertIn('data-root', JS)
        self.assertIn('browseLocations', JS)
        self.assertIn('（未接入）', JS)                      # 拔盘时位置标「未接入」
        self.assertIn('button.disabled = true', JS)          # 未接入的位置不给点

    def test_select_folder_adds_a_share_with_an_absolute_path(self):
        self.assertIn("$('selectFolder').onclick", JS)
        self.assertIn('addShareAt(browseAbsolute)', JS)
        self.assertIn("call('share/add', { account, path: value })", JS)
        self.assertIn('选择此目录', HTML)

    def test_manual_absolute_path_entry_is_kept(self):
        self.assertIn("$('manualAdd').onclick", JS)
        self.assertIn("$('manualPath')", JS)
        self.assertIn('可填 /nas/mnt 下的目录（如 /nas/mnt/usb）', HTML)

    def test_browse_errors_are_shown_as_is(self):
        self.assertIn('setBrowseError(error.message)', JS)
        self.assertIn('id="browseError"', HTML)

    def test_shared_directories_are_marked_in_the_browser(self):
        self.assertIn("element('span', 'chip', '已共享')", JS)

    def test_old_checkbox_dialog_is_gone(self):
        """旧的勾选式「编辑共享」弹窗要彻底删掉，别留下失效代码/样式。"""
        for leftover in ('dirDialog', 'dirList', 'dir-box', 'dir-item', 'editShare',
                         'dirPathInput', 'dirGroup', 'dir-group', 'dirError'):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, HTML)
                self.assertNotIn(leftover, JS)
                self.assertNotIn(leftover, CSS)


if __name__ == '__main__':
    unittest.main()
