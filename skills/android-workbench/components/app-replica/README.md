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

指定 `--task` 且未指定 `--group` 时，派发只检查并恢复该任务，避免先巡检所有无关交付、MVP 和视图；它不会顺带唤醒其他等待任务。内部 `dispatch.inspect` 返回带范围标记的调度快照，不代表交付或全项目检查通过。当前依赖仍由正常的 `task.resume`、`task.claim` 在受保护的状态中重新校验；全局并发、派发锁及已有预约的恢复规则不变。普通 `status`、全局派发和分组派发保留完整检查行为。

搬迁到另一台机器时，`workspace.relocate` 可指定 `workbench_source` 为新机器上实际存在的维护 checkout 绝对路径。迁移计划校验其入口和角色文件，并将它记入迁移绑定；后续提示使用这个位置，原始 project 字段和历史文件来源保持不变。此选项不安装工具或启动服务，也不改变目录内嵌的旧命令参数。

`coordination_protocol:2` 提供完整输出清单引用与 `task.group`。提示只展开当前新增/明确指定的路径，完整授权仍保留在可核验清单和正式状态中；`--focus-output` 不改变权限。实际相关、尚未启动的同角色简单类可以通过 `native-next --group GROUP` 依次复用原生线程，保留每类的唯一负责人、原依赖和独立验收。不能跨角色、跳过真实停止或借组名取得别人的文件。具体字段、复用检查格式与升级边界见[执行流程更新](../../../../docs/app-replica-execution-upgrade.md)。

确认真实停止后，可把**一个**原有 `run.stopped` 请求保存为 JSON 文件，使用 `native-next --external-active N --stopped-file STOP.json`。它在同一派发锁下登记停止、处理后续待办，再预约下一项，返回 `stopped` 回执及原有启动指令。文件保留真实 `run_id`、`outcome`、`observation.confirmed:true`、`writers_stopped:true` 和实际证据；不能以提交通过代替停止观察。未提供 `request_id` 时按内容生成稳定 ID，重试不会重复停止；已有预约按 `recovery:true` 恢复，仍须核对宿主是否已启动。停止和下一次预约分别持久提交，不是一个事务：后续派发失败不撤销真实停止，先查状态再重试；没有就绪任务也保留停止回执。该选项仅适用于原生后端，不执行宿主启动工具。

## Agent 文件交互

