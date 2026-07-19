#!/usr/bin/env python3
"""Winscope 一键会话导出页面和启动入口的静态行为约束。"""

import pathlib
import re
import subprocess
import tempfile
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

    def test_webui_recovers_only_verified_winscope_loopback_listeners(self):
        self.assertIn('ss -ltnp4 "sport = :$port"', self.script)
        self.assertIn('"/proc/${pid}/cmdline"', self.script)
        self.assertIn('[[ "${cmdline_args[1]}" == "-m" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[2]}" == "http.server" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[3]}" == "$ui_port" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[4]}" == "--bind" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[5]}" == "127.0.0.1" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[6]}" == "--directory" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[1]}" == "$proxy_path" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[2]}" == "-p" ]]', self.script)
        self.assertIn("read -r -d '' arg", self.script)
        self.assertIn('local -a cmdline_args=("$@")', self.script)
        self.assertIn('interpreter="${cmdline_args[0]##*/}"', self.script)
        self.assertIn('[[ "$interpreter" == "python3" || "$interpreter" == "python" ]]', self.script)
        self.assertIn('[[ "${#cmdline_args[@]}" -eq 8 ]]', self.script)
        self.assertIn('[[ "${#cmdline_args[@]}" -eq 4 ]]', self.script)
        self.assertIn('[[ "${cmdline_args[7]}" == "$ui_dir" ]]', self.script)
        self.assertIn('[[ "${cmdline_args[3]}" == "$proxy_port" ]]', self.script)
        self.assertNotIn('[[ " $cmdline " ==', self.script)
        self.assertRegex(
            self.script,
            r'collect_loopback_listener_pids "\$port"[\s\S]*verify_listener_commands[\s\S]*send_verified_signal "\$role" "\$pid" 15 ""')
        self.assertRegex(
            self.script,
            r'send_verified_signal "\$role" "\$pid" 15 ""[\s\S]*collect_loopback_listener_pids "\$port"[\s\S]*verify_listener_commands[\s\S]*send_verified_signal "\$role" "\$pid" 9 ""')
        self.assertIn('recover_winscope_port "$ui_port" ui', self.script)
        self.assertIn('recover_winscope_port "$proxy_port" proxy', self.script)
        self.assertRegex(
            self.script,
            r'\[\[ "\$ui_port" != "\$proxy_port" \]\] \|\| error "UI 与 Proxy 端口不能相同: \$ui_port"')

    def test_webui_cleanup_revalidates_owned_children_before_signals(self):
        self.assertIn('read_cmdline_args()', self.script)
        self.assertIn('"/proc/${pid}/status"', self.script)
        self.assertIn('[[ "$parent_pid" == "$$" ]]', self.script)
        self.assertIn("if ! while IFS= read -r -d '' arg; do", self.script)
        self.assertIn('if ! while read -r key value; do', self.script)
        self.assertIn('cleanup_owned_process proxy "$proxy_pid"', self.script)
        self.assertIn('cleanup_owned_process ui "$webui_pid"', self.script)
        self.assertRegex(
            self.script,
            r'cleanup_owned_process\(\) \{[\s\S]*send_verified_signal "\$role" "\$pid" 15 "\$\$"[\s\S]*send_verified_signal "\$role" "\$pid" 9 "\$\$"')
        self.assertRegex(
            self.script,
            r'wait "\$proxy_pid"[\s\S]*check_shutdown[\s\S]*proxy_pid=""')

    def test_webui_sends_signals_only_through_pidfd(self):
        self.assertIn('send_verified_signal()', self.script)
        self.assertIn('os.pidfd_open(pid)', self.script)
        self.assertIn('signal.pidfd_send_signal(pidfd, signum)', self.script)
        self.assertIn("open(f'/proc/{pid}/cmdline', 'rb')", self.script)
        self.assertIn("if parent_pid is not None:", self.script)
        self.assertIn("line.startswith(b'PPid:')", self.script)
        self.assertNotIn('kill -TERM "$pid"', self.script)
        self.assertNotIn('kill -KILL "$pid"', self.script)

    def test_webui_defers_shutdown_and_preserves_proxy_stop_grace_period(self):
        self.assertIn('trap cleanup EXIT', self.script)
        self.assertIn("trap 'request_shutdown 130' INT", self.script)
        self.assertIn("trap 'request_shutdown 143' TERM", self.script)
        self.assertNotIn('trap cleanup INT TERM', self.script)
        self.assertIn('check_shutdown()', self.script)
        self.assertGreaterEqual(self.script.count('check_shutdown'), 7)
        self.assertRegex(
            self.script,
            r'python3 -m http\.server[\s\S]*&\nwebui_pid=\$!\ncheck_shutdown')
        self.assertRegex(
            self.script,
            r'WINSCOPE_EXPORT_DIR=.* &\nproxy_pid=\$!\ncheck_shutdown')
        self.assertIn('wait_for_process_exit "$role" "$pid"', self.script)
        self.assertIn('wait_for_port_free "$port" "$role"', self.script)
        self.assertRegex(
            self.script,
            r'proxy\)\n\s+max_attempts=350')
        self.assertRegex(
            self.script,
            r'ui\)\n\s+max_attempts=50')

    def test_sourcing_launcher_runs_child_without_exiting_parent_shell(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'. "{WEBUI_SCRIPT}" --help; printf "source-shell-alive\\n"',
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("用法: winscope_webui.sh", result.stdout)
        self.assertIn("source-shell-alive", result.stdout)

    def test_sourcing_launcher_ignores_only_child_interrupt_exit_with_errexit(self):
        self.assertRegex(
            self.script,
            r'if "\$launcher_path" "\$@"; then\n'
            r'\s+return 0\n'
            r'\s+else\n'
            r'\s+launcher_status=\$\?\n'
            r'\s+if \[\[ "\$launcher_status" -eq 130 \]\]; then\n'
            r'\s+return 0\n'
            r'\s+fi\n'
            r'\s+return "\$launcher_status"\n'
            r'\s+fi')

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            launcher_path = temporary_path / "launcher.sh"
            source_guard_path = temporary_path / "source_guard.sh"
            launcher_path.write_text("#!/usr/bin/env bash\nexit \"$1\"\n", encoding="utf-8")
            launcher_path.chmod(0o700)
            source_guard_path.write_text(
                "#!/usr/bin/env bash\n"
                "launcher_path=" + repr(str(launcher_path)) + "\n"
                "if \"$launcher_path\" \"$@\"; then\n"
                "    return 0\n"
                "else\n"
                "    launcher_status=$?\n"
                "    if [[ \"$launcher_status\" -eq 130 ]]; then\n"
                "        return 0\n"
                "    fi\n"
                "    return \"$launcher_status\"\n"
                "fi\n",
                encoding="utf-8")

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'set -e; . "{source_guard_path}" 130; printf "source-shell-alive\\n"',
                ],
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "source-shell-alive\n")


if __name__ == "__main__":
    unittest.main()
