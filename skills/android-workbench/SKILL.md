---
name: android-workbench
description: 统一组织 Android 环境准备、APK 导出、静态分析、Frida 定制、设备场景和目标手机管理，并通过共享服务协调多个 Session 的手机与工具环境占用。用于需要组合这些能力、查询队列或解决设备操作冲突的分析任务。
license: MIT
metadata:
  author: johnsonconnor97815
  version: "0.2.3"
---

# Android Workbench

这是 Android 分析插件的唯一入口 Skill。先读取 [共享调度与路由规则](references/workbench-routing.md)，再按任务读取一个必要的模式文档；不要为了普通请求加载全部组件。

| 任务 | 读取 |
| --- | --- |
| 多能力组合、队列、冲突与恢复 | `references/workbench-routing.md` |
| 设备信息、截图、页面操作、状态观测、受管 Hook | `references/device-analysis.md` |
| 静态环境、分析 MCP、工具链版本 | `components/static-env/README.md` |
| APK 权限清单、风险分诊与代码用途报告 | `components/permission-audit/README.md` |
| 分析请求路由、包概览、Manifest/资源/预览、JADX 反编译、源码导航、签名、字符串、知识检索、Python 计算/session 状态、持久 scratchpad 与宿主执行 | `components/analysis-agent/README.md` |
| APK 逆向流程、方法级 DEX 补丁、USB 联网代理、重打包、能力矩阵与 `apkrev.*` 操作索引 | `references/apk-reverse.md` |
| Frida 选版、修改、构建、部署 | `components/frida/README.md` |
| 已安装 App 的 APK 导出与校验 | `components/apk-export/README.md` |
| Google Play 安装、紧凑详情卡、未知布局的 LLM 修复 | `components/play-install/README.md` |
| 目标手机刷机研究、刷系统、root、Frida server 安装 | `components/device-manager/README.md` |

刷机、安装或重装 Root，以及其他需要手机按键、屏幕确认的操作，开始前先提醒用户必须有人在手机旁边，并说明预计的人工步骤。人在场的确认不能从任务授权、`--confirm` 或研究缓存中推断；本次流程已明确有人在场时，不重复询问。已有 Root 授权不每次提醒：先读 `devices_state.device_status`，`stale:false` 且 `root_access.status:granted` 时，按已验证的执行身份直接进行普通 Root 操作；首次授权、记录失效、命令失败或实际出现弹窗时才重新核验，确需手机操作时再提示。无人能完成必要人工步骤时，先完成研究、下载、校验和不接触手机的演练，实际设备操作等有人在场再开始。

普通 `device_manager.preflight` 只读取状态，不请求 Root 权限；任务确需验证 Root 且没有可复用记录时，才加 `--check-root`，该检查可能触发手机授权。复用结果保留原 Root 验证时间；刷机等操作后，`device_status` 和 `last_device_info` 各自标记失效并由对应操作刷新。

插件中的 Claude Agent 只是窄域分工入口，不是资源所有者。设备、ADB、Frida、共享分析工程和输出路径都通过 Workbench MCP 或已登记操作协调；服务不可用时报告阻塞，不退回裸 ADB/Frida。

Google Play 链接使用 `apk.play_install`，不替换成第三方 APK 镜像。返回 `outcome:needs_llm` 时，当前 LLM 必须读取 `llm-repair.json` 和页面证据，按照 `components/play-install/README.md` 建立离线复现、修复通用规则、通过回归，再以新任务标识受管重试；同一故障最多两轮，每轮必须有可验证的进展。普通 UI 布局变化不要求用户代点。`requires_user` 表示账号、支付、政策或锁屏等真实人工门槛；点击后的 `uncertain` 先核验和恢复，不盲目重放安装。

项目专用扩展仍以 `workbench.project.json` 显式登记为准。目标手机管理可在无分析项目时使用共享设备管理模式；不要把 APK、证据、设备序列号、凭据或生成的工具环境写进本插件。