`coordination/state.json` 是唯一已提交的元数据入口。schema 2 通过哈希引用不可变清单、范围决定和回执；清单分片包含全部原对象，不截断。普通查询使用 `inventory/manifest.json` 和 `status --summary`，单对象使用 `object --id ORIGINAL_OBJECT_ID`。CLI `status` 默认省略大清单，`status --full` 显式返回兼容的完整结果。`inventory/objects.json` 仅在 `export --output inventory/objects.json` 时生成完整快照，其版本可能落后于当前状态。`project.json`、`inventory/`、`classes/`、`tasks/`、`messages/`、`mvp/` 是带版本的可恢复视图，不能直接修改来提交。代码和证据在 `artifacts/` 或已声明的输出工程。

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work status --summary
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work status --task TASK_ID
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work apply --file /path/request.json
```

`status --task TASK_ID` 直接读当前正式记录，只核对该任务及其所用依赖的产物、输入、类版本和内容标识；不遍历无关交付、MVP 或全局派发候选，也不修复视图。需要修复视图时使用 `recover`，全局进度仍使用 `status --summary`。单任务查询和全局状态返回 `coordination_protocol:2`，交付协议仍为 `delivery_protocol:2`。机械核对不能证明代码语义一致；语义自查仍由责任人执行，Main 针对具体缺口或变化复核，不为普通交接重复原版分析。

`apply` 接受一个 JSON 请求或按顺序执行的请求数组。数组不是一个事务；每个请求有独立 receipt。省略 `request_id` 时按完整内容生成稳定 ID，相同文件重试不会重复执行。需要同样内容作为新事件时显式给新 ID。已提交但视图写出失败时保留 `committed:true`，先 `recover`，不要重做代码/外部动作。

常用请求（完整字段在协议）：

- `project.update`：`changes`、`evidence`、可选 `actor`，保存范围说明、SDK 选择、入口顺序、用户调整；不重写初始化时的身份/源/输出根。
- `task.add`：`task` 里写 `role`、`mode`、`description`、`sources`、`outputs`，对象任务加 `object_id`/`class_id`；无原对象的专项任务写明确 `id`。相同原对象/模式自动合并，多入口/调用者复用。结构任务和实现任务不同。
- `class.publish`：`class_id`、`owner`（所属结构任务 ID）、`expected_revision`（首次为 0）、`structure`、`sources`，可选 `writer`。继承/字段/初始化由这个唯一结构负责人维护。
- `message.send`：`type`、`sender`、`recipient`、`payload`、`evidence`，可选 `task_id`/`related_request_id`/`message_id`。需要已有负责人处理时设置 `wake:true`，recipient 使用该任务 ID；程序在它空闲后安排续跑，同一请求只分派一次。普通备注不唤醒任务。CLI 后端在下一回合提供请求文件，不支持向当前正在运行的回合推送。通知不替代执行和交付。
- `scope.set`：`object_id`、`disposition`（copy/reuse/platform/exclude/unknown）、`reason`、`evidence`。方法未到达不是排除证据。

### 简单类在一次工作中确认范围与结构

当 `analysis/scope` 已经完整确认一个简单类的声明且决定复制时，可使用 `scope.class.publish`：`task_id`、当前 `run_id`、`expected_revision`、`reason`、完整 `structure` 和非空 `evidence`（每项为实际文件的 `path`、`sha256`）。程序从该任务取得原对象，要求其种类为 `class`、运行状态为 `running`，模型/强度与配置的结构角色相同，并保留已有唯一结构负责人。已有其他结构负责人时操作失败且不改变范围，不抢占或批量重建任务。

这个请求同时保存 `copy` 决策与类声明，原 `analysis/scope` 任务成为结构负责人。声明应覆盖身份、修饰符、继承/接口、泛型、注解、成员和初始化等实际相关内容；空方法体不是完整证据。若有尚未解释的复杂结构，继续原来的独立 `class/structure` 路线。

发布后仍须正常提交与确认停止；提交的 `class_revisions` 必须包含刚发布且实际使用的类版本，发布时的证据散列自动加入交付 `inputs`。自查失败、未知、原文件变化或仍有活动写入者都不会产生可复用交付。重复 `task.discover` 复用原分析任务，不额外创建结构负责人。为该对象添加 Sol `class/code` 任务时，程序自动保留类声明和这个结构负责人的交付依赖；消费者必须提交实际消费的 `dependency_tokens`，不能用旧任务 revision 代替这个合并结构依赖，以免原版输入变化只影响负责人而漏掉下游。其他旧依赖格式不变，原角色分工和唯一代码写入者不变。

当前会话的切换步骤与可直接转发的提示词见 [协调流程更新](../../../../docs/app-replica-coordination-upgrade.md)。旧任务不需要批量迁移。

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

### 按内容复用交付与后续验证

新版 `status --summary` 返回 `delivery_protocol:2`、`occupied` 和 `followup_counts`；`status --task TASK_ID` 的 `task.delivery_token` 是当前已通过且实际停止的交付内容标识，不可用时为 `null`。读取依赖产物时保存这个标识，提交时用 `--dependency-tokens used-tokens.json`，文件形如 `{"DEPENDENCY_TASK_ID":"delivery-v1:实际64位散列"}`。不能临提交时抄最新标识冒充之前使用的输入。同一依赖使用 token 或旧 `--dependency-revisions` 之一；类声明仍使用实际读过的 `--class-revisions`。

任务暂停、恢复、领取和停止不改变内容标识。产物、检查内容、声明版本、真正的前置条件或依赖交付改变时仍会失效；正在写入、失败和未知交付不可复用。`--inputs used-inputs.json` 可以绑定工作流或已声明输出根目录内相关分析、构建设置及检查输入的 `{path,sha256}`；改动后旧检查失效。已有分析只在原载体散列、对象、问题和条件仍适用时复用；新疑点和反例仍需调查。

方法/类代码可用 `--scope source` 明确交付源代码。尚未实现的逻辑、占位、失败/未知检查、影响当前代码正确性的疑点继续放在 `--unresolved`；它们仍阻止交付通过。后续组装、构建或成对运行验证可放在 `--followups followups.json`，例如：

```json
[
  {
    "id": "assemble-and-build",
    "stage": "build",
    "task_id": "EXISTING_FINAL_WRITER_TASK_ID",
    "description": "将本方法组装到候选工程并验证对应编译目标",
    "evidence": [{"path": "artifacts/method/build-requirement.md", "sha256": "实际文件散列"}]
  }
]
```

`stage` 是 `assembly`、`build` 或 `runtime`。运行待办还须提供 `runtime_scope`，明确 `kind` 和 `conditions`（需要时包括设备等限制）；结项 MVP 必须匹配，`host_fixture` 不能结项要求 `android` 的待办。责任任务必须已经登记且不同于来源任务；证据必须是实际文件。程序保存稳定的全局 followup ID，在源任务交付可用且确认停止后，向责任任务发送可唤醒消息，不提前或另行唤醒 App。同一来源任务的局部 `id` 定义不可修改；相同内容重复提交不重复通知，实际内容变化则再通知责任人处理过期验证。后续源代码提交即使省略 followups，已登记待办也不会消失，也不会仅因省略待办定义改变代码内容标识。

责任任务读取共享待办，用 `--addresses-followups addressed.json` 列出本次实际覆盖的全局 ID，绑定所检查来源的 `dependency_tokens`，并提交具有对应 `stage` 的通过检查和真实产物。例如检查项增加 `"stage":"build"`。**组装/构建待办在程序确认责任任务成功停止后自动结项**；缺少绑定、对应检查或证据时仍保留待办及原因，无需为了报完成再启动一个模型回合。

运行待办必须在成对观察完成后通过 `apply --file` 提交 `followup.resolve`：`followup_id`、`actor`（责任任务 ID）、实际检查的 `source_token`、`owner_token`、带 SHA-256 的 `evidence` 和 `mvp_id`。MVP 必须明确包含来源与责任任务，版本/条件/证据当前有效且观察通过；宿主模拟的通过仍只代表宿主场景。组装/构建待办也可用同一操作补交明确证据，但不能省略责任任务的绑定和阶段检查。依据改变后结项显示 `stale`，必须重新处理受影响部分。

`coordination/followups.json` 是不可手改的待办视图，`status` 给出当前 open/resolved/stale 状态。`coverage.source_scope_complete` 只表示源码覆盖；`scope_complete` 还要求所有登记后续待办当前结项；两者都不能代替指定范围的 Android 构建、安装或成对运行证明。不要把普通源代码完成都通知 App；有范围/SDK 决策、跨负责人冲突或实际阻塞时才唤醒它。

schema 2/3 无需批量迁移。旧交付的失败/未知、旧 revision 绑定和历史记录保持原样；它们不会自动转换或变成通过。只有原负责人有实际证据地重新交付时，才使用新字段。升级、备份和给同事的分阶段提示词见[交付流程升级手册](../../../../docs/app-replica-delivery-upgrade.md)。运行中的旧协调器不会热更新；接入新协议后不得混用旧程序读写或直接降级。

需要等待时先保存 checkpoint，再 `wait --task TASK_ID --run RUN_ID --requires /path/requires.json --checkpoint /path/checkpoint.md` 并结束本轮。实现前置一般为 `["TASK_ID"]` 或 `[{"class_id":"CLASS_ID","revision":1}]`；App 要在依赖失败后也恢复处理时用 `[{"task_id":"TASK_ID","when":"stopped"}]`。具体答复可等待 `[{"message_id":"REPLY_ID"}]`。程序确认执行进程及其写入后代结束才释放名额；仅请求等待不会释放。

旧的等待条件确需修正时，由 App 明确决定，再通过 `apply` 提交 `task.requires.replace`。请求包含 App/plan 任务 ID `actor`、目标 `task_id`、当前 `expected_revision`、完整有序的 `expected_requires`、替换后的 `requires`、`reason` 和带 SHA-256 的文件 `evidence`。证据可来自工作流内的文件（包括不可变消息）或已声明的工程输出根目录；只读校验，不登记输出，拒绝符号链接。任务必须处于空闲的 queued/waiting/failed 状态，且没有仍占用的运行；实现任务保留自身类声明前置。版本或旧条件不符、证据无效或依赖成环时拒绝。该操作保存修正历史并增加任务版本，不改变执行状态、交付结论或文件负责人；继续运行仍走正常调度。`task.add` 和 `task.wait` 仍只合并前置条件，不能删除旧条件。

App 的 `role=app`、`mode=plan` 任务收到尚未分配的 `wake:true` 消息时，可以先处理新规划请求，不必等旧的 `when:stopped` 观察对象再次结束。交付、类版本和指定回复等其他前置条件仍须满足，原 `requires` 不会被删除或标为完成。状态中的 `planning_wake_messages` 和恢复记录注明触发消息；领取时只分配一次。其他角色或模式仍检查全部前置条件，已有运行实例和全局并发限制不变。

## 工程自动提交与批量推送

此功能仅保存复制 App 工程，不保存 Workbench 的开发修改、APK、运行证据或 Agent 对话。Main 在工程文件负责人已结束本轮写入的稳定边界接入；App 工程须已有 Git 仓库、提交身份和已配置的远端。使用当前 checkout：

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work git-enable --repo /path/project/replica-app --remote origin
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work git-status
```

