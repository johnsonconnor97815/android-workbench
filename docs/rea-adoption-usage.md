# REA 机制吸收后的分析入口

本页对应当前源码接口。本次 Tools 扩展已通过 501 项全量测试及本机固定样本验收，详情见实施记录。已安装的插件缓存、已启动服务和旧项目登记不会仅因源码变化就自动更新。先用 `operations` 确认项目登记了需要的操作；源码、项目登记、运行服务与插件缓存应分别检查。不要直接修改插件缓存作为源码维护。

示例假定 checkout 在 `/path/android-workbench`，分析项目在 `/path/analysis`，已经登记且服务可用；示例输入是已有本地文件，输出目录先存在。实际克隆路径可以不同。统一从共享队列执行：

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis operations
```

`run` 默认等待任务结束，并输出适配器日志和任务状态。操作名后面的 `--` 分隔适配器参数；如需调度器的 `--device`、`--timeout` 等参数，应放在操作名前。下面的静态命令不需要设备。已登记项目中直接运行带 Workbench entry 的脚本也会走队列，不能用它绕过失败的受管入口。

## 路由与统一工具入口

入口可从 [唯一 Skill](../skills/android-workbench/SKILL.md)、[调度路由](../skills/android-workbench/references/workbench-routing.md)、[分析组件](../skills/android-workbench/components/analysis-agent/README.md)和[样本完成检查](../skills/android-workbench/references/sample-analysis.md)找到。`analysis.route` 根据当前问题和明确本地输入推荐操作；缺输入返回 `required_inputs`。建议不证明路径存在、不自动执行或安装，也不批准设备操作。独立 JS/map/HAR 文件无需 APK，HAR 历史文件不变成实时观察。

工具定义、锁文件和适配器统一放在 [Tools](../Tools/README.md)，完整目录见 [catalog.json](../Tools/catalog.json)。新增七个受管入口：

| 操作 | 参数开头 | 用途和使用条件 |
| --- | --- | --- |
| `tools.inventory` | `inventory` | 登记目录、安装路径与支持条件；目录不等于安装成功 |
| `tools.check` | `check [--tool babel]` | 核对工具与声明的固定样本/CLI 范围 |
| `tools.install` | `install --tool babel` | 明确请求安装时使用；共享环境修改须先解除合法恢复门槛 |
| `tools.js_ast` | `js-ast --input app.js` | Babel 真正语法解析；函数/调用表达式不等于完整行为图 |
| `tools.js_deobfuscate` | `js-deobfuscate --input bundle.js` | Wakaru 可读 JS 恢复；保留原文件并审阅恢复源码 |
| `tools.binary_layout` | `binary-layout --input libapp.so` | ELF 段、节、符号、依赖；适配器核对实际格式，不按后缀保证 |
| `tools.firmware_scan` | `firmware-scan --tool binwalk --input firmware.bin` | Binwalk 有限签名扫描；选 `unblob` 时有限扫描与字节切出，不启用外部解包器 |

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run tools.check -- check --tool babel
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run tools.js_ast -- \
  js-ast --input /path/analysis/assets/app.js --output /path/analysis/evidence/ast.json
```

默认 JSON 写入任务目录；显式 `--output` 和相邻 `.artifacts/` 路径参加锁定及容量检查。生成工具环境放在项目 `Tools/runtime/`，既有 `.android-static`/`.tools` 仍复用。实际安装版本、检查结果和产物以当次任务为准，不从目录或旧 manifest 推断。工具输出不替代证据审阅；解析、恢复、ELF 布局与固件扫描均不证明设备行为。Babel AST 与下方 `analysis.offline_js` 的词法试点分开报告，详细格式、预算与平台限制以 Tools 文档为准。

## 原字节索引查询

`analysis.query` 读取已有 SQLite 原字节索引；它不构建索引，不接受自由 SQL，也不执行 APK。例子按完整 DEX descriptor 查询直接调用者；descriptor 是包含类、方法、参数和返回类型的精确标识。

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.query -- \
  --db /path/analysis/index/code.sqlite \
  --query-json '{"schema":"apk-query/v1","kind":"callers","selector":{"descriptor":"Lexample/Guard;->check()V"},"page_size":20}' \
  --output /path/analysis/evidence/callers.json
