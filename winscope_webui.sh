#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    launcher_path="${BASH_SOURCE[0]}"
    [[ "$launcher_path" == /* ]] || launcher_path="$PWD/$launcher_path"
    if "$launcher_path" "$@"; then
        return 0
    else
        launcher_status=$?
        if [[ "$launcher_status" -eq 130 ]]; then
            return 0
        fi
        return "$launcher_status"
    fi
fi

set -euo pipefail

readonly DEFAULT_UI_PORT=8080
readonly DEFAULT_PROXY_PORT=5544
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_root="$SCRIPT_DIR"
ui_port="$DEFAULT_UI_PORT"
proxy_port="$DEFAULT_PROXY_PORT"
open_browser=true
webui_pid=""
proxy_pid=""
shutdown_exit_code=""

umask 077

usage() {
    cat <<'EOF'
用法: winscope_webui.sh [选项]

启动本地官方 Winscope Web UI 和 Winscope ADB Proxy，直接通过浏览器抓取 Winscope trace。

选项:
  --root DIRECTORY       standalone 包根目录，默认脚本所在目录
  --ui-port PORT         Winscope Web UI 端口，默认 8080
  --proxy-port PORT      Winscope Proxy 端口，默认 5544
  --no-browser           不自动打开浏览器
  -h, --help             显示本帮助

启动后:
  1. 浏览器打开输出的本地 UI 地址
  2. 在 Winscope Proxy 设置中填入输出的 Proxy 地址和 Token
  3. 选择 adb 设备并开始抓取 Winscope trace
EOF
}

error() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

request_shutdown() {
    shutdown_exit_code="$1"
}

check_shutdown() {
    if [[ -n "$shutdown_exit_code" ]]; then
        exit "$shutdown_exit_code"
    fi
    return 0
}


cleanup() {
    cleanup_owned_process proxy "$proxy_pid"
    proxy_pid=""
    cleanup_owned_process ui "$webui_pid"
    webui_pid=""
}

is_valid_port() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]] && (( "$1" <= 65535 ))
}

is_port_free() {
    local port="$1"
    ! python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(('127.0.0.1', port))
except OSError:
    sys.exit(0)
finally:
    sock.close()
sys.exit(1)
PY
}

wait_for_port_free() {
    local port="$1"
    local role="$2"
    local attempt
    local max_attempts

    case "$role" in
        proxy)
            max_attempts=350
            ;;
        ui)
            max_attempts=50
            ;;
        *)
            return 1
            ;;
    esac
    for ((attempt = 0; attempt < max_attempts; attempt++)); do
        is_port_free "$port" && return 0
        sleep 0.1
    done
    is_port_free "$port"
}

wait_for_process_exit() {
    local role="$1"
    local pid="$2"
    local attempt
    local max_attempts

    case "$role" in
        proxy)
            max_attempts=350
            ;;
        ui)
            max_attempts=50
            ;;
        *)
            return 1
            ;;
    esac
    for ((attempt = 0; attempt < max_attempts; attempt++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            return 0
        fi
        sleep 0.1
    done
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || true
        return 0
    fi
    return 1
}

collect_loopback_listener_pids() {
    local port="$1"
    local ss_output
    local line
    local remaining
    local matched
    local pid

    command -v ss >/dev/null 2>&1 || error "端口 $port 被占用，但缺少 ss，拒绝清理未知监听进程"
    ss_output="$(ss -ltnp4 "sport = :$port")" || error "无法使用 ss 查询端口 $port，拒绝清理未知监听进程"
    listener_pids=()

    while IFS= read -r line; do
        [[ "$line" == *"127.0.0.1:$port"* ]] || continue
        remaining="$line"
        while [[ "$remaining" =~ pid=([0-9]+) ]]; do
            pid="${BASH_REMATCH[1]}"
            matched="${BASH_REMATCH[0]}"
            if [[ " ${listener_pids[*]} " != *" $pid "* ]]; then
                listener_pids+=("$pid")
            fi
            remaining="${remaining#*"$matched"}"
        done
    done <<< "$ss_output"

    ((${#listener_pids[@]} > 0)) || error "端口 $port 被占用，但无法从 ss 获取 IPv4 loopback 监听 PID，拒绝清理未知进程"
}

read_cmdline_args() {
    local pid="$1"
    local arg

    cmdline_args=()
    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    if ! while IFS= read -r -d '' arg; do
        cmdline_args+=("$arg")
    done < "/proc/${pid}/cmdline"; then
        return 1
    fi
    ((${#cmdline_args[@]} > 0))
}


command_matches_role() {
    local role="$1"
    shift
    local -a cmdline_args=("$@")
    local interpreter

    ((${#cmdline_args[@]} > 0)) || return 1
    interpreter="${cmdline_args[0]##*/}"
    [[ "$interpreter" == "python3" || "$interpreter" == "python" ]] || return 1

    case "$role" in
        ui)
            [[ "${#cmdline_args[@]}" -eq 8 ]] &&
                [[ "${cmdline_args[1]}" == "-m" ]] &&
                [[ "${cmdline_args[2]}" == "http.server" ]] &&
                [[ "${cmdline_args[3]}" == "$ui_port" ]] &&
                [[ "${cmdline_args[4]}" == "--bind" ]] &&
                [[ "${cmdline_args[5]}" == "127.0.0.1" ]] &&
                [[ "${cmdline_args[6]}" == "--directory" ]] &&
                [[ "${cmdline_args[7]}" == "$ui_dir" ]]
            ;;
        proxy)
            [[ "${#cmdline_args[@]}" -eq 4 ]] &&
                [[ "${cmdline_args[1]}" == "$proxy_path" ]] &&
                [[ "${cmdline_args[2]}" == "-p" ]] &&
                [[ "${cmdline_args[3]}" == "$proxy_port" ]]
            ;;
        *)
            return 1
            ;;
    esac
}

