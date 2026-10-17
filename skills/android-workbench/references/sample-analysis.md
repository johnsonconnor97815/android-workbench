# 通用样本识别与分析完成检查

任何新安装包先确认问题、来源、版本、base 和全部已提供的 split。通过 `analysis.recon` 保存各文件的 SHA-256 和结构线索，再把结果交给 `analysis.route --recon`。不能只根据问题文字猜测 App 的实现方式。

## 选择分析层

- DEX：先定位字符串、类和调用位置，再用 JADX 阅读；重要结论回到 Smali/DEX 核对。
- 已有原字节 SQLite 索引：用 `analysis.query` 按完整 DEX descriptor 或 Native artifact/ZIP entry 加地址查询；多候选时消歧，不随意选中。续页保留查询条件并使用返回的 cursor；图查询保留深度、节点和边预算。没有索引时先按既有索引流程构建，不能把查询当作构建。
- 原生库：有相关 `.so` 或调用链指向底层时，读取 [原生分析](../components/apk-reverse/references/native-and-so.md)。仅出现 `.so` 不证明业务逻辑在其中。
- Flutter、Unity、React Native：结构命中只是候选，先确认版本和目标逻辑归属，再按 [框架说明](../components/apk-reverse/references/framework-runtimes.md) 选专项工具。JADX 结果不能代表完整覆盖。
- 加固：命中特征后先按 [样本侦察](../components/apk-reverse/references/recon.md) 验证真实代码可读性；没有命中也不能证明没有加固。`Application` 是自有类、没有壳特征或反编译成功都不是完整覆盖证明。
- 权限、隐私、数据收集：使用 [权限审计](../components/permission-audit/README.md) 建立基线，再追踪读取、处理、存储及网络去向。分别报告权限声明、实际代码和影响判断。

独立 JS、source map 或 HAR 文件使用 `analysis.offline_js/source_map/har/compare`，不要求 APK；只读提供的文件，不执行脚本、不抓取网络。JS 是词法线索，HAR 是已保存请求，比较仅覆盖观察子集。格式、预算及命令见 [REA 使用说明](../../../docs/rea-adoption-usage.md)。仅文件名或路由建议不证明输入存在，先核对实际路径和 `operations` 登记。

## 保存和推进分析计划

在分析项目中用 `analysis.plan --question ... --recon evidence/recon.json --output evidence/plan.json` 创建计划。设备观察可以无 APK；静态分析必须有样本。每项任务选择独立计划文件，已有文件不会覆盖。

有样本时，计划保存已完成的样本确认和结构识别，然后记录静态取证、按任务明确要求加入的运行验证、审阅、报告。工具缺失、真实代码不可见或必要设备不可用时，用 `analysis.plan_update --plan ... --step ... --status blocked --note ...` 保存原因；不能把未知当作完成。

需要跟踪未知问题时，用 `analysis.unknown_record` 指定 ledger 路径，创建计划时用 `--unknown-ledger` 关联。只有本次必需的问题加 `--required`。用 `analysis.unknown_update` 传 ID、`--expected-revision`、理由和已审阅的 `--evidence`；解决后再用 `analysis.unknown_check` 核对，证据改变会标 stale，不能沿用旧答案。非必需的研究备注不扩大任务范围。详细命令见 [REA 使用说明](../../../docs/rea-adoption-usage.md)。

每个阶段完成后，用 `analysis.plan_update --plan ... --step ... --status completed --evidence ... --note ...` 保存实际证据。`--evidence` 可重复。步骤按顺序推进，重新打开前一阶段会使后续结论失效。证据和样本变化后不得复用旧完成状态。

审阅证据使用 JSON，至少包含下面的字段。`evidence` 引用非空文件，路径相对该记录的位置或使用绝对路径。每个重要结论应注明代码位置、触发条件、交叉核对和未确认范围；机器只检查记录与文件，不能替代阅读代码。

```json
{"review_complete":true,"claims":[{"claim":"具体结论及适用条件","evidence":["code-evidence.json"]}],"limitations":["尚未完成的验证范围"]}
```

运行验证记录必须来自受管任务，包含登记的手机 ID、任务 ID、实际 UI/日志/文件/状态证据的 SHA-256，以及完整任务执行窗口。下例中的 ID、时间、路径和哈希都是占位，必须替换成 `jobs_status` 和可信发布文件的实际值；不能自行虚构。窗口起点是 `job.started`，终点是 `job.result.finished_at`，不是单文件采集时刻。

```json
{"runtime_verified":true,"device":"REGISTERED_DEVICE_ID","job_id":"ACTUAL_MANAGED_JOB_ID","observation_window":{"started_at":100.1,"finished_at":100.8},"observations":[{"kind":"ui","path":"/absolute/published/screenshot.png","sha256":"ACTUAL_64_HEX_SHA256"}]}
```

`analysis.plan_update` 和 `analysis.plan_check` 通过已认证的共享服务核对真实任务、同项目归属、设备、成功和清理状态、可信发布绑定及当前文件摘要。也可直接调用 MCP 工具 `jobs_verify_runtime`，参数去掉上述 `runtime_verified` 字段；通过时返回 `verified:true`、`window_scope:"job_execution"`。服务不可用、旧任务缺绑定或证据改变时未核验。核对来源仍不能替代审阅证据是否支持具体行为；返回码、进程存活和无崩溃不足以证明业务成功。完整格式见 [运行证据说明](../../../docs/rea-runtime-evidence.md)。

最终报告可为 Markdown、HTML、PDF 或 DOCX，不能用采集草稿代替。交付前必须执行 `analysis.plan_check --plan ... --output evidence/completion.json`。只有 `complete:true` 才能报告当前任务范围完成；未完成返回退出码 `2`，列出欠缺阶段或变化文件。静态任务仍需明确 `runtime_scope:not_requested`，不能声称真机已验证。

这些记录补充已有任务和产物索引，不自动运行阶段，不自动批准设备操作，也不提供任意工作流恢复。
