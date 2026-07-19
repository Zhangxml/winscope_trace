#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf '%s\n' '用法: winscope_export.sh --serial <serial> --remote-path <绝对设备路径> --output <archive.zip>' >&2
}

fail() {
    printf '错误: %s\n' "$1" >&2
    usage
    exit 2
}

serial=''
remote_path=''
output=''

while (($#)); do
    case "$1" in
        --serial)
            (($# >= 2)) || fail '--serial 缺少参数'
            [[ -z $serial ]] || fail '--serial 只能指定一次'
            serial=$2
            shift 2
            ;;
        --remote-path)
            (($# >= 2)) || fail '--remote-path 缺少参数'
            [[ -z $remote_path ]] || fail '--remote-path 只能指定一次'
            remote_path=$2
            shift 2
            ;;
        --output)
            (($# >= 2)) || fail '--output 缺少参数'
            [[ -z $output ]] || fail '--output 只能指定一次'
            output=$2
            shift 2
            ;;
        *)
            fail "不支持的参数: $1"
            ;;
    esac
done

[[ -n $serial ]] || fail '缺少 --serial'
[[ -n $remote_path ]] || fail '缺少 --remote-path'
[[ -n $output ]] || fail '缺少 --output'
[[ $remote_path == /* ]] || fail '--remote-path 必须是绝对设备路径'
[[ $output == *.zip ]] || fail '--output 必须以 .zip 结尾'

output_dir=$(dirname -- "$output")
[[ -d $output_dir ]] || fail '输出 ZIP 的父目录不存在'

trace_name=$(basename -- "$remote_path")
case "$trace_name" in
    ''|.|..|/*|*'\'*|*[[:cntrl:]]*)
        fail '最终文件名不安全'
        ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fetch_script=${WINSCOPE_FETCH_SCRIPT:-"$script_dir/winscope_fetch.py"}
[[ -f $fetch_script ]] || fail "下载脚本不存在: $fetch_script"

umask 077
work_dir=''
cleanup() {
    if [[ -n $work_dir ]]; then
        rm -rf -- "$work_dir"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

work_dir=$(mktemp -d "$output_dir/.winscope-export.XXXXXX")
trace_path="$work_dir/$trace_name"
temporary_archive="$work_dir/archive.zip"

python3 "$fetch_script" --serial "$serial" --output "$trace_path" "$remote_path"

python3 - "$trace_path" "$temporary_archive" "$trace_name" "$output" <<'PY'
import os
import pathlib
import sys
import zipfile

trace_path = pathlib.Path(sys.argv[1])
temporary_archive = pathlib.Path(sys.argv[2])
entry_name = sys.argv[3]
output_path = pathlib.Path(sys.argv[4])

with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_STORED) as archive:
    archive.write(trace_path, arcname=entry_name)
os.replace(temporary_archive, output_path)
PY
