#!/usr/bin/python3

# Copyright (C) 2019 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# This is an ADB proxy for Winscope.
#
# Requirements: python3.10 and ADB installed and in system PATH.
#
# Usage:
#     run: python3 winscope_proxy.py
#

import argparse
import base64
import gzip
import io
import json
import logging
import os
import re
import secrets
import select
import shutil
import stat
import signal
import socket
import subprocess
import sys
import threading
import time
from abc import abstractmethod
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging import DEBUG, INFO
from tempfile import NamedTemporaryFile, mkdtemp

version = sys.version_info
assert version.major == 3 and version.minor >= 10, "This script requires Python 3.10+ and ADB installed and in system PATH."

# GLOBALS #

log: logging.Logger = logging.getLogger("winscope_proxy")
secret_token = ""

# Keep in sync with winscope_proxy_utils VERSION in Winscope
VERSION = '6.0.0'

WINSCOPE_VERSION_HEADER = "Winscope-Proxy-Version"
WINSCOPE_TOKEN_HEADER = "Winscope-Token"

# 保存代理安全 Token 的位置；standalone 启动脚本通过环境变量收拢到工作区。
WINSCOPE_TOKEN_LOCATION = os.environ.get(
    'WINSCOPE_TOKEN_LOCATION',
    os.path.expanduser('~/.config/winscope/.token'),
)

# Max interval between the client keep-alive requests in seconds
KEEP_ALIVE_INTERVAL_S = 30

# Perfetto's default timeout for getting an ACK from producer processes is 5s
# We need to be sure that the timeout is longer than that with a good margin.
COMMAND_TIMEOUT_S = 15
MAX_FETCH_SIZE_BYTES = 200 * 1024 * 1024
MAX_CONCURRENT_FETCHES = 1
FETCH_TIMEOUT_S = 10 * 60
FETCH_IDLE_TIMEOUT_S = 30
FETCH_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)
ACTIVE_FETCH_PROCESSES = set()
ACTIVE_FETCH_PROCESSES_LOCK = threading.Lock()

EXPORT_TIMEOUT_S = 15 * 60
EXPORT_TERMINATE_TIMEOUT_S = 5
EXPORT_TTL_S = 10 * 60
MAX_CONCURRENT_EXPORTS = 1
EXPORT_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_EXPORTS)
PENDING_EXPORTS: dict[str, tuple[str, float]] = {}
PENDING_EXPORTS_LOCK = threading.Lock()
PENDING_EXPORT_TIMERS: dict[str, threading.Timer] = {}
ACTIVE_DOWNLOAD_ARCHIVES: set[str] = set()
ACTIVE_EXPORT_PROCESSES: dict[subprocess.Popen, tuple[str, int]] = {}
ACTIVE_EXPORT_PROCESSES_LOCK = threading.Lock()


class Base64JsonWriter(io.RawIOBase):
    """将二进制流编码为可直接嵌入 JSON 字符串的 Base64 数据。"""

    def __init__(self, stream):
        super().__init__()
        self._stream = stream
        self._pending = b''

    def writable(self):
        return True

    def write(self, data):
        payload = self._pending + data
        encoded_length = len(payload) - (len(payload) % 3)
        if encoded_length:
            self._stream.write(base64.b64encode(payload[:encoded_length]))
        self._pending = payload[encoded_length:]
        return len(data)

    def finish(self):
        if self._pending:
            self._stream.write(base64.b64encode(self._pending))
            self._pending = b''

    def flush(self):
        self._stream.flush()


# CONFIG #

def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Proxy for go/winscope', prog='winscope_proxy')

    parser.add_argument('--info', '-i', dest='loglevel', action='store_const', const=INFO)
    parser.add_argument('--port', '-p', default=5544, type=int)

    parser.set_defaults(loglevel=DEBUG)

    return parser

def get_token() -> str:
    """Returns saved proxy security token or creates new one"""
    token_dir = os.path.dirname(WINSCOPE_TOKEN_LOCATION)
    if token_dir:
        os.makedirs(token_dir, mode=0o700, exist_ok=True)
    try:
        with open(WINSCOPE_TOKEN_LOCATION, 'r') as token_file:
            token = token_file.readline().strip()
            if not token:
                raise IOError('Token file is empty')
            os.chmod(WINSCOPE_TOKEN_LOCATION, 0o600)
            log.debug("Loaded proxy token from {}".format(
                WINSCOPE_TOKEN_LOCATION))
            return token
    except IOError:
        token = secrets.token_hex(32)
        try:
            fd = os.open(
                WINSCOPE_TOKEN_LOCATION,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, 'w') as token_file:
                token_file.write(token)
            os.chmod(WINSCOPE_TOKEN_LOCATION, 0o600)
            log.debug("Created proxy token at {}".format(
                WINSCOPE_TOKEN_LOCATION))
        except IOError:
            log.error("Unable to save persistent token to {}".format(
                WINSCOPE_TOKEN_LOCATION))
        return token


