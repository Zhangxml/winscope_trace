#!/usr/bin/env bash
set -euo pipefail

MAX_SESSION_FILE_COUNT=256
MAX_SESSION_SIZE_MIB=512
MAX_SESSION_SIZE_BYTES=$((MAX_SESSION_SIZE_MIB * 1024 * 1024))

usage() {
    printf '%s\n' '用法: winscope_export.sh --serial <serial> (--remote-path <绝对设备路径> | --session-dir <绝对设备目录>) --output <archive.zip>' >&2
}

fail() {
    printf '错误: %s\n' "$1" >&2
    usage
    exit 2
}

validate_value() {
    case "$2" in
        ''|--*|*[[:cntrl:]]*)
            fail "$1 参数无效"
            ;;
    esac
}

validate_safe_basename() {
    case "$1" in
        ''|.|..|*/*|*'\'*|*:*|*[[:cntrl:]]*)
            fail '最终文件名不安全'
            ;;
    esac
}

shell_quote() {
    printf "'%s'" "${1//\'/\'\\\'\'}"
}

is_safe_stage_path() {
    [[ $1 =~ ^/data/local/tmp/\.winscope-export\.[A-Za-z0-9]+$ ]]
}

is_safe_stage_inode() {
    [[ $1 =~ ^[0-9]+$ ]]
}

serial=''
remote_path=''
session_dir=''
output=''
serial_seen=false
remote_path_seen=false
session_dir_seen=false
output_seen=false

while (($#)); do
    case "$1" in
        --serial)
            (($# >= 2)) || fail '--serial 缺少参数'
            validate_value "$1" "$2"
            [[ $serial_seen == false ]] || fail '--serial 只能指定一次'
            serial=$2
            serial_seen=true
            shift 2
            ;;
        --remote-path)
            (($# >= 2)) || fail '--remote-path 缺少参数'
            validate_value "$1" "$2"
            [[ $remote_path_seen == false ]] || fail '--remote-path 只能指定一次'
            remote_path=$2
            remote_path_seen=true
            shift 2
            ;;
        --session-dir)
            (($# >= 2)) || fail '--session-dir 缺少参数'
            validate_value "$1" "$2"
            [[ $session_dir_seen == false ]] || fail '--session-dir 只能指定一次'
            session_dir=$2
            session_dir_seen=true
            shift 2
            ;;
        --output)
            (($# >= 2)) || fail '--output 缺少参数'
            validate_value "$1" "$2"
            [[ $output_seen == false ]] || fail '--output 只能指定一次'
            output=$2
            output_seen=true
            shift 2
            ;;
        *)
            fail "不支持的参数: $1"
            ;;
    esac
done

[[ -n $serial ]] || fail '缺少 --serial'
if [[ -n $remote_path && -n $session_dir ]]; then
    fail '--remote-path 与 --session-dir 不能同时指定'
fi
if [[ -z $remote_path && -z $session_dir ]]; then
    fail '缺少 --remote-path 或 --session-dir'
fi
[[ -n $output ]] || fail '缺少 --output'
if [[ -n $remote_path ]]; then
    [[ $remote_path == /* ]] || fail '--remote-path 必须是绝对设备路径'
fi
if [[ -n $session_dir ]]; then
    [[ $session_dir == /* ]] || fail '--session-dir 必须是绝对设备目录'
    [[ $session_dir == /data/local/tmp/last_winscope_tracing_session ]] || fail '--session-dir 只能是 /data/local/tmp/last_winscope_tracing_session'
fi
[[ $output == *.zip ]] || fail '--output 必须以 .zip 结尾'

output_dir=$(dirname -- "$output")
[[ -d $output_dir ]] || fail '输出 ZIP 的父目录不存在'

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fetch_script=${WINSCOPE_FETCH_SCRIPT:-"$script_dir/winscope_fetch.py"}
[[ -f $fetch_script ]] || fail "下载脚本不存在: $fetch_script"

umask 077
work_dir=''
adb_command=''
stage_path=''
stage_inode=''
cleanup() {
    local status=$?
    local cleanup_command

    if [[ -n $stage_path ]]; then
        if [[ -n $adb_command ]] && is_safe_stage_path "$stage_path" && is_safe_stage_inode "$stage_inode"; then
            cleanup_command="expected_inode=$stage_inode; if [ -d $(shell_quote "$stage_path") ]; then current_inode=\$(stat -c %i $(shell_quote "$stage_path")) || { printf '警告: 无法读取设备侧暂存目录 inode\\n' >&2; exit 0; }; case \"\$current_inode\" in ''|*[!0-9]*) printf '警告: 设备侧暂存目录 inode 无效\\n' >&2 ;; *) if [ \"\$current_inode\" = \"\$expected_inode\" ]; then rm -rf -- $(shell_quote "$stage_path"); else printf '警告: 设备侧暂存目录 inode 已变化，拒绝清理\\n' >&2; fi ;; esac; else printf '警告: 设备侧暂存目录已不存在或不是目录\\n' >&2; fi"
            if ! "$adb_command" -s "$serial" shell "su root sh -c $(shell_quote "$cleanup_command")"; then
                printf '警告: 无法清理设备侧暂存目录: %s\n' "$stage_path" >&2
            fi
        else
            printf '警告: 拒绝清理不安全的设备侧暂存目录: %s\n' "$stage_path" >&2
        fi
    fi
    if [[ -n $work_dir ]]; then
        rm -rf -- "$work_dir" || printf '警告: 无法清理本地临时目录: %s\n' "$work_dir" >&2
    fi
    return "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

work_dir=$(mktemp -d "$output_dir/.winscope-export.XXXXXX")
files_dir="$work_dir/files"
mkdir "$files_dir"
temporary_archive="$work_dir/archive.zip"

trace_paths=()
trace_remote_paths=()

if [[ -n $remote_path ]]; then
    trace_name=$(basename -- "$remote_path")
    validate_safe_basename "$trace_name"
    trace_path="$files_dir/$trace_name"
    python3 "$fetch_script" --serial "$serial" --output "$trace_path" "$remote_path"
    trace_paths+=("$trace_path")
else
    adb_command=${WINSCOPE_ADB:-adb}
    command -v -- "$adb_command" >/dev/null 2>&1 || fail "ADB 命令不存在: $adb_command"
    quoted_session_dir=$(shell_quote "$session_dir")
    preflight_command="dir=$quoted_session_dir; for file in \"\$dir\"/* \"\$dir\"/.[!.]* \"\$dir\"/..?*; do [ -e \"\$file\" ] || continue; [ -L \"\$file\" ] && continue; [ -f \"\$file\" ] || continue; name=\${file##*/}; size=\$(wc -c < \"\$file\") || exit 1; printf '%s\\t%s\\n' \"\$name\" \"\$size\"; done"
    preflight_root_command="su root sh -c $(shell_quote "$preflight_command")"
    preflight_path="$work_dir/preflight.tsv"
    "$adb_command" -s "$serial" shell "$preflight_root_command" >"$preflight_path"

    selected_names=()
    selected_blocks=()
    reserved_total_size=0
    max_total_size=$MAX_SESSION_SIZE_BYTES
    while IFS= read -r listing_line || [[ -n $listing_line ]]; do
        case "$listing_line" in
            *$'\t'*)
                trace_name=${listing_line%%$'\t'*}
                trace_size=${listing_line#*$'\t'}
                [[ $trace_size != *$'\t'* ]] || fail '目录列表格式无效'
                ;;
            *)
                fail '目录列表格式无效'
                ;;
        esac
        validate_safe_basename "$trace_name"
        [[ $trace_size =~ ^[0-9]+$ ]] || fail '目录列表文件大小无效'
        if [[ ${#trace_size} -gt 9 ]]; then
            fail '目录列表文件大小超过限制'
        fi
        if ((10#$trace_size == 0)); then
            continue
        fi
        file_blocks=$(((10#$trace_size + 511) / 512))
        ((reserved_total_size += file_blocks * 512))
        ((reserved_total_size <= max_total_size)) || fail "导出文件总大小超过 ${MAX_SESSION_SIZE_MIB} MiB 限制"
        ((${#selected_names[@]} < MAX_SESSION_FILE_COUNT)) || fail "导出文件数量超过 ${MAX_SESSION_FILE_COUNT} 个限制"
        selected_names+=("$trace_name")
        selected_blocks+=("$file_blocks")
    done <"$preflight_path"

    ((${#selected_names[@]} > 0)) || fail '目录中没有可导出的非空常规文件'

    stage_copy_commands=''
    for trace_index in "${!selected_names[@]}"; do
        trace_name=${selected_names[$trace_index]}
        file_blocks=${selected_blocks[$trace_index]}
        validate_safe_basename "$trace_name"
        [[ $file_blocks =~ ^[0-9]+$ ]] || fail '预检文件块数无效'
        stage_copy_commands+=" name=$(shell_quote "$trace_name"); file=\"\$dir/\$name\"; [ -L \"\$file\" ] && exit 1; [ -f \"\$file\" ] || exit 1; ( ulimit -f $file_blocks; cp -P -- \"\$file\" \"\$stage/\$name\" ) || exit 1; [ -L \"\$stage/\$name\" ] && exit 1; [ -f \"\$stage/\$name\" ] || exit 1; size=\$(wc -c < \"\$stage/\$name\") || exit 1; printf '%s\\t%s\\n' \"\$name\" \"\$size\";"
    done
    stage_command="dir=$quoted_session_dir; stage=''; cleanup_stage() { [ -z \"\$stage\" ] || rm -rf -- \"\$stage\"; }; trap 'cleanup_stage' EXIT; trap 'cleanup_stage; exit 1' HUP INT TERM; stage=\$(mktemp -d /data/local/tmp/.winscope-export.XXXXXX) || exit 1; chmod 700 \"\$stage\" || exit 1; stage_inode=\$(stat -c %i \"\$stage\") || exit 1; case \"\$stage_inode\" in ''|*[!0-9]*) exit 1 ;; esac; printf '__WINSCOPE_STAGE__\\t%s\\t%s\\n' \"\$stage\" \"\$stage_inode\";$stage_copy_commands toybox setsid -d sh -c 'sleep 3600; stage=\$1; stage_inode=\$2; [ -d \"\$stage\" ] || exit 0; current_inode=\$(stat -c %i \"\$stage\") || exit 0; [ \"\$current_inode\" = \"\$stage_inode\" ] || exit 0; rm -rf -- \"\$stage\"' sh \"\$stage\" \"\$stage_inode\" >/dev/null 2>&1 & trap - EXIT HUP INT TERM"
    stage_root_command="su root sh -c $(shell_quote "$stage_command")"
    stage_listing_path="$work_dir/stage.tsv"
    stage_status=0
    "$adb_command" -s "$serial" shell "$stage_root_command" >"$stage_listing_path" || stage_status=$?
    ((stage_status == 0)) || fail '设备侧暂存复制失败'

    staged_total_size=0
    staged_names=()
    {
        IFS= read -r stage_line || fail '设备侧暂存目录创建失败'
        case "$stage_line" in
            __WINSCOPE_STAGE__$'\t'*)
                stage_marker=${stage_line#*$'\t'}
                stage_path=${stage_marker%%$'\t'*}
                stage_inode=${stage_marker#*$'\t'}
                [[ $stage_inode != *$'\t'* ]] || fail '设备侧暂存目录格式无效'
                ;;
            *)
                fail '设备侧暂存目录格式无效'
                ;;
        esac
        is_safe_stage_path "$stage_path" || fail '设备侧暂存目录不安全'
        is_safe_stage_inode "$stage_inode" || fail '设备侧暂存目录 inode 无效'

        while IFS= read -r listing_line || [[ -n $listing_line ]]; do
            case "$listing_line" in
                *$'\t'*)
                    trace_name=${listing_line%%$'\t'*}
                    trace_size=${listing_line#*$'\t'}
                    [[ $trace_size != *$'\t'* ]] || fail '暂存目录列表格式无效'
                    ;;
                *)
                    fail '暂存目录列表格式无效'
                    ;;
            esac
            validate_safe_basename "$trace_name"
            selected=false
            for selected_name in "${selected_names[@]}"; do
                if [[ $trace_name == "$selected_name" ]]; then
                    selected=true
                    break
                fi
            done
            [[ $selected == true ]] || fail '暂存目录包含未验证文件'
            for staged_name in "${staged_names[@]}"; do
                [[ $trace_name != "$staged_name" ]] || fail '暂存目录包含重复文件'
            done
            [[ $trace_size =~ ^[0-9]+$ ]] || fail '暂存目录文件大小无效'
            if [[ ${#trace_size} -gt 9 ]]; then
                fail '暂存目录文件大小超过限制'
            fi
            if ((10#$trace_size == 0)); then
                continue
            fi
            ((staged_total_size += 10#$trace_size))
            ((staged_total_size <= max_total_size)) || fail "暂存文件总大小超过 ${MAX_SESSION_SIZE_MIB} MiB 限制"
            staged_names+=("$trace_name")
            trace_paths+=("$files_dir/$trace_name")
            trace_remote_paths+=("$stage_path/$trace_name")
        done
    } <"$stage_listing_path"

    ((${#trace_paths[@]} > 0)) || fail '暂存目录中没有可导出的非空常规文件'

    for trace_index in "${!trace_paths[@]}"; do
        trace_path=${trace_paths[$trace_index]}
        python3 "$fetch_script" --serial "$serial" --max-size-mib "$MAX_SESSION_SIZE_MIB" --output "$trace_path" "${trace_remote_paths[$trace_index]}"
    done

    downloaded_total_size=0
    for trace_path in "${trace_paths[@]}"; do
        downloaded_size=$(wc -c <"$trace_path")
        [[ $downloaded_size =~ ^[0-9]+$ ]] || fail '下载后文件大小无效'
        if [[ ${#downloaded_size} -gt 9 ]]; then
            fail '下载后文件大小超过限制'
        fi
        ((downloaded_total_size += 10#$downloaded_size))
        ((downloaded_total_size <= max_total_size)) || fail "下载后文件总大小超过 ${MAX_SESSION_SIZE_MIB} MiB 限制"
    done
fi

python3 - "$temporary_archive" "$output" "${trace_paths[@]}" <<'PY'
import os
import pathlib
import sys
import zipfile

temporary_archive = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
trace_paths = [pathlib.Path(argument) for argument in sys.argv[3:]]

with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_STORED) as archive:
    for trace_path in trace_paths:
        archive.write(trace_path, arcname=trace_path.name)
os.replace(temporary_archive, output_path)
PY
