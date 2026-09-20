# 静态分析 MCP

MCP 把现有分析引擎接给 Agent。选择时区分：工具已经安装、MCP 协议可用、实际分析调用成功。`initialize` 和 `tools/list` 只能证明第二项。

## 随附集成

| 服务 | 固定版本 / 引擎 | 用途 | 前置条件 |
| --- | --- | --- | --- |
| JADX MCP + JADX AI MCP 插件 | 配套 `V6.4.1` + 本地 loopback 补丁；JADX 版本见工具链锁 | Java/Kotlin 反编译结果、方法、交叉引用、资源与 Manifest 导航 | 启动专用 JADX GUI、打开样本；桥接目标 `127.0.0.1:8650` |
| Apktool MCP | `V3.0.2` | 解码 APK、查询 Manifest、Smali、资源 | 本地 Apktool；解码输出写到独立项目目录 |
| PyGhidra-MCP | `0.2.5` | 无界面导入 `.so`、反编译、反汇编、导入导出、交叉引用 | Ghidra、JDK；一个 stdio 进程使用一个 Ghidra 项目 |
| Semgrep MCP | 随工具链固定的 Semgrep，内置 `semgrep mcp` | 用明确的自定义规则检查源码/反编译代码、查询支持语言 | 无需 Semgrep 账号；此配置关闭云端 findings、远程默认扫描和供应链扫描工具 |

前三项 MCP 是独立维护的社区集成；Semgrep 使用引擎仓库内置实现。不要把社区插件称作 JADX/Ghidra 引擎官方提供的 MCP。

所有客户端连接均使用本地 stdio，由客户端启动。JADX 的 Python 桥接进程还需要 GUI 插件的 HTTP 后端。它与纯 headless 服务不同；默认 Codex 配置禁用 `android-jadx`，通用 JSON 模板把它单列为可选项。

## 安装与配置

先按项目指令激活已有环境，例如 `source .envrc`。`SKILL_DIR` 是当前安装的 Skill 路径。

```bash
python3 "$SKILL_DIR/scripts/mcp_setup.py" plan --workspace "$PWD"
python3 "$SKILL_DIR/scripts/mcp_setup.py" install --workspace "$PWD"
python3 "$SKILL_DIR/scripts/mcp_setup.py" check --workspace "$PWD"
python3 "$SKILL_DIR/scripts/mcp_setup.py" configure --workspace "$PWD" --client codex
```

可用 `--servers apktool,ghidra,semgrep` 选择子集。安装器自动补装选中服务的引擎依赖，安装位置仍为 `.android-static/`，不会为了 MCP 接入安装 SDK 或接受新的 SDK 条款。不会替换已有 `.envrc`、系统 Java 或已有 Python venv。