class RequestType(Enum):
    GET = 1
    POST = 2
    HEAD = 3

class RequestEndpoint:
    """Request endpoint to use with the RequestRouter."""
    requires_token = True


    @abstractmethod
    def process(self, server, path):
        pass

class AdbError(Exception):
    """Unsuccessful ADB operation"""
    pass

class BadRequest(Exception):
    """Invalid client request"""
    pass

class ExportError(Exception):
    """无法创建或读取导出归档。"""
    pass


class RequestRouter:
    """Handles HTTP request authentication and routing"""

    def __init__(self, handler):
        self.request = handler
        self.endpoints = {}

    def register_endpoint(self, method: RequestType, name: str, endpoint: RequestEndpoint):
        self.endpoints[(method, name)] = endpoint

    def _bad_request(self, error: str):
        log.warning("Bad request: " + error)
        self.request.respond(HTTPStatus.BAD_REQUEST, b"Bad request!\nThis is Winscope ADB proxy.\n\n"
                             + error.encode("utf-8"), 'text/txt')

    def _internal_error(self, error: str):
        log.error("Internal error: " + error)
        self.request.respond(HTTPStatus.INTERNAL_SERVER_ERROR,
                             error.encode("utf-8"), 'text/txt')

    def _bad_token(self):
        log.warning("Bad token")
        self.request.respond(HTTPStatus.FORBIDDEN, b"Bad Winscope authorization token!\nThis is Winscope ADB proxy.\n",
                             'text/txt')

    def process(self, method: RequestType):
        path = [part for part in self.request.path.strip('/').split('/') if part]
        if not path:
            return self._bad_request("No endpoint specified")

        endpoint_name = path[0]
        endpoint = self.endpoints.get((method, endpoint_name))
        if endpoint is None:
            return self._bad_request("Unknown endpoint /{}/".format(endpoint_name))

        if endpoint.requires_token:
            token = self.request.headers.get(WINSCOPE_TOKEN_HEADER)
            if not token or token != secret_token:
                return self._bad_token()

        try:
            return endpoint.process(self.request, path[1:])
        except AdbError as ex:
            return self._internal_error(str(ex))
        except BadRequest as ex:
            return self._bad_request(str(ex))
        except ExportError as ex:
            log.error("Export error: " + str(ex))
            return self._internal_error("Unable to export archive")
        except Exception as ex:
            return self._internal_error(repr(ex))

def call_adb(
    params: str,
    device: str | None = None,
    timeout: int | None = None,
):
    command = ['adb'] + (['-s', device] if device else []) + params.split(' ')
    command_str = ' '.join(command)
    try:
        log.debug("Call: " + command_str)
        return subprocess.check_output(
            command,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        ).decode('utf-8', errors='replace')
    except OSError as ex:
        raise AdbError('OS Error executing adb command: {}\n{}'.format(command_str, repr(ex)))
    except subprocess.TimeoutExpired as ex:
        raise AdbError('Timeout executing adb command: {}\n{}'.format(command_str, repr(ex)))
    except subprocess.CalledProcessError as ex:
        output = ex.output.decode('utf-8', errors='replace') if ex.output else ''
        raise AdbError('Error executing adb command: {}: {}'.format(command_str, output))


def detach_background_command(command: str) -> str:
    return re.sub(r'\s&\s', ' >/dev/null 2>&1 & ', command, count=1)


# ENDPOINTS #

class ListDevicesEndpoint(RequestEndpoint):
    ADB_INFO_RE = re.compile("^([A-Za-z0-9._:\\-]+)\\s+(\\w+)(.*model:(\\w+))?")

    def process(self, server, path):
        lines = list(filter(None, call_adb(
            'devices -l', timeout=COMMAND_TIMEOUT_S).split('\n')))
        devices = []
        for m in [ListDevicesEndpoint.ADB_INFO_RE.match(d) for d in lines[1:]]:
            if m:
                authorized = str(m.group(2)) != 'unauthorized'
                device = {
                    'id': m.group(1),
                    'authorized': authorized,
                    'model': m.group(4).replace('_', ' ') if m.group(4) else '',
                }
                devices.append(device)
        j = json.dumps(devices)
        log.info("Detected devices: " + j)
        server.respond(HTTPStatus.OK, j.encode("utf-8"), "text/json")

