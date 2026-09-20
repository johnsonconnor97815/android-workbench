# 平台与故障处理

## 随附安装器

- 目标为 Linux x86_64（含 WSL2），Python 3.12+；当前主机为 Ubuntu 24.04。Ubuntu/Debian 可用 `--install-system-deps` 安装系统工具和 Python 编译依赖。
- 默认使用项目内 Temurin JDK 21 和私有 SDK。SDK 只安装 Command-Line Tools 与 Build-Tools，静态检查不要求 Android Studio、platform-tools、API platform、NDK 或模拟器。
- `plan` 不写文件、不访问网络。`install` 和 `check` 保留日志；`check` 不安装或升级依赖。首次 `check` 必须已有受管理环境。
- 只补装个别组件时使用 `--only`。全部选中组件均要通过，失败返回非零；后续同参数重跑复用缓存。
- 缺少 Python 时先通过发行版安装 Python 3.12 和 venv。启用 apt 的步骤会修改系统包；不需要时省略该选项。

## macOS / Linux ARM64 / Windows

随附制品锁没有覆盖这些平台。Skill 可按上游说明适配，不能直接声称同一命令已经跨平台验证。

- macOS：先核对 Intel/Apple Silicon；可使用 Homebrew 的 JDK、JADX、Apktool、Rizin、YARA、GNU binutils、LLVM、Graphviz，再为 Python 工具建独立 venv。Android 工具使用官方 macOS 包；区分系统 Mach-O 工具与分析 Android ELF 所需的 GNU/LLVM 工具。Ghidra 核对发布包包含的 native 架构。
- Linux ARM64：检查 Google SDK 主机工具是否提供适用架构，不能把 x86_64 Linux 工具当成 ARM64 本机可执行；优先使用已有 x86_64 分析主机/容器，明确仿真要求和未测试项。
- Windows：优先使用 WSL2 + Ubuntu 24.04 的路径；原生安装使用 Windows JDK、SDK 和 `.bat` 入口，并核对 Python 包 wheel 可用性。不要把 Bash 脚本当 PowerShell。
- 不自动安装新的包管理器、修改系统默认 Java 或覆盖已有工具。用户指定容器方案时可据锁文件生成相应构建方案。

## 常见失败

| 现象 | 处理 |
| --- | --- |
| TLS、代理、下载限流 | 保留日志，使用组织已有网络配置；不关闭 TLS 校验、不切换不明镜像。失败下载最多重试 3 次 |
| 哈希不匹配 | 保留失败文件，核对上游发布和下载链；不能重算一个新哈希后声称校验通过 |
| `UnsupportedClassVersionError` | 核对实际 `java` 路径和 JDK 版本；先 source 私有 env，再核对新版本工具最低要求 |
| `sdkmanager` 嵌套目录错误 | 正确路径为 `sdk/cmdline-tools/21.0/bin/sdkmanager`；bin/lib/source.properties 应属于同一个包 |
| SDK 许可未接受 | 按已有授权提供明确许可参数，或在终端交互处理；失败不能被 `|| true` 掩盖 |
| APKiD 找不到 YARA dex 模块 | 检查 APKiD 私有 venv 的 `yara-python-dex`，不能安装普通 `yara-python` 替代 |
| Python resolver/原生编译失败 | 读取对应 pip 日志、Python 版本和平台 wheel；保持各工具隔离，补齐编译依赖或选择支持的 Python 版本 |
| Ghidra GUI 无法显示 | headless 与 GUI 分开；无桌面环境先验证 `analyzeHeadless`，不把显示服务器缺失误判为反编译器损坏 |
| Quark 无规则或尝试联网更新 | 使用 `.android-static/quark-rules.path` 中的固定目录，通过 `quark --help` 核对 `-r` |
| Docker 不可用或没有权限 | 其余本机工具仍可安装；报告 MobSF 失败原因，不自动更改用户组或 Docker daemon 配置 |
| 原始 APK 分析失败 | 记录来源/哈希，先确认格式、base/split 完整性和资源/DEX 版本；CLI 可启动不保证每个 APK 都兼容 |

## 回退与复现

现有环境和安装输出各自独立。回退优先停止 source 新 env 或打开新 shell；确认所有需要的日志、规则和产物已留存后再移除 `.android-static/`。不要自动删除项目样本或 Docker 数据卷。系统 apt 包不由脚本自动卸载。

下载制品用 `locks/downloads.json` 追踪，Python 依赖和 pip 报告位于 `locks/`。跨机器重现需要同平台、同解释器和同系统依赖；pip 的 sdist 构建产物还受构建环境影响。不要把版本冻结等同于完整的离线镜像。
