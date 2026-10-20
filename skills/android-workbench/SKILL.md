---
name: android-workbench
description: 统一组织 Android 环境准备、APK 导出、源码索引、App 复制编排、保活/拉起/弹窗与 DEX-Native 分析、Frida 定制、设备场景和目标手机管理，并通过共享服务协调多个 Session 的手机与工具环境占用。用于组合这些能力、分析 APK 行为或解决设备操作冲突的任务。
license: MIT
metadata:
  author: johnsonconnor97815
  version: "0.2.17"
---

# Android Workbench

这是 Android 分析插件的唯一入口 Skill。先读取 [共享调度与路由规则](references/workbench-routing.md)，再按任务读取一个必要的模式文档；不要为了普通请求加载全部组件。

新 App 分析先按 [样本识别与完成检查](references/sample-analysis.md) 保存 `analysis.recon`，结合实际样本调用 `analysis.route`，创建 `analysis.plan`。按证据推进静态、按需运行、审阅和报告阶段；交付前执行 `analysis.plan_check`。采集成功不代表审阅或整个分析完成，受阻时保存原因。简单设备观察可不提供 APK。

已有原字节索引用 `analysis.query` 精确查询；未知问题用 `analysis.unknown_record/update/check` 保存和核对；显式提供的 JS、source map 或 HAR 文件用 `analysis.offline_*` 离线分析。先确认输入和 `operations` 登记，再执行路由建议；缺输入时补齐 `required_inputs`，不猜文件路径。这些离线入口不要求 APK，不证明真机行为。命令和限制见 [分析组件](components/analysis-agent/README.md#rea-机制入口) 与 [REA 使用说明](../../docs/rea-adoption-usage.md)。

| 任务 | 读取 |
| --- | --- |
| 多能力组合、队列、冲突与恢复 | `references/workbench-routing.md` |
| App 复制：架构盘点、入口驱动实现、多 Agent 并发与具体 MVP | [App 复制组件](components/app-replica/README.md) |
| 设备信息、截图、页面操作、状态观测、受管 Hook | `references/device-analysis.md` |
| 静态环境、分析 MCP、工具链版本 | `components/static-env/README.md` |
| APK 权限清单、风险分诊与代码用途报告 | `components/permission-audit/README.md` |
| 整包代码对账、保活/守护/强停复活、拉起/弹窗、DEX↔Native 调用链与证据报告 | `references/code-first-behavior.md` |
| 分析请求路由、包概览、Manifest/资源/预览、JADX 反编译、源码导航、签名、字符串、知识检索、Python 计算/session 状态、持久 scratchpad 与宿主执行 | `components/analysis-agent/README.md` |
| 精确索引查询、未知问题修订、JS/source map/HAR 离线分析及运行来源核对 | `components/analysis-agent/README.md#rea-机制入口` |
| 统一工具目录、工具检查/安装、JS 语法树/解混淆、ELF 布局与有限固件扫描 | `../../Tools/README.md` |
| APK 逆向流程、方法级 DEX 补丁、USB 联网代理、重打包、能力矩阵与 `apkrev.*` 操作索引 | `references/apk-reverse.md` |
| Frida 选版、修改、构建、部署 | `components/frida/README.md` |
| 已安装 App 的 APK 导出与校验 | `components/apk-export/README.md` |
| Google Play 安装、紧凑详情卡、未知布局的 LLM 修复 | `components/play-install/README.md` |
| 目标手机刷机研究、刷系统、root、Frida server 安装 | `components/device-manager/README.md` |

开始刷机任务时，先确认本次保留数据还是允许清空数据，再选择安装路线和所需安装包；已有明确选择就沿用，不重复询问。用户只说“开始刷机”或“使用官方脚本”不能代替清数据授权。发现所选路线会清数据而尚未获得授权时，先说明会删除哪些数据及现有备份范围，再等待选择；同时可继续检查缓存、校验文件和其他不依赖该选择的准备。不要到写入前才首次提出数据取舍，也不要因为未询问而默认用户选择保数据路线。

刷机、安装或重装 Root，以及其他需要手机按键、屏幕确认的操作，开始前先提醒用户必须有人在手机旁边，并说明预计的人工步骤。人在场的确认不能从任务授权、`--confirm` 或研究缓存中推断；本次流程已明确有人在场时，不重复询问。已有 Root 授权不每次提醒：先读 `devices_state.device_status`，`stale:false` 且 `root_access.status:granted` 时，按已验证的执行身份直接进行普通 Root 操作；首次授权、记录失效、命令失败或实际出现弹窗时才重新核验，确需手机操作时再提示。无人能完成必要人工步骤时，先完成研究、下载、校验和不接触手机的演练，实际设备操作等有人在场再开始。

普通 `device_manager.preflight` 只读取状态，不请求 Root 权限；任务确需验证 Root 且没有可复用记录时，才加 `--check-root`，该检查可能触发手机授权。复用结果保留原 Root 验证时间；刷机等操作后，`device_status` 和 `last_device_info` 各自标记失效并由对应操作刷新。

刷机前同时核对电量、电池健康和读数是否实时更新。健康异常、模拟或冻结状态、系统服务与电池驱动读数矛盾时，先通过受管只读检查查明原因；不能凭正常温度或较高电量放行，也不能为通过检查擅自重置电池状态。具体字段和执行器覆盖范围见机型研究文档。

实际刷机必须先按 [机型研究与失败案例核对流程](references/flashing-research.md) 确认产品代号、硬件变体和当前版本，再联网核对官方或维护者方案及失败案例。保留来源和适用结论，准备绑定当前档案的 `device_review`、明确的镜像写入规则与硬件检查。存在未解决问题、研究过期或关键状态无法核实时，不得写入；不能自动批准研究或仅更新日期继续复用。

刷机实际写入前必须准备设备管理文档中的 `flash_plan`，绑定目标手机、镜像校验值、当前和目标启动分区、启动程序版本、防降级核对及功能验收项目。清数据前必须准备可校验的 `backup_record`，记录原始镜像和数据备份范围，或用户明确放弃数据的原因。第三方系统按机型查证额外镜像的顺序、模式和手机操作步骤，附加包逐个安装；保数据的官方完整 OTA 使用独立的 `device_manager.install_ota`。旧档案只可用于不接触手机的演练。单步 `complete` 不代表刷机结束：完成首次设置、电脑授权和功能检查后，调用 `device_manager.flash_verify --health-record <功能检查记录>`，检查 `workflow_complete: true`。中断后读取已保存步骤，通过对应恢复入口核实状态；恢复队列不代表系统已修复，不能自动重放刷写。

App 复制先读 [App 复制组件](components/app-replica/README.md)。Main 保存用户要求并交给 Astra App 负责人，按全库盘点、首入口、类声明、方法实现、具体 MVP、其余入口、剩余清单推进。通过新的文件协调 CLI 启动指定模型的 Agent；发现后续依赖才委派，所有任务共享实际并发名额。旧 `replication.*` 流程和角色已删除，不从历史状态恢复。代码交付、构建、运行及原 App 对比的结论分别记录。

插件中的 Agent 只是分工入口，不是资源所有者。设备、ADB、Frida、共享分析工程和输出路径都通过 Workbench MCP 或已登记操作协调；服务不可用时报告阻塞，不退回裸 ADB/Frida。

通用设备连通性和工具能力验收每次以 `devices_discover` 实际发现的接入设备为准，自动登记后逐台受管检查。历史登记不构成固定目标；正常换机无需确认，也不记作故障。已有明确来源的任务保持其设备身份，新设备重新核验状态和权限，不能沿用旧手机的缓存结论。

工具默认在同机同账户的固定版本工作区共享，样本和证据仍属于各分析项目；项目显式覆盖优先。`toolchain.auto_install:true` 时缺失或损坏自动通过 `tools.ensure` 补装/修复，检查通过后继续分析，不为普通补装重复请求确认，也不把升级混入修复。声明了 `requires_tools` 的操作自动建立准备任务依赖；其他必要工具按 [Tools](../../Tools/README.md) 先准备。修复必须在独立受管任务中持写锁，不能在仍持环境读锁的分析/MCP 里安装。通用工具、MCP 实际调用和真机 Hook 分别验收；商业参考项与平台/目标版本限定项不计作已就绪。

Google Play 链接使用 `apk.play_install`，不替换成第三方 APK 镜像。返回 `outcome:needs_llm` 时，当前 LLM 必须读取 `llm-repair.json` 和页面证据，按照 `components/play-install/README.md` 建立离线复现、修复通用规则、通过回归，再以新任务标识受管重试；同一故障最多两轮，每轮必须有可验证的进展。普通 UI 布局变化不要求用户代点。`requires_user` 表示账号、支付、政策或锁屏等真实人工门槛；点击后的 `uncertain` 先核验和恢复，不盲目重放安装。

项目专用扩展仍以 `workbench.project.json` 显式登记为准。目标手机管理可在无分析项目时使用共享设备管理模式；不要把 APK、证据、设备序列号、凭据或生成的工具环境写进本插件。
