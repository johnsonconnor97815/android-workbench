# Tools：统一工具入口

这里登记工具、固定版本和受管调用方法。`Tools/` 内的代码和锁文件随插件发布；下载的 Node/Python/Rust 工具放在工作项目的 `Tools/runtime/`，不进 Git。现有 `.android-static/` 和 `.tools/` 原地复用；统一的是管理入口，不是把已有字节搬到新目录。

操作都通过共享队列提交。不能直接调用下载目录里的二进制绕过队列。示例中的 `<Workbench>` 是本仓库的实际路径，`<项目>` 是已经登记的分析项目。当前目录可以是任意目录。

```bash
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.inventory -- inventory
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.check -- check --tool babel
python3 <Workbench>/scripts/workbench.py --project <项目> run --timeout 3600 tools.install -- install --tool babel
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.js_ast -- js-ast --input app.js
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.js_deobfuscate -- js-deobfuscate --input bundle.js
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.binary_layout -- binary-layout --input libapp.so
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.firmware_scan -- firmware-scan --tool binwalk --input firmware.bin
python3 <Workbench>/scripts/workbench.py --project <项目> run tools.firmware_scan -- firmware-scan --tool unblob --input archive.bin
```

队列默认把 JSON 结果放在任务目录的 `tool-result.json`。可显式设置 `--output <新JSON文件>`；分析产物放在相邻的 `<JSON文件>.artifacts/`。这两个路径都参与锁定和输出容量检查。已有显式结果路径和分析产物不覆盖，原输入只读；`Tools/runtime/*-install.json` 是可刷新的内部安装回执。结果保留命令、退出码、输入 SHA-256、实际分析输出、工具版本（未知时写 unknown）和失败原因。没有安装成功的记录、只有版本检查，都不能称为真实样本验证。

## 本轮可用范围

| 工具 | 固定版本 | 安装参数 | 分析能力 |
| --- | --- | --- | --- |
| Babel parser + traverse | 7.28.4 | `--tool babel` | JS/JSX/TS/TSX 语法树、函数、调用表达式、导入及分作用域的变量引用；不执行源码 |
| Wakaru | 1.13.0 | `--tool wakaru` | `.js/.mjs/.cjs` 可读语法恢复；实际源码路径记录在 JSON 的 `analysis.recovered_source` |
| Rust 最小工具链 | 1.90.0 | `--tool rust`，Binwalk 自动补齐 | 官方 rustc/Cargo/标准库，本项目内编译，不修改全局配置 |
| Binwalk | 3.1.0 | `--tool binwalk` | 离线签名扫描；不启用解包 |
| Unblob | 26.6.4 | `--tool unblob` | 单层签名扫描和切出已识别字节；禁用外部解包器 |
| pyelftools | 0.33，复用 native-python | `--tool native-python` | 静态读取 ELF 的段、节、符号和依赖；支持 Android ELF，机器类型从文件读取 |
| Frida | 调用者明确指定版本 | 原 `frida.fetch` / `frida.build` 等操作 | 登记 `.tools/frida-src` 来源，继续使用既有设备和构建约束；源码存在不证明可运行 |
| 原静态工具链 | [原锁文件](../skills/android-workbench/components/static-env/assets/toolchain.lock.json) | `--tool jadx` 等 | 复用安装器、版本锁、来源、已有下载及环境，不重新定义它们的功能 |

`tools.inventory` 区分 `installed`（路径存在）、`not_installed` 和 `unsupported`，另列当前检查 `not_checked/passed/failed`；旧安装 manifest 只作历史证据。`tools.check` 对 Babel/Wakaru/ELF/两种扫描器运行固定合成样本；其他既有工具报告 CLI/import 检查范围，完整样本检查继续使用 `static.smoke`。任何未安装、失败或不支持项让整表检查标记 partial，不能把部分通过写成全套完成。按需检查单项可避免把目录项当成必装需求。