class DeviceRequestEndpoint(RequestEndpoint):
    def process(self, server, path):
        if len(path) > 0 and re.fullmatch("[A-Za-z0-9._:\\-]+", path[0]):
            self.process_with_device(server, path[1:], path[0])
        else:
            raise BadRequest("Device id not specified")

    @abstractmethod
    def process_with_device(self, server, path, device_id):
        pass

    def get_request(self, server):
        try:
            length = int(server.headers["Content-Length"])
        except KeyError as err:
            raise BadRequest("Missing Content-Length header\n" + str(err))
        except ValueError as err:
            raise BadRequest("Content length unreadable\n" + str(err))
        try:
            return json.loads(server.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise BadRequest("Request body must be valid JSON") from err

class FetchEndpoint(DeviceRequestEndpoint):
    def process_with_device(self, server, path: list[str], device_id):
        filepath = '/'.join(path)
        log.debug(filepath)
        if not FETCH_SEMAPHORE.acquire(blocking=False):
            raise BadRequest("Too many concurrent fetch requests")
        response_started = False
        try:
            with NamedTemporaryFile() as tmp:
                self._fetch_to_tempfile(filepath, tmp, device_id)
                if hasattr(server, 'connection'):
                    server.connection.settimeout(FETCH_IDLE_TIMEOUT_S)
                server.send_response(HTTPStatus.OK)
                server.send_header('Content-type', 'text/json')
                if hasattr(server, 'add_standard_headers'):
                    server.add_standard_headers()
                else:
                    server.end_headers()
                response_started = True
                server.wfile.write(b'{' + json.dumps(filepath).encode('utf-8') + b':"')
                encoded_output = Base64JsonWriter(server.wfile)
                with gzip.GzipFile(fileobj=encoded_output, mode='wb') as compressed:
                    while chunk := tmp.read(64 * 1024):
                        compressed.write(chunk)
                encoded_output.finish()
                server.wfile.write(b'"}')
        except (BrokenPipeError, ConnectionResetError, socket.timeout) as ex:
            if response_started:
                log.warning("Client disconnected while fetching {}: {}".format(filepath, ex))
                return
            raise AdbError("Unable to fetch {}: {}".format(filepath, ex))
        finally:
            FETCH_SEMAPHORE.release()

    def _fetch_to_tempfile(self, filepath, tmp, device_id):
        log.debug(f"Fetching file {filepath} from device to {tmp.name}")
        self.call_adb_outfile(
            'exec-out su root cat ' + filepath,
            tmp,
            device_id,
            max_bytes=MAX_FETCH_SIZE_BYTES,
            timeout_s=FETCH_TIMEOUT_S,
        )
        tmp.seek(0, os.SEEK_END)
        file_size = tmp.tell()
        if file_size > MAX_FETCH_SIZE_BYTES:
            raise AdbError(
                'Refusing to fetch {} bytes from {}: limit is {} bytes'.format(
                    file_size, filepath, MAX_FETCH_SIZE_BYTES))
        tmp.seek(0)

    def call_adb_outfile(
        self,
        params: str,
        outfile,
        device: str,
        max_bytes: int,
        timeout_s: int,
    ):
        process = None
        try:
            with ACTIVE_FETCH_PROCESSES_LOCK:
                if SHUTTING_DOWN.is_set():
                    raise AdbError("Proxy is shutting down")
                process = subprocess.Popen(
                    ['adb', '-s', device] + params.split(' '),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                ACTIVE_FETCH_PROCESSES.add(process)
            if not process.stdout or not process.stderr:
                raise AdbError('Unable to capture adb output: adb {}'.format(params))

            deadline = time.monotonic() + timeout_s
            total_bytes = 0
            stderr = bytearray()
            streams = [process.stdout, process.stderr]
            while streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise AdbError('Timeout executing adb command: adb {}'.format(params))
                readable, _, _ = select.select(
                    streams, [], [], min(remaining, FETCH_IDLE_TIMEOUT_S))
                if not readable:
                    process.kill()
                    process.wait()
                    raise AdbError('Fetch stalled while executing adb command: adb {}'.format(params))
                for stream in readable:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                    if not chunk:
                        streams.remove(stream)
                        continue
                    if stream is process.stdout:
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            process.kill()
                            process.wait()
                            raise AdbError(
                                'Refusing to fetch more than {} bytes from {}'.format(
                                    max_bytes, params))
                        outfile.write(chunk)
                    elif len(stderr) < 64 * 1024:
                        stderr.extend(chunk[:64 * 1024 - len(stderr)])

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise AdbError('Timeout executing adb command: adb {}'.format(params))
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as ex:
                process.kill()
                process.wait()
                raise AdbError(
                    'Timeout executing adb command: adb {}\n{}'.format(params, repr(ex)))
            outfile.seek(0)
            if process.returncode != 0:
                raise AdbError('Error executing adb command: adb {}\n{}'.format(
                    params, stderr.decode('utf-8', errors='replace')))
        except OSError as ex:
            raise AdbError(
                'Error executing adb command: adb {}\n{}'.format(params, repr(ex)))
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            if process:
                with ACTIVE_FETCH_PROCESSES_LOCK:
                    ACTIVE_FETCH_PROCESSES.discard(process)

def get_export_dir() -> str:
    export_dir = os.environ.get("WINSCOPE_EXPORT_DIR")
    if not export_dir or not os.path.isabs(export_dir):
        raise BadRequest("WINSCOPE_EXPORT_DIR must be an absolute private directory")
    try:
        export_dir = os.path.realpath(export_dir)
        directory_stat = os.stat(export_dir)
    except (OSError, ValueError) as ex:
        raise BadRequest("WINSCOPE_EXPORT_DIR is unavailable") from ex
    if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise BadRequest("WINSCOPE_EXPORT_DIR must be a private directory")
    return export_dir


def _remove_export_archive(archive_path: str) -> None:
    try:
        os.remove(archive_path)
    except FileNotFoundError:
        pass
    except OSError as ex:
        log.warning("Unable to remove exported archive: {}".format(ex))
    try:
        shutil.rmtree(os.path.dirname(archive_path))
    except FileNotFoundError:
        pass
    except OSError as ex:
        log.warning("Unable to remove exported archive directory: {}".format(ex))
    finally:
        with PENDING_EXPORTS_LOCK:
            ACTIVE_DOWNLOAD_ARCHIVES.discard(archive_path)


def _cancel_export_timer(timer) -> None:
    if timer:
        timer.cancel()


def _expire_pending_export(download_id: str, expires_at: float) -> None:
    with PENDING_EXPORTS_LOCK:
        entry = PENDING_EXPORTS.get(download_id)
        if not entry or entry[1] != expires_at:
            return
        archive_path = PENDING_EXPORTS.pop(download_id)[0]
        timer = PENDING_EXPORT_TIMERS.pop(download_id, None)
    _cancel_export_timer(timer)
    _remove_export_archive(archive_path)


def cleanup_expired_exports() -> None:
    now = time.monotonic()
    with PENDING_EXPORTS_LOCK:
        expired_exports = [
            (PENDING_EXPORTS.pop(download_id)[0], PENDING_EXPORT_TIMERS.pop(download_id, None))
            for download_id, (_, expires_at) in list(PENDING_EXPORTS.items())
            if expires_at <= now
        ]
    for archive_path, timer in expired_exports:
        _cancel_export_timer(timer)
        _remove_export_archive(archive_path)


def cleanup_residual_export_artifacts(export_dir: str) -> None:
    with PENDING_EXPORTS_LOCK:
        protected_dirs = {
            os.path.dirname(archive_path)
            for archive_path, _ in PENDING_EXPORTS.values()
        } | {os.path.dirname(archive_path) for archive_path in ACTIVE_DOWNLOAD_ARCHIVES}
    try:
        entries = list(os.scandir(export_dir))
    except OSError as ex:
        log.warning("Unable to inspect export directory: {}".format(ex))
        return
    for entry in entries:
        if entry.name.startswith(".winscope-export-") and entry.path not in protected_dirs:
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
            except OSError as ex:
                log.warning("Unable to remove residual export directory: {}".format(ex))


def register_pending_export(download_id: str, archive_path: os.PathLike[str] | str, expires_at: float) -> None:
    archive_path = os.fspath(archive_path)
    timer = threading.Timer(
        max(0, expires_at - time.monotonic()),
        _expire_pending_export,
        args=(download_id, expires_at),
    )
    timer.daemon = True
    with PENDING_EXPORTS_LOCK:
        old_entry = PENDING_EXPORTS.get(download_id)
        old_timer = PENDING_EXPORT_TIMERS.get(download_id)
        PENDING_EXPORTS[download_id] = (archive_path, expires_at)
        PENDING_EXPORT_TIMERS[download_id] = timer
    _cancel_export_timer(old_timer)
    if old_entry and old_entry[0] != archive_path:
        _remove_export_archive(old_entry[0])
    if expires_at <= time.monotonic():
        _expire_pending_export(download_id, expires_at)
    else:
        timer.start()


def create_pending_export(archive_path: str) -> str:
    expires_at = time.monotonic() + EXPORT_TTL_S
    while True:
        download_id = secrets.token_urlsafe(32)
        with PENDING_EXPORTS_LOCK:
            if download_id not in PENDING_EXPORTS:
                break
    register_pending_export(download_id, archive_path, expires_at)
    return download_id


def take_pending_export(download_id: str) -> str | None:
    cleanup_expired_exports()
    expired_archive = None
    timer = None
    with PENDING_EXPORTS_LOCK:
        entry = PENDING_EXPORTS.get(download_id)
        if not entry:
            return None
        archive_path, expires_at = entry
        PENDING_EXPORTS.pop(download_id)
        timer = PENDING_EXPORT_TIMERS.pop(download_id, None)
        if expires_at <= time.monotonic():
            expired_archive = archive_path
        else:
            ACTIVE_DOWNLOAD_ARCHIVES.add(archive_path)
    _cancel_export_timer(timer)
    if expired_archive:
        _remove_export_archive(expired_archive)
        return None
    return archive_path


def clear_pending_exports() -> None:
    with PENDING_EXPORTS_LOCK:
        archive_paths = [archive_path for archive_path, _ in PENDING_EXPORTS.values()]
        archive_paths.extend(ACTIVE_DOWNLOAD_ARCHIVES)
        timers = list(PENDING_EXPORT_TIMERS.values())
        PENDING_EXPORTS.clear()
        PENDING_EXPORT_TIMERS.clear()
    for timer in timers:
        _cancel_export_timer(timer)
    for archive_path in set(archive_paths):
        _remove_export_archive(archive_path)
    try:
        cleanup_residual_export_artifacts(get_export_dir())
    except BadRequest:
        pass


def terminate_export_process(process, process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=EXPORT_TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=EXPORT_TERMINATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            process.wait(timeout=EXPORT_TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ValueError):
        pass


def call_export(command: list[str], work_dir: str) -> None:
    process = None
    process_group = None
    terminate_required = False
    try:
        with ACTIVE_EXPORT_PROCESSES_LOCK:
            if SHUTTING_DOWN.is_set():
                raise ExportError("Proxy is shutting down")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            process_group = process.pid
            ACTIVE_EXPORT_PROCESSES[process] = (work_dir, process_group)
        try:
            process.communicate(timeout=EXPORT_TIMEOUT_S)
        except subprocess.TimeoutExpired as ex:
            terminate_required = True
            raise ExportError("Export command timed out") from ex
        if process.returncode != 0:
            raise ExportError("Export command failed")
    except OSError as ex:
        raise ExportError("Export command failed") from ex
    finally:
        if process and process_group is not None:
            if terminate_required or process.poll() is None:
                terminate_export_process(process, process_group)
            with ACTIVE_EXPORT_PROCESSES_LOCK:
                ACTIVE_EXPORT_PROCESSES.pop(process, None)



class ExportZipEndpoint(DeviceRequestEndpoint):
    def process_with_device(self, server, path, device_id):
        request = self.get_request(server)
        remote_path = request.get("remotePath") if isinstance(request, dict) else None
        if not isinstance(remote_path, str) or not remote_path.startswith("/") or "\x00" in remote_path:
            raise BadRequest("remotePath must be an absolute path without NUL")
        if not EXPORT_SEMAPHORE.acquire(blocking=False):
            raise BadRequest("Too many concurrent export requests")

        work_dir = None
        download_id = None
        try:
            export_dir = get_export_dir()
            cleanup_expired_exports()
            cleanup_residual_export_artifacts(export_dir)
            work_dir = mkdtemp(prefix=".winscope-export-", dir=export_dir)
            archive_path = os.path.join(work_dir, "archive.zip")
            command = [
                os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "winscope_export.sh")),
                "--serial", device_id,
                "--remote-path", remote_path,
                "--output", archive_path,
            ]
            call_export(command, work_dir)
            if not os.path.isfile(archive_path):
                raise ExportError("Export command failed")
            download_id = create_pending_export(archive_path)
            work_dir = None
            try:
                server.respond(
                    HTTPStatus.OK,
                    json.dumps({"downloadUrl": "/download/" + download_id}).encode("utf-8"),
                    "text/json",
                )
            except Exception:
                archive_path = take_pending_export(download_id)
                if archive_path:
                    _remove_export_archive(archive_path)
                raise
        finally:
            if work_dir:
                _remove_export_archive(os.path.join(work_dir, "archive.zip"))
            EXPORT_SEMAPHORE.release()


class DownloadEndpoint(RequestEndpoint):
    requires_token = False

    def process(self, server, path):
        if len(path) != 1:
            raise BadRequest("Download id not specified")
        archive_path = take_pending_export(path[0])
        if not archive_path:
            raise BadRequest("Download is unavailable")
        response_started = False
        try:
            archive_size = os.path.getsize(archive_path)
            server.send_response(HTTPStatus.OK)
            response_started = True
            server.send_header("Content-type", "application/zip")
            server.send_header("Content-Disposition", "attachment; filename=\"winscope-export.zip\"")
            server.send_header("Content-Length", str(archive_size))
            if hasattr(server, "add_standard_headers"):
                server.add_standard_headers()
            else:
                server.end_headers()
            with open(archive_path, "rb") as archive_file:
                while chunk := archive_file.read(64 * 1024):
                    server.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError) as ex:
            if response_started:
                log.warning("Unable to stream download after response: {}".format(ex))
                return
            raise ExportError("Unable to download archive") from ex
        finally:
            _remove_export_archive(archive_path)



