# Android Perfetto 采集与分析

本目录提供一份面向 Android 平台问题定位的 Perfetto 文本配置，以及批量采集脚本。它适用于分析调度延迟、CPU 频率、Atrace、logcat、进程状态和 SurfaceFlinger FrameTimeline。

## 目录内容

| 文件 | 作用 |
| --- | --- |
| `config.txtpb` | Perfetto 文本格式 TraceConfig，默认采集 10 秒。 |
| `loop_perfetto.sh` | 将配置通过标准输入交给设备侧 `perfetto`，导出本机 `.pftrace` 文件，并按需重复采集。 |

## 环境要求

- 主机已安装 `adb`、`bash`，并且 `adb` 位于 `PATH` 中。
- Android 设备已启用 USB 调试，且 `adb devices -l` 显示状态为 `device`。
- Android 9 及以上设备可使用设备侧 Perfetto；Android 11 及以上系统通常默认启用 `traced` 服务。Android 9 和 10 的部分非 Pixel 设备可能还需要先执行 `adb shell setprop persist.traced.enable 1`。
- 在 `userdebug` 或 `eng` 镜像上采集到的内核事件和 Atrace 分类通常更完整；`user` 镜像会受 SELinux、内核配置及厂商裁剪影响。

连接多台设备时，请先指定目标设备，避免 `adb` 因设备选择不明确而失败：

```bash
export ANDROID_SERIAL=<设备序列号>
adb devices -l
```

## 当前配置

`config.txtpb` 默认使用两个 `DISCARD` 缓冲区，总大小为 68 MiB，并在 `duration_ms: 10000` 后自动结束。缓冲区写满后，后续事件会被丢弃；它不是持续覆盖旧数据的环形缓冲区。

已配置的数据如下：

- `linux.ftrace`：调度切换、唤醒、进程创建/退出、阻塞原因、CPU 频率、CPU 空闲和 `ftrace/print`。
- Atrace：`am`、`wm`、`view`、`input`、`binder_driver`、`gfx`、`hal`、`dalvik`、`memory`、`thermal` 等分类，并覆盖所有应用进程。
- `linux.process_stats`：在采集开始时扫描进程信息，写入独立缓冲区。
- `linux.sys_stats`：每秒记录 CPU 时间、fork 数和 CPU 频率统计。
- `android.log`：采集 logcat。
- `android.surfaceflinger.frametimeline`：采集 SurfaceFlinger 帧时间线。

实际可用的数据源和事件依赖设备内核与系统镜像。采集前可查看已注册的数据源：

```bash
adb shell perfetto --query
```

## 采集 Trace

进入本目录后执行脚本。脚本把 `config.txtpb` 通过标准输入传入设备侧 `perfetto`，避免旧系统上读取 `/data/local/tmp` 配置文件时受到 SELinux 限制。

```bash
cd /path/to/winscope_trace/perfetto
./loop_perfetto.sh
```

默认无限循环，每轮约 10 秒。按 `Ctrl+C` 停止后续采集。

### 指定采集次数

```bash
./loop_perfetto.sh --count 3
```

每个 trace 会保存到当前目录，文件名格式为：

```text
trace_YYYYMMDD_HHMMSS_序号.pftrace
```

脚本会先删除设备侧旧文件 `/data/misc/perfetto-traces/trace.pftrace`，再执行采集，并使用 `adb exec-out cat` 导出文件。采集完成后会检查本机文件是否为空。

## 查看 Trace