```

请求也可写进文件，用 `--request /absolute/request.json` 代替 `--query-json`；两者只能选一个。方法有多个候选时返回 `ambiguous_selector` 和候选，不随意选择。可增加 `artifact`，或用 `artifact` 加 `method_idx` 消歧。Native 查询用 artifact 或完整 ZIP entry 加地址，例如：

```json
{"schema":"apk-query/v1","kind":"native_sites","selector":{"entry":"lib/arm64-v8a/libexample.so","address":"0x1000"},"page_size":20}
```

| 查询类型 | 用途 |
| --- | --- |
| `methods`、`callers`、`callees` | 方法清单、直接调用位置 |
| `fields`、`field_sites`、`constants`、`string_sites` | 原字节字段、常量及其引用位置 |
| `native_functions`、`native_sites` | 指定 ELF 的函数和指令位置 |
| `jni_bindings` | 已索引的 DEX/Native 绑定候选 |
| `dex_graph`、`native_graph` | 带深度、节点和边预算的静态关系图 |

平面列表 `page_size` 为 1..200，最多观察 10,000 行，输出最多 4 MiB。续页将返回的 `pagination.next_cursor` 原样复制到下一次请求的 `cursor` 字段，保留相同的 `kind`、`selector` 和 `page_size`；没有 next_cursor 就无需继续。游标绑定数据库和 WAL 内容身份及查询版本，数据库或查询改变后不能继续使用旧游标。游标不是可随意转用于其他查询的 offset。

图查询使用 `depth` 0..8、`max_nodes` 1..200、`max_edges` 1..2000；返回预算耗尽原因，不做嵌套无限分页。动态反射、虚派发、间接调用和运行执行仍未证明，图结果不能当行为完成证明。

## 计划、运行核对和未知问题

未知问题先记录，再由审阅者根据具体证据更新；`required` 表示该问题是否阻止这次交付。

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.unknown_record -- \
  unknown-record --ledger /path/analysis/unknowns.json --question '这条分支是否执行？' --required \
  --output /path/analysis/evidence/question-created.json
```

记录返回稳定 `id` 和当前 `revision`。解决时替换下面占位 ID 和版本号，提供理由与已审阅的非空文件；可以重复 `--evidence`。

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.unknown_update -- \
  unknown-update --ledger /path/analysis/unknowns.json --id unk_ACTUAL_ID --expected-revision 1 \
  --status resolved --reason '已审阅指定日志中的执行记录' --evidence /path/analysis/evidence/observed.log \
  --output /path/analysis/evidence/question-updated.json
```

同时从旧版本写入只能一个成功。已解决证据改变或消失后，`analysis.unknown_check` 返回 `effective_status: stale`；不会自动给出新答案。非 required 的研究备注不阻止交付。重新打开用 `--status open` 并写明理由。

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.unknown_check -- \
  unknown-check --ledger /path/analysis/unknowns.json --output /path/analysis/evidence/questions-checked.json

python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.plan -- \
  plan --question '分析指定样本，只看源码' --apk /path/analysis/samples/base.apk \
  --unknown-ledger /path/analysis/unknowns.json --output /path/analysis/plan.json
```

旧计划没有 ledger 时保持原静态检查。计划阶段并不自动执行；`analysis.plan_update` 用 `plan-update --plan ... --step static|runtime|review|report --status pending|blocked|completed --evidence ... --note ...` 记录阶段，`analysis.plan_check` 用 `plan-check --plan ... --output ...` 重新检查。完成 review 需要带 claims、引用文件和 limitations 的实际审阅记录；命令退出 0 或收集成功不足以替代审阅。

运行记录还须真实任务关联。格式见 [运行记录和未知问题](rea-runtime-evidence.md)。窗口必须精确等于 `job.started` 至 `job.result.finished_at`，只能称任务执行范围；没有逐文件采集时间。服务会检查同项目归属、允许的设备观察操作、最终成功、清理、可信发布绑定和当前文件摘要。旧任务缺可信旁置绑定、服务不可用或文件变更时未核验。

MCP 工具 `jobs_verify_runtime` 对应已认证 RPC `jobs.verify_runtime`，参数仅有：

```json
{
  "job_id": "ACTUAL_MANAGED_JOB_ID",
  "device": "REGISTERED_DEVICE_ID",
  "observation_window": {"started_at": 100.1, "finished_at": 100.8},
  "observations": [{"kind": "ui", "path": "/absolute/published/screen.png", "sha256": "ACTUAL_64_HEX_SHA256"}]
}
```

返回 `verified` 和失败原因；通过时 `window_scope: "job_execution"`。参数不接受 caller job JSON、owner 或任意 state 目录。单文件 32 MiB、总观察 64 MiB、元数据各 8 MiB 的预算超限返回未核验。真实来源与哈希核对仍不能替代判断观察是否支持具体声明。

