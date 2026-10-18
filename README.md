# Android Workbench

统一维护 Android 分析和目标手机管理能力，并让多个 Session 共用同一条设备队列。同一台手机由一个任务控制；不同手机和互不冲突的本地任务可以并行。

本仓库是一个 Android Workbench 插件，包含共享调度服务、CLI、MCP、唯一入口 Skill `android-workbench`，以及三个 Claude Code 子代理。原先的五个 Skill 收敛为入口 Skill 内的组件和参考文档；源码、测试、说明与许可证都在这里维护，不需要另行克隆原来的三个组件仓库。

这里是上述能力的唯一维护源；独立历史仓库和插件缓存不是同步来源。静态环境组件的 full 工具链包含 Droid ASC，用于大型 APK 的类与引用快速定位。

## 插件结构

- `.claude-plugin/plugin.json` 是唯一维护的插件清单。`.codex-plugin/plugin.json` 是构建校验强制保持字节相同的 Codex 兼容镜像，不是第二来源。
- `skills/android-workbench/SKILL.md` 是唯一自动加载的 Skill。调度规则在 `references/`，静态环境、权限审计、分析代理、Frida、APK 导出、APK 逆向补丁和设备管理实现在 `components/`。
- `agents/` 提供 `static-analyst`、`device-analyst`、`report-auditor` 三个 Claude Code 子代理。它们只是分工入口，所有设备和共享环境操作仍必须走 Workbench MCP。
- App 复制使用新的 [入口驱动流程](skills/android-workbench/components/app-replica/README.md)：Astra 负责规划、分析和类结构，Sol 负责代码；文件协调、发现后再派发、具体 MVP 验证。旧流程和 Agent 已删除。
- `.mcp.json` 指向当前共享调度运行时。插件安装本身不下载 SDK、Frida 或分析工具链。

## 独立安装

调度服务需要 Linux 和 Python 3.11+，使用 Python 标准库。内置静态环境安装器要求 Linux x86_64 和 Python 3.12+；使用全部能力时建议以 Python 3.12+ 初始化项目。设备操作另外需要 ADB；分区刷写和 root 需要 `fastboot` 和已解锁 bootloader；官方完整 OTA 使用原厂恢复模式，允许锁定的 bootloader；APK 校验需要 Android SDK 工具；Hook 需要合适的 Frida 环境。仓库提供对应准备能力，不携带 SDK、Frida 二进制或 APK 样本。

