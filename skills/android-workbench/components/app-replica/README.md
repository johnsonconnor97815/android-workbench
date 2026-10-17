# App 复制：从入口沿调用实现

旧 `replication.*` 流程已经删除。本组件使用新的文件协议和五种角色。源码入口为 [app_replica.py](../../../../scripts/app_replica.py)，设计依据见 [设计记录](../../../../docs/app-replication-redesign.md)，字段定义见 [协议](../../../../docs/app-replica-protocol.md)。

## 启动顺序

1. Main 保存用户要求、原 APK/所有 split 的路径、分析来源、输出工程路径。为这个 App 创建独立工作区；真实样本、证据不放进 Workbench 仓库。
2. `init` 只创建文件和 Astra App 负责人任务。App 安排分析：盘点模块、SDK、权限、进程、全部入口、类、实际声明的方法、SO、资源与其他代码载体，决定复制/复用/未知范围。不要一开始把所有方法派出。
3. App 选择第一个入口及可运行 MVP。先建立需要的类声明，再实现方法；方法中的后续调用用 `discover` 登记，有实际空位才派出。其余入口共享已有实现。
4. 实现者自查并交付。对具体 MVP 运行原 App 与复制 App，记录版本、条件和观察目标；差异返回相应实现/结构/分析任务。设备操作仍走共享 Workbench。
5. 处理其余入口，再核对完整清单。未到达的方法可能是遗漏入口或动态调用，不能自动归为无用。最终交付 App/源码、验证范围、排除清单及证据、未解决项。

使用实际 checkout 的绝对路径运行。以下命令中的 `/path` 要换成已确认的本地路径。所有 `--workspace` 都表示复制工作区，不是 Workbench 源码目录。

准备 `project-input.json`：

```json
{
  "id": "sample-replica",
  "requirements": "用户授权的完整复制范围及明确排除项",
  "source_paths": ["/path/original/base.apk", "/path/original/split.apk"],
  "analysis_root": "/path/project/analysis",
  "execution_root": "/path/project",
  "output_path": "/path/project/replica-app"
}
```

