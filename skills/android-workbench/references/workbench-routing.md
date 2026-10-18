# Android Workbench 路由与调度

刷机前按设备管理文档准备 `flash_plan`，绑定手机与文件校验值，并核对分区、启动程序版本、防降级要求和验收项目。`backup_data` 导出指定的共享存储文件；`backup_prepare` 保存原始镜像及数据取舍记录，`backup_verify` 检查是否可用于下一步恢复研究。第三方系统用 `install_rom` 的机型顺序和分阶段附加包；保数据的官方完整 OTA 用 `install_ota`。刷写、等待手机人工操作、验收是不同阶段；用 `device_manager.flash_verify --flash-record <刷写结果> --health-record <功能检查记录>` 确认目标系统、启动分区和指定功能，只有 `workflow_complete: true` 才代表本流程验收完成。中断使用对应的 `recover_flash`、`recover_flash_factory`、`recover_install_rom` 或 `recover_ota` 核实连接并恢复队列；它不代表系统已恢复，不能自动重放刷写。

先读取使用者项目的 `workbench.project.json`；项目有 `.envrc` 时执行 `source .envrc`。未初始化时，用 `python3 <插件目录>/scripts/bootstrap.py --project <项目目录>` 安装共享运行时并登记本仓库能力。项目专用能力保留在项目中，通用组件随插件分发；不要将项目 APK 或证据复制进插件。仅做目标手机管理时可以不初始化分析项目：把 `--project` 指向本仓库或插件目录，服务会进入只包含 `device_manager.*` 的设备管理模式。

本地环境与命令入口检查：

- `.envrc` 是可选文件，缺失不表示项目或服务故障。在项目目录单独执行 `if [ -f .envrc ]; then source ./.envrc; else printf '%s\n' '.envrc 不存在，沿用当前环境'; fi`；加载失败仍保留原退出码。不要用 `ls | rg` 判断文件是否存在，也不要为了消除提示创建空文件。
- 读文档、检查文件和查询服务分别执行并记录结果，避免用分号串联后只看到最后一项的退出码。`rg` 的退出码 `1` 表示无匹配，`2` 表示错误；仅在允许无匹配的探索性搜索中把 `1` 转成明确的“无匹配”结果，保留其他错误，不用 `|| true` 一律忽略。检查路径存在后再搜索；不要让 `| head` 隐藏搜索状态。
- 查询命令用 `python3 <插件目录>/scripts/workbench.py --help`，查询已登记能力用 `operations`。`scripts/workbench.py` 只是启动入口，需要查看参数实现时读取 `workbench/cli.py`。判断调用失败时查看完整命令、退出码和错误输出；`cat`、`sed`、`tail` 打印出的源码不代表源码已经执行。

使用 `service_capabilities` 确认共享服务、模型模式及 `client_session`。连接优先使用 `CODEX_THREAD_ID`；若没有该环境变量，保存返回的身份，重连时先用 `sessions_select` 恢复同一身份。用 `operations_list` 获取已登记脚本及参数约定。任务提交、进度、取消和产物分别使用 `jobs_submit`、`jobs_status`、`jobs_cancel`、`jobs_artifacts`。提交必须带稳定的 `request_key`；调用超时后用相同内容和相同标识查询或重试。先用 `devices_discover` 查看 ADB/fastboot 可见连接，用 `devices_register` 登记共享设备 ID，再用 `devices_list` 确认；多台设备时使用明确设备 ID，不能按型号替换来源手机。需要长期预留时用 `devices_project_assign` 指定项目；其他项目不能再排队该设备任务，恢复操作除外。用 `devices_project_release` 在无活动或排队任务时解除指定。

通用工具就绪、ADB 连通性和能力验收以本次 `devices_discover` 发现的接入设备为准，每轮重新发现；历史登记和缓存不代表当前连接，也不构成固定验收目标。自动登记新发现的连接后，通过队列检查当前设备；单台无需因换机询问，多台按设备分别记录适用检查结果。普通换机不记作故障或待用户确认项，历史设备离线不阻塞当前设备验收。任务已明确绑定的来源设备、运行中或排队任务及其恢复记录仍保持原身份，不能把旧设备的授权、状态或结果套到新设备。

能力路由：

- App 复制：先读 [App 复制组件](../components/app-replica/README.md)，使用新的本地文件协调入口。Main 对接用户，Astra App 任务安排盘点、入口和 MVP，Sol 沿发现的类/方法实现。协调程序不占用长期队列项目锁；实际分析、设备和共享环境操作仍通过既有受管入口。旧 `replication.*` 流程和 Agent 已删除，历史状态不用于恢复旧执行器。

- 新 App 分析：先读 `references/sample-analysis.md`。对 base 和全部已提供 split 执行 `analysis.recon`，保存结构线索和限制，再用 `analysis.route --recon` 结合样本选择流程。通过 `analysis.plan`、`analysis.plan_update` 和 `analysis.plan_check` 保存阶段与证据；最终 `complete:true` 只表示声明的任务范围完成，静态任务不因此成为真机验证。重新打开阶段或输入变化会使后续完成状态失效。