print_cmdline_args() {
    local arg

    for arg in "$@"; do
        printf '%q ' "$arg"
    done
}

send_verified_signal() {
    local role="$1"
    local pid="$2"
    local signum="$3"
    local parent_pid="$4"

    python3 - "$role" "$pid" "$signum" "$parent_pid" "$ui_port" "$ui_dir" "$proxy_path" "$proxy_port" <<'PY'
import os
import signal
import sys

pidfd = None
try:
    role = sys.argv[1]
    pid = int(sys.argv[2])
    signum = int(sys.argv[3])
    parent_pid = int(sys.argv[4]) if sys.argv[4] else None
    ui_port = sys.argv[5]
    ui_dir = sys.argv[6]
    proxy_path = sys.argv[7]
    proxy_port = sys.argv[8]
    if signum not in (signal.SIGTERM, signal.SIGKILL):
        raise ValueError

    pidfd = os.pidfd_open(pid)
    with open(f'/proc/{pid}/cmdline', 'rb') as cmdline_file:
        cmdline_args = cmdline_file.read().split(b'\0')
    if not cmdline_args or cmdline_args.pop() != b'':
        raise ValueError
    if not cmdline_args or os.path.basename(cmdline_args[0]) not in (b'python3', b'python'):
        raise ValueError

    if role == 'ui':
        expected_args = [
            b'-m', b'http.server', os.fsencode(ui_port), b'--bind',
            b'127.0.0.1', b'--directory', os.fsencode(ui_dir),
        ]
    elif role == 'proxy':
        expected_args = [os.fsencode(proxy_path), b'-p', os.fsencode(proxy_port)]
    else:
        raise ValueError
    if cmdline_args[1:] != expected_args:
        raise ValueError

    if parent_pid is not None:
        current_parent_pid = None
        with open(f'/proc/{pid}/status', 'rb') as status_file:
            for line in status_file:
                if line.startswith(b'PPid:'):
                    current_parent_pid = int(line.split()[1])
                    break
        if current_parent_pid != parent_pid:
            raise ValueError

    signal.pidfd_send_signal(pidfd, signum)
except (IndexError, OSError, ValueError):
    sys.exit(1)
finally:
    if pidfd is not None:
        os.close(pidfd)
PY
}


verify_listener_commands() {
    local role="$1"
    local port="$2"
    shift 2
    local pid

    for pid in "$@"; do
        if ! read_cmdline_args "$pid"; then
            printf '拒绝清理: 端口=%s PID=%s 命令=<无法读取 /proc/%s/cmdline>\n' "$port" "$pid" "$pid" >&2
            return 1
        fi
        if ! command_matches_role "$role" "${cmdline_args[@]}"; then
            printf '拒绝清理: 端口=%s PID=%s 命令=' "$port" "$pid" >&2
            print_cmdline_args "${cmdline_args[@]}" >&2
            printf '\n' >&2
            return 1
        fi
        printf '回收 Winscope %s 监听: 端口=%s PID=%s 命令=' "$role" "$port" "$pid"
        print_cmdline_args "${cmdline_args[@]}"
        printf '\n'
    done
}

