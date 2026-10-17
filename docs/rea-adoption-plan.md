# REA 机制吸收计划

当前状态：工具扩展源码、本机安装及固定样本验收已通过；各部署环境的插件与服务一致性另用 `check_installation.py --runtime` 核对。此前 476 项测试是上一阶段的历史结果，本次最终全量为 501 项。保留仓库此前已有的未提交修改。本文记录实施约定与实际结果，不替代已有 REA 研究报告。各阶段入口说明见 [使用说明](rea-adoption-usage.md)，新增工具见 [Tools](../Tools/README.md)。

下表保留上一阶段六项机制的完成证据；本次工具扩展单独记录在文末，不用旧测试数量替代新验收。

| 阶段 | 完成结果 | 验证范围与限制 |
| --- | --- | --- |
| 1 结果身份 | worker 发布身份旁置文件；live MCP 关联逐调用请求身份、provider 声明、原始响应摘要和受限前缀；独立 MCP 测试 12 项通过 | 全量验证和最终复核通过；外部引擎身份仍明确 unknown，嵌套 MCP 仅父任务输出约束 |
| 2 运行关联 | 服务核对真实任务和可信发布绑定；workflow 重查；预算和完整任务时间范围已落实 | 全量验证通过；合成任务与临时服务，不包含真机效果评测 |
| 3 精确查询 | 受管 `analysis.query`、结构化选择、游标和图预算已落实 | 全量验证通过；固定 fixture 的静态查询，不证明动态调用 |
| 4 知识 freshness | 本地内容/配置重查、URL 每次重取、向量配置绑定和原子保存已落实 | 全量验证通过；重查仅覆盖声明的来源与预算 |
| 5 未知问题 | 锁内版本冲突、修订历史、证据 stale 与 required 范围已落实 | 全量验证通过；文件完整性不替代语义审阅 |
| 6 离线试点 | JS 词法、flat v3 source map 子集、HAR 和有限比较已落实；词法缺陷和注释扫描预算问题已修复；独立测试 12 项通过 | 全量验证和最终复核通过；无完整 JS 模块图或行为等价证明 |

## 目标和边界

让分析结果回答五件事：读的是哪份输入、用的是什么工具和配置、实际取得了哪些证据、哪些部分没有完成、运行声明能否找到受管任务记录。先完善现有 Android 路径，再加入可离线验证的 JS 与 HAR 小范围能力。HAR 是保存 HTTP 请求和响应的记录文件。

上一阶段实施六阶段。前三项为可信分析基础，不能只做文档或空接口；第四项修复现有索引复用，第五项防止问题记录被旧写入覆盖，第六项提供有限新能力。所有新增入口继续参加共享队列和路径资源检查。现有静态结果不得因此成为运行证明。

上一阶段没有安装或复制 REA 全套系统，也没有新增 Babel/Wakaru、固件扫描等引擎。本次用户授权扩大范围：加入 Babel 解析、Wakaru 可读恢复、Binwalk/Unblob 有限扫描与 ELF 布局入口，并在 `Tools/` 统一登记和管理；安装与实际验收状态按文末新记录更新。Hopper/IDA、浏览器采集、mitmproxy 抓包、.NET/EVM/macOS 等仍只登记条件和限制，不宣称已有可运行适配器。原 `analysis.offline_js` 仍是词法试点，真实解析另走 `tools.js_ast`；两者均不宣称完整调用图、Hermes HBC 解码或行为等价。

## 1. 统一结果身份与部分完成含义

实施记录：`workbench/analysis_evidence.py` 与 worker 产物发布保存 declared inputs、配置、适配代码和直接执行程序身份。版本不能确认时保留 unknown，`cache_reusable: false`。live MCP 的 `mcp-call.json` 与 `mcp-response.bin` 纳入发布结果；错误、断开或截断为 partial。响应只保留最多 256 KiB 前缀；完整响应摘要与 provider 身份来自 proxy 观察/声明，不能当引擎语义证明。嵌套 proxy 使用父任务输出约束，不宣称已有独立逐调用记录。

以增量字段或旁置文件提供版本化结果约定，保留已有输出字段和退出码。至少覆盖原字节查询、JADX 反编译和受管分析 MCP 调用；MCP 指连接外部分析工具的接口。结果包含输入内容摘要、操作和规范化参数、实际可确认的工具身份、配置摘要、原始结果文件摘要、覆盖范围、限制、失败原因和清理状态。外部引擎版本取不到时明确 unknown，不能把适配脚本版本当引擎版本。

输入、配置、适配代码和工具身份分别记录。未知工具身份不得取得“可精确复用”资格；不以执行一次任意工具的 `--version` 作为无条件通用办法。不记录密钥或 Session token。部分输出在失败时仍可查看，状态必须区分成功、部分完成、失败及清理未知。新增统一约定不改变设备调度的成功判据。

