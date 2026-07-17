#!/usr/bin/env bash
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

cleanup() {
    if [[ -n "$proxy_pid" ]] && kill -0 "$proxy_pid" 2>/dev/null; then
        kill "$proxy_pid" 2>/dev/null || true
        wait "$proxy_pid" 2>/dev/null || true
    fi
    if [[ -n "$webui_pid" ]] && kill -0 "$webui_pid" 2>/dev/null; then
        kill "$webui_pid" 2>/dev/null || true
        wait "$webui_pid" 2>/dev/null || true
    fi
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
try:
    sock.bind(('127.0.0.1', port))
except OSError:
    sys.exit(0)
finally:
    sock.close()
sys.exit(1)
PY
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

is_valid_port "$ui_port" || error "无效 UI 端口: $ui_port"
is_valid_port "$proxy_port" || error "无效 Proxy 端口: $proxy_port"

for cmd in python3 adb; do
    command -v "$cmd" >/dev/null 2>&1 || error "缺少依赖: $cmd"
done

readonly ui_dir="$install_root/vendor/winscope-ui"
readonly proxy_path="$install_root/vendor/winscope-proxy/winscope_proxy.py"
readonly runtime_dir="$install_root/winscope-aosp/runtime"
readonly webui_log="$runtime_dir/webui.log"
readonly proxy_log="$runtime_dir/proxy.log"
readonly token_file="$runtime_dir/.token"

[[ -f "$ui_dir/index.html" ]] || error "缺少 Winscope Web UI 产物: $ui_dir/index.html。请先在有 AOSP 的机器完成 standalone 打包。"
[[ -f "$proxy_path" ]] || error "缺少 Winscope Proxy: $proxy_path。请先在有 AOSP 的机器完成 standalone 打包。"

is_port_free "$ui_port" || error "UI 端口已被占用: $ui_port，可使用 --ui-port 指定其他端口"
is_port_free "$proxy_port" || error "Proxy 端口已被占用: $proxy_port，可使用 --proxy-port 指定其他端口"

mkdir -p "$runtime_dir"
rm -f "$webui_log" "$proxy_log"

trap cleanup EXIT INT TERM

python3 -m http.server "$ui_port" --bind 127.0.0.1 --directory "$ui_dir" > "$webui_log" 2>&1 &
webui_pid=$!

for _ in $(seq 1 50); do
    if ! kill -0 "$webui_pid" 2>/dev/null; then
        error "Winscope Web UI 启动失败，请查看: $webui_log"
    fi
    if ! is_port_free "$ui_port"; then
        break
    fi
    sleep 0.1
done
is_port_free "$ui_port" && error "Winscope Web UI 未监听端口 $ui_port，请查看: $webui_log"

WINSCOPE_TOKEN_LOCATION="$token_file" python3 "$proxy_path" -p "$proxy_port" > "$proxy_log" 2>&1 &
proxy_pid=$!

token=""
for _ in $(seq 1 50); do
    if [[ -f "$proxy_log" ]]; then
        while IFS= read -r line; do
            case "$line" in
                'Winscope token: '*) token="${line#Winscope token: }" ;;
            esac
        done < "$proxy_log"
    fi
    if [[ -z "$token" ]] && [[ -f "$token_file" ]]; then
        token="$(head -1 "$token_file")"
    fi
    [[ -n "$token" ]] && break
    kill -0 "$proxy_pid" 2>/dev/null || error "Winscope Proxy 启动失败，请查看: $proxy_log"
    sleep 0.1
done
[[ -n "$token" ]] || error "未读取到 Winscope Token，请查看: $proxy_log"

adb_output="$(adb devices -l 2>/dev/null || true)"
device_hint=""
if ! printf '%s\n' "$adb_output" | grep -E '^[^[:space:]]+[[:space:]]+device[[:space:]]' >/dev/null; then
    device_hint='未检测到已授权设备，请检查 adb devices -l。'
fi

printf '\n========================================\n'
printf 'Winscope Web UI 已启动\n'
printf '========================================\n'
printf 'UI:         http://127.0.0.1:%s\n' "$ui_port"
printf 'Proxy:      http://127.0.0.1:%s\n' "$proxy_port"
printf 'Token:      %s\n' "$token"
printf 'Runtime:    %s\n' "$runtime_dir"
if [[ -n "$device_hint" ]]; then
    printf '提示:       %s\n' "$device_hint"
fi
printf '\n按 Ctrl+C 停止服务\n'

if [[ "$open_browser" == true ]] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:$ui_port" >/dev/null 2>&1 &
fi

wait "$proxy_pid"