刷机、安装或重装 Root 和部分设备模式切换需要有人在手机旁边，按提示按键、点屏幕或授权。流程开始前会提醒并说明预计的人工步骤；到了必须人工完成的步骤，等待用户操作并验证后再继续。已有 Root 授权记录到共享设备状态，普通 Root 操作和预检不重复提醒；记录失效或命令失败后再核验，确需人工授权时才提示。没人能完成必要人工步骤时，可以先研究、下载和校验文件、演练命令。具体步骤见[设备管理说明](skills/android-workbench/components/device-manager/README.md#operator-presence-and-manual-steps)。

普通预检只读取状态，需要验证 Root 权限时才加 `--check-root`。手机详细信息和 Root 状态分别持久保存、分别标记失效；普通检查复用授权时保留原验证时间。

在本仓库目录执行，分析项目可以放在任意位置：

```bash
python3 scripts/bootstrap.py --project /path/to/analysis --configure-mcp
python3 scripts/workbench.py --project /path/to/analysis operations
```

这会安装独立调度运行时、创建 `workbench.project.json`、启动共享服务，并在分析项目中配置 MCP。基础安装无需网络、旧仓库、已有 `.venv` 或 `.envrc`。已有 Python 环境可用 `--python /path/to/python` 指定；需要的 Python 库装到该项目解释器中。

运行时位于 `${XDG_STATE_HOME:-~/.local/state}/android-workbench`。同一主机、同一账户的 Session 必须共用这个状态目录，才会进入同一条队列。`ANDROID_WORKBENCH_STATE` 可用于隔离测试；不要给共用手机的 Session 设置不同目录。

只做目标手机管理时，不需要创建分析项目。把 `--project` 指向本仓库即可发现、登记和指定手机；该模式只开放 `device_manager.*` 设备管理操作。

项目配置用 `workbench_root` 登记此仓库位置，用 `@workbench/skills/...` 引用内置代码。项目与仓库都可单独放置，不要求分析项目内再放一个 `android-workbench/`。仓库移动后，从新位置执行：

```bash
python3 scripts/configure_project.py /path/to/analysis --update
python3 scripts/configure_mcp.py /path/to/analysis
```

更新会备份旧配置，保留项目环境和自定义操作，重绑内置操作，并清除指向已移除组件的旧内置 `replication.workflow`、`replication.emulator_compare` 登记。旧 `android-static-env`、`pull-android-apk`、`frida-modified` 和 `apk-permission-audit` 脚本路径会转到已登记的新组件；未登记的脚本仍拒绝执行。仓库的 `.claude-plugin/plugin.json` 与 `.mcp.json` 是插件入口；插件本身仍需上述初始化步骤，安装插件不会自动下载整个分析工具链。

## 使用

以下命令在仓库目录执行；有 `.envrc` 的分析项目先在该项目中加载它。

```bash
python3 scripts/workbench.py --project . discover-devices
python3 scripts/workbench.py --project . register-device phone-1 YOUR_ADB_SERIAL
python3 scripts/workbench.py --project . devices
python3 scripts/workbench.py --project /path/to/analysis capabilities
python3 scripts/workbench.py --project /path/to/analysis register-device phone-1 YOUR_ADB_SERIAL
python3 scripts/workbench.py --project /path/to/analysis devices
python3 scripts/workbench.py --project /path/to/analysis run apk.pull --device phone-1 -- \
  pull --serial YOUR_ADB_SERIAL --package com.example.app --output /path/to/analysis/apps/export
```

`operations` 列出当前项目实际登记的脚本操作及其参数约定，其中 `static.permission_audit` 提供安装包权限清单、代码证据和待审阅的报告草稿，`apkrev.*` 覆盖 APK 逆向、补丁、重打包、运行时和服务端分析，`analysis.*` 覆盖样本识别、分析阶段与完成检查、请求路由、包证据、Manifest、资源、预览、JADX 反编译、源码导航、知识检索、Python 计算（支持可保存 session 状态）、持久 scratchpad 和宿主执行，`device_manager.*` 覆盖目标手机预检、动态分区检查与修复、设备刷机研究、用户刷机决策、镜像下载与校验、原始镜像与共享存储备份、官方整包回刷、保数据完整 OTA、机型安装步骤与附加包、刷分区、功能验收、中断恢复、root 和 Frida server 安装。截图、设备观察、设备信息采集和有限时长场景是服务提供的操作，不计入脚本操作清单。各组件的原参数与校验过程保留；在已登记项目的工作目录调用内置脚本时，也会转交共享队列。

工具目录、版本锁和受管入口统一见 [Tools](Tools/README.md)。新项目默认共享同机固定版本工具，项目可以显式覆盖；已有安装原地登记，样本、分析状态和证据仍各自保存。用 `tools.inventory` 查看来源、`tools.check` 核对可用性。默认 `toolchain.auto_install:true`，声明工具依赖的操作自动先排队 `tools.ensure`，补装或修复并复查通过后再开始分析；版本升级另行处理。JS 语法树、可读恢复、ELF 布局和有限固件扫描分别使用 `tools.js_ast`、`tools.js_deobfuscate`、`tools.binary_layout`、`tools.firmware_scan`。原字节精确查询、未知问题修订、JS/map/HAR 离线入口和路由条件见 [REA 使用说明](docs/rea-adoption-usage.md)。修复失败时保留阻塞，不把目录登记或 CLI 启动当作完整样本、真机或 Hook 验证。

Google Play 应用通过 `apk.play_install` 安装，支持紧凑详情卡。未知布局输出 `needs_llm` 和证据，由当前 LLM 离线复现、修复通用解析规则并受管重试；账号、支付或设备政策门槛才交给用户。安装后使用 `apk.pull` 导出全部已安装 split。见 [Google Play 安装与 LLM 修复](skills/android-workbench/components/play-install/README.md)。

初始化后，可以用静态环境组件安装工具，例如：

```bash
python3 scripts/workbench.py --project /path/to/analysis run environment.setup -- \
  plan --workspace /path/to/analysis --profile core
```

根据计划和环境要求再执行 `install`；SDK 许可证接受、系统依赖安装沿用原组件的明确参数。权限审计、运行环境、分析代理、APK、Frida、APK 逆向补丁和目标手机管理的细节分别见 [权限审计组件](skills/android-workbench/components/permission-audit/README.md)、[静态环境组件](skills/android-workbench/components/static-env/README.md)、[分析代理组件](skills/android-workbench/components/analysis-agent/README.md)、[APK 导出组件](skills/android-workbench/components/apk-export/README.md)、[Frida 组件](skills/android-workbench/components/frida/README.md)、[APK 逆向组件](skills/android-workbench/components/apk-reverse/README.md)、[设备管理组件](skills/android-workbench/components/device-manager/README.md)。

有限场景示例：

```bash
python3 scripts/workbench.py --project /path/to/analysis submit - <<'JSON'
{
  "operation": "device.scene",
  "device": "phone-1",
  "request_key": "example-observation-001",
  "timeout": 30,
  "steps": [
    {"action": "observe"},
    {"action": "checkpoint", "seconds": 5, "budget": 3, "max_insertions": 1},
    {"action": "screenshot"}
  ]
}
JSON
```

另一个 Session 可提交绑定此任务 `scene` 的截图请求，并带上 `app`、`activity`、`queue_timeout`；接受现有 Hook 环境时设置 `accept_hooks:true`。检查点只能插入兼容观察，仍由原控制者执行。需要指定业务页面时设置 `ui_expect`，例如 `[{"resource-id":"com.example.app:id/title","text":"订单详情"}]`；采集前后都验证，XML 留作证据。详情见 [设备场景](skills/android-workbench/references/device-analysis.md)。

用 `status`、`explain`、`artifacts` 查询任务，用 `cancel` 请求停止。取消受理不表示手机已释放；任务结果同时记录功能结果、`cleanup_ok`、资源用量和资源状态。`partial` 表示有部分可用结果；未知结果或未清理占用保留阻塞，不自动重放设备动作。

MCP 工具清单以当前服务的 `tools/list` 返回为准；脚本操作用 `operations` 查询，两者分别计数。用 `service_capabilities` 查询服务与 Session 身份，用 `sessions_select` 恢复稳定身份；任务提交必须使用稳定 `request_key`。`artifacts_list` 查询命名产物索引；`evidence_state` 查看内部证据、保留策略和上次清理结果；`evidence_apply_retention` 立即执行保留策略；`hooks_subscribe` 在活动 Hook 场景检查点加入只读订阅者。`jobs_verify_runtime` 核对同项目真实任务、完整任务执行窗口与可信发布观察文件的 SHA-256；它只证明来源与文件一致，具体行为仍须审阅。运行记录格式见 [样本完成检查](skills/android-workbench/references/sample-analysis.md)。新 Session 才会加载更新后的插件和 MCP 配置。

Claude Code 中可用 `@android-workbench:static-analyst`、`@android-workbench:device-analyst`、`@android-workbench:report-auditor` 调用子代理。子代理会预载唯一入口 Skill，但仍通过同一 MCP 队列执行设备与环境操作。

## 调度、模型与恢复

设备、ADB 服务、Python/SDK 环境、共享分析工程和输出路径都参与冲突检查。分析 MCP 通过代理登记实际使用时间；环境维护会等待使用者收尾。已有分析 MCP 可用 `scripts/configure_mcp.py /path/to/analysis` 接入。

默认新服务不调用 LLM。状态目录 `config.json` 中的 `llm` 支持 `none`、`codex`、`command`、`openai-compatible` 后端；修改配置后在空闲时重启服务。示例：

```json
{"backend":"codex","timeout":15,"calls_per_hour":30,"max_concurrent":2}
```

LLM 只在程序给出的合法候选之间建议顺序，不能越过锁、前置条件和清理要求。等待达到 120 秒的合法候选优先避免饥饿；模型失败、超时或建议过期时采用确定性排序。模型调用也有延迟，不能假定引入 LLM 会提高吞吐量。模型凭据不进入项目清单和任务证据。

`environment.recover_host_lease` 会通过共享队列验证旧的只读宿主访问已无存活进程，再恢复它阻塞的环境资源；未知或仍存活的进程继续阻止恢复。

`recover <设备ID> --wait` 可重检只读故障、人工交还和已适配的原生 Frida 探针清理；未知修改或残留进程继续阻止分配，不提供无条件清锁。

## 项目扩展

特定 App 的还原 Skill、项目脚本和样本留在分析项目内，通过项目清单显式登记。新项目默认不包含任何特定 App 或设备序列号。登记方式见 [项目扩展](docs/project-extensions.md)。`--update` 保留这类自定义操作。

## 维护与验证

GitHub 自动检查分别验证 Python 3.11 下的调度器和 Python 3.12 下的全部组件。

直接修改本仓库内的代码。`sources.lock.json` 只记录最初导入来源；不再从旧仓库同步覆盖。组件许可证与补丁例外见 [NOTICE](NOTICE.md)。

```bash
# 完整离线回归：调度器、独立安装、APK、Frida、静态环境
python3 scripts/check.py
# 验证插件结构；打包当前仓库内容
python3 scripts/build_plugin.py --check
python3 scripts/build_plugin.py --output dist/android-workbench.zip
# 升级共享运行时；默认等待 5 秒让空闲分析服务退出，仍有活动任务就拒绝切换
python3 scripts/upgrade_runtime.py --project /path/to/analysis
```

更新本地插件时，将验证过的 ZIP 解压到临时目录，备份并整体替换插件市场登记的源目录，再执行 `codex plugin add android-workbench@<市场名称>`。不要直接修改插件缓存。重复发布相同版本号时，安装器可能复用旧缓存，应先确认安装结果的版本与内容；项目环境里的旧独立 Skill 可以备份到 Skill 搜索目录外。新会话才会加载更新后的入口。

每次内容变化使用新版本号。发布包的 `release-manifest.json` 保存版本和每个文件的 SHA-256，安装后执行下面的内容校验；它同时检查旧文件残留、源码与缓存差异，以及运行中服务的指纹，不能只看版本号。

```bash
python3 scripts/check_installation.py --installed /path/to/installed/plugin --runtime
```

新 App 先运行 `analysis.recon`，再把结果交给 `analysis.route --recon`；通过 `analysis.plan`、`analysis.plan_update` 和 `analysis.plan_check` 保存阶段和完成证据。具体步骤和审阅记录格式见 [样本识别与完成检查](skills/android-workbench/references/sample-analysis.md)。权限采集任务的结果会保留 `analysis_status.collection_only` / `review_required`，不能把采集成功作为最终审阅通过。

下面的验收通过共享队列覆盖结构识别、流程选择、证据、审阅、报告及完成检查。默认生成的包仅用于结构测试，不能安装到手机；提供实际 APK 和已知断言时另外验证权限与反编译采集。两种验收均不证明真机或 Hook 兼容性。

```bash
python3 scripts/analysis_acceptance.py --project /path/to/analysis --output-dir /path/to/analysis/evidence/acceptance-new
python3 scripts/analysis_acceptance.py --project /path/to/analysis --output-dir /path/to/analysis/evidence/apk-acceptance-new --apk /path/to/fixture.apk --expected-permission android.permission.CAMERA --expected-code checkSelfPermission
```

ZIP 包包含相同源码、入口 Skill、组件与测试，可解压到其他目录使用。`pyproject.toml` 提供可选的调度器 Python 包；完整插件交付使用仓库或 ZIP，而不是仅安装调度器 wheel。

真实设备与模型对照需另外执行，不在离线回归中调用手机或付费模型：

```bash
python3 scripts/acceptance.py --project /path/to/analysis \
  --devices phone-1 phone-2 --output /path/to/analysis/evidence/device-acceptance.json
python3 scripts/model_acceptance.py --output /path/to/analysis/evidence/model-comparison.json
```

保证范围是同一 Linux 主机、同一账户、已接入的入口。原始 ADB/Frida、人工触屏、多账户或多主机连接不受这些锁强制限制。当前实现与尚未实现部分见 [架构和边界](docs/architecture.md)。