默认创建并检出 `replica/<project-id>`，已有工程源码改动一起形成首次保存。若同名分支已存在但当前未检出，先由 Main 明确选择，程序不替换当前工作树。保存范围内仍有工程文件负责人运行时会拒绝初次接入；范围外的规划和分析任务不阻止接入。默认范围为工程仓库，可用重复的 `--include src --include build.gradle.kts` 限定文件/目录，`--exclude 'private/*'` 增加排除项。内置排除构建目录、环境、证据、APK、密钥文件和本地配置；它不是任意源码内容的秘密扫描器。

已有仓库同时包含工程和协调资料时，沿用该仓库并限定范围：`git-enable --repo /path/project --include replica-app --remote origin`，无需另建嵌套仓库。如果旧历史包含排除文件，仅当当前 HEAD 已是同一推送远端的某个分支头，才可登记为已发布历史基线；未发布的禁止历史仍拒绝接入。后续推送检查增量历史，拒绝新加入或改动的禁止文件，并拒绝将该基线改推到其他地址，不改写已有历史。

CLI 工作者沿用现有目录布局：`execution_root=/path/project`，工程为其中的 `/path/project/replica-app`，协调工作区为另一子目录。本机 Codex CLI 0.163.0 的 `:workspace` 沙箱下已实测该保存路径；若把 App 仓库本身当作执行根目录，其 `.git` 会受只读保护。首次接入及搬迁后的服务登记由有相应权限的 Main 完成，普通保存不重复写外部服务状态目录。自定义沙箱及外置 linked-worktree 元数据须另行确认权限，程序不会放宽沙箱。