验收：相同固定输入产生相同内容身份；替换样本/配置/适配代码后身份变化；缺工具版本保留 unknown；截断及工具失败仍关联已保存原始结果；身份中不包含凭据。

## 2. 运行记录核对真实任务

实施前，`analysis_workflow.semantic_records()` 只检查 `runtime_verified`、`device`、`job_id` 和文件，不能证明任务实际存在。现已增加服务端只读核对，通过已认证会话查询 Store 中的任务和发布产物，再供 plan-update/plan-check 使用。调用者不能传一份自制 job JSON 充当权威数据，也不能从用户指定的任意 state 目录读取“可信任务”。

核对任务归属、项目、设备、最终状态、执行时间、产物发布摘要和观察路径；窗口须精确等于 `job.started` 至 `job.result.finished_at`，返回 `window_scope: "job_execution"`。缺少可靠的逐文件时间，不能把窄窗口当作截图或日志采集时刻。失败、取消、partial、未完成、needs_recovery 或清理不确定都不能通过完整 runtime 阶段。静态或纯宿主操作不能通过只填设备名升级。服务不可用时返回未核验原因；静态计划继续可用。

任务资格核对不替代语义审阅：真实截图、日志或状态文件仍要支持具体声明。旧 runtime 记录读得出，但缺少服务核对或可信发布绑定时不能继续声称 complete。采用现有 `Service.shared()` 的同项目只读规则，允许同项目 Session 复用，拒绝其他项目；内部保留真实执行归属，返回值不暴露 owner token 或摘要。

接口为 `jobs.verify_runtime`，由当前 Client 认证后调用。请求只带 job ID、声明设备、完整执行窗口和观察文件；服务从真实 job、`result.cleanup_ok`、Store 保存的 `result.analysis_evidence` 摘要核验身份旁置文件，再核对 `raw_results` 与发布清单。只修改 `artifacts.json` 追加文件不能取得运行资格。内部产物按 job 目录和 `files[].name` 定位，外部产物按 `external_outputs[].files[]` 定位。`semantic_records`、`update_plan`、`check_plan` 接受 runtime verifier callback；独立调用没有 callback 时 runtime 为未核验。重新检查仍执行服务 lookup。单观察最多 32 MiB、总观察最多 64 MiB、元数据各 8 MiB；超限未核验，不返回部分通过。

验收：虚构任务、其他项目任务、错误设备、错误时间、文件摘要变更、静态任务、失败/取消/partial/清理未知均拒绝；成功受管任务及其发布产物通过，同项目共享符合现有规则。以临时服务和合成产物测试，不连接设备。

## 3. 精确 DEX/Native 查询与稳定续读

现有 `scripts/apk_code_index.py` 已有方法、调用者、常量、类型、字段和原生反向导航，直接复用。增加正式受管查询入口和结构化选择条件：DEX 方法用完整 descriptor，并允许以 artifact、raw method index 消除多份 DEX 的歧义；Native 用 artifact 或完整 ZIP entry 加地址。只有一个候选时才自动选中，多 ABI 同名库或多方法候选返回明确歧义和可用选择项。

查询保留 DEX/ELF 原字节身份、offset、PC 和原始引用类型。平面列表明确 ORDER BY 和全局数量上限；分页游标绑定数据库内容身份、查询参数、结果种类和版本，不使用跨任意查询可复用的裸 offset。数据库或查询变化时拒绝旧游标。嵌套调用链只做明确深度、节点和边预算，并返回预算耗尽原因；不能声称无限嵌套结果也完成分页。

验收：已知 fixture 的方法、caller、字段和 JNI 候选准确；重复 descriptor/库名拒绝含糊选择；首尾页合起来无重漏；相同 PC 的不同 artifact 不合并；改数据库/selector 后游标失效；环和高扇出图受全局预算约束。结果仍写明反射、虚派发和间接调用未求解。

## 4. 知识索引 freshness

实施前，`knowledge_facts()` 只按文件存在与 schema 复用。现已增加索引构建约定身份：规范化 roots/URLs、文件清单和内容摘要、最大文件/字节预算、分块规则、实现版本及 embedding 配置。embedding 是把文本转换成检索用数值向量；已有向量仅在文本和模型配置匹配时复用。

本地文件新增、删除、修改、配置变化均需重建或返回明确 stale，不默默提供旧结果。远程 URL 没有重新取得内容就不能标 fresh；区分已缓存历史内容、验证失败与本次新取内容。明确处理旧 schema 与缺 freshness 数据的索引。检索排序对同分结果增加稳定次序。索引写入使用原子替换；密钥不进入身份。

