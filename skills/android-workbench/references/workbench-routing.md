# Android Workbench 路由与调度

先读取使用者项目的 `workbench.project.json`；项目有 `.envrc` 时执行 `source .envrc`。未初始化时，用 `python3 <插件目录>/scripts/bootstrap.py --project <项目目录>` 安装共享运行时并登记本仓库能力。项目专用能力保留在项目中，通用组件随插件分发；不要将项目 APK 或证据复制进插件。仅做目标手机管理时可以不初始化分析项目：把 `--project` 指向本仓库或插件目录，服务会进入只包含 `device_manager.*` 的设备管理模式。

使用 `service_capabilities` 确认共享服务、模型模式及 `client_session`。连接优先使用 `CODEX_THREAD_ID`；若没有该环境变量，保存返回的身份，重连时先用 `sessions_select` 恢复同一身份。用 `operations_list` 获取已登记脚本及参数约定。任务提交、进度、取消和产物分别使用 `jobs_submit`、`jobs_status`、`jobs_cancel`、`jobs_artifacts`。提交必须带稳定的 `request_key`；调用超时后用相同内容和相同标识查询或重试。先用 `devices_discover` 查看 ADB/fastboot 可见连接，用 `devices_register` 登记共享设备 ID，再用 `devices_list` 确认；多台设备时使用明确设备 ID，不能按型号替换来源手机。需要长期预留时用 `devices_project_assign` 指定项目；其他项目不能再排队该设备任务，恢复操作除外。用 `devices_project_release` 在无活动或排队任务时解除指定。

能力路由：

- 环境安装及分析 MCP：读取 `components/static-env/README.md`。修改环境要等实际使用者收尾。只读宿主访问因已退出进程留下故障占用时，使用 `environment.recover_host_lease` 核验并恢复；仍存活或未知的进程不能清锁。
- APK 权限与代码用途审计：读取 `components/permission-audit/README.md`，通过 `static.permission_audit` 提交 base APK 和全部 split APK；可以用 `--source-dir` 复用已有反编译源码。输出权限清单、代码证据和报告草稿后继续人工或模型审阅，草稿不能直接当作最终报告。输入文件哈希、源码目录、工具环境和输出目录参与队列检查，纯静态操作不申请手机。
- 分析请求路由、包概览、Manifest 组件、资源、包内预览、JADX 反编译、源码导航、签名、字符串、知识检索、Python 计算/session 状态、持久 scratchpad 和宿主执行：读取 `components/analysis-agent/README.md`。先用 `analysis.route` 获得模式、上下文预算和候选操作，再按需执行 `analysis.overview`、`analysis.files`、`analysis.entry_points`、`analysis.manifest`、`analysis.resources`、`analysis.preview`、`analysis.decompile`、`analysis.code`、`analysis.signature`、`analysis.strings`、`analysis.snapshot`、`analysis.knowledge`、`analysis.python`、`analysis.scratchpad` 或 `analysis.exec`。
- 已安装 App 的 APK：读取 `components/apk-export/README.md`，通过 `apk.pull` 操作提交原有 `pull` 参数；保留拆分、签名与来源校验。
- APK 补丁、方法级 DEX 补丁、USB 联网代理、重打包、去广告、客户端限制与复杂加固样本：读取 `references/apk-reverse.md` 获取流程、能力矩阵和已登记操作索引；需要完整命令、脚本参数或证据时再打开它链接的 `components/apk-reverse/README.md`。先确认交付物、环境、归属层和基线，再修改。仅导出、静态阅读或环境搭建时不加载。
- Frida 版本、补丁、构建和部署：读取 `components/frida/README.md`；通过 `frida.*` 登记操作执行，不用日常 Hook 代替构建验证。
- 目标手机刷系统、root、Frida server 安装：读取 `components/device-manager/README.md`。先执行 `device_manager.preflight`，再联网检索机型专属刷机方法并生成研究档案，明确刷机范围、是否 root、是否保数据、是否重启、是否安装 Frida，经 `device_manager.research` 校验并可用 `--cache` 复用相同结论；需要镜像时先用 `device_manager.image_download` 下载并校验，或用 `device_manager.image_verify` 复核本地文件。官方整包回刷用 `device_manager.flash_factory`，它会核对研究档案、镜像 `android-info.txt` 和实际手机 `product`；自定义 ROM 布局阻塞时先用 `device_manager.partition_inspect`，再按文档使用 `partition_repair` 或 `flash_factory --reset-super --repair-dynamic-partitions`，然后才可执行 `device_manager.flash`、`device_manager.root` 或 `device_manager.install_frida`。
- 截图、设备信息、界面操作、受管 Hook：读取 `references/device-analysis.md`。同设备任务默认连续执行，只有适配后的检查点允许兼容观察。
- 项目专用能力：仅使用项目清单显式登记的扩展，读取对应项目说明；保留其输入哈希、适用版本和部分完成含义。纯离线操作不申请手机。

