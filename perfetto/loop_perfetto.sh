#!/usr/bin/env bash
set -euo pipefail

LOCAL_CONFIG="config.txtpb"
REMOTE_TRACE="/data/misc/perfetto-traces/trace.pftrace"
COUNT=""

usage() {
  cat <<'USAGE'
Usage: ./loop_perfetto.sh [--count N]

Push config.txtpb to device, then repeatedly capture Perfetto traces into the current directory.

Options:
  --count N, -n N  Capture N traces and exit. Default is infinite loop.
  --help, -h      Show this help.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count|-n)
      [[ $# -ge 2 ]] || die "$1 requires a positive integer"
      is_positive_integer "$2" || die "$1 requires a positive integer"
      COUNT="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

command -v adb >/dev/null 2>&1 || die "adb not found in PATH"
[[ -f "${LOCAL_CONFIG}" ]] || die "${LOCAL_CONFIG} not found in current directory"

if ! adb devices | grep -q $'\tdevice$'; then
  die "no adb device in 'device' state"
fi

on_interrupt() {
  printf '\nStopped by user\n' >&2
  exit 130
}
trap on_interrupt INT TERM

index=1
while true; do
  if [[ -n "${COUNT}" && "${index}" -gt "${COUNT}" ]]; then
    break
  fi

  timestamp="$(date +%Y%m%d_%H%M%S)"
  local_trace="trace_${timestamp}_${index}.pftrace"

  printf '[%s] Start capture %d -> %s\n' "$(date '+%F %T')" "${index}" "${local_trace}"

  adb shell rm -f "${REMOTE_TRACE}" || die "failed to remove old remote trace"
  adb shell perfetto -c - --txt -o "${REMOTE_TRACE}" < "${LOCAL_CONFIG}" 2>&1 | \
    sed '/^[[:space:]]*$/d' || die "perfetto capture failed"
  adb exec-out cat "${REMOTE_TRACE}" > "${local_trace}" || die "failed to export trace to current directory"

  local_trace_path="$(pwd -P)/${local_trace}"
  [[ -s "${local_trace}" ]] || die "exported trace is empty: ${local_trace_path}"

  printf '\033[31m[%s] Pull succeeded: %s\033[0m\n\n' "$(date '+%F %T')" "${local_trace_path}"
  index=$((index + 1))
done