Babel/Wakaru 用已提交的 npm 完整依赖锁安装，关闭安装脚本，无全局 npm 写入。Unblob 使用独立虚拟环境、固定顶层版本，保留 pip 制品报告及依赖冻结表；转递依赖首次解析不是完全复现锁。Binwalk 使用 `cargo install --version 3.1.0 --locked --root ...`，自动准备固定 Rust 1.90.0 最小工具链，仍依赖宿主 C 编译器及 liblzma 开发库；缺少系统依赖时明确失败，不自动 apt。Rust 三个独立组件从官方固定日期下载，逐项核对 [rust.lock.json](rust.lock.json) 中的官方 SHA-256，再安全解包并运行本地 `install.sh --prefix=<Tools/runtime/rust/1.90.0> --disable-ldconfig`。这不是 GPG 签名验证。Cargo 缓存也在 `Tools/runtime/cargo-home`；编译命令显式设置工具路径，不写 `~/.cargo`、`~/.rustup` 或 shell profile。已安装版本符合检查时复用，不盲目升级。SDK 条款只有传入 `--accept-sdk-licenses` 才接受。

工具 stdout/stderr 直接写盘，合计超过 2 MiB 时停止，JSON 中分别记录字节数、保留路径和截断标记；不会把截断输出解析成完整结果。

扫描器固定上限：输入 64 MiB、一个工作进程、扫描深度 1、CPU 90 秒、墙钟 120 秒、单文件 128 MiB、地址空间 4 GiB。共享任务另有累计输出预算。Unblob 保留 Linux Landlock 沙箱，不暴露关闭沙箱、强制覆盖、自定义插件或任意参数。Landlock 是系统限制程序读写范围的功能；系统不支持时失败，不自动关闭。扫描前复制输入到任务产物目录，避免上游切割触碰原文件。切出的文件不执行。扫描不是通用解包、固件重建、解密或设备运行证明。

补充工具通过相同的 `tools.install` / `tools.check` 入口管理：

| 工具 | 固定版本 | 安装参数 | 检查范围 |
| --- | --- | --- | --- |
| ILSpyCmd / .NET SDK | 11.1.0.9782 / 10.0.401 | `--tool ilspy` | CLI 启动与版本；SDK 包含运行时 |
| Evmole | 0.9.3 | `--tool evmole` | 导入、版本和固定 STOP 字节码的空选择器结果 |
| Pwntools | 4.15.0 | `--tool pwntools` | 导入、版本和固定字节打包/循环序列检查 |
| GDB / Pwndbg | 官方 portable 2026.09.15 | `--tool gdb-pwndbg` | 自带 GDB 与 Pwndbg 批处理启动、版本；不修改 `~/.gdbinit` |
| mitmproxy | 12.2.3 | `--tool mitmproxy` | CLI、版本和内存中 HTTP 请求对象；不监听端口、不采集流量 |

Python 工具各自独立 venv，保留 pip 制品报告与依赖冻结表，顶层固定版本，转递依赖首次解析不是完整复现锁。.NET SDK 与 Pwndbg portable 下载按 [optional.lock.json](optional.lock.json) 的官方 SHA-512/SHA-256 校验后安全解包。ILSpy 使用固定 NuGet 包；.NET/NuGet 缓存与 CLI home 放在项目运行时目录，不修改 shell 或全局 SDK。这些安装和检查不代表已经提供对应样本分析适配器，也不证明真实样本兼容性。

Hopper、IDA 和 macOS 运行时继续登记来源与限制。Hopper 官方有 Linux 下载，但完整使用需要商业许可，试用不能算完整授权安装；IDA 复用使用者已有安装，本入口不重装或读取许可。macOS/Xcode 不能安装成 Linux 本机运行时。所有条目见 [catalog.json](catalog.json)。

## 来源与核对

本轮核对使用 Exa 查官方入口、Tavily 读取文档、Context7 查 Babel API，并直接查上游包元数据及固定版源码。锁定版本来自 REA 已核对基线和包仓库，未跟随 latest。

