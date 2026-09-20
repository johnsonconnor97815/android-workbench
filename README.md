# Android Workbench

统一维护 Android 分析能力，并让多个 Session 共用同一条设备队列。同一台手机由一个任务控制；不同手机和互不冲突的本地任务可以并行。

本仓库包含共享调度服务、CLI、MCP，以及五个 Skill：`android-analysis`、`android-device`、`android-static-env`、`frida-modified`、`pull-android-apk`。源码、测试、说明与许可证都在这里维护，不需要另行克隆原来的三个组件仓库。

## 独立安装

调度服务需要 Linux 和 Python 3.11+，使用 Python 标准库。设备操作另外需要 ADB；APK 校验需要 Android SDK 工具；Hook 需要合适的 Frida 环境。仓库提供对应准备能力，不携带 SDK、Frida 二进制或 APK 样本。

在本仓库目录执行，分析项目可以放在任意位置：

```bash
python3 scripts/bootstrap.py --project /path/to/analysis --configure-mcp
python3 scripts/workbench.py --project /path/to/analysis operations
```

这会安装独立调度运行时、创建 `workbench.project.json`、启动共享服务，并在分析项目中配置 MCP。基础安装无需网络、旧仓库、已有 `.venv` 或 `.envrc`。已有 Python 环境可用 `--python /path/to/python` 指定；需要的 Python 库装到该项目解释器中。

运行时位于 `${XDG_STATE_HOME:-~/.local/state}/android-workbench`。同一主机、同一账户的 Session 必须共用这个状态目录，才会进入同一条队列。`ANDROID_WORKBENCH_STATE` 可用于隔离测试；不要给共用手机的 Session 设置不同目录。

项目配置用 `workbench_root` 登记此仓库位置，用 `@workbench/skills/...` 引用内置代码。项目与仓库都可单独放置，不要求分析项目内再放一个 `android-workbench/`。仓库移动后，从新位置执行：

```bash
python3 scripts/configure_project.py /path/to/analysis --update
python3 scripts/configure_mcp.py /path/to/analysis
```

更新会备份旧配置，保留项目环境和自定义操作，重绑内置操作。仓库的 `.codex-plugin/plugin.json` 与 `.mcp.json` 是插件入口；插件本身仍需上述初始化步骤，安装插件不会自动下载整个分析工具链。

## 使用

以下命令在仓库目录执行；有 `.envrc` 的分析项目先在该项目中加载它。

```bash
python3 scripts/workbench.py --project /path/to/analysis capabilities
python3 scripts/workbench.py --project /path/to/analysis register-device phone-1 YOUR_ADB_SERIAL
python3 scripts/workbench.py --project /path/to/analysis devices
python3 scripts/workbench.py --project /path/to/analysis run apk.pull --device phone-1 -- \
  pull --serial YOUR_ADB_SERIAL --package com.example.app --output /path/to/analysis/apps/export
```

`operations` 列出 19 个内置脚本操作及其参数约定。截图、设备观察和有限时长场景是服务提供的操作，不计入这 19 个脚本。各 Skill 的原参数与校验过程保留；在已登记项目的工作目录调用内置脚本时，也会转交共享队列。

初始化后，可以用环境 Skill 安装工具，例如：

```bash
python3 scripts/workbench.py --project /path/to/analysis run environment.setup -- \
  plan --workspace /path/to/analysis --profile core
```

根据计划和环境要求再执行 `install`；SDK 许可证接受、系统依赖安装沿用原 Skill 的明确参数。运行环境、APK、Frida 的细节分别见 [环境 Skill](skills/android-static-env/SKILL.md)、[APK Skill](skills/pull-android-apk/SKILL.md)、[Frida Skill](skills/frida-modified/SKILL.md)。

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

另一个 Session 可提交绑定此任务 `scene` 的截图请求，并带上 `app`、`activity`、`queue_timeout`；接受现有 Hook 环境时设置 `accept_hooks:true`。检查点只能插入兼容观察，仍由原控制者执行。需要指定业务页面时设置 `ui_expect`，例如 `[{"resource-id":"com.example.app:id/title","text":"订单详情"}]`；采集前后都验证，XML 留作证据。详情见 [设备 Skill](skills/android-device/SKILL.md)。

用 `status`、`explain`、`artifacts` 查询任务，用 `cancel` 请求停止。取消受理不表示手机已释放；任务结果同时记录功能结果、`cleanup_ok` 和资源状态。`partial` 表示有部分可用结果；未知结果或未清理占用保留阻塞，不自动重放设备动作。

MCP 提供 18 个工具。用 `service_capabilities` 查询服务与 Session 身份，用 `sessions_select` 恢复稳定身份；任务提交必须使用稳定 `request_key`。新 Session 才会加载更新后的插件和 MCP 配置。

## 调度、模型与恢复

设备、ADB 服务、Python/SDK 环境、共享分析工程和输出路径都参与冲突检查。分析 MCP 通过代理登记实际使用时间；环境维护会等待使用者收尾。已有分析 MCP 可用 `scripts/configure_mcp.py /path/to/analysis` 接入。

默认新服务不调用 LLM。状态目录 `config.json` 中的 `llm` 支持 `none`、`codex`、`command`、`openai-compatible` 后端；修改配置后在空闲时重启服务。示例：

```json
{"backend":"codex","timeout":15,"calls_per_hour":30,"max_concurrent":2}
```

LLM 只在程序给出的合法候选之间建议顺序，不能越过锁、前置条件和清理要求。等待达到 120 秒的合法候选优先避免饥饿；模型失败、超时或建议过期时采用确定性排序。模型调用也有延迟，不能假定引入 LLM 会提高吞吐量。模型凭据不进入项目清单和任务证据。

`recover <设备ID> --wait` 可重检只读故障、人工交还和已适配的原生 Frida 探针清理；未知修改或残留进程继续阻止分配，不提供无条件清锁。

## 项目扩展

特定 App 的还原 Skill、项目脚本和样本留在分析项目内，通过项目清单显式登记。新项目默认不包含任何特定 App 或设备序列号。登记方式见 [项目扩展](docs/project-extensions.md)。`--update` 保留这类自定义操作。

## 维护与验证

直接修改本仓库内的代码。`sources.lock.json` 只记录最初导入来源；不再从旧仓库同步覆盖。组件许可证与补丁例外见 [NOTICE](NOTICE.md)。

```bash
# 完整离线回归：调度器、独立安装、APK、Frida、静态环境
python3 scripts/check.py
# 验证插件结构；打包当前仓库内容
python3 scripts/build_plugin.py --check
python3 scripts/build_plugin.py --output dist/android-workbench.zip
# 空闲时升级已安装的共享运行时；有活动任务会拒绝切换
python3 scripts/upgrade_runtime.py --project /path/to/analysis
```

ZIP 包包含相同源码、Skill 与测试，可解压到其他目录使用。`pyproject.toml` 提供可选的调度器 Python 包；完整插件交付使用仓库或 ZIP，而不是仅安装调度器 wheel。

真实设备与模型对照需另外执行，不在离线回归中调用手机或付费模型：

```bash
python3 scripts/acceptance.py --project /path/to/analysis \
  --devices phone-1 phone-2 --output /path/to/analysis/evidence/device-acceptance.json
python3 scripts/model_acceptance.py --output /path/to/analysis/evidence/model-comparison.json
```

保证范围是同一 Linux 主机、同一账户、已接入的入口。原始 ADB/Frida、人工触屏、多账户或多主机连接不受这些锁强制限制。当前实现与尚未实现部分见 [架构和边界](docs/architecture.md)。
