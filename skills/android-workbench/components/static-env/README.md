<!-- android-workbench-routing -->
在含 `workbench.project.json` 的工作项目中，使用 Android Workbench 统一入口提交已登记操作。本仓库内的脚本会转交共享服务；不要绕过队列操作手机或改写共用环境。服务不可用时先检查 `python3 <Workbench目录>/scripts/workbench.py --project <项目目录> capabilities`，不能退回未协调的直接执行。原能力范围、输入校验和退出码含义仍以本文为准。
<!-- /android-workbench-routing -->


# Android 静态分析环境组件

交付可运行的工具链、激活命令、版本与来源清单和实际验证结果。默认覆盖字节码、原生库和规则扫描；不要把“静态分析环境”缩成 JADX、Apktool 和 SDK 三项。

## 选择安装范围

先读取项目指令、已有 `.envrc`、工具安装与检查脚本。使用已有项目环境前执行 `source .envrc`；存在 `scripts/check_tools.sh` 时运行并记录结果。区分静态工具缺失与已有动态工具故障，不为搭建静态环境修理或升级 Frida 等无关组件。

读取 [工具范围与选型](references/tool-matrix.md)，按需求选择：

- **full（默认）**：Java、Android SDK、JADX、Apktool、Google smali/baksmali、dex2jar、bundletool、Androguard、Droid ASC、APKiD、Quark 及固定规则、Semgrep、Rizin、Ghidra、LIEF、pyelftools、Capstone、checksec，以及通用文件、ELF、YARA 工具。
- **core**：Java、SDK、JADX、Apktool、smali/baksmali、bundletool、Androguard。用户要求轻量环境时使用。
- **MobSF**：通过 `--with-mobsf --mobsf-image <官方版本标签或 digest>` 准备容器和 Compose 文件；需要 Docker。是否启动由用户的任务决定，安装脚本本身只准备镜像。
- **MCP 接入**：需要 Agent 直接调用分析引擎时，读取 [MCP 配置与验证](references/mcp.md)。随附安装器支持 JADX、Apktool、PyGhidra 与 Semgrep；生成 Codex、Claude Code、Cursor、VS Code 项目配置，并区分协议握手与样本功能验收。
- **专项补充**：CFR/JD-GUI、Cutter、Flutter/Dart、Unity IL2CPP、Hermes、源码 Lint/detekt 等按实际输入选择；见工具范围文档。商业工具使用用户已有安装和许可。

## 执行

`COMPONENT_DIR` 指实际包含本文件的目录，不能假设当前工作目录就是组件目录。

随附安装器面向 **Linux x86_64 / WSL2，Python 3.12+**。Ubuntu 24.04 是当前验证环境。其他系统先读 [平台与故障处理](references/platforms.md)，按官方渠道适配，不执行错误架构的二进制。

```bash
python3 "$COMPONENT_DIR/scripts/setup.py" plan --workspace "$PWD"
python3 "$COMPONENT_DIR/scripts/setup.py" install --workspace "$PWD" \
  --profile full --install-system-deps --accept-sdk-licenses
source "$PWD/.android-static/env.sh"
python3 "$COMPONENT_DIR/scripts/setup.py" check --workspace "$PWD" --profile full
python3 "$COMPONENT_DIR/scripts/smoke.py" --workspace "$PWD"
```

`--install-system-deps` 安装记录在锁文件中的 apt 包；依赖已满足时可省略。非 root 使用 `sudo -n`，权限不足时给出具体缺失项和命令。`--accept-sdk-licenses` 明确接受 Android SDK 条款；按用户已有授权使用，未提供参数时脚本不会自动接受。插件只复制说明和资源。仅在用户要求搭建环境时执行工具链安装。

补装可使用 `--only apkid,quark,rizin`；Java 依赖自动加入。`check` 使用相同范围，输出失败项并返回非零状态。网络失败后可重跑同一命令：已验证下载和已安装版本会复用，下载校验失败不会被忽略。

版本入口为 [assets/toolchain.lock.json](assets/toolchain.lock.json)。默认不跟随 `latest` 升级。用户要求更新、固定产物不可用或新格式不兼容时，读取 [官方来源与核对记录](references/sources.md)，核对上游发布、运行时要求和架构，再修改版本锁并验证。Exa、Tavily 用于找官方页面，Context7 用于查具体安装/API 文档；它们是研究辅助，不是环境运行依赖。搜索摘要与上游制品元数据冲突时以实际官方产物和运行验证为准。

## 安装约束

- 工具与 Python 环境安装到 `<workspace>/.android-static/`。每个 Python 工具独立 venv，尤其不能把 APKiD 的 `yara-python-dex` 与普通 `yara-python` 混装。
- 保留已有 `.envrc`、`.venv`、SDK、shell 启动文件和版本锁。不存在 `.envrc` 时才生成；已有配置的用户单独 `source .android-static/env.sh`。
- 下载先校验再解包。上游仅提供 SHA-1 时记录算法；上游没有校验值时，记录“首次下载哈希”，不能称为上游签名或来源真实性验证。
- 保留下载锁、Python 依赖锁及 pip 制品信息。规则库固定 commit；升级规则与升级扫描器分别记录。系统包的版本由发行版仓库决定，不能把整个环境描述为字节级可复现。
- MobSF 使用明确版本并记录镜像 digest，Compose 只绑定 `127.0.0.1:8000`，保留数据卷。未经任务要求不上传样本到在线服务、不连接设备、不运行 APK 或原生样本。

## 验证与交付

CLI 启动检查和真实分析分开报告。随附 `smoke.py` 使用现场生成的 Smali/DEX 和 Java 规则样本，验证汇编、反汇编、反编译与本地扫描；可用 `--apk <文件> --source <来源说明>` 增加真实 APK 的静态检查。先记录样本 SHA-256 和来源，原样本保持只读。

`.so` 功能验证使用 `--so <文件> --so-source <来源> --so-symbol <导出函数>`，按 ELF 机器类型和函数地址区分 ARM/Thumb；不能把主机 ELF 测试当作 Android ARM64 已验证。`so-info <文件> --source <来源>` 使用 native-python 环境静态读取 ELF、JNI 导出、依赖、保护位并做有限指令解码。JNI 动态注册和完整函数语义仍需 Ghidra/Rizin 等继续分析。

以 `.android-static/manifest.json`、`.android-static/logs/`、`.android-static/locks/` 和 `docs/android-static-manifest.json` 为安装证据；以 smoke 输出为功能证据。所有选中组件成功后才能报告该安装范围完成。部分失败时继续处理独立组件，明确失败和未测试范围，不把 `--help` 成功当成完整分析能力验证。

交付只需说明安装位置、激活/检查命令、已验证范围、失败或限制和证据路径。首次使用 GUI、Docker 服务健康、特定样本兼容性需分别验证；CLI 检查不能替代它们。

本次开发验收范围和未测试项见 [验证记录](references/validation.md)，用于评估这套基线的实际覆盖。
