#!/usr/bin/env python3
"""winscope_export.sh 的目录导出行为测试。"""

import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPORT_SCRIPT = REPO_ROOT / "winscope_export.sh"
SESSION_DIR = "/data/local/tmp/last_winscope_tracing_session"
DEFAULT_STAGE_PATH = "/data/local/tmp/.winscope-export.AbCd12"


class WinscopeExportTest(unittest.TestCase):
    def write_executable(self, path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def make_fake_adb(self, directory):
        fake_adb = directory / "fake-adb"
        self.write_executable(
            fake_adb,
            """#!/usr/bin/env python3
import os
import sys

if sys.argv[1:4] != ["-s", "test-serial", "shell"] or len(sys.argv) != 5:
    raise SystemExit("adb 必须以单个 shell 命令列出目录")
if "ADB_LOG" in os.environ:
    with open(os.environ["ADB_LOG"], "a", encoding="utf-8") as log:
        log.write(sys.argv[4] + "\\n")
exit_code = int(os.environ.get("ADB_EXIT_CODE", "0"))
if exit_code:
    raise SystemExit(exit_code)
if "mktemp -d /data/local/tmp/.winscope-export.XXXXXX" in sys.argv[4]:
    stage_exit_code = int(os.environ.get("ADB_STAGE_EXIT_CODE", "0"))
    if stage_exit_code:
        raise SystemExit(stage_exit_code)
    print("__WINSCOPE_STAGE__\\t" + os.environ.get(
        "STAGE_PATH", "/data/local/tmp/.winscope-export.AbCd12") + "\\t" +
        os.environ.get("STAGE_INODE", "12345"))
    sys.stdout.write(os.environ.get("ADB_STAGE_LISTING", os.environ.get("ADB_LISTING", "")))
    raise SystemExit(0)
if "rm -rf --" in sys.argv[4]:
    raise SystemExit(0)
preflight_exit_code = int(os.environ.get("ADB_PREFLIGHT_EXIT_CODE", "0"))
if preflight_exit_code:
    raise SystemExit(preflight_exit_code)
sys.stdout.write(os.environ.get("ADB_LISTING", ""))
""",
        )
        return fake_adb

    def make_fake_fetch(self, directory):
        fake_fetch = directory / "fake-fetch.py"
        self.write_executable(
            fake_fetch,
            """#!/usr/bin/env python3
import os
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
remote_path = sys.argv[-1]
expected = os.environ.get("EXPECTED_REMOTE_PATH")
if expected and remote_path != expected:
    raise SystemExit("下载了非预期的远端文件: " + remote_path)
expected_max_size_mib = os.environ.get("EXPECTED_MAX_SIZE_MIB")
if expected_max_size_mib:
    try:
        max_size_mib = sys.argv[sys.argv.index("--max-size-mib") + 1]
    except ValueError:
        raise SystemExit("缺少 --max-size-mib")
    if max_size_mib != expected_max_size_mib:
        raise SystemExit("下载上限不匹配: " + max_size_mib)
elif os.environ.get("EXPECT_NO_MAX_SIZE_MIB") and "--max-size-mib" in sys.argv:
    raise SystemExit("不应覆盖单文件下载上限")
if os.environ.get("FETCH_FAIL"):
    raise SystemExit("模拟下载失败")
if os.environ.get("FETCH_MODE") == "oversize":
    with output.open("wb") as destination:
        destination.seek(536870912)
        destination.write(b"x")
else:
    output.write_bytes(os.environ.get("FETCH_CONTENT", "window trace").encode("utf-8"))
""",
        )
        return fake_fetch

    def run_export(self, directory, arguments, **environment):
        fake_adb = self.make_fake_adb(directory)
        fake_fetch = self.make_fake_fetch(directory)
        env = {
            **os.environ,
            "WINSCOPE_ADB": str(fake_adb),
            "WINSCOPE_FETCH_SCRIPT": str(fake_fetch),
            **environment,
        }
        return subprocess.run(
            ["bash", str(EXPORT_SCRIPT), "--serial", "test-serial", *arguments],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def test_session_dir_exports_only_nonempty_direct_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\nempty_trace\t0\n",
                ADB_STAGE_LISTING="window_trace\t12\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/window_trace",
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            root_shell_command = adb_log.read_text(encoding="utf-8")
            self.assertNotIn("find ", root_shell_command)
            self.assertIn('for file in "$dir"/*', root_shell_command)
            self.assertIn('[ -L "$file" ] && continue', root_shell_command)
            self.assertIn('( ulimit -f 1; cp -P -- "$file" "$stage/$name" ) || exit 1', root_shell_command)
            self.assertIn('size=$(wc -c < "$stage/$name") || exit 1', root_shell_command)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["window_trace"])
                self.assertEqual(archive.read("window_trace"), b"window trace")

    def test_session_dir_rejects_any_directory_except_fixed_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"

            result = self.run_export(
                temporary_path,
                ["--session-dir", "/data/misc/wmtrace", "--output", str(output)],
                ADB_LISTING="window_trace\t1\n",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--session-dir 只能是", result.stderr)
            self.assertFalse(output.exists())

    def test_session_dir_exports_hidden_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING=".hidden_trace\t12\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/.hidden_trace",
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            root_shell_command = adb_log.read_text(encoding="utf-8")
            self.assertIn('"$dir"/.[!.]*', root_shell_command)
            self.assertIn('"$dir"/..?*', root_shell_command)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), [".hidden_trace"])
                self.assertEqual(archive.read(".hidden_trace"), b"window trace")

    def test_session_dir_listing_failure_does_not_publish_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"
            output.write_bytes(b"previous archive")

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_EXIT_CODE="7",
                ADB_LOG=str(adb_log),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"previous archive")
            self.assertNotIn("-exec sh -c", adb_log.read_text(encoding="utf-8"))

    def test_session_dir_total_limit_blocks_staging_command(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t536870913\n",
                ADB_LOG=str(adb_log),
            )

            self.assertNotEqual(result.returncode, 0)
            commands = adb_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(commands), 1)
            self.assertNotIn("mktemp -d", commands[0])
            self.assertNotIn("cp -P --", commands[0])

    def test_session_dir_accepts_current_session_size_below_512_mib(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"
            listing = (
                "eventlog\t643261\n"
                "screen_recording_active\t66832705\n"
                "trace.perfetto-trace\t195848644\n"
                "view_capture_trace.zip\t22\n"
                "window_trace\t16512902\n"
            )

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING=listing,
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())
            self.assertIn("mktemp -d", adb_log.read_text(encoding="utf-8"))

    def test_session_dir_raises_per_file_limit_to_512_mib(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="trace.perfetto-trace\t379485621\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/trace.perfetto-trace",
                EXPECTED_MAX_SIZE_MIB="512",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.exists())

    def test_session_dir_rejects_more_than_256_nonempty_files_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"
            output.write_bytes(b"previous archive")
            listing = "".join(f"trace_{index}\t1\n" for index in range(257))

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING=listing,
                ADB_LOG=str(adb_log),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"previous archive")
            commands = adb_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(commands), 1)
            self.assertNotIn("mktemp -d", commands[0])
            self.assertNotIn("cp -P --", commands[0])

    def test_session_dir_rounding_reserve_blocks_staging_at_raw_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="large_trace\t536870911\nsmall_trace\t1\n",
                ADB_LOG=str(adb_log),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("导出文件总大小超过 512 MiB 限制", result.stderr)
            commands = adb_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(commands), 1)
            self.assertNotIn("mktemp -d", commands[0])

    def test_session_dir_stage_copy_limits_each_file_to_preflight_blocks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="tiny_trace\t1\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/tiny_trace",
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ulimit -f 1; cp -P --", adb_log.read_text(encoding="utf-8"))

    def test_session_dir_delayed_cleanup_requires_matching_stage_inode(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/window_trace",
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stage_command = adb_log.read_text(encoding="utf-8")
            self.assertIn("stat -c %i", stage_command)
            self.assertIn('[ -d "$stage" ]', stage_command)
            self.assertIn('"$current_inode" = "$stage_inode"', stage_command)

    def test_session_dir_fallback_cleanup_waits_at_least_one_hour(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/window_trace",
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stage_command = adb_log.read_text(encoding="utf-8")
            self.assertIn("toybox setsid -d", stage_command)
            self.assertIn("sleep 3600", stage_command)

    def test_session_dir_stage_failure_does_not_register_or_clean_unknown_stage(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\n",
                ADB_LOG=str(adb_log),
                ADB_STAGE_EXIT_CODE="7",
            )

            self.assertNotEqual(result.returncode, 0)
            commands = adb_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(commands), 2)
            self.assertNotIn("mktemp -d", commands[0])
            self.assertIn("mktemp -d", commands[1])

    def test_session_dir_fetches_from_root_owned_staging_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"
            stage_path = "/data/local/tmp/.winscope-export.AbCd12"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\n",
                STAGE_PATH=stage_path,
                EXPECTED_REMOTE_PATH=f"{stage_path}/window_trace",
                ADB_LOG=str(adb_log),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            root_shell_commands = adb_log.read_text(encoding="utf-8")
            self.assertIn("mktemp -d /data/local/tmp/.winscope-export.XXXXXX", root_shell_commands)
            self.assertIn("cp -P --", root_shell_commands)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["window_trace"])

    def test_session_dir_fetch_failure_cleans_staging_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"
            output.write_bytes(b"previous archive")

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\n",
                ADB_LOG=str(adb_log),
                FETCH_FAIL="1",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"previous archive")
            root_shell_commands = adb_log.read_text(encoding="utf-8")
            self.assertIn("rm -rf --", root_shell_commands)
            self.assertIn("trap", root_shell_commands)
            self.assertIn("sleep 3600", root_shell_commands)

    def test_session_dir_local_cleanup_requires_matching_stage_inode(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            adb_log = temporary_path / "adb.log"
            output.write_bytes(b"previous archive")

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t12\n",
                ADB_LOG=str(adb_log),
                FETCH_FAIL="1",
                STAGE_INODE="12345",
            )

            self.assertNotEqual(result.returncode, 0)
            cleanup_command = adb_log.read_text(encoding="utf-8").splitlines()[-1]
            self.assertIn("[ -d ", cleanup_command)
            self.assertIn("current_inode=$(stat -c %i", cleanup_command)
            self.assertIn("expected_inode=12345", cleanup_command)
            self.assertIn('"$current_inode" = "$expected_inode"', cleanup_command)
            self.assertIn("rm -rf --", cleanup_command)

    def test_session_dir_keeps_remote_archive_zip_as_a_trace_entry(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="archive.zip\t12\n",
                EXPECTED_REMOTE_PATH=f"{DEFAULT_STAGE_PATH}/archive.zip",
                FETCH_CONTENT="remote archive content",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["archive.zip"])
                self.assertEqual(archive.read("archive.zip"), b"remote archive content")

    def test_session_dir_rejects_downloaded_total_over_limit_without_replacing_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"
            output.write_bytes(b"previous archive")
            fake_bin = temporary_path / "fake-bin"
            fake_bin.mkdir()
            self.write_executable(
                fake_bin / "python3",
                f"""#!{sys.executable}
import os
import pathlib
import sys

if len(sys.argv) > 1 and sys.argv[1] == "-":
    temporary_archive = pathlib.Path(sys.argv[2])
    output = pathlib.Path(sys.argv[3])
    temporary_archive.write_bytes(b"replacement archive")
    os.replace(temporary_archive, output)
    raise SystemExit(0)
os.execv(os.environ["REAL_PYTHON"], [os.environ["REAL_PYTHON"], *sys.argv[1:]])
""",
            )

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="window_trace\t1\n",
                FETCH_MODE="oversize",
                REAL_PYTHON=sys.executable,
                PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("下载后文件总大小超过 512 MiB 限制", result.stderr)
            self.assertEqual(output.read_bytes(), b"previous archive")

    def test_session_dir_rejects_dangerous_filename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"

            result = self.run_export(
                temporary_path,
                ["--session-dir", SESSION_DIR, "--output", str(output)],
                ADB_LISTING="../unsafe\t1\n",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("最终文件名不安全", result.stderr)
            self.assertFalse(output.exists())

    def test_session_dir_rejects_empty_or_only_zero_byte_files(self):
        for listing in ("", "empty_trace\t0\n"):
            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as temporary_directory:
                temporary_path = pathlib.Path(temporary_directory)
                output = temporary_path / "traces.zip"

                result = self.run_export(
                    temporary_path,
                    ["--session-dir", SESSION_DIR, "--output", str(output)],
                    ADB_LISTING=listing,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("目录中没有可导出的非空常规文件", result.stderr)
                self.assertFalse(output.exists())

    def test_session_dir_and_remote_path_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = pathlib.Path(temporary_directory) / "traces.zip"

            result = self.run_export(
                pathlib.Path(temporary_directory),
                [
                    "--session-dir",
                    SESSION_DIR,
                    "--remote-path",
                    "/data/local/tmp/window_trace",
                    "--output",
                    str(output),
                ],
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--remote-path 与 --session-dir 不能同时指定", result.stderr)

    def test_remote_path_mode_remains_compatible(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"

            result = self.run_export(
                temporary_path,
                ["--remote-path", "/data/local/tmp/window_trace", "--output", str(output)],
                EXPECTED_REMOTE_PATH="/data/local/tmp/window_trace",
                EXPECT_NO_MAX_SIZE_MIB="1",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["window_trace"])
                self.assertEqual(archive.read("window_trace"), b"window trace")

    def test_remote_path_archive_zip_remains_a_trace_entry(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output = temporary_path / "traces.zip"

            result = self.run_export(
                temporary_path,
                ["--remote-path", "/data/local/tmp/archive.zip", "--output", str(output)],
                EXPECTED_REMOTE_PATH="/data/local/tmp/archive.zip",
                FETCH_CONTENT="remote archive content",
                EXPECT_NO_MAX_SIZE_MIB="1",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["archive.zip"])
                self.assertEqual(archive.read("archive.zip"), b"remote archive content")


if __name__ == "__main__":
    unittest.main()