- 下载包校验值在 `assets/mcp.lock.json`；基础引擎版本仍在 `assets/toolchain.lock.json`。
- 每个 Python MCP 使用独立 venv；首次解析后保存完整依赖版本、制品哈希和 pip report，重建时使用该锁。不能把首次解析描述为跨平台的完整依赖锁。
- `V6.4.1` 原始插件实测监听 `0.0.0.0:8650`，日志却写 `127.0.0.1`。安装器保留原 JAR，校验固定 Git commit 的 `PluginServer.java` blob，用 JDK 重新编译这一个类，将监听地址限定为 `127.0.0.1`。编译时导入名适配发布 JAR 已重定位的 Javalin；其他 class 不变。派生 JAR、源码 diff 与输入/输出哈希在 `.android-static/tools/mcp-jadx-loopback/`。它是本地派生产物，不是未修改的上游发布包。
- JADX GUI 的插件、配置与缓存隔离到 `.android-static/`。通过 `.android-static/bin/mcp-jadx-gui path/to/app.apk` 启动专用 GUI。
- PyGhidra 的 Chroma 语义索引模型在安装阶段下载并校验 SHA-256。一个小启动入口只调整 Chroma 的模型缓存位置至 `.android-static/tools/mcp-ghidra-embedding-model/`，不修改 `HOME` 或上游包；样本反编译与向量计算均在本地。
- Ghidra 每个 stdio 实例自动分配 `evidence/android-mcp/ghidra-projects/sessions/session-*/` 下的独立项目，避免多个客户端争用同一项目锁。退出后保留数据，各实例的分析状态分别保存；启动日志记录实际目录。旧项目保持原位置。需要继续已有分析时，可在启动命令后追加 `--project-path /absolute/path/to/project`（也支持上游的 `.gpr` 路径），但同一项目只能由一个活跃实例打开。Ghidra 拒绝包含以点开头目录的项目路径；工作区存在这类目录时，用 `--ghidra-project-dir` 指定兼容根目录。
- Semgrep 本地启动入口复用固定版本的工具注册和生命周期，仅接受 `stdio`，不执行 HTTP 登录配置查询，因此启动无需访问登录服务器。网络服务仍可通过上游 `semgrep mcp -t streamable-http` 使用原有认证流程；这个本地入口不提供 HTTP 服务。
- 生成的 `.android-static/mcp/servers.json` 供检查脚本使用；`codex.toml`、`mcp.json`、`vscode.json` 和 `jadx-optional.json` 是客户端配置模板。
- `configure --client codex|claude|cursor|vscode` 分别写入项目 `.codex/config.toml`、`.mcp.json`、`.cursor/mcp.json`、`.vscode/mcp.json`。保留其他配置；变更前保存 `.bak-*`，名称冲突时保留原文件并报错。Codex 的受管区块代表本次选择的服务集合。
- 路径为当前机器的绝对路径。移动工作区后应重新生成，不能把别人的模板路径直接复制使用。

启用 JADX 前，打开样本并核对插件端口；`--jadx-port` 调整的是 Python 桥接目标，不会自动修改 GUI 的监听端口。然后执行：

```bash
python3 "$SKILL_DIR/scripts/mcp_setup.py" configure --workspace "$PWD" --client codex --enable-jadx
```

Codex 的项目配置仅在受信任项目加载。重新打开会话后用 `/mcp` 查看连接状态。配置写入不意味着当前会话已经热加载。Exa、Tavily、Context7 用于研究和文档核对，已有服务无需重复安装，也不是 APK 分析引擎的运行依赖。

## 验证实际功能

`check` 为每项服务发出 `initialize`、`notifications/initialized`、分页 `tools/list`，保留工具 schema、原始 JSON-RPC 和 stderr；任何失败返回非零，并继续检查其余服务。对于 JADX，GUI 未启动时可能仍能列工具，不能据此声称能读取 APK。

`mcp_smoke.py` 可自动完成已知样本的解码、代码读取、原生反编译/语义检索与规则扫描，并核对测试前后样本哈希：

```bash
python3 "$SKILL_DIR/scripts/mcp_smoke.py" --workspace "$PWD" \
  --servers apktool,ghidra,semgrep \
  --apk /absolute/path/to/fixture.apk --apk-source 'fixture build record' \
  --class-name org.example.Probe --package-name org.example --apk-marker KNOWN_MARKER \
  --so /absolute/path/to/libfixture.so --so-source 'NDK build record' \
  --so-symbol Java_org_example_Probe_answer --so-marker KNOWN_NATIVE_MARKER
```

加入 `jadx` 并传入 `--start-jadx-gui` 可测试 GUI 桥接；无桌面环境另外传 `--xvfb`，需要系统已有 Xvfb。测试还检查实际监听地址，拒绝把通配地址监听算作通过。每次使用新的分析项目目录；Ghidra 程序名从接口返回值取得，不能假设它等于原始文件名。完整首次语义索引可能比反编译耗时更长。

实际样本先记录来源与 SHA-256。使用 `mcp_probe.py` 在同一个会话中按序调用，`--calls` 是 JSON 数组，每项包含 `name`、`arguments` 和可选的 `expect` 字符串；工具参数以该版本的 `tools.json` 为准。