class TraceThread(threading.Thread):
    def __init__(self, target_id: str, device_id: str, start_command: str, stop_command: str):
        self.trace_command = start_command
        self._stop_command = stop_command
        self.target_id = target_id
        self._device_id = device_id
        self._keep_alive_timer = None
        self._timer_generation = 0
        self.out = b''
        self.err = b''
        self._command_timed_out = False
        self._success = False
        self._stop_event = threading.Event()
        self._stop_complete = threading.Event()
        self._start_complete = threading.Event()
        self._state_lock = threading.Lock()
        self._trace_started = False
        self._stop_requested = False

        super().__init__(daemon=True)

    def timeout(self, generation):
        self.end_trace(expected_timer_generation=generation)

    def reset_timer(self):
        log.info(
            "Resetting keep-alive clock for {} trace on {}".format(self.target_id, self._device_id))
        with self._state_lock:
            if self._stop_requested:
                return
            if self._keep_alive_timer:
                self._keep_alive_timer.cancel()
            self._timer_generation += 1
            self._keep_alive_timer = threading.Timer(
                KEEP_ALIVE_INTERVAL_S, self.timeout, args=(self._timer_generation,))
            self._keep_alive_timer.start()

    def end_trace(self, expected_timer_generation=None):
        wait_for_start = False
        keep_alive_timer = None
        should_stop = False
        with self._state_lock:
            if (
                expected_timer_generation is not None
                and expected_timer_generation != self._timer_generation
            ):
                return True
            if self._stop_requested:
                wait_for_stop = True
            else:
                wait_for_stop = False
                self._stop_requested = True
                keep_alive_timer = self._keep_alive_timer
                wait_for_start = not self._start_complete.is_set()
                self._timer_generation += 1

        if wait_for_stop:
            return self._stop_complete.wait(timeout=(2 * COMMAND_TIMEOUT_S) + 1)

        if wait_for_start:
            self._start_complete.wait()
        with self._state_lock:
                should_stop = self._trace_started and bool(self._stop_command.strip())

        try:
            if keep_alive_timer:
                keep_alive_timer.cancel()
            log.warning("Stopping {} trace on {}".format(
                self.target_id, self._device_id))
            log.info("Stopping {} trace on {}".format(
                self.target_id,
                self._device_id))

            if should_stop:
                try:
                    stop_out = call_adb(f"shell {self._stop_command}",
                        device=self._device_id,
                        timeout=COMMAND_TIMEOUT_S)
                    self.out += stop_out.encode('utf-8')
                except AdbError as ex:
                    self.err += str(ex).encode('utf-8')
                    if 'Timeout executing' in str(ex):
                        self._command_timed_out = True
            self._stop_event.set()

            if threading.current_thread() is not self and self.is_alive():
                log.debug("Waiting for {} trace worker to exit for {}".format(
                    self.target_id,
                    self._device_id))
                self.join(timeout=COMMAND_TIMEOUT_S)
            if self.is_alive():
                self._command_timed_out = True
                log.error("TIMEOUT - {} trace worker did not exit for {}".format(
                    self.target_id,
                    self._device_id))
        finally:
            self._stop_complete.set()
        return True

    def run(self):
        with self._state_lock:
            if self._stop_requested:
                self._start_complete.set()
                return
        try:
            start_out = call_adb(
                f"shell {detach_background_command(self.trace_command)}",
                device=self._device_id,
                timeout=COMMAND_TIMEOUT_S)
            with self._state_lock:
                self.out += start_out.encode('utf-8')
                self._trace_started = True
                self._start_complete.set()
        except AdbError as ex:
            with self._state_lock:
                self.err += str(ex).encode('utf-8')
                self._start_complete.set()
                if 'Timeout executing' in str(ex):
                    self._command_timed_out = True
            return

        log.info("Trace {} started on {}".format(self.target_id, self._device_id))
        if not self._stop_command.strip():
            with self._state_lock:
                self._success = self._trace_started and len(self.err) == 0
            return
        self.reset_timer()
        self._stop_event.wait()
        log.info("Trace {} ended on {}".format(self.target_id, self._device_id))
        with self._state_lock:
            self._success = self._trace_started and len(self.err) == 0

    def success(self):
        return self._success

    def timed_out(self):
        return self._command_timed_out