启用后，`submit`、`wait` 和真实 `run.stopped` 共用同步保存入口。长任务在稳定组装完成时调用：

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work git-checkpoint --task TASK_ID --run RUN_ID --request-id UNIQUE_SAVE_ID
```

默认保存这个任务的全部工程输出；部分文件仍在写入时，用重复的 `--path` 选择已经稳定的输出。负责人及其写入后代须保持选中文件稳定直到命令返回。相关文件一起提交，未组装的方法片段仍留在原产物协议中。未编译通过也可以保存，检查结论继续如实记录。省略 `--request-id` 时 CLI 生成新 ID；重试同一次保存使用原 ID，新的进展使用新 ID。无变化不创建空提交，Git 记录不修改任务修订。

推送默认在未推送提交达到 **5 笔**，或最早一笔等待 **600 秒**时触发，配置项为 `--push-count`、`--push-seconds`。已有 Workbench 服务每 5 秒检查一次，网络工作在独立线程执行，不占设备/项目队列锁，也不阻塞代码提交。`git-enable` 会使用原有启动入口确保服务存在；若现有服务仍运行旧代码，返回 `service_restart_required`，Main 应在受管工作收尾后更新/重启原服务，不能中断正在执行的设备任务。此时本地提交可用，定时推送尚未启用。`--service-state` 可指定已有服务状态目录，默认沿用 Workbench 的环境配置。

`git-status` 和普通 `status --summary` 的 `git` 字段显示最近保存、待推送数、最老等待时间、最近同步/错误及 `timer_running`；尚未接入时明确显示 `enabled:false, state:not_enabled`。健康标记是最近检查记录，突然退出后可能短暂滞后；服务停机、休眠、断网或权限问题不满足十分钟到达远端的承诺。失败保留工作文件和已有提交，不自动强推、清理人工暂存或合并远端分歧；修复后可继续保存/重试。单个 Git 子进程和等待提交锁均有 30 秒超时，仓库 hooks 仍生效，因此重型 hook 会增加本地等待时间。

代码更新不会使旧服务热加载。使用现有 `scripts/upgrade_runtime.py --project /path/project --skip-project-update` 在共享任务收尾后切换运行时；它先暂停新调度，确认没有活跃任务再更新服务，已有排队任务和 Session 身份保留。接入验收须同时确认 `git` 已启用、服务报告 `replica_git_sync:true`，并核对一次实际保存和远端同步；只修改源码或只看到 `git-enable` 返回不能代替这些检查。

Main 结束整项复制或准备阶段交付时可请求提前推送；App 负责人以 `delivered` 实际结束时也会自动请求：

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work git-flush
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/project/replica-work git-disable
```