is_current_script_child() {
    local pid="$1"
    local key
    local value
    local parent_pid=""

    [[ -r "/proc/${pid}/status" ]] || return 1
    if ! while read -r key value; do
        if [[ "$key" == "PPid:" ]]; then
            parent_pid="$value"
            break
        fi
    done < "/proc/${pid}/status"; then
        return 1
    fi
    [[ "$parent_pid" == "$$" ]]
}

is_owned_cleanup_process() {
    local role="$1"
    local pid="$2"

    if ! is_current_script_child "$pid"; then
        printf '警告: 无法确认当前 Winscope %s 子进程，跳过清理: PID=%s\n' "$role" "$pid" >&2
        return 1
    fi
    if ! read_cmdline_args "$pid"; then
        printf '警告: 无法读取当前 Winscope %s 子进程命令，跳过清理: PID=%s\n' "$role" "$pid" >&2
        return 1
    fi
    if ! command_matches_role "$role" "${cmdline_args[@]}"; then
        printf '警告: 当前 Winscope %s 子进程命令不匹配，跳过清理: PID=%s 命令=' "$role" "$pid" >&2
        print_cmdline_args "${cmdline_args[@]}" >&2
        printf '\n' >&2
        return 1
    fi
    return 0
}

cleanup_owned_process() {
    local role="$1"
    local pid="$2"
    local attempt

    [[ -n "$pid" ]] || return 0
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || true
        return 0
    fi
    is_owned_cleanup_process "$role" "$pid" || return 0
    if ! send_verified_signal "$role" "$pid" 15 "$$"; then
        printf '警告: pidfd 验证失败，跳过向当前 Winscope %s 子进程发送 SIGTERM: PID=%s\n' "$role" "$pid" >&2
        return 0
    fi

    if wait_for_process_exit "$role" "$pid"; then
        return 0
    fi

    is_owned_cleanup_process "$role" "$pid" || return 0
    if ! send_verified_signal "$role" "$pid" 9 "$$"; then
        printf '警告: pidfd 验证失败，跳过向当前 Winscope %s 子进程发送 SIGKILL: PID=%s\n' "$role" "$pid" >&2
        return 0
    fi
    wait "$pid" 2>/dev/null || true
    return 0
}

recover_winscope_port() {
    local port="$1"
    local role="$2"
    local pid

    is_port_free "$port" && return 0

    collect_loopback_listener_pids "$port"
    verify_listener_commands "$role" "$port" "${listener_pids[@]}" || error "端口 $port 存在非当前 Winscope $role 监听进程，拒绝启动"

    # SIGTERM 前重新查询并验证全部当前监听 PID，避免端口被新进程接管后误杀。
    collect_loopback_listener_pids "$port"
    verify_listener_commands "$role" "$port" "${listener_pids[@]}" || error "端口 $port 的监听进程已变化，拒绝启动"
    for pid in "${listener_pids[@]}"; do
        send_verified_signal "$role" "$pid" 15 "" || error "拒绝向端口 $port 的 PID $pid 发送 SIGTERM"
    done

    wait_for_port_free "$port" "$role" && return 0

    collect_loopback_listener_pids "$port"
    # SIGKILL 前同样重新验证当前监听 PID，绝不按旧 PID 盲杀。
    verify_listener_commands "$role" "$port" "${listener_pids[@]}" || error "端口 $port 的监听进程已变化，拒绝启动"
    for pid in "${listener_pids[@]}"; do
        send_verified_signal "$role" "$pid" 9 "" || error "拒绝向端口 $port 的 PID $pid 发送 SIGKILL"
    done

    wait_for_port_free "$port" "$role" || error "清理 Winscope $role 监听进程后端口仍被占用: $port"
}


while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --root)
            [[ "$#" -ge 2 ]] || error '--root 缺少目录参数'
            install_root="$2"
            shift 2
            ;;
        --ui-port)
            [[ "$#" -ge 2 ]] || error '--ui-port 缺少端口参数'
            ui_port="$2"
            shift 2
            ;;
        --proxy-port)
            [[ "$#" -ge 2 ]] || error '--proxy-port 缺少端口参数'
            proxy_port="$2"
            shift 2
            ;;
        --no-browser)
            open_browser=false
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            error "未知参数: $1"
            ;;
    esac
done

[[ -d "$install_root" ]] || error "--root 不是目录: $install_root"
install_root="$(cd -- "$install_root" && pwd -P)"