验收：本地文件新增/删除/修改、换 root、改预算和模型均不复用旧文本/旧向量；配置相同且内容未变可复用；URL 失败保留可读旧证据但标记未验证；旧索引迁移或失效行为明确。

## 5. 未知问题与修订记录

为现有计划增加小型 unknown ledger：稳定问题 ID、问题描述、状态、证据引用及摘要、revision、修订历史；状态至少有 open/resolved，解决必须提供理由和证据。更新带 expected_revision；先在锁内读当前 revision，再写入，旧写入必须冲突。不要仅靠 Python 对象中的 revision 比较宣称解决了并发覆盖。

引用证据变化使已解决状态失去资格，返回 stale 或重开原因；不自动推断新答案。若接入 plan_check，只阻止明示为本次必需的问题，不能让所有研究备注都扩大任务范围。原计划 schema 保持可读，旧计划无 ledger 时沿用原静态检查。

验收：两个 writer 从同 revision 开始只能一个成功；历史顺序保留；resolved 缺证据拒绝；已解决证据变化被发现；非必需问题不错误阻塞静态交付。

## 6. 明文 JS / source map / HAR 有限离线试点

新增受管离线入口，读取显式给定文件，不运行其中代码，不联网抓取。第一版给明文 JS 文件身份、可复核的 import/require、bridge/URL 字面量位置及提取限制；若没有真正语法解析器，这些必须叫词法线索，不能叫完整 AST/语义图。source map 支持声明的 v3 位置映射子集，拒绝不支持或畸形结构，保留缺 map/缺源文件的 unknown。嵌入 map、sourcesContent、文件数量和字节均设预算；不按 map 中的 URL 自动下载或任意读取本地文件。

HAR 读取已有请求/响应的 method、URL、status、时间和受限内容摘要，保留原始记录身份；比较仅覆盖明确声明的请求集合或 JS 线索变化。缺记录、截断、解码失败或配置不一致时返回 partial/unknown，不能把空集或被截断结果判作一致。默认结果不展开认证头或 Cookie 值；原文件仍作为本地证据。

验收：模块/bridge 字面量位置、已知 source-map VLQ 位置、HAR base64 内容、重复请求与两版固定差异；超限、坏 map/坏编码、外部路径和内容缺失分别有明确结果；不执行 JS、不发网络请求。没有真实样本效果评测时只宣称 fixture 验证通过。

## 上一阶段分工与文件归属

按用户要求，编码、测试代码和共享接线由 GPT-6.1 Sol agent 承担；GPT-6 Astra 负责架构与实现复核；root 负责协调、查看证据和运行最终验证。共享文件由 Sol agent 串行接手，避免同时写入。以下为实际任务归属，不表示修改了已安装插件缓存。

| 分工 | 文件组与职责 | 交接接口 |
|---|---|---|
| Sol：runtime、ledger 与验证 | `runtime_evidence.py`、`analysis_workflow.py`、`analysis_ledger.py`；runtime/ledger/workflow/MCP 专用测试；实施/使用文档 | runtime callback、可信发布核对、ledger CLI 和计划关联 |
| Sol：查询与离线 | `apk_code_index.py`、`apk_navigation.py`、`analysis_query.py`、`analysis_offline.py` 及专用测试 | 精确查询、续读预算、离线子集及失败原因 |
| Sol：knowledge 与共享集成 | `analysis.py` freshness 与 parser/dispatch；`service.py`、`worker.py`、`registry.py`、`project.py`、`mcp.py`、`mcp_proxy.py`、`leases.py`、`mcp_evidence.py`、`analysis_evidence.py` 等共享接线 | 队列/path 约束、服务核对路由、逐调用 receipt、worker 发布身份 |
| Astra：复核 | 只读架构和代码审查；指出可信发布、窗口、预算及离线词法缺陷 | 发现交由对应 Sol owner 修复并补回归 |
| root：协调与最终验证 | 不继续编辑实施代码；协调接口和文件归属、查看测试结果、运行最终检查 | 最终状态、完整验证结果及真实限制 |

`analysis_workflow.py` 的完整路径是 `skills/android-workbench/components/analysis-agent/scripts/analysis_workflow.py`。这些归属覆盖现有用户修改，实施只能增量修改相关段落；不得用旧 HEAD 整文件替换。新 helper 必须在任意克隆路径可导入，不依赖开发机绝对路径。

实施顺序：先定结果/helper 和 runtime callback 契约，再并行各独占文件组；Sol 集成 owner 完成共享注册和接线；Astra 复核后由对应 Sol owner 修复；最后 root 统一运行验收。不要先加能力数字再等待实现对齐。

## 上一阶段验收和交付记录