```bash
python3 "$SKILL_DIR/scripts/mcp_probe.py" \
  --config .android-static/mcp/servers.json --server android-apktool \
  --calls evidence/apktool-calls.json --output evidence/apktool-mcp
```

最小闭环：

- Apktool：`decode_apk` → `get_manifest` → `get_smali_file`，核对包名、类名和已知字符串。
- JADX：专用 GUI 打开同一 APK → `get_android_manifest` → `get_class_source`，核对相同标记。插件的调试工具不属于静态验收。
- Ghidra：导入已知架构 `.so` → 列出符号 → 反编译已知导出函数，核对函数体；主机 ELF 不能代替 Android ARM64 验证。
- Semgrep：`semgrep_scan_with_custom_rule` 使用现场构造的代码和本地规则，断言预期 rule ID 命中；工具能列出语言不等于扫描已验证。

探针拒绝 JSON-RPC 错误、`isError`、不符合预期的结果与超时。某些上游工具会在成功响应里嵌入错误文本，因此实际验收应指定 `expect` 并检查输出内容。一次调用结束会关闭 stdio 子进程，不留测试服务后台运行。

上游服务还可能暴露重命名、构建、修改或删除分析项目等工具。此集成不是只读沙箱；静态流程使用读取与派生输出操作，保留原始样本。不要把客户端工具可见性过滤当成操作系统级隔离。

## 其他工具的选型

| 候选 | 适用情形 | 当前支持状态 |
| --- | --- | --- |
| `samudoria/revoid`、`xjoker/delamain` | 必须无 GUI 查询 JADX 时评估；前者直接使用 jadx-core，后者提供容器化 HTTP 接口 | 已发现上游，尚未集成或功能验证；delamain 上游明确说明接口仍可能变化 |
| `radareorg/radare2-mcp` | 已有 radare2 工作流，希望 Agent 使用原生分析状态 | radareorg 提供的 MCP；需要 radare2，不能把已有 Rizin 当作兼容替代；本次未安装 |
| MobSF REST API 与社区 MCP 适配 | 需要本地批量报告、CI 或 APK 多项检查 | 现有安装器保留固定镜像选项；容器健康、API key、适配器与报告生成必须逐项验证，当前未集成 MCP |
| FlowDroid | 需要基于 Android 生命周期的数据流/污点分析 | 专项分析器，按 SDK、sources/sinks 和样本条件安装；不是 MCP，也不保证反射/动态加载覆盖 |
| 已有许可的 IDA/JEB/Binary Ninja MCP | 商业引擎工作流 | 按已有版本与许可证选择，不自动获取商业软件 |
| Flutter / Unity IL2CPP / Hermes | JADX 无法覆盖对应业务逻辑时 | 见工具矩阵；版本匹配的专项工具优先于增加通用 MCP 数量 |

## 来源

核对日期：2026-09-16。Exa、Tavily 用于发现；Context7 查询 JADX/PyGhidra；发布制品和参数再核对上游。

- [JADX AI MCP V6.4.1（插件及配套 Python 服务）](https://github.com/zinja-coder/jadx-ai-mcp/releases/tag/V6.4.1)
- [Apktool MCP V3.0.2](https://github.com/zinja-coder/apktool-mcp-server/releases/tag/V3.0.2)
- [PyGhidra-MCP v0.2.5](https://github.com/clearbluejar/pyghidra-mcp/releases/tag/v0.2.5)
- [Semgrep MCP 实现](https://github.com/semgrep/semgrep/tree/v1.177.0/cli/src/semgrep/mcp)
- [Codex MCP 配置](https://developers.openai.com/codex/mcp)
- [radare2-mcp](https://github.com/radareorg/radare2-mcp)
- [Revoid](https://github.com/samudoria/revoid) · [delamain](https://github.com/xjoker/delamain)
- [MobSF](https://github.com/MobSF/Mobile-Security-Framework-MobSF) · [FlowDroid](https://github.com/secure-software-engineering/FlowDroid)