TRACE_THREADS: dict[str, dict[str, TraceThread]] = {}
TRACE_THREADS_LOCK = threading.Lock()
SHUTTING_DOWN = threading.Event()


def stop_active_traces():
    with TRACE_THREADS_LOCK:
        SHUTTING_DOWN.set()
        active_threads = [
            thread
            for device_threads in TRACE_THREADS.values()
            for thread in device_threads.values()
        ]

    stop_active_fetches()
    stop_active_exports()

    stop_workers = [
        threading.Thread(target=thread.end_trace, daemon=True)
        for thread in active_threads
    ]
    for worker in stop_workers:
        worker.start()
    deadline = time.monotonic() + (2 * COMMAND_TIMEOUT_S) + 1
    for worker in stop_workers:
        worker.join(timeout=max(0, deadline - time.monotonic()))

    with TRACE_THREADS_LOCK:
        TRACE_THREADS.clear()
    clear_pending_exports()


def stop_active_fetches():
    with ACTIVE_FETCH_PROCESSES_LOCK:
        active_processes = list(ACTIVE_FETCH_PROCESSES)

    for process in active_processes:
        if process.poll() is None:
            process.kill()
            process.wait()
    with ACTIVE_FETCH_PROCESSES_LOCK:
        for process in active_processes:
            ACTIVE_FETCH_PROCESSES.discard(process)