2026-10-09 上一阶段最终验证通过：`python3 scripts/check.py` 退出 0，共 **476 项测试**，包括主套件 414 项（95.351 秒）、APK export 25 项（15.221 秒）、Frida 23 项（0.205 秒）、static-env 14 项（2.162 秒）。验证日志：`/tmp/awb-rea-final-check.log`。该记录保留为历史基线，不是本次 Tools 扩展测试总数。新增入口后的旧目录数量断言已由 Sol 集成 owner 修正。

`python3 scripts/build_plugin.py --check` 验证 291 个文件；`claude plugin validate .` 退出 0，仅有不阻止通过的通用 README marketplace 安装命令建议。`.codex-plugin/plugin.json` 与 `.claude-plugin/plugin.json` 字节一致，`git diff --check` 通过。GPT-6 Astra 在最后复核中未发现尚未修复的确定性阻断问题。

上述上一阶段结果包括实际 proxy → 模拟本地 MCP provider → 临时共享服务 → 发布响应/身份的链路，以及合成任务、文件和固定 fixture。当时未安装新引擎、运行在线目标或操作真实设备；不声称新引擎准确率、运行效果或真机 Hook 兼容性已验证。当时源码变化未自动同步已安装插件缓存或旧运行服务。

分别运行新增的失败路径测试、现有 analysis/workflow/index 测试，然后运行 `python3 scripts/check.py`、`python3 scripts/build_plugin.py --check`，以及可用时的 `claude plugin validate .`。检查两个 plugin.json 字节相同、`git diff --check`，并从非仓库 cwd 验证相关新 CLI。新增接口至少一次通过临时共享服务的真实提交、执行、产物发布和读取链路；模拟设备不得写成真机成功。

上述六阶段历史交付范围为源码修改和离线验收；当时插件缓存未同步、未接新引擎、未做真机验证。提交、推送和部署依用户授权处理，不把源码验证自动扩大为线上更新。

## 本次 Tools 扩展与验收

`Tools/` 保存统一目录、固定版本/依赖锁、受管管理器和适配器；生成环境在分析项目的 `Tools/runtime/`，不进 Git。已有 `.android-static`、`.tools` 原地复用，不搬动活动环境。七个入口为 `tools.inventory`、`tools.check`、`tools.install`、`tools.js_ast`、`tools.js_deobfuscate`、`tools.binary_layout`、`tools.firmware_scan`，具体命令与能力限制见 [Tools 使用说明](../Tools/README.md)。登记来源、路径存在、版本检查和实际固定样本运行分别记录，不能互相替代。

本次同时补齐日常使用接线：`analysis.route` 根据当前问题与明确本地输入推荐原字节查询、unknown ledger、离线文件和 Tools 操作；缺输入返回 `required_inputs`，不从历史猜文件，不把 HAR 当运行证明。`jobs_verify_runtime` 作为 MCP 方法单独推荐，不能当作受管操作提交。唯一 [Skill](../skills/android-workbench/SKILL.md)、[调度路由](../skills/android-workbench/references/workbench-routing.md)、[分析组件](../skills/android-workbench/components/analysis-agent/README.md)及[样本完成检查](../skills/android-workbench/references/sample-analysis.md)提供可发现的用法，运行示例须带 SHA-256 和完整任务时间窗口。

本机已通过受管安装和实际固定样本验收：Babel parser/traverse 7.28.4 识别两个函数及两个不同作用域的同名变量；Wakaru 1.13.0 生成可读源码，保存的文件摘要一致；复用 pyelftools 0.33 读取 ELF64 的结构、符号和依赖；Binwalk 3.1.0 与 Unblob 26.6.4 都识别出 offset 7、size 45 的 gzip 数据。各独立分析入口保留任务产物，原始输入摘要未改变。Rust 1.90.0 三个制品的 SHA-256 与官方清单一致，独立环境安装和固定源码编译通过，未执行编译产物。

最终 `python3 scripts/check.py` 退出 0：主套件 439 项（104.187 秒）、APK export 25 项（14.880 秒）、Frida 23 项（0.209 秒）、static-env 14 项（2.174 秒），合计 501 项。日志在本机 `/tmp/awb-tools-acceptance-k69jx6ec/check-final.log`，受管工具任务保留在共享服务证据目录，不提交 Git。后台服务已受控更新，项目登记包含七个 Tools 操作；旧只读 host lease 经受管恢复检查后释放。

发布使用验证过的插件包更新 marketplace 源目录，再由 `codex plugin add` 更新缓存；最终分别用 `check_installation.py --installed <目录> --runtime` 比较包、缓存和运行内容。保留项目启用范围，每台机器重新检查，不能仅凭版本号复用本机结果。此范围不包含真机、在线抓包、商业购买或未适配平台的运行验证；JADX/Ghidra 的启动检查也不能替代真实 APK/Native 样本效果评测。