`execution_root` 是工具执行目录，必须包含复制工作区和需要写入的工程/证据；原始样本可以在外部只读目录。模型后端遵循当前 Codex 的权限与沙箱。路径写权限不足时调整明确的项目路径/宿主授权，不能启用危险绕过参数。

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work init --project /path/project/project-input.json --capacity 3
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work import --index-db /path/project/analysis/code-index/code-index.sqlite3 --manifest /path/project/analysis/manifest.json --recon /path/project/analysis/recon.json
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work run --host-capacity 3
```

`3` 只是示例，必须改为宿主实际允许给这项工作使用的名额。不要让每个子 Agent 自己再获得一份额度。`--capacity` 是此工作区的上限；CLI 各工作区还共享 `HostSlots.default_path()` 返回的宿主记录和 `--host-capacity`。同一宿主上的生产工作区不得各自使用隔离的 slots 文件来绕过总额度。`--slots-file` 供隔离测试或明确由宿主管理的另一个执行池使用。

已有索引才执行 `import`，没有则让分析任务按现有 Skill 取得。可多次传 `--manifest`、`--recon` 以保留 base/split。导入将对象与全部入口作为一个事务提交；重复执行不会重复登记，旧版中断的逐入口导入也可继续。导入不执行方法任务，不凭 SO 符号表宣称原生函数盘点完整；结构盘点完整与全 App 范围确定分开记录。`inventory.add` 的 `complete` 必须由分析/App 在补齐已知缺口后明确决定。

CLI 是本地文件协调程序，不新增长期占用项目锁的队列任务，否则子 Agent 再调用受管分析/设备操作会互相等待。分析、设备、Frida、共享环境动作继续用原有 Workbench MCP/操作。

## 角色与实际执行

| 逻辑角色 | 任务模式 | 默认模型 / 思考强度 |
| --- | --- | --- |
| Main | 用户交互 | 跟随宿主，不额外启动 |
| App | plan | `gpt-6-astra` / `xhigh` |
| Analysis | architecture、scope、具体调查 | `gpt-6-astra` / `xhigh` |
| Class | structure | `gpt-6-astra` / `xhigh` |
| Class | code，含工程/资源/原生模块代码 | `gpt-6.1-sol` / `high` |
| Function | code | `gpt-6.1-sol` / `high` |
| Analysis 的可选提取模式 | extract | `gpt-6-luna` / `low` |

角色定义在 [agents/app-replica](../../../../agents/app-replica/)。这是本组件加载的角色源文件，不能把 TOML 文件存在当成 Claude/Codex 已自动注册或实际启动。`run` 真正调用 `codex exec`，按任务把模型和强度传给后端，保留工具能力；恢复用记录的准确 thread ID。未提供的实际模型/缓存用量保留未知，不从请求值推定。不可用时报错，不静默降级。复杂实现可在 `task.add` 明确设 `effort: "xhigh"` 并记录原因。没有额外上下文、材料、字符或调用次数限额。

若当前会话有原生子 Agent 工具，可用 `native-next --external-active N`。N 是刚观察到的本工作区以外活跃 Agent 数（包括占位的 Main）。返回 `spawn_agent` 或 `followup_task` 的实际参数，由宿主执行，再 `apply` 提交 `run.started` 和真实 handle。重试时 `recovery:true` 表示已有未确认预约，必须先查宿主实际任务，不能直接重复启动。宿主确认结束及其写入子进程结束后提交 `run.stopped`；仅收到文本“做完了”不够。原生续跑工具不能改变模型，配置不匹配时以新任务运行交接，旧运行必须已确认停止。

## Agent 文件交互

`coordination/state.json` 是唯一已提交的元数据入口。schema 2 通过哈希引用不可变清单、范围决定和回执；清单分片包含全部原对象，不截断。普通查询使用 `inventory/manifest.json` 和 `status --summary`，单对象使用 `object --id ORIGINAL_OBJECT_ID`。CLI `status` 默认省略大清单，`status --full` 显式返回兼容的完整结果。`inventory/objects.json` 仅在 `export --output inventory/objects.json` 时生成完整快照，其版本可能落后于当前状态。`project.json`、`inventory/`、`classes/`、`tasks/`、`messages/`、`mvp/` 是带版本的可恢复视图，不能直接修改来提交。代码和证据在 `artifacts/` 或已声明的输出工程。

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work status --summary
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work status --task TASK_ID
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work apply --file /path/request.json
```

`apply` 接受一个 JSON 请求或按顺序执行的请求数组。数组不是一个事务；每个请求有独立 receipt。省略 `request_id` 时按完整内容生成稳定 ID，相同文件重试不会重复执行。需要同样内容作为新事件时显式给新 ID。已提交但视图写出失败时保留 `committed:true`，先 `recover`，不要重做代码/外部动作。

常用请求（完整字段在协议）：

- `project.update`：`changes`、`evidence`、可选 `actor`，保存范围说明、SDK 选择、入口顺序、用户调整；不重写初始化时的身份/源/输出根。
- `task.add`：`task` 里写 `role`、`mode`、`description`、`sources`、`outputs`，对象任务加 `object_id`/`class_id`；无原对象的专项任务写明确 `id`。相同原对象/模式自动合并，多入口/调用者复用。结构任务和实现任务不同。
- `class.publish`：`class_id`、`owner`（所属结构任务 ID）、`expected_revision`（首次为 0）、`structure`、`sources`，可选 `writer`。继承/字段/初始化由这个唯一结构负责人维护。
- `message.send`：`type`、`sender`、`recipient`、`payload`、`evidence`，可选 `task_id`/`related_request_id`/`message_id`。需要已有负责人处理时设置 `wake:true`，recipient 使用该任务 ID；程序在它空闲后安排续跑，同一请求只分派一次。普通备注不唤醒任务。CLI 后端在下一回合提供请求文件，不支持向当前正在运行的回合推送。通知不替代执行和交付。
- `scope.set`：`object_id`、`disposition`（copy/reuse/platform/exclude/unknown）、`reason`、`evidence`。方法未到达不是排除证据。