def stop_active_exports():
    with ACTIVE_EXPORT_PROCESSES_LOCK:
        active_processes = list(ACTIVE_EXPORT_PROCESSES.items())
    for process, (work_dir, process_group) in active_processes:
        try:
            terminate_export_process(process, process_group)
        except OSError as ex:
            log.warning("Unable to stop export process: {}".format(ex))
        finally:
            _remove_export_archive(os.path.join(work_dir, "archive.zip"))
            with ACTIVE_EXPORT_PROCESSES_LOCK:
                ACTIVE_EXPORT_PROCESSES.pop(process, None)

class StartTraceEndpoint(DeviceRequestEndpoint):
    def process_with_device(self, server, path, device_id):
        request: dict = self.get_request(server)
        target_id = request.get("targetId")
        start_cmd = request.get("startCmd")
        stop_cmd = request.get("stopCmd")

        if not isinstance(target_id, str) or not target_id.strip():
            raise BadRequest("targetId, startCmd and stopCmd must be non-empty strings")
        if not isinstance(start_cmd, str) or not start_cmd.strip():
            raise BadRequest("targetId, startCmd and stopCmd must be non-empty strings")
        if not isinstance(stop_cmd, str):
            raise BadRequest("targetId, startCmd and stopCmd must be non-empty strings")

        log.debug(f"Executing start command for {target_id} on {device_id}...")
        with TRACE_THREADS_LOCK:
            if SHUTTING_DOWN.is_set():
                raise BadRequest("Proxy is shutting down")
            threads = TRACE_THREADS.setdefault(device_id, {})
            active_thread = threads.get(target_id)
            if active_thread and active_thread.is_alive():
                raise BadRequest("{} trace already in progress for {}".format(target_id, device_id))
            thread = TraceThread(target_id, device_id, start_cmd, stop_cmd)
            threads[target_id] = thread
            thread.start()

        server.respond(HTTPStatus.OK, ''.encode('utf-8'), "text/json")