浏览器打开 [Perfetto UI](https://ui.perfetto.dev)，选择 **Open trace file**，或将 `.pftrace` 文件拖入页面。Perfetto UI 默认在浏览器本地解析 trace，不会自动上传文件；只有主动执行 Share 操作才会涉及上传。

时间线常用定位方向：

- CPU 线程轨道：确认目标线程何时运行、被谁抢占，以及处于 runnable 或阻塞状态的时长。
- Atrace 轨道：把 Framework、应用和 Binder 的关键 Slice 与调度时序关联起来。
- CPU Frequency/Idle：检查性能问题是否伴随降频、深度休眠或 CPU 资源不足。
- Android Logs：将 logcat 异常与调度、输入、窗口和渲染时序对齐。
- FrameTimeline：定位帧提交、合成及显示时延。

## PerfettoSQL 示例

在 Perfetto UI 左侧打开 **Query (SQL)**，执行以下查询。

### 调度上下文

```sql
INCLUDE PERFETTO MODULE sched.with_context;

SELECT
  ts,
  dur,
  cpu,
  thread_name,
  process_name,
  end_state
FROM sched_with_thread_process
WHERE process_name = '<目标进程名>'
ORDER BY ts
LIMIT 100;
```

### CPU 频率

```sql
INCLUDE PERFETTO MODULE linux.cpu.frequency;

SELECT
  ts,
  cpu,
  freq
FROM cpu_frequency_counters
ORDER BY ts
LIMIT 100;
```

### Atrace Slice

```sql
INCLUDE PERFETTO MODULE slices.with_context;

SELECT
  ts,
  dur,
  name,
  thread_name,
  process_name
FROM thread_or_process_slice
WHERE process_name = '<目标进程名>'
ORDER BY dur DESC
LIMIT 100;
```

`ts` 和 `dur` 的单位均为纳秒。分析前应先用时间线确定问题区间，再用 SQL 缩小到具体线程、进程或事件名称。

## GitHub Releases 的使用

[Perfetto Releases](https://github.com/google/perfetto/releases) 提供各平台的预编译工具包。适合需要离线保存固定版本、使用设备侧二进制，或要求主机分析工具与特定版本一致的场景。

### 选择资产

选择最新稳定版本的 Tag，并按**运行二进制的机器**选择资产，而不是按被分析 Android 设备选择：

| 运行环境 | Releases 资产 |
| --- | --- |
| Linux x86_64 主机 | `linux-amd64.zip` |
| Linux ARM64 主机 | `linux-arm64.zip` |
| macOS Intel 主机 | `mac-amd64.zip` |
| macOS Apple Silicon 主机 | `mac-arm64.zip` |
| Windows x86_64 主机 | `windows-amd64.zip` |
| Android ARM64 设备 | `android-arm64.zip` |
| Android x86_64 模拟器/设备 | `android-x64.zip` |

`perfetto-cpp-sdk-src.zip` 和 `perfetto-c-sdk-src.zip` 是 SDK 源码包，用于把 Perfetto 埋点集成到自有原生程序，不是查看 trace 所需的主机工具。

### 下载、校验与解压

下面以 Linux x86_64 为例。将 `<版本标签>` 替换为 Releases 页面中的 Tag，例如 `v57.2`；SHA-256 值应复制同一 Release 资产旁显示的值。

```bash
TAG=<版本标签>
ASSET=linux-amd64.zip
curl -fL -O "https://github.com/google/perfetto/releases/download/${TAG}/${ASSET}"
printf '%s  %s\n' '<Release 页面显示的 SHA-256>' "${ASSET}" | sha256sum -c -
unzip "${ASSET}" -d "perfetto-${TAG}"
```

校验命令输出 `OK` 后再使用压缩包内容。Windows 可在 PowerShell 中执行：

```powershell
Get-FileHash .\windows-amd64.zip -Algorithm SHA256
```

将输出的哈希值与 Release 页面中的 SHA-256 比较。macOS 可使用：

```bash
shasum -a 256 mac-arm64.zip
```

解压后先列出文件，确认工具名称：

```bash
unzip -l "${ASSET}"
```

在 Linux 或 macOS 上，常用的主机端分析工具是 `trace_processor_shell`。从解压目录运行：

```bash
./trace_processor_shell /path/to/trace.pftrace
```

进入交互式 PerfettoSQL 后，例如查询耗时最长的 Slice：

```sql
SELECT name, dur
FROM slice
ORDER BY dur DESC
LIMIT 20;
```

当前版本也支持子命令形式的一次性查询：

```bash
./trace_processor_shell query /path/to/trace.pftrace \
  "SELECT name, dur FROM slice ORDER BY dur DESC LIMIT 20"
```

不同 Release 的工具文件名和命令参数可能演进。先运行 `./trace_processor_shell --help`，并以当前版本帮助输出为准。

### 推荐的主机端快捷安装方式

只需要分析 trace 时，官方推荐下载 `trace_processor` 启动脚本。该脚本会按主机平台下载并缓存匹配的 `trace_processor_shell`，需要 Python 3：

```bash
curl -LO https://get.perfetto.dev/trace_processor
chmod +x trace_processor
./trace_processor /path/to/trace.pftrace
```

执行一次性 SQL：

```bash
./trace_processor query /path/to/trace.pftrace \
  "SELECT name, dur FROM slice ORDER BY dur DESC LIMIT 20"
```

使用固定 Release 压缩包可保证版本可复现；使用下载脚本可减少手动选择资产和解压步骤。两种方式都应尽量使用不早于产生 trace 的 Perfetto 版本，避免新 trace 字段无法被旧版 Trace Processor 解析。

## 常见问题

### `no adb device in 'device' state`

执行以下命令确认设备状态：

```bash
adb devices -l
```

设备显示为 `unauthorized` 时，在设备端确认调试授权；显示为 `offline` 时，重新插拔或执行 `adb kill-server && adb start-server` 后重试。

### `perfetto capture failed`

先检查设备是否提供 Perfetto 服务和目标数据源：

```bash
adb shell perfetto --query
```

如果 Android 9 或 10 设备没有启用服务，执行：

```bash
adb shell setprop persist.traced.enable 1
```

重启设备后再次检查。若特定 ftrace 事件或 Atrace 分类不可用，应根据 `perfetto --query` 输出和设备内核配置删减 `config.txtpb` 中对应项。

### 导出的 trace 为空或无法读取

确认设备侧 trace 文件存在并且大于 0：

```bash
adb shell ls -lh /data/misc/perfetto-traces/trace.pftrace
```

设备侧存储权限、`traced` 服务状态、缓冲区过小或数据源启动失败都可能导致文件异常。采集时保留脚本的标准错误输出，并在 Perfetto UI 导入失败时使用本地 `trace_processor_shell` 复查解析报错。

## 官方参考

- [Recording system traces with Perfetto](https://perfetto.dev/docs/getting-started/system-tracing)
- [Advanced System Tracing on Android](https://perfetto.dev/docs/learning-more/android)
- [Trace configuration](https://perfetto.dev/docs/concepts/config)
- [Trace Processor](https://perfetto.dev/docs/analysis/trace-processor)
- [Getting Started with PerfettoSQL](https://perfetto.dev/docs/analysis/perfetto-sql-getting-started)
- [Perfetto Releases](https://github.com/google/perfetto/releases)