`git-flush` 只登记后台推送请求，返回不代表远端已经同步；`git-disable` 停止后续自动保存/推送，不撤销已生成历史或切换分支，已经发出的网络请求可能完成。恢复原配置用 `git-resume`；重新设置策略才用 `git-enable`。schema 3 的工程路径通过已有绑定解析，搬迁后的首次保存会更新服务登记；Git 自身的 linked-worktree 路径仍需按其实际仓库布局处理。

本次实现包含自动保存和批量备份；验证后自动发布开发分支仍是后续独立功能。保存成功不表示 MVP 通过。设计取舍见 [Git 保存方案](../../../../docs/app-replica-git-design.md)。

用户要求中断时由助手核对后处理，可定时调用检查脚本：

```bash
python3 /path/android-workbench/scripts/replica_git_notify.py --workspace /path/project/replica-work --thread REVIEWER_SESSION_UUID
```

脚本只检查和通过 `codex queue` 向指定的既有会话排入提醒，**不会提交、推送或重新启用自动保存**。检查报告和消息回执在 `coordination/git-notify.json`；同一问题限频提醒，队列失败下次重试。助手收到消息后重新核对差异及写入归属，再决定提交；远端跟踪引用只是本地观察，实际推送结果仍需核实。运行方式与限制见上述设计记录末节。

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

## Restored workflow at a new path

Use the supported native-only `relocate --file REQUEST.json --dry-run` and then the identical `relocate --file REQUEST.json` request. This schema-2→3 or repeated schema-3 relocation preserves historical paths, IDs, inventory blobs, receipts, deliveries and lifecycle. It requires exact authority guards, a current-clone ownership observation, matching saved run/supervisor/completion proofs and current host-slot checks. Request/fence fields and replay recovery are defined in [the protocol](../../../../docs/app-replica-protocol.md#native-only-workspace-relocation). Main performs actual fencing, integration, migration and lifecycle reconciliation; workers use isolated fixtures.

`resolve --kind path|locator|file_uri --value VALUE [--exists]` selects a current filesystem operand explicitly. The separate `coordination/path-bindings.json` projection is referenced by raw historical projections. Never recursively replace paths in provenance or edit projections. Bindings do not rewrite hard-coded paths inside source/scripts/commands. Use normal ownership to update runtime operands when necessary. Old/current output aliases share one owner and recorded hashes still check actual current bytes; a stale artifact remains stale.

After actual stop proof, Main uses existing `run.stopped` with `backend_handle` omitted, sends an explicit `host_adoption` wake message containing the old checkpoint and inherited inbox IDs, then calls targeted `native-next` with a just-observed outside-agent count. The host must execute the returned directive and acknowledge its real target/configuration. A prior CLI UUID is preserved as history; it is not a native continuation target. Adopted workflows reject CLI Runner/step/run, codex claims, full reimport . Repeated relocation retains historical and intermediate path aliases and requires the exact current schema/binding revision. Active native writers still require actual stop/recovery before moving. No HostSlots release, original-host retirement, source repair or Android success follows from path adoption.

已登记项目仅恢复原自动提交配置时，使用 `git-resume`。它保留历史与阈值，不读取仍在编辑的文件；下一次稳定保存点继续提交。
