---
name: android-analysis
description: 统一组织 Android 环境准备、APK 导出、静态分析、Frida 定制和设备场景，并通过共享服务协调多个 Session 的手机与工具环境占用。用于需要组合这些能力、查询队列或解决设备操作冲突的分析任务。
---

# Android 分析工作台

先读取使用者项目的 `workbench.project.json`；项目有 `.envrc` 时执行 `source .envrc`。未初始化时，用 `python3 <插件目录>/scripts/bootstrap.py --project <项目目录>` 安装共享运行时并登记本仓库能力。项目专用能力保留在项目中，通用组件随插件分发；不要将项目 APK 或证据复制进插件。

使用 `service_capabilities` 确认共享服务、模型模式及 `client_session`。连接优先使用 `CODEX_THREAD_ID`；若没有该环境变量，保存返回的身份，重连时先用 `sessions_select` 恢复同一身份。用 `operations_list` 获取已登记脚本及参数约定。任务提交、进度、取消和产物分别使用 `jobs_submit`、`jobs_status`、`jobs_cancel`、`jobs_artifacts`。提交必须带稳定的 `request_key`；调用超时后用相同内容和相同标识查询或重试。多台设备时使用明确设备 ID，不能按型号替换来源手机。

能力路由：

- 环境安装及分析 MCP：读取 `android-static-env`。修改环境要等实际使用者收尾。
- 已安装 App 的 APK：读取 `pull-android-apk`，通过 `apk.pull` 操作提交原有 `pull` 参数；保留拆分、签名与来源校验。
- Frida 版本、补丁、构建和部署：读取 `frida-modified`；通过 `frida.*` 登记操作执行，不用日常 Hook 代替构建验证。
- 截图、界面操作、受管 Hook：读取 `android-device`。同设备任务默认连续执行，只有适配后的检查点允许兼容观察。
- 项目专用能力：仅使用项目清单显式登记的扩展，读取对应 Skill；保留其输入哈希、适用版本和部分完成含义。纯离线操作不申请手机。

已有脚本在登记项目中运行时会转交服务。服务不可用时先检查启动状态，不退回裸 ADB/Frida。CLI 同样可用：`python <插件目录>/scripts/workbench.py --project <项目目录> operations`；执行前用 `start` 启动独立服务。

队列中的任务可能因当前页面可复用而重排。LLM 只给排序建议，不能替代前置条件、资源检查或清理。`partial` 表示有可用结果但未全部完成；`needs_recovery` 表示不能继续信任占用或操作结果。取消已受理也不表示设备已经空闲。

用户要看“当前这一页”时，指定场景、App、Activity 和有限排队时间；过期后说明现场已消失，不用另一页截图代替。需要无 Hook 基线时不得借用活动 Hook 的现场。读取已有不可变证据可直接进行，不重新占用手机。