## 知识索引重查

`analysis.knowledge` 在搜索前重查本地文件和配置；新增、删除、修改、预算或向量配置变化不能默默复用旧文本或旧向量。

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.knowledge -- \
  knowledge --root /path/analysis/references --query 'permission guard' \
  --index /path/analysis/index/knowledge.json --output /path/analysis/evidence/knowledge.json
```

`cache.status` 是 `revalidated`、`bounded_snapshot` 或验证失败原因，`cache.reused` 说明是否复用。`bounded_snapshot` 表示预算范围内的快照，不能称全部文件已覆盖。远程 `--url` 每次重新获取；失败时旧索引可以保留，但本次结果标明验证失败，不把历史内容称为 fresh。旧 schema 或缺构建身份的索引需重建。embedding 参数仍用 `--embedding-endpoint`、`--embedding-model` 和 `--embedding-api-key-env`；密钥值不进入构建身份。

## JS、source map 和 HAR 离线试点

以下操作只读显式文件，不联网、不执行 JS、不按 source map 内容任意读取路径。注册名与脚本子命令不同，需保留表中写法。

| 受管操作 | 参数开头和输入 |
| --- | --- |
| `analysis.offline_js` | `js --input script.js [--map script.js.map]` |
| `analysis.offline_source_map` | `source-map --input script.js.map [--line 1 --column 0]` |
| `analysis.offline_har` | `har --input capture.har` |
| `analysis.offline_compare` | `compare --baseline old.har --candidate new.har --kind har`；JS 用 `--kind js` |

```sh
python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.offline_js -- \
  js --input /path/analysis/assets/app.js --map /path/analysis/assets/app.js.map \
  --output /path/analysis/evidence/js-clues.json

python3 /path/android-workbench/scripts/workbench.py --project /path/analysis run analysis.offline_har -- \
  har --input /path/analysis/evidence/capture.har --output /path/analysis/evidence/har-summary.json
```

所有子命令支持 `--max-bytes`、`--max-items`、`--max-content-bytes`。默认输入 8 MiB、条目 1,000、内容 1 MiB；允许上限分别为 32 MiB、5,000、4 MiB，输出上限 4 MiB。

JS 结果仅为可复核的 import/require、bridge 和 URL 字面量词法线索，位置包含 UTF-8 字节 offset 和 UTF-16 列号。未支持的字符串/模板/正则语法保留 unknown；没有完整 AST、模块图、调用图、解混淆或 Hermes HBC 解码。source map 仅 flat v3 的声明子集和 1/4/5 项 VLQ 段；支持显式 map 或受预算限制的 base64 JSON 内嵌 map，indexed map 等不支持；缺 map/源内容保持 unknown，不抓取 sources URL。

HAR 仅 1.2；保留重复请求的 ordinal、方法、状态、已有时间、URL/支持正文摘要。输出 URL 隐藏 userinfo、query value 和 fragment，不展开认证头、Cookie 或正文值；原始本地文件仍可能含秘密。比较只针对已观察的词法线索或明确记录序列，缺内容、空集、截断或 partial 结果返回 unknown。`same_observed_subset` 不代表程序等价或网络行为全部一致。

## 结果身份和 live MCP

任务发布 `analysis-evidence.json`，关联输入、配置、适配代码、直接执行程序、原始产物及最终结果；Store 中 `result.analysis_evidence` 保存旁置文件摘要。外部引擎版本无法确认时明确 unknown，`cache_reusable: false`，不能用适配器版本冒充引擎版本。

live MCP proxy 每次 `tools/call` 使用独立 analysis lease；发布 `mcp-call.json` 和 `mcp-response.bin`，并把请求身份、provider 声明和响应状态绑定到 worker 结果身份。保留原始响应最多 256 KiB 前缀；tool error、RPC error、断开、截断或 transport budget 超限为 partial，不能用“成功释放 lease”声称工具成功。完整响应摘要与 provider 身份是 proxy 观察/声明，仍需审阅具体内容。原始响应前缀可能包含应用数据，应留在项目证据目录，不提交 Git。嵌套 proxy 当前使用父任务输出约束，未提供独立逐调用记录。

源码改变不等于已安装缓存或旧服务已同步。本次已验证本机工具安装和固定样本，并通过受管任务保存结果；部署后的缓存与服务仍须执行内容一致性检查。不包含在线目标执行或真机兼容性证明。历史和本次记录分开保存在 [实施计划](rea-adoption-plan.md)，以实际验收记录为准。
