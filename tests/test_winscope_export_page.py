#!/usr/bin/env python3
"""Winscope 一键会话导出页面和启动入口的静态行为约束。"""

import pathlib
import re
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPORT_PAGE = REPO_ROOT / "vendor/winscope-ui/winscope-export.html"
WEBUI_SCRIPT = REPO_ROOT / "winscope_webui.sh"


class WinscopeExportPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = EXPORT_PAGE.read_text(encoding="utf-8")
        cls.script = WEBUI_SCRIPT.read_text(encoding="utf-8")

    def test_page_only_collects_token_and_discovers_authorized_devices(self):
        self.assertNotIn('id="proxy-url"', self.page)
        self.assertNotIn('id="serial"', self.page)
        self.assertNotIn('id="remote-path"', self.page)
        self.assertEqual(len(re.findall(r"<input\b", self.page)), 1)
        self.assertIn('id="token"', self.page)
        self.assertIn("'/devices'", self.page)
        self.assertIn("authorized === true", self.page)

    def test_page_uses_strict_proxy_port_from_query_and_rejects_invalid_value(self):
        self.assertIn("new URLSearchParams(window.location.search)", self.page)
        self.assertIn("get('proxyPort')", self.page)
        self.assertRegex(self.page, r"\^\[1-9\]\[0-9\]\*\$")
        self.assertIn("Number(port) > 65535", self.page)
        self.assertIn("http://127.0.0.1:${port}", self.page)
        self.assertIn("Proxy 端口配置错误", self.page)

    def test_page_exports_one_device_or_requires_selection_for_many_devices(self):
        self.assertIn("'/exportsession/' + encodeURIComponent(device.id)", self.page)
        self.assertIn("devices.length === 1", self.page)
        self.assertIn("document.createElement('select')", self.page)
        self.assertIn("请选择要导出的设备", self.page)
        self.assertIn("redirect: 'error'", self.page)
        self.assertIn("downloadUrl.startsWith('/download/')", self.page)

    def test_webui_prints_export_link_with_proxy_port_and_normalizes_root(self):
        self.assertIn(
            'http://127.0.0.1:%s/winscope-export.html?proxyPort=%s', self.script)
        self.assertRegex(
            self.script,
            r'\[\[ -d "\$install_root" \]\] \|\| error "--root 不是目录: \$install_root"')
        self.assertRegex(
            self.script,
            r'install_root="\$\(cd -- "\$install_root" && pwd -P\)"')


if __name__ == "__main__":
    unittest.main()
