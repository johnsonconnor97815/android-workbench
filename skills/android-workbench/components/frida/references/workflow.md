# 操作与证据

命令中的 `COMPONENT` 指包含本参考文档所在组件的实际安装目录；`RUN` 指使用者新建的任务目录。下例先将 `COMPONENT` 替换为实际绝对路径，适用于全局、项目级或插件安装。以下变量均为本次任务输入，不是组件内置设备或版本。脚本要求 Python 3.10+；设备侧脚本依赖 ADB，功能测试还需要匹配版 `frida`。构建适配器首版针对 Linux x86_64 主机。

```bash
COMPONENT="/absolute/path/to/android-workbench/skills/android-workbench/components/frida"
RUN="$PWD/frida-run"
mkdir "$RUN"
python3 "$COMPONENT/scripts/inspect_device.py" --serial "$SERIAL" --check-root --output "$RUN/device.json"
```

`--serial` 在只有一台在线设备时可省略。未知属性写入 `unavailable`。环境采集不会推荐版本、安装 APK 或停止服务。可用 `--package` 补充目标 App 的安装路径及系统报告的 ABI；`null` ABI 不等于已验证进程架构。

## 选版与下载

保存 `requirements.json` 和 `selection.md`：必需/可选能力、server/Gadget、目标 App、候选列表、来源和顺序。选定候选后设置 `VERSION`。每次候选独立目录及客户端 venv。

```bash
python3 "$COMPONENT/scripts/fetch.py" --version "$VERSION" --kind source \
  --destination "$RUN/source" --record "$RUN/source-download.json"
python3 "$COMPONENT/scripts/fetch.py" --version "$VERSION" --kind server --abi "$ABI" \
  --destination "$RUN/official" --record "$RUN/server-download.json"
python3 -m venv "$RUN/client"
"$RUN/client/bin/pip" install "frida==$VERSION" frida-tools
"$RUN/client/bin/pip" freeze > "$RUN/client-packages.txt"
```

`ABI` 为 release 产物的 `arm`、`arm64`、`x86` 或 `x86_64`。探针里的进程架构名称使用 Frida 的 `arm`、`arm64`、`ia32`、`x64`。Gadget 下载用 `--kind gadget`。下载记录保留官方 URL、实际 commit、子模块、压缩与解压文件哈希；若 release 未提供 digest，记录本地哈希但不声称完成上游摘要核验。

获取完成不代表适配确认。先检查该版本工具链、配置/最小构建，再建立官方基线。缺少官方产物时可以使用锁定源码的无补丁构建，明确来源差异。

## 基线与回归

根据 [部署说明](deployment.md) 记录现有服务并串行切换版本。不要让两个 server 同时参与同一设备的注入。使用目标版本客户端运行：

```bash
"$RUN/client/bin/python" "$COMPONENT/scripts/probe_native.py" --serial "$SERIAL" \
  --version "$VERSION" --arch arm64 --runtime qjs --record "$RUN/baseline-native.json"
"$RUN/client/bin/python" "$COMPONENT/scripts/probe_native.py" --serial "$SERIAL" \
  --version "$VERSION" --arch arm64 --runtime v8 --spawn --record "$RUN/baseline-native-spawn.json"
```

这是新建原生进程的检查，包含注入、Hook、RPC、自身文件读取、卸载/detach 和存活检查。默认 root shell 可读取 server 创建的进程；适用的非 root 场景使用 `--no-root`。`--endpoint` 使用已有转发；不传时创建并清理专属转发。自定义 server 端口用 `--server-port`。

Java/App spawn 使用 [固定测试 App](app-regression.md) 或与目标版本适配的自有测试场景。按需求验证 QJS/V8、ARM32 和 Gadget，不把未选择能力强制加入任务。

Gadget 独立检查需先用对应 NDK 编译 `assets/native-probe.c`，并与 Gadget ABI 一致：

```bash
"$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android21-clang" \
  "$COMPONENT/assets/native-probe.c" -o "$RUN/native-probe-arm64" -ldl
"$RUN/client/bin/python" "$COMPONENT/scripts/probe_native.py" --serial "$SERIAL" \
  --version "$VERSION" --arch arm64 --gadget "$GADGET" --harness "$RUN/native-probe-arm64" \
  --no-root --record "$RUN/gadget.json"
```

ARM32 使用 `armv7a-linux-androideabi21-clang` 并传 `--arch arm`。检查完整 `dlclose`，但没有进行 APK 集成或重新签名。

## 记录和续作

源码输入可以锁定并再次核对：

```bash
python3 "$COMPONENT/scripts/provenance.py" --source "$RUN/source" --file "$PATCH_PROFILE" --record "$RUN/inputs.json"
python3 "$COMPONENT/scripts/provenance.py" --source "$RUN/source" --file "$PATCH_PROFILE" --record "$RUN/inputs.json" --verify
```

该检查覆盖实际 commit、子模块、tracked diff 和非忽略的未跟踪文件；构建脚本另外记录工具链、参数、生成资源和产物。设备、客户端、探针及构建产物仍需分别核对。不要把 provenance 成功视为设备状态已恢复。

测试脚本退出码：`0` 通过，`1` 功能/清理失败，`2` 缺少条件或已识别的探针不适配。检查 `functional_pass`、`pass`、`cleanup_errors` 及原始错误；功能通过但清理失败时 `pass` 必须为 `false`。

交付结果分别回答：本次选了什么版本及依据；必需/可选能力各是什么状态；指定修改是否到达实际运行产物；部署/恢复/清理是否完成；哪些目标 App 行为仍未验证。原始日志包含本机路径及设备标识，保存在使用者工作目录，不自动上传。