class EndTraceEndpoint(DeviceRequestEndpoint):
    def process_with_device(self, server, path, device_id):
        request = self.get_request(server)
        target_id = request.get("targetId")
        with TRACE_THREADS_LOCK:
            threads = TRACE_THREADS.get(device_id)
            if not threads or target_id not in threads:
                raise BadRequest("No {} trace in progress for {}".format(target_id, device_id))
            thread = threads[target_id]

        errors: list[str] = []
        if not thread.end_trace():
            raise BadRequest("{} trace is still stopping".format(target_id))
        success = thread.success()

        if (thread.timed_out()):
            timeout_message = "Trace {} timed out during cleanup".format(target_id)
            errors.append(timeout_message)
            log.error(timeout_message)

        if not success:
            log.error("Error ending trace {} on the device".format(target_id))
            errors.append("Error ending trace {} on the device: {}".format(target_id, thread.err))

        out = b"### Shell script's stdout ###\n" + \
            (thread.out if thread.out else b'<no stdout>') + \
            b"\n### Shell script's stderr ###\n" + \
            (thread.err if thread.err else b'<no stderr>') + b"\n"
        log.debug("### Output ###\n".format(target_id) + out.decode("utf-8"))

        with TRACE_THREADS_LOCK:
            threads = TRACE_THREADS.get(device_id)
            if threads and threads.get(target_id) is thread:
                threads.pop(target_id)
                if len(threads) == 0:
                    TRACE_THREADS.pop(device_id)
        server.respond(HTTPStatus.OK, json.dumps(errors).encode("utf-8"), "text/plain")

