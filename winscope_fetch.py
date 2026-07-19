#!/usr/bin/env python3
"""通过 ADB 可靠导出大型 Winscope 文件，支持中断恢复与 SHA-256 校验。"""

import argparse
import hashlib
import json
import os
import pathlib
import select
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass


DEFAULT_MAX_SIZE_BYTES = 200 * 1024 * 1024
DEFAULT_IDLE_TIMEOUT_S = 30
COPY_BUFFER_BYTES = 1024 * 1024


class TransferError(RuntimeError):
    """表示远端元数据、传输或完整性校验失败。"""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    sha256: str


def part_path(output: pathlib.Path) -> pathlib.Path:
    return output.with_name(output.name + ".part")


def metadata_path(output: pathlib.Path) -> pathlib.Path:
    return output.with_name(output.name + ".part.json")


def save_resume_metadata(output: pathlib.Path, remote: RemoteFile) -> None:
    metadata = metadata_path(output)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=metadata.parent, prefix=metadata.name + ".", delete=False
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), 0o600)
            json.dump(asdict(remote), temporary, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, metadata)
        os.chmod(metadata, 0o600)
    finally:
        if temporary_name:
            pathlib.Path(temporary_name).unlink(missing_ok=True)


def load_resume_metadata(output: pathlib.Path) -> RemoteFile | None:
    try:
        return RemoteFile(**json.loads(metadata_path(output).read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


def prepare_resume(output: pathlib.Path, remote: RemoteFile) -> int:
    partial = part_path(output)
    saved = load_resume_metadata(output)
    if partial.exists() and saved == remote and partial.stat().st_size <= remote.size:
        return partial.stat().st_size
    partial.unlink(missing_ok=True)
    metadata_path(output).unlink(missing_ok=True)
    return 0


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(COPY_BUFFER_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def verify_and_finalize(output: pathlib.Path, remote: RemoteFile) -> None:
    partial = part_path(output)
    if not partial.exists() or partial.stat().st_size != remote.size:
        raise TransferError("下载文件大小与远端元数据不一致")
    actual = sha256_file(partial)
    if actual != remote.sha256:
        raise TransferError("下载文件 SHA-256 与远端文件不一致")
    os.replace(partial, output)
    metadata_path(output).unlink(missing_ok=True)


def root_shell(command: str) -> str:
    return "su root sh -c " + shlex.quote(command)


def dd_command(remote_path: str, offset: int) -> str:
    return "dd if={} iflag=skip_bytes skip={} bs={} status=none".format(
        shlex.quote(remote_path), offset, COPY_BUFFER_BYTES
    )


def resume_dd_command(remote_path: str, offset: int) -> str:
    return root_shell(dd_command(remote_path, offset))


def adb_exec_out_args(serial: str, remote_path: str, offset: int) -> list[str]:
    return [
        "adb",
        "-s",
        serial,
        "exec-out",
        resume_dd_command(remote_path, offset),
    ]


def adb_shell(serial: str, command: str) -> str:
    result = subprocess.run(
        ["adb", "-s", serial, "shell", command],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise TransferError("ADB 命令失败: {}".format(output))
    return output


def remote_file_size(serial: str, remote_path: str) -> int:
    size_text = adb_shell(
        serial, root_shell("stat -c %s {}".format(shlex.quote(remote_path)))
    )
    try:
        return int(size_text)
    except ValueError as error:
        raise TransferError("无法读取远端文件大小") from error


def remote_file_metadata(serial: str, remote_path: str, size: int) -> RemoteFile:
    hash_text = adb_shell(
        serial, root_shell("sha256sum {}".format(shlex.quote(remote_path)))
    )
    try:
        sha256 = hash_text.split()[0].lower()
    except IndexError as error:
        raise TransferError("无法读取远端文件元数据") from error
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise TransferError("远端 SHA-256 格式无效")
    return RemoteFile(remote_path, size, sha256)


def copy_from_device(
    serial: str,
    remote: RemoteFile,
    output: pathlib.Path,
    offset: int,
    idle_timeout_s: int,
) -> None:
    if offset == remote.size:
        return
    process = subprocess.Popen(
        adb_exec_out_args(serial, remote.path, offset),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise TransferError("无法读取 ADB 输出流")

    written = offset
    stderr = bytearray()
    streams = [process.stdout, process.stderr]
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(part_path(output), flags, 0o600)
        with os.fdopen(descriptor, "ab") as destination:
            os.fchmod(destination.fileno(), 0o600)
            while streams:
                readable, _, _ = select.select(streams, [], [], idle_timeout_s)
                if not readable:
                    raise TransferError("ADB 文件传输连续 {} 秒无数据".format(idle_timeout_s))
                for stream in readable:
                    chunk = os.read(stream.fileno(), COPY_BUFFER_BYTES)
                    if not chunk:
                        streams.remove(stream)
                        continue
                    if stream is process.stdout:
                        written += len(chunk)
                        if written > remote.size:
                            raise TransferError("远端文件在传输期间变大")
                        destination.write(chunk)
                    elif len(stderr) < 64 * 1024:
                        stderr.extend(chunk[: 64 * 1024 - len(stderr)])
        return_code = process.wait(timeout=idle_timeout_s)
        if return_code != 0:
            raise TransferError(
                "ADB 文件传输失败: {}".format(stderr.decode("utf-8", errors="replace"))
            )
    except (KeyboardInterrupt, TransferError):
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TransferError("本机文件或 ADB 管道错误: {}".format(error)) from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def default_output(remote_path: str) -> pathlib.Path:
    return pathlib.Path("runtime/downloads") / pathlib.PurePosixPath(remote_path).name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("remote_path", help="设备侧文件路径")
    parser.add_argument("--serial", required=True, help="adb 设备序列号")
    parser.add_argument("--output", type=pathlib.Path, help="本机目标文件")
    parser.add_argument(
        "--max-size-mib", type=int, default=200, help="允许的最大文件大小，默认 200"
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT_S, help="无数据超时秒数"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_size_mib <= 0 or args.idle_timeout <= 0:
        raise TransferError("文件大小和空闲超时必须为正数")
    size = remote_file_size(args.serial, args.remote_path)
    max_size = args.max_size_mib * 1024 * 1024
    if size > max_size:
        raise TransferError("远端文件 {} MiB 超过 {} MiB 限制".format(
            size // (1024 * 1024), args.max_size_mib))
    remote = remote_file_metadata(args.serial, args.remote_path, size)

    output = args.output or default_output(remote.path)
    output.parent.mkdir(parents=True, exist_ok=True)
    offset = prepare_resume(output, remote)
    save_resume_metadata(output, remote)
    print("下载 {}，从偏移 {} / {} 恢复".format(remote.path, offset, remote.size))
    copy_from_device(args.serial, remote, output, offset, args.idle_timeout)
    verify_and_finalize(output, remote)
    print("下载完成: {}".format(output.resolve()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransferError as error:
        print("错误: {}".format(error), file=sys.stderr)
        raise SystemExit(1)