App 已确认的调度顺序通过 `dispatch.set` 生效；`project.update` 中的入口/顺序说明只是规划记录。先在 `status --summary` 读取顶层 `dispatch_policy.revision`（未设置为 `0`），把批准的现有任务 ID 按顺序写进请求文件：

```json
{
  "op": "dispatch.set",
  "expected_revision": 0,
  "task_ids": ["existing-task-first", "existing-task-second"],
  "actor": "main-for-app",
  "evidence": ["/path/workflow/artifacts/app-owner/approved-dispatch-order.json"]
}
```

通过上面的 `apply --file` 提交，重试保持文件不变。ID 必须存在且不能重复；证据数组不能为空。过期的策略版本会拒绝，普通任务新增不会使策略版本过期。`task_ids:[]` 配合当前策略版本和 App 证据可清除指定顺序。

`status` 的 `ready_task_ids` 和不指定 ID 的 `task.claim` 使用同一规则：先按有效优先级选择实际就绪任务，再按 `created_at` 从早到晚，同一时间按 ID。列表内尚未结束的任务将优先级沿未满足的任务前置条件和类声明前置条件传给现有负责人；传递依赖继续继承，共享依赖取最早排名。已满足前置、已交付或失败任务、消息等待和普通调用发现不传播优先级；App 规划唤醒仍保留已有的终态观察豁免。不能运行的任务仍不可领取，不添加等待、不打断工作者。有效排名只在内存计算，不修改任务修订；空策略仍按原 FIFO 排序。显式 `task.claim(task_id)` 仍需满足依赖和容量。策略不改变共享名额、模型或最终文件负责人。现有 schema 2 无需迁移；Main 整合代码并让协调器使用新源码后才会生效，旧协调器不会采用这个新增字段。详见[协议](../../../../docs/app-replica-protocol.md#app-dispatch-order)。

发现后续对象：

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work discover --task TASK_ID --target ORIGINAL_OBJECT_ID --source '/path/source.java:42'
```

这只是调用关系；仅在缺失信息影响写法时才等待。类尚无声明时程序先安排结构任务。递归、循环依赖先补声明；不要让方法互等完整实现。未知归宿会进入 analysis/scope；不能自动当复制。每个最终文件只有一个 Sol 写入者，同类方法并行写自己的片段。

写新产物前先 `claim-output --task TASK_ID --path artifacts/TASK_ID/method.patch`。已由别的任务领取的路径会拒绝。然后 `submit --task TASK_ID --run RUN_ID --artifact artifacts/TASK_ID/method.patch --checks /path/checks.json`；类相关实现附 `--class-revisions /path/used-classes.json`，必要时附 `--dependency-revisions` 和 `--unresolved`。检查数组例：`[{"name":"具体检查","result":"pass","evidence":"实际命令/结果文件位置"}]`。`--checks` 文件若已列为该任务输出，CLI 会自动将它一起作为产物提交；同一运行在实际结束前允许补充/修正提交，保留历史。类版本记录例：`{"CLASS_ID": 1}`，必须是实现时实际使用的版本，不能提交时取最新值掩盖过期实现。

输出登记应指向文件。误把现有目录登记为输出时，先让任务实际停止，再通过 `apply` 提交 `task.outputs.remove_directories`，提供 `task_id`、原 `owner`、当前 `expected_revision`、待撤销的 `directories`、`reason` 和带 SHA-256 的文件 `evidence`。每个目录内的文件必须已逐个登记给同一任务；符号链接或特殊文件会拒绝。此操作只撤销指定目录的登记，保留文件、子文件负责人、依赖和交付历史，并记录修正及新任务版本。它不能撤销普通文件、尚不存在的产物或其他任务的登记，也不会自动通过交付。随后由原负责人继续正常提交。

需要等待时先保存 checkpoint，再 `wait --task TASK_ID --run RUN_ID --requires /path/requires.json --checkpoint /path/checkpoint.md` 并结束本轮。实现前置一般为 `["TASK_ID"]` 或 `[{"class_id":"CLASS_ID","revision":1}]`；App 要在依赖失败后也恢复处理时用 `[{"task_id":"TASK_ID","when":"stopped"}]`。具体答复可等待 `[{"message_id":"REPLY_ID"}]`。程序确认执行进程及其写入后代结束才释放名额；仅请求等待不会释放。

App 的 `role=app`、`mode=plan` 任务收到尚未分配的 `wake:true` 消息时，可以先处理新规划请求，不必等旧的 `when:stopped` 观察对象再次结束。交付、类版本和指定回复等其他前置条件仍须满足，原 `requires` 不会被删除或标为完成。状态中的 `planning_wake_messages` 和恢复记录注明触发消息；领取时只分配一次。其他角色或模式仍检查全部前置条件，已有运行实例和全局并发限制不变。

## MVP 与恢复

`mvp.define` 的 `mvp` 包含 `id`、`entry_ids`、`required_task_ids`、`target_versions:{original,candidate}`、`conditions`、`goals`。观察目标要可复现，例如冷启动进入指定 Activity、共享状态值、一次函数输入/返回和后续调用。

`mvp.record` 包含 `mvp_id`、`original_version`、`candidate_version`、`runtime_scope:{kind,conditions}`、两边不同的实际证据文件 `observations:{original:{path,sha256},candidate:{path,sha256}}`，以及 `results:{GOAL_ID:{result,original,candidate}}`。每个 goal 的 original/candidate 引用对应证据文件及位置。conditions 必须与定义一致；真机 scope 再记录实际设备。代码、类版本或证据变了，旧验证会变为 stale，不能冒充新版本通过。

`step` 只执行一次调度/恢复；`run` 用程序观察进程状态并连续调度，不让 LLM 反复轮询。Ctrl-C 停调度但保留已经运行的 workers，之后用同一工作区 `step/run` 恢复。启动身份不明时保留占位，查 `coordination/backend/RUN_ID/` 的日志/进程证据；时间久或没输出不能自动抢回。

`coverage` 给出复制待办、已交付、复用、平台、排除、未知及盘点限制。`idle` 只说明当前没有可运行任务，不表示 App 完成。任务交付也不代表已组装、已构建、已安装或运行一致；最终报告分别说明。宿主模拟测试和本地 Python 示例不能证明 Android 真机行为。

本机沙箱修复（2026-10-15）：先前 CLI shell 的 `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` 已定位为 Ubuntu AppArmor 拒绝 Bubblewrap 所需权限。通过共享队列加载发行版的 `bwrap-userns-restrict` 规则后，项目内读写、项目外拒写和网络限制检查通过，真实 Sol/high CLI 已完成写代码与执行测试。没有取消沙箱或全局限制。其他宿主出现相同错误时按[沙箱排查说明](../../../../docs/app-replica-sandbox.md)定位；不要让实现 Agent 反复改业务代码。已安装插件缓存/共享服务不会因源码变化自动升级。

## 大清单存储与迁移

旧 schema 1 必须由 Main 停止旧调度、确认工作者和元数据写入已经收尾，再校验原状态的 revision 和 SHA-256，用当前 checkout 执行：

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow migrate --expected-revision REV --expected-sha256 SHA256
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow status --summary
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow object --id ORIGINAL_OBJECT_ID
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow export --output inventory/objects.json
```

迁移持有原 store 锁，保留并校验 `coordination/legacy/SHA256.json` 的原字节备份，逐对象核对后原子切换。切换前中断仍用旧状态；切换后引用的文件均已写盘。使用相同参数重试会核验已有迁移并修复视图。不要手改状态或拿备份覆盖已有后续提交。完整步骤、失败恢复和接口兼容说明见[协议](../../../../docs/app-replica-protocol.md#explicit-schema-1-migration)。源码整合、完整宿主测试、实际项目迁移和重启都由 Main 负责，单个补丁交付不能宣称已经迁移。
