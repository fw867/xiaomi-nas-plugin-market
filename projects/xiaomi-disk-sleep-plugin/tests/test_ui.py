"""页面与接口的静态检查：资源引用、占位符替换、移动端要点、活动面板接线。"""
from __future__ import annotations

import http.client
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import server

WEB = Path(__file__).resolve().parents[1] / 'web'


class AssetTests(unittest.TestCase):
    def test_html_references_existing_files(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js|png))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((WEB / name).is_file(), name)

    def test_activity_panel_ids_match_script(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        for name in ('activityPanel', 'activityDot', 'activityNotes', 'activityMounts',
                     'activityWriters', 'activityEvents', 'refreshActivity'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
                self.assertIn("$('%s')" % name, script)

    def test_activity_is_a_tab_of_the_log_card(self):
        """「谁在写盘」并进日志卡片的标签页，不再单独占一张卡片。"""
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('id="activityCard"', html)
        log_card = html.split('id="logCard"')[1].split('</details>')[0]
        self.assertIn('data-tab="activity"', log_card)
        self.assertIn('id="activityPanel"', log_card)
        self.assertIn('data-tab="events"', log_card)
        self.assertIn('data-tab="hdidle"', log_card)

    def test_script_calls_activity_api_relative(self):
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertIn("'activity?force=1'", script)
        self.assertIn(": 'activity'", script)

    def test_paths_are_rendered_as_text_not_html(self):
        """文件名来自文件系统，可能带 < > &，必须用 textContent 而不是 innerHTML。"""
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        file_renderer = script.split('function renderFiles')[1].split('function renderActivity')[0]
        self.assertNotIn('innerHTML', file_renderer)
        self.assertIn('textContent', script)

    def test_mobile_essentials(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        css = (WEB / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('viewport-fit=cover', html)
        self.assertIn('--safe-bottom: env(safe-area-inset-bottom', css)
        self.assertIn('--tap: 44px', css)
        self.assertIn('min-height: var(--tap)', css)
        self.assertIn('font-size: 16px', css)          # 输入框 ≥16px，iOS 不放大
        self.assertIn('touch-action: manipulation', css)
        self.assertIn('prefers-color-scheme: dark', css)

    def test_version_is_filled_from_api_not_placeholder(self):
        """nginx 直接 alias 出 index.html，服务端没机会替换占位符。

        所以页面不能依赖 __PLUGIN_VERSION__，必须由脚本用 /api/status 的
        version 字段填到 id="version" 里。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('__PLUGIN_VERSION__', html)
        self.assertIn('id="version"', html)
        self.assertIn("$('version').textContent", script)

    def test_card_text_is_minimal(self):
        """卡片上不放长说明；说明集中在最下面的「说明与限制」里。"""
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        cards = html.split('<details class="card fold-card" id="notes">')[0]
        self.assertNotIn('drop-in', cards)
        self.assertNotIn('hdparm', cards)
        notes = html.split('<details class="card fold-card" id="notes">')[1]
        for phrase in ('drop-in', 'hdparm', 'CONFIG_TASK_IO_ACCOUNTING'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, notes)

    def test_presets_were_removed(self):
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        script = (WEB / 'app.js').read_text(encoding='utf-8')
        css = (WEB / 'styles.css').read_text(encoding='utf-8')
        self.assertNotIn('presets', html)
        self.assertNotIn("$('presets')", script)
        self.assertNotIn('.preset', css)


class ServeTests(unittest.TestCase):
    """页脚必须显示真实版本，而不是 __PLUGIN_VERSION__ 占位符。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(server, 'WEB_DIR', WEB)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        def close():
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join()

        self.addCleanup(close)

    def get(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_index_has_no_server_side_placeholder(self):
        status, body = self.get('/')
        text = body.decode('utf-8')
        self.assertEqual(status, 200)
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn('id="version"', text)

    def test_status_reports_installed_version(self):
        # Windows 上没有 uci/systemctl，把这些外部命令打桩后再问状态接口
        import json as _json
        fake = type('R', (), {'returncode': 0, 'stdout': ''})()
        with patch.object(engine, 'run', return_value=fake):
            status, body = self.get('/api/status')
        self.assertEqual(status, 200)
        data = _json.loads(body)
        self.assertEqual(data['version'], engine.installed_version())
        self.assertIn('active', data)

    def test_static_assets_served(self):
        for name in ('styles.css', 'app.js'):
            with self.subTest(name=name):
                status, body = self.get('/' + name)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_activity_endpoint_responds(self):
        status, body = self.get('/api/activity')
        self.assertEqual(status, 200)
        data = __import__('json').loads(body)
        self.assertTrue(data['ok'])
        self.assertIn('mounts', data)
        self.assertIn('notes', data)
        self.assertIn('writers', data)
        self.assertIn('events', data)

    def test_traversal_is_rejected(self):
        status, _ = self.get('/../engine.py')
        self.assertIn(status, (400, 403, 404))


if __name__ == '__main__':
    unittest.main()