is_valid_port "$ui_port" || error "无效 UI 端口: $ui_port"
is_valid_port "$proxy_port" || error "无效 Proxy 端口: $proxy_port"

for cmd in python3 adb; do
    command -v "$cmd" >/dev/null 2>&1 || error "缺少依赖: $cmd"
done

readonly ui_dir="$install_root/vendor/winscope-ui"
readonly proxy_path="$install_root/vendor/winscope-proxy/winscope_proxy.py"
readonly runtime_dir="$install_root/runtime"
readonly export_dir="$runtime_dir/downloads"
readonly webui_log="$runtime_dir/webui.log"
readonly proxy_log="$runtime_dir/proxy.log"
readonly token_file="$runtime_dir/.token"

[[ -f "$ui_dir/index.html" ]] || error "缺少 Winscope Web UI 产物: $ui_dir/index.html。请先在有 AOSP 的机器完成 standalone 打包。"
[[ -f "$proxy_path" ]] || error "缺少 Winscope Proxy: $proxy_path。请先在有 AOSP 的机器完成 standalone 打包。"

[[ "$ui_port" != "$proxy_port" ]] || error "UI 与 Proxy 端口不能相同: $ui_port"
recover_winscope_port "$ui_port" ui
recover_winscope_port "$proxy_port" proxy

mkdir -p "$runtime_dir"
chmod 700 "$runtime_dir"
mkdir -p "$export_dir"
chmod 700 "$export_dir"
find -P "$export_dir" -mindepth 1 -maxdepth 1 -name '.winscope-export-*' -exec rm -rf -- {} +
rm -f "$webui_log" "$proxy_log"

trap cleanup EXIT
trap 'request_shutdown 130' INT
trap 'request_shutdown 143' TERM

check_shutdown
python3 -m http.server "$ui_port" --bind 127.0.0.1 --directory "$ui_dir" > "$webui_log" 2>&1 &
webui_pid=$!
check_shutdown

for ((attempt = 0; attempt < 50; attempt++)); do
    check_shutdown
    if ! kill -0 "$webui_pid" 2>/dev/null; then
        error "Winscope Web UI 启动失败，请查看: $webui_log"
    fi
    if ! is_port_free "$ui_port"; then
        break
    fi
    sleep 0.1
done
check_shutdown
is_port_free "$ui_port" && error "Winscope Web UI 未监听端口 $ui_port，请查看: $webui_log"

check_shutdown
WINSCOPE_EXPORT_DIR="$export_dir" WINSCOPE_TOKEN_LOCATION="$token_file" python3 "$proxy_path" -p "$proxy_port" > "$proxy_log" 2>&1 &
proxy_pid=$!
check_shutdown

token=""
for ((attempt = 0; attempt < 50; attempt++)); do
    check_shutdown
    if [[ -f "$token_file" ]]; then
        token="$(head -1 "$token_file")"
    fi
    [[ -n "$token" ]] && break
    kill -0 "$proxy_pid" 2>/dev/null || error "Winscope Proxy 启动失败，请查看: $proxy_log"
    sleep 0.1
done
check_shutdown
[[ -n "$token" ]] || error "未读取到 Winscope Token，请查看: $proxy_log"

adb_output="$(adb devices -l 2>/dev/null || true)"
device_hint=""
if ! printf '%s\n' "$adb_output" | grep -E '^[^[:space:]]+[[:space:]]+device[[:space:]]' >/dev/null; then
    device_hint='未检测到已授权设备，请检查 adb devices -l。'
fi

printf '\n=============================================\n'
printf 'Winscope Web UI 已启动，按 Ctrl+C 停止服务\n'
printf '=============================================\n'
printf 'UI:         http://127.0.0.1:%s\n' "$ui_port"
printf '导出:       http://127.0.0.1:%s/winscope-export.html?proxyPort=%s\n' "$ui_port" "$proxy_port"
printf 'Proxy:      http://127.0.0.1:%s\n' "$proxy_port"
printf 'Token:      %s\n' "$token"
printf 'Runtime:    %s\n' "$runtime_dir"
if [[ -n "$device_hint" ]]; then
    printf '提示:       %s\n' "$device_hint"
fi

if [[ "$open_browser" == true ]] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:$ui_port" >/dev/null 2>&1 &
fi

wait_status=0
wait "$proxy_pid" || wait_status=$?
check_shutdown
proxy_pid=""
(( wait_status == 0 )) || exit "$wait_status"