- [Babel parser API](https://babeljs.io/docs/babel-parser)、[traverse API](https://babeljs.io/docs/babel-traverse)。Babel 语法树是真正的语法结构，不把词法搜索当成语法树；调用表达式也不等于完整调用图。
- [Wakaru 官方仓库](https://github.com/pionxzh/wakaru)、[CLI 参考](https://wakarujs.com/docs/reference/cli)、[1.13.0 npm 元数据](https://registry.npmjs.org/wakaru/1.13.0)。固定版使用 Linux x64 Rust CLI 包，锁中包含其制品完整性值。
- [Rust 官方独立安装器说明](https://forge.rust-lang.org/infra/other-installation-methods.html)、[固定 1.90.0 组件清单与校验](https://static.rust-lang.org/dist/channel-rust-1.90.0.toml)。Binwalk v3.1.0 官方 release API 没有上传的预编译资产，因此用已核对工具链编译。
- [Binwalk 官方仓库](https://github.com/ReFirmLabs/binwalk)、[3.1.0 crate 元数据](https://crates.io/api/v1/crates/binwalk/3.1.0)。仅扫描命令不启用 `--extract`。
- [Unblob 安装与外部解包器要求](https://unblob.org/installation/)、[26.6.4 PyPI 元数据](https://pypi.org/pypi/unblob/26.6.4/json)。固定源码 `cli.py` 确认 `--skip-extraction` 仍会切割字节，不能表述为没有输出。

这里只记录源码和离线能力。每台机器是否安装、每项样本是否通过，以当次受管任务记录为准。

补充工具安装依据：[ILSpy 官方仓库](https://github.com/icsharpcode/ILSpy)、[微软 Linux 本地 SDK 安装说明](https://learn.microsoft.com/en-us/dotnet/core/install/linux-scripted-manual)、[Evmole 官方仓库](https://github.com/cdump/evmole)、[Pwntools 安装文档](https://docs.pwntools.com/en/stable/install.html)、[Pwndbg portable 发布](https://github.com/pwndbg/pwndbg/releases/tag/2026.09.15)、[mitmproxy 安装文档](https://docs.mitmproxy.org/stable/overview/installation/)、[Hopper Linux 下载](https://www.hopperapp.com/download.html)。

静态补充工具：CFR 0.152、JD-GUI 1.6.6、Cutter 2.5.0、Il2CppDumper 6.7.46、Cpp2IL 2022.1.0-pre-release.21，可通过相同入口按名称安装。CFR 和 JD-GUI 的内置反编译器检查固定 Java 类；Cutter 使用宿主已有 xvfb-run 的隔离 X 显示检查 CLI 版本，不证明图形界面可用。Il2CppDumper 用项目 .NET 10 的明确 `--roll-forward Major` 启动官方 net6 包；Cpp2IL 为官方预发行包。两者只有启动检查，固定下载版本另记为 `pinned_artifact_version`，未读取版本时实际版本为 unknown；不证明任意 Unity 样本兼容。

Cutter/Cpp2IL 使用上游资产 SHA-256；CFR 同时核对 Maven SHA-1，其 SHA-256、JD-GUI 与 Il2CppDumper SHA-256 为首次官方下载固定值，不是上游签名。blutter 和 Hermes hbcdump 需要实际目标的 Dart/Hermes 版本，暂不下载猜测版本。动态分析用 Objection/tshark 暂不安装。来源：[CFR](https://www.benf.org/other/cfr/)、[JD-GUI](https://github.com/java-decompiler/jd-gui)、[Cutter](https://github.com/rizinorg/cutter/releases/tag/v2.5.0)、[Il2CppDumper](https://github.com/Perfare/Il2CppDumper)、[Cpp2IL](https://github.com/SamboyCoding/Cpp2IL/releases)、[.NET roll-forward](https://learn.microsoft.com/en-us/dotnet/core/versions/selection)、[blutter](https://github.com/worawit/blutter)、[Hermes](https://github.com/facebook/hermes)。