class StatusEndpoint(DeviceRequestEndpoint):
    def process_with_device(self, server, path, device_id):
        if not path:
            raise BadRequest("Trace id not specified")
        target_id = path[0]
        with TRACE_THREADS_LOCK:
            threads = TRACE_THREADS.get(device_id)
            thread = threads.get(target_id) if threads else None
        if not thread:
            log.debug(target_id)
            server.respond(HTTPStatus.OK, str(False).encode("utf-8"), "text/plain")
        else:
            thread.reset_timer()
            server.respond(HTTPStatus.OK, str(thread.is_alive()).encode("utf-8"), "text/plain")

class RunAdbCmdEndpoint(DeviceRequestEndpoint):
    def process_with_device(self, server, path, device_id):
        request: dict = self.get_request(server)
        cmd = request.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise BadRequest("cmd must be a non-empty string")
        try:
            output = call_adb(cmd, device_id, timeout=COMMAND_TIMEOUT_S)
        except AdbError as error:
            if self.is_optional_missing_find(cmd, error):
                output = ""
            else:
                raise
        server.respond(HTTPStatus.OK, json.dumps(output).encode("utf-8"), "text/plain")

    @staticmethod
    def is_optional_missing_find(command: str, error: AdbError) -> bool:
        normalized = command.strip()
        is_find = (
            normalized.startswith("find ")
            or normalized.startswith("su root find ")
            or normalized.startswith("shell find ")
            or normalized.startswith("shell su root find ")
        )
        return is_find and "No such file or directory" in str(error)


class ADBWinscopeProxy(BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server):
        self.router = RequestRouter(self)
        listDevicesEndpoint = ListDevicesEndpoint()
        self.router.register_endpoint(
            RequestType.GET, "devices", listDevicesEndpoint)
        self.router.register_endpoint(
            RequestType.GET, "status", StatusEndpoint())
        self.router.register_endpoint(
            RequestType.GET, "fetch", FetchEndpoint())
        self.router.register_endpoint(
            RequestType.GET, "download", DownloadEndpoint())
        self.router.register_endpoint(
            RequestType.POST, "runadbcmd", RunAdbCmdEndpoint())
        self.router.register_endpoint(
            RequestType.POST, "starttrace", StartTraceEndpoint())
        self.router.register_endpoint(
            RequestType.POST, "endtrace", EndTraceEndpoint())
        self.router.register_endpoint(
            RequestType.POST, "exportzip", ExportZipEndpoint())
        super().__init__(request, client_address, server)

    def respond(self, code: int, data: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header('Content-type', mime)
        self.add_standard_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.router.process(RequestType.GET)

    def do_POST(self):
        self.router.process(RequestType.POST)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.OK)
        self.send_header('Allow', 'GET,POST')
        self.add_standard_headers()
        self.end_headers()
        self.wfile.write(b'GET,POST')

    def log_request(self, code='-', size='-'):
        log.info('{} {} {}'.format(self.requestline, str(code), str(size)))

    def add_standard_headers(self):
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                        WINSCOPE_TOKEN_HEADER + ', Content-Type, Content-Length')
        self.send_header('Access-Control-Expose-Headers',
                        'Winscope-Proxy-Version')
        self.send_header(WINSCOPE_VERSION_HEADER, VERSION)
        self.end_headers()


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        clear_pending_exports()
        super().server_close()


def create_http_server(port: int) -> ThreadingHTTPServer:
    return LoopbackHTTPServer(('127.0.0.1', port), ADBWinscopeProxy)


if __name__ == '__main__':
    args = create_argument_parser().parse_args()

    logging.basicConfig(stream=sys.stderr, level=args.loglevel,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    log = logging.getLogger("ADBProxy")
    secret_token = get_token()

    print("Winscope ADB Connect proxy version: " + VERSION)

    httpd = create_http_server(args.port)
    def shutdown_proxy(_signum, _frame):
        log.info("Shutting down Winscope Proxy")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_proxy)
    signal.signal(signal.SIGTERM, shutdown_proxy)
    try:
        httpd.serve_forever()
    finally:
        stop_active_traces()
        httpd.server_close()
