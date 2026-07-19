# Standalone Winscope Web UI

这是一个可直接拷贝使用的 Winscope 抓取工具包。它已经包含官方 Winscope Web UI 静态产物和 Winscope ADB Proxy，不需要目标机器具备 AOSP 源码、Node.js、npm、Java 或 Gradle 环境。

## 功能

- 在本机启动官方 Winscope Web UI。
- 通过本地 Winscope ADB Proxy 访问 `adb` 已连接的 Android 设备。
- 在浏览器中采集 WindowManager、SurfaceFlinger、Perfetto、输入、录屏等 Winscope trace。
- 直接导入已有的 `.winscope` 文件进行查看；这类使用不依赖 `adb`。

## 运行环境

运行脚本的主机需要以下环境：

| 依赖 | 是否必须 | 用途 |
| --- | --- | --- |
| `bash` | 是 | 执行启动脚本。 |
| `python3` 3.10+ | 是 | 启动本地 Web UI 静态文件服务和 Winscope Proxy。仅使用 Python 标准库，无需安装第三方 Python 包。 |
| `adb` | 直接抓取时必须 | 查询 Android 设备、执行抓取与拉取 trace 文件。 |
| 现代浏览器 | 是 | 打开 Winscope Web UI。推荐 Chrome 或 Chromium。 |
| `xdg-open` | 否 | 自动打开浏览器；缺失时使用 `--no-browser` 后手动访问页面。 |

目标 Android 设备需要：

- 已通过 USB 或网络连接到本机，并在 `adb devices -l` 中显示为 `device`。
- 使用 `userdebug` 或 `eng` 系统镜像可获得最完整的 trace 能力。
- `user` 镜像可能限制 `su root`、WindowManager/SF legacy trace 或 Perfetto data source，具体可抓取类型取决于设备镜像和系统版本。

不需要：

- AOSP 源码树。
- Android 构建环境。
- Node.js、npm、yarn、pnpm。
- Java、Gradle。

启动脚本只检查 `python3` 命令是否存在；Proxy 会在启动时校验 Python 版本，低于 3.10 会直接退出。

## 目录结构

```text
winscope_trace/
├── README.md
├── winscope_webui.sh
├── vendor/
│   ├── winscope-ui/                 # 官方 Winscope Web UI 静态产物
│   └── winscope-proxy/
│       └── winscope_proxy.py        # 打包后的 Winscope ADB Proxy
└── runtime/                          # 运行时生成的 Token 和日志
    ├── .token
    ├── proxy.log
    └── webui.log
```

`runtime/` 是运行时目录，存放本地 Web UI 和 Proxy 的日志及 Token。启动脚本会将目录设为 `0700`，Token 和日志文件使用仅当前用户可读写的权限；Token 不会写入 `proxy.log`。

## 实现方式

### 本地 Web UI

启动脚本使用 Python 标准库启动静态文件服务：

```bash
python3 -m http.server 8080 --bind 127.0.0.1 --directory vendor/winscope-ui
```

浏览器访问 `http://127.0.0.1:8080` 即可加载打包后的官方 Winscope Web UI。服务只绑定 `127.0.0.1`，不会对局域网暴露。

### ADB Proxy

脚本同时启动：

```bash
WINSCOPE_TOKEN_LOCATION=runtime/.token \
python3 vendor/winscope-proxy/winscope_proxy.py -p 5544
```

Proxy 只监听本机 IPv4 回环地址 `127.0.0.1`。Winscope Web UI 通过该地址访问 Proxy，Proxy 再调用本机 `adb` 与设备交互；服务不会对局域网暴露。

手动填写 Proxy 地址时使用：

```text
http://127.0.0.1:5544
```

### Trace 启停处理

早期官方 Proxy 的停止逻辑依赖向本地 `adb shell` 进程发送 `SIGINT`，并期待远端 shell trap 执行停止命令。在部分 `adb + emulator` 组合中，远端 trap 不会触发，导致：

```text
TimeoutExpired(['adb', '-s', '<serial>', 'shell'], 15)
```

本工具包的 Proxy 已调整为：

1. 开始抓取时直接执行 Web UI 下发的 `startCmd`。
2. 结束抓取、保活超时或 Proxy 收到终止信号时，直接执行对应的 `stopCmd`，且每条 trace 只会停止一次。脚本等待最多 35 秒完成优雅停止，超时后终止本地 Proxy。
3. Trace 启停和普通 ADB 命令均应用 15 秒超时；浏览器 Proxy 的大文件 fetch 使用 10 分钟总时限与 30 秒无数据时限。非零退出会返回明确错误，避免把失败文本当作成功结果。
4. 每个运行中的 trace 需要 Web UI 持续调用 `/status` 保活；连续 30 秒没有收到保活请求时，Proxy 自动执行该 trace 的 `stopCmd`。
5. 单次文件拉取上限为 200 MiB，同一时刻只处理一个拉取请求；超过限制会被拒绝。文件先写入临时文件，再流式 gzip/Base64 编码为响应，避免为大文件同时保留多个完整内存副本。