`analysis.exec` 和 `analysis.python` 是通用本地执行入口，服务不会分析命令或代码的全部副作用。它们独占项目与 `--cwd` 目录；其他输入路径用 `--read-path` 声明，其他写入路径用 `--write-path` 声明。设备操作、共享环境修改仍使用对应登记操作，不能通过通用执行入口替代。`device_manager.root`、`root_prepare` 和 `root_collect` 目前只支持研究档案明确记录 `root_partition: "boot"` 的流程；需要 `init_boot` 或 `recovery` 时报告能力限制。

已有脚本在登记项目中运行时会转交服务。服务不可用时先检查启动状态，不退回裸 ADB/Frida。CLI 同样可用：`python <插件目录>/scripts/workbench.py --project <项目目录> operations`；执行前用 `start` 启动独立服务。`devices_list` 和 `devices_state` 的 `project_occupied`、`occupying_projects` 标识当前活动任务或已完成后人工交接占用的项目；排队任务和 `manual_pending` 不算占用，但会出现在 `queued_jobs` 与 `project.queued`。`project.assigned` 是长期项目指定，`project.conflict` 表示指定与占用/排队项目不一致。`last_device_info` 持久保存最近一次成功 `device.info` 的缓存，带 `finished_at`、`age_seconds` 和 `stale`；它不是实时读手机，失效时需重新提交 `device.info`，不能用新预检替代。

队列中的任务可能因当前页面可复用而重排。LLM 只给排序建议，不能替代前置条件、资源检查或清理。`partial` 表示有可用结果但未全部完成；`needs_recovery` 表示不能继续信任占用或操作结果。取消已受理也不表示设备已经空闲。

刷机、安装或重装 Root 和设备模式切换开始前，先按设备管理文档列出需要按键、触屏或授权的步骤，并提醒用户必须有人在手机旁边；机型是否需要人工操作尚未确认时，也要提前说明。已有 Root 授权不重复提醒：先读 `devices_state` 或 `devices_list` 的 `device_status`，`stale:false` 且 `root_access.status:granted` 可复用最近的执行身份授权记录；失效或命令失败后用受管预检重新核验，确需手机操作时才提示。研究、下载、镜像校验和不接触手机的 `--dry-run` 可以先做。必须人工完成的步骤是后续任务的前置条件：给出当前步骤的操作说明和完成标志，等待用户完成，再通过共享队列验证，不能只凭上一条命令成功就提交下一阶段。人工接管仍遵循 `devices_manual_acquire` / `devices_manual_release`；已有任务未释放或处于 `needs_recovery` 时，按其实际状态处理，不绕过队列。

普通 `preflight` 只读取状态，不执行请求 Root 权限的命令；上下文未变时可复用旧授权，并保留原验证时间。需要 Root 的任务没有可复用授权时才用 `preflight --check-root`；它可能出现手机授权弹窗。不要为刷新型号、电量等信息主动请求 Root，也不要把 `not_checked` 当作已经授权或 Root 失败。

用户要看“当前这一页”时，指定场景、App、Activity 和有限排队时间；过期后说明现场已消失，不用另一页截图代替。需要无 Hook 基线时不得借用活动 Hook 的现场。读取已有不可变证据可直接进行，不重新占用手机。
