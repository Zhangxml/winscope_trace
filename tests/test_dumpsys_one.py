#!/usr/bin/env python3
"""dumpsys_one.sh 的设备选择和原子采集行为测试。"""

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "dumpsys/dumpsys_one.sh"


class DumpsysOneTest(unittest.TestCase):
    def write_executable(self, path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def make_fake_adb(self, directory):
        fake_bin = directory / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_adb = fake_bin / "adb"
        self.write_executable(
            fake_adb,
            """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["ADB_LOG"], "a", encoding="utf-8") as log:
    log.write("\\x1f".join(args) + "\\n")

if args == ["devices"]:
    print("List of devices attached")
    sys.stdout.write(os.environ.get("ADB_DEVICES", "test-serial\\tdevice\\n"))
    raise SystemExit(int(os.environ.get("ADB_DEVICES_EXIT_CODE", "0")))

if len(args) < 3 or args[0] != "-s":
    raise SystemExit("设备命令缺少 -s serial")

command = " ".join(args[2:])
fail_match = os.environ.get("ADB_FAIL_MATCH")
if fail_match and fail_match in command:
    print("模拟 ADB 失败", file=sys.stderr)
    raise SystemExit(1)

if args[2:] == ["root"]:
    print("restarting adbd as root")
elif args[2:] == ["wait-for-device"]:
    pass
elif args[2:] == ["shell", "dumpsys", "SurfaceFlinger", "--display-id"]:
    sys.stdout.write(os.environ.get(
        "ADB_DISPLAY_LISTING",
        'Display 4619827259835644672 (HWC display 0): port=0 displayName="main"\\n',
    ))
elif args[2:4] == ["shell", "dumpsys"]:
    sys.stdout.write("dumpsys data\\n")
elif args[2:5] == ["exec-out", "screencap", "-p"]:
    if os.environ.get("ADB_INVALID_PNG"):
        sys.stdout.buffer.write(b"not a png")
    else:
        sys.stdout.buffer.write(b"\\x89PNG\\r\\n\\x1a\\nimage")
else:
    raise SystemExit("未知 ADB 命令: " + command)
""",
        )
        fake_mv = fake_bin / "mv"
        self.write_executable(
            fake_mv,
            """#!/bin/bash
if [[ -n "${FAIL_MV:-}" ]]; then
    echo "模拟 mv 失败" >&2
    exit 1
fi
exec /bin/mv "$@"
""",
        )
        return fake_bin

    def run_script(self, directory, arguments, **environment):
        fake_bin = self.make_fake_adb(directory)
        adb_log = directory / "adb.log"
        env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "ADB_LOG": str(adb_log),
            **environment,
        }
        try:
            result = subprocess.run(
                ["bash", str(SCRIPT), *arguments],
                cwd=directory,
                env=env,
                input="",
                text=True,
                capture_output=True,
                timeout=2,
            )
            return result, adb_log, False
        except subprocess.TimeoutExpired as error:
            result = subprocess.CompletedProcess(
                error.cmd,
                124,
                error.stdout or "",
                error.stderr or "",
            )
            return result, adb_log, True

    def test_without_serial_rejects_multiple_authorized_devices(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            result, _, timed_out = self.run_script(
                temporary_path,
                [],
                ADB_DEVICES="serial-one\tdevice\nserial-two\tdevice\n",
            )

            self.assertFalse(timed_out, "多设备场景不应进入采集循环")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("多个已授权设备", result.stderr)

    def test_explicit_serial_is_used_and_each_run_has_unique_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output_root = temporary_path / "output"

            first, first_log, first_timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
            )
            second, second_log, second_timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
            )

            self.assertFalse(first_timed_out)
            self.assertFalse(second_timed_out)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            run_directories = sorted(path for path in output_root.iterdir() if path.is_dir())
            self.assertEqual(len(run_directories), 2)
            self.assertNotEqual(run_directories[0], run_directories[1])
            for run_directory in run_directories:
                round_directories = list(run_directory.glob("round_*"))
                self.assertEqual(len(round_directories), 1)
                names = {path.name for path in round_directories[0].iterdir()}
                self.assertEqual(
                    names,
                    {
                        "activity.txt",
                        "window.txt",
                        "SurfaceFlinger.txt",
                        "display.txt",
                        "input.txt",
                        "screen_0.png",
                    },
                )
            for adb_log in (first_log, second_log):
                commands = adb_log.read_text(encoding="utf-8").splitlines()
                self.assertTrue(commands)
                self.assertTrue(
                    all(command.startswith("-s\x1ftarget-serial\x1f") for command in commands),
                    commands,
                )

    def test_failed_item_is_not_published_and_round_is_not_reported_complete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output_root = temporary_path / "output"
            result, adb_log, timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
                ADB_FAIL_MATCH="shell dumpsys window windows",
            )

            self.assertFalse(timed_out)
            self.assertNotEqual(result.returncode, 0)
            round_directory = next(output_root.glob("*/round_*"))
            self.assertTrue((round_directory / "activity.txt").is_file())
            self.assertFalse((round_directory / "window.txt").exists())
            self.assertFalse(any(round_directory.glob("*.part")))
            self.assertNotIn("本轮数据收集完成", result.stdout)
            self.assertIn("本轮数据收集失败", result.stderr)
            commands = adb_log.read_text(encoding="utf-8")
            self.assertIn(
                "-s\x1ftarget-serial\x1fexec-out\x1fscreencap\x1f-p\x1f-d\x1f4619827259835644672",
                commands,
            )
            self.assertNotIn("/data/local/tmp/screen.png", commands)

    def test_stdin_eof_exits_after_one_round(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            result, _, timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(temporary_path / "output")],
            )

            self.assertFalse(timed_out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("输入已关闭，停止收集", result.stdout)

    def test_adb_devices_failure_is_reported_before_collection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            result, adb_log, timed_out = self.run_script(
                temporary_path,
                ["--output", str(temporary_path / "output")],
                ADB_DEVICES="partial-serial\tdevice\n",
                ADB_DEVICES_EXIT_CODE="1",
            )

            self.assertFalse(timed_out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("获取设备列表失败", result.stderr)
            self.assertEqual(adb_log.read_text(encoding="utf-8").splitlines(), ["devices"])

    def test_publish_failure_does_not_report_success_or_leave_part_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output_root = temporary_path / "output"
            result, _, timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
                FAIL_MV="1",
            )

            self.assertFalse(timed_out)
            self.assertNotEqual(result.returncode, 0)
            round_directory = next(output_root.glob("*/round_*"))
            self.assertFalse(any(round_directory.iterdir()))
            self.assertNotIn("本轮数据收集完成", result.stdout)
            self.assertIn("本轮数据收集失败", result.stderr)

    def test_invalid_screenshot_is_not_published(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output_root = temporary_path / "output"
            result, _, timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
                ADB_INVALID_PNG="1",
            )

            self.assertFalse(timed_out)
            self.assertNotEqual(result.returncode, 0)
            round_directory = next(output_root.glob("*/round_*"))
            self.assertFalse((round_directory / "screen_0.png").exists())
            self.assertFalse((round_directory / "screen_0.png.part").exists())
            self.assertIn("截图格式无效", result.stderr)

    def test_every_surfaceflinger_display_is_captured_separately(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output_root = temporary_path / "output"
            result, adb_log, timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
                ADB_DISPLAY_LISTING=(
                    'Display 111 (HWC display 0): port=0 displayName="main"\n'
                    'Display 222 (HWC display 1): port=1 displayName="cluster"\n'
                    'Display 111 (duplicate line)\n'
                ),
            )

            self.assertFalse(timed_out)
            self.assertEqual(result.returncode, 0, result.stderr)
            round_directory = next(output_root.glob("*/round_*"))
            self.assertTrue((round_directory / "screen_0.png").is_file())
            self.assertTrue((round_directory / "screen_1.png").is_file())
            self.assertFalse((round_directory / "screen_2.png").exists())
            commands = adb_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [command for command in commands if "\x1fexec-out\x1fscreencap\x1f" in command],
                [
                    "-s\x1ftarget-serial\x1fexec-out\x1fscreencap\x1f-p\x1f-d\x1f111",
                    "-s\x1ftarget-serial\x1fexec-out\x1fscreencap\x1f-p\x1f-d\x1f222",
                ],
            )

    def test_display_enumeration_failure_falls_back_but_marks_round_failed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            output_root = temporary_path / "output"
            result, adb_log, timed_out = self.run_script(
                temporary_path,
                ["--serial", "target-serial", "--output", str(output_root)],
                ADB_FAIL_MATCH="shell dumpsys SurfaceFlinger --display-id",
            )

            self.assertFalse(timed_out)
            self.assertNotEqual(result.returncode, 0)
            round_directory = next(output_root.glob("*/round_*"))
            self.assertTrue((round_directory / "screen_0.png").is_file())
            self.assertIn("无法枚举物理显示屏", result.stderr)
            self.assertIn("本轮数据收集失败", result.stderr)
            commands = adb_log.read_text(encoding="utf-8")
            self.assertIn(
                "-s\x1ftarget-serial\x1fexec-out\x1fscreencap\x1f-p\n",
                commands,
            )


if __name__ == "__main__":
    unittest.main()