- 精确索引、未知问题与离线文件：读取 [分析组件的 REA 入口](../components/analysis-agent/README.md#rea-机制入口)。`analysis.query` 使用已有 SQLite 原字节索引及精确 selector；不自动建索引。`analysis.unknown_record/update/check` 使用明确 ledger 路径；更新必须带当前 revision，解决须审阅证据。`analysis.offline_js/source_map/har/compare` 只读取明确提供的本地文件；比较须两份同类输入。`analysis.route` 按当前问题推荐，缺输入返回 `required_inputs`；文件名线索不证明文件存在，执行前核对路径和登记。单独离线文件无需 APK，不采集网络或申请手机；HAR 记录不代替真实任务的运行证明。
- 统一工具入口：工具目录、适配器和锁文件放在仓库 `Tools/`，见 [工具说明](../../../Tools/README.md)。先用 `tools.inventory` 查来源、`tools.check` 查可用性。新项目默认使用同机共享的固定版本工具；项目显式覆盖优先，缺失时不退回其他版本。`toolchain.auto_install:true` 时，缺失或损坏自动通过受管 `tools.ensure` 补装或修复，检查通过后继续原任务；声明了工具依赖的操作由客户端自动建立准备任务依赖。不得在持有环境读锁的分析或 MCP 进程内安装；关闭或释放该进程后再排队修复。版本升级另行处理。JS 语法树用 `tools.js_ast`，可读恢复用 `tools.js_deobfuscate`；ELF 布局用 `tools.binary_layout`；有限固件扫描用 `tools.firmware_scan`。执行须有明确本地输入，不把后缀当格式证明或把离线解析当运行证明。更新旧项目登记时将已安装且已登记的环境保存为显式覆盖，不擅自移动工具字节。
- 运行来源核对：`jobs_verify_runtime` 核对认证项目内真实任务、完整任务执行窗口和可信发布文件的 SHA-256；`analysis.plan_update` 与 `analysis.plan_check` 也重新调用服务核对。服务或可信发布记录缺失时保留未核验，不能自行填写任务 JSON 批准运行结论。格式见 [样本完成检查](sample-analysis.md) 和 [REA 使用说明](../../../docs/rea-adoption-usage.md)。

- 环境安装及分析 MCP：读取 `components/static-env/README.md`。修改环境要等实际使用者收尾。只读宿主访问因已退出进程留下故障占用时，使用 `environment.recover_host_lease` 核验并恢复；仍存活或未知的进程不能清锁。
- APK 权限与代码用途审计：读取 `components/permission-audit/README.md`，通过 `static.permission_audit` 提交 base APK 和全部 split APK；可以用 `--source-dir` 复用已有反编译源码。输出权限清单、代码证据和报告草稿后继续人工或模型审阅，草稿不能直接当作最终报告。输入文件哈希、源码目录、工具环境和输出目录参与队列检查，纯静态操作不申请手机。
- 分析请求路由、包概览、Manifest 组件、资源、包内预览、JADX 反编译、源码导航、签名、字符串、知识检索、Python 计算/session 状态、持久 scratchpad 和宿主执行：读取 `components/analysis-agent/README.md`。先用 `analysis.route` 获得模式、上下文预算和候选操作，再按需执行 `analysis.overview`、`analysis.files`、`analysis.entry_points`、`analysis.manifest`、`analysis.resources`、`analysis.preview`、`analysis.decompile`、`analysis.code`、`analysis.signature`、`analysis.strings`、`analysis.snapshot`、`analysis.knowledge`、`analysis.python`、`analysis.scratchpad` 或 `analysis.exec`。
- 已安装 App 的 APK：读取 `components/apk-export/README.md`，通过 `apk.pull` 操作提交原有 `pull` 参数；保留拆分、签名与来源校验。
- Google Play 安装：读取 `components/play-install/README.md`，优先 `apk.play_install`，成功后用 `apk.pull`。来源保持 Google Play。`needs_llm` 进入当前 agent 的取证、离线回放、解析器修复与受管重试流程；布局失败不直接要求用户点击。`requires_user` 与点击后的 `uncertain` 分别按人工门槛和未知设备状态处理。
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

官方 OTA 的 `prepare` 成功后，人工接管并等待用户选中 `Apply update from ADB`。此时用 `devices_manual_release(device=..., transport="sideload", prepared_job="<本次准备任务 ID>")` 归还，等待返回的核验任务成功再提交旁加载；默认归还会检查正常开机的 Android，不能用于此阶段。专用归还只核验已审核的准备记录和旁加载连接，不表示系统已修复，也不适用于第三方 ROM 或 fastboot。

用户要看“当前这一页”时，指定场景、App、Activity 和有限排队时间；过期后说明现场已消失，不用另一页截图代替。需要无 Hook 基线时不得借用活动 Hook 的现场。读取已有不可变证据可直接进行，不重新占用手机。