这样可确保 WindowManager trace、`screenrecord` 和 detached Perfetto session 在结束时执行真实的设备侧清理命令。

## 使用方法

### 1. 连接并确认设备

```bash
adb devices -l
```

正常示例：

```text
List of devices attached
emulator-5554 device product:sdk_car_x86_64 model:Car_on_x86_64_emulator
```

如果设备显示为 `unauthorized`，请在设备上确认 adb 调试授权；如果显示为 `offline`，先恢复 adb 连接再启动 Winscope。

### 2. 启动工具

```bash
cd /path/to/winscope_trace
./winscope_webui.sh
```

脚本当前输出：

```text
UI:         http://127.0.0.1:8080
Proxy:      http://127.0.0.1:5544
Token:      <随机 Token>
Runtime:    /path/to/winscope_trace/runtime
```

按 `Ctrl+C` 会同时停止本地 Web UI 服务和 Proxy。

### 3. 在浏览器连接 Proxy

浏览器打开脚本输出的 UI 地址。首次进入 Winscope 后：

1. 在连接类型中选择 `Winscope Proxy`。
2. 填入 `Proxy`：`http://127.0.0.1:5544`。
3. 填入终端输出的 `Token`。
4. 选择显示的 adb 设备。
5. 选择需要的 trace 类型并开始抓取。
6. 完成操作后点击结束抓取，Winscope 会拉取文件并打开分析视图。

### 不自动打开浏览器

```bash
./winscope_webui.sh --no-browser
```

### 自定义端口

当默认端口已被占用时：

```bash
./winscope_webui.sh --ui-port 18080 --proxy-port 15544
```

对应访问地址为：

```text
UI:     http://127.0.0.1:18080
Proxy:  http://127.0.0.1:15544
```

### 从其他目录启动

```bash
/path/to/winscope_trace/winscope_webui.sh --root /path/to/winscope_trace
```

通常不需要指定 `--root`；默认使用脚本所在目录。

## 大文件导出

浏览器中的 Winscope Proxy 兼容接口适合普通 trace。视频或大型 trace 建议使用 `winscope_fetch.py`，它直接通过 ADB 导出文件，避免浏览器 Base64 解码占用大量内存。

该工具支持最大 200 MiB 文件、30 秒无数据超时、中断后续传、远端与本地 SHA-256 校验，以及校验成功后的原子完成。

```bash
python3 winscope_fetch.py \
  --serial <设备序列号> \
  /data/misc/wmtrace/screen.mp4
```

默认目标文件位于：

```text
runtime/downloads/<远端文件名>
```

可指定本机目标路径：

```bash
python3 winscope_fetch.py \
  --serial <设备序列号> \
  --output /path/to/screen.mp4 \
  /data/misc/wmtrace/screen.mp4
```

传输中断后再次执行相同命令，工具会检查 `<目标文件>.part.json` 中保存的远端路径、文件大小和 SHA-256；元数据一致时从 `.part` 已有字节偏移继续。远端文件变化、大小不一致或 SHA-256 不一致时，不会生成目标文件，方便保留现场分片排查。

主机临时目录需要至少保留目标文件大小的可用空间。下载完成后，在 Winscope UI 中通过 **Open trace file** 导入生成的文件。

## 常见问题

### 浏览器页面空白

先停止旧服务后重新启动：

```bash
./winscope_webui.sh
```

浏览器执行强制刷新：

```text
Ctrl+Shift+R
```

也可用无痕窗口重新打开 UI 地址。

### 提示端口被占用

使用其他端口：

```bash
./winscope_webui.sh --ui-port 18080 --proxy-port 15544
```

或者确认之前启动的脚本已经通过 `Ctrl+C` 正常退出。

### 文件拉取超时

Proxy 对 trace 启停使用 15 秒超时。浏览器 Proxy 的大文件 fetch 使用 10 分钟总时限与 30 秒无数据时限；推荐使用 `winscope_fetch.py` 导出视频和大型 trace，它只在连续 30 秒无数据时中断，并保留 `.part` 分片用于恢复。

重新连接设备后需要重新发起抓取；已经因设备退出而未拉取完成的 trace 文件无法从 Proxy 恢复。

### 未检测到已授权设备

检查：

```bash
adb devices -l
```

处理常用命令：

```bash
adb kill-server
adb start-server
adb devices -l
```

### 某些 trace 类型不可用或抓取失败

这通常是设备系统能力限制，不是 standalone 包缺少 AOSP。优先检查：

```bash
adb -s <serial> shell perfetto --query
adb -s <serial> shell cmd window tracing status
```

具体表现包括：

- `android.windowmanager` 未注册为 Perfetto data source 时，Winscope 会退回 legacy WindowManager trace。
- `user` 构建或受限设备没有 root 权限时，`su root` 命令会失败。
- 设备不支持某项 SurfaceFlinger、输入、ViewCapture 或录屏能力时，该 trace 不能采集。

详细运行日志位于：

```text
runtime/proxy.log
runtime/webui.log
```
