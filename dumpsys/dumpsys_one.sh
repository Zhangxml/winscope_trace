#!/bin/bash

set -uo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
serial=""
output_root="$script_dir/output"
current_part=""
had_failure=0

usage() {
    cat <<'EOF'
用法: dumpsys_one.sh [--serial SERIAL] [--output DIRECTORY]

选项:
  --serial SERIAL      指定目标设备；未指定时要求仅连接一个已授权设备
  --output DIRECTORY   指定输出根目录，默认写入 dumpsys/output
  -h, --help           显示帮助
EOF
}

error() {
    echo "错误: $*" >&2
    exit 1
}

cleanup_part() {
    if [[ -n "$current_part" ]]; then
        rm -f -- "$current_part"
        current_part=""
    fi
}

handle_signal() {
    cleanup_part
    echo >&2
    echo "收集已中止" >&2
    exit 130
}

trap cleanup_part EXIT
trap handle_signal INT TERM

while (($# > 0)); do
    case "$1" in
        --serial)
            (($# >= 2)) || error "--serial 缺少参数"
            serial=$2
            shift 2
            ;;
        --output)
            (($# >= 2)) || error "--output 缺少参数"
            output_root=$2
            shift 2
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

command -v adb >/dev/null 2>&1 || error "未找到 adb"

if [[ -z "$serial" ]]; then
    if ! device_listing=$(adb devices); then
        error "获取设备列表失败"
    fi
    mapfile -t devices < <(awk 'NR > 1 && $2 == "device" { print $1 }' <<<"$device_listing")
    case ${#devices[@]} in
        0)
            error "未找到已授权设备"
            ;;
        1)
            serial=${devices[0]}
            ;;
        *)
            error "检测到多个已授权设备，请使用 --serial 指定设备"
            ;;
    esac
fi

[[ "$serial" =~ ^[A-Za-z0-9._:-]+$ ]] || error "设备序列号包含非法字符: $serial"

ADB=(adb -s "$serial")
"${ADB[@]}" wait-for-device || error "等待设备失败: $serial"
if ! "${ADB[@]}" root; then
    echo "警告: adb root 失败，将继续以当前权限采集" >&2
fi
"${ADB[@]}" wait-for-device || error "adb root 后等待设备失败: $serial"

mkdir -p -- "$output_root" || error "无法创建输出目录: $output_root"
output_root=$(cd -- "$output_root" && pwd -P) || error "无法访问输出目录: $output_root"
[[ -w "$output_root" ]] || error "输出目录不可写: $output_root"

start_time=$(date +"%Y%m%d_%H%M%S")
run_directory="$output_root/${serial}_${start_time}_$$"
mkdir -- "$run_directory" || error "无法创建本次运行目录: $run_directory"
echo "设备: $serial"
echo "输出目录: $run_directory"

collect_file() {
    local label=$1
    local destination=$2
    local validation=$3
    local png_signature
    shift 3

    current_part="${destination}.part"
    rm -f -- "$current_part"
    echo "$label"
    if "$@" >"$current_part" && [[ -s "$current_part" ]]; then
        if [[ "$validation" == "png" ]]; then
            png_signature=$(od -An -tx1 -N8 -- "$current_part" | tr -d '[:space:]')
            if [[ "$png_signature" != "89504e470d0a1a0a" ]]; then
                echo "截图格式无效: $label" >&2
            elif mv -- "$current_part" "$destination"; then
                current_part=""
                return 0
            fi
        elif mv -- "$current_part" "$destination"; then
            current_part=""
            return 0
        fi
    fi

    rm -f -- "$current_part"
    current_part=""
    echo "采集失败: $label" >&2
    return 1
}

collect_screenshots() {
    local round_directory=$1
    local display_listing=""
    local display_id
    local index=0
    local screenshots_ok=1
    local -a display_ids=()

    if display_listing=$("${ADB[@]}" shell dumpsys SurfaceFlinger --display-id); then
        mapfile -t display_ids < <(
            awk '$1 == "Display" && $2 ~ /^[0-9]+$/ && !seen[$2]++ { print $2 }' \
                <<<"$display_listing"
        )
    fi

    if ((${#display_ids[@]} == 0)); then
        echo "无法枚举物理显示屏，仅回退截取默认屏幕" >&2
        collect_file "screencap default display" "$round_directory/screen_0.png" png \
            "${ADB[@]}" exec-out screencap -p || true
        return 1
    fi

    for display_id in "${display_ids[@]}"; do
        collect_file "screencap display $display_id" \
            "$round_directory/screen_${index}.png" png \
            "${ADB[@]}" exec-out screencap -p -d "$display_id" || screenshots_ok=0
        ((index += 1))
    done

    ((screenshots_ok))
}

round=0
while true; do
    ((round += 1))
    round_time=$(date +"%Y%m%d_%H%M%S")
    printf -v round_name 'round_%04d_%s' "$round" "$round_time"
    round_directory="$run_directory/$round_name"
    mkdir -- "$round_directory" || error "无法创建轮次目录: $round_directory"
    round_ok=1

    collect_file "dumpsys activity" "$round_directory/activity.txt" text \
        "${ADB[@]}" shell dumpsys activity || round_ok=0
    collect_file "dumpsys window" "$round_directory/window.txt" text \
        "${ADB[@]}" shell dumpsys window windows || round_ok=0
    collect_file "dumpsys SurfaceFlinger" "$round_directory/SurfaceFlinger.txt" text \
        "${ADB[@]}" shell dumpsys SurfaceFlinger || round_ok=0
    collect_file "dumpsys display" "$round_directory/display.txt" text \
        "${ADB[@]}" shell dumpsys display || round_ok=0
    collect_file "dumpsys input" "$round_directory/input.txt" text \
        "${ADB[@]}" shell dumpsys input || round_ok=0
    collect_screenshots "$round_directory" || round_ok=0

    if ((round_ok)); then
        echo "本轮数据收集完成: $round_directory"
    else
        echo "本轮数据收集失败，已保留成功项目: $round_directory" >&2
        had_failure=1
    fi

    echo "按回车键继续下一轮收集，按Ctrl+C退出..."
    if ! IFS= read -r; then
        echo "输入已关闭，停止收集"
        break
    fi
done

exit "$had_failure"
