# 请求路由与证据契约

这个文档把 ReArk 的 Agent 路由思想转换成 Workbench 的本地证据接口。它不选择模型，不执行模型，也不拥有设备；它只返回一个可复现的路由计划和上下文预算。

## 路由模式

| 模式 | 触发例子 | 上下文预算 | 设备工具 |
| --- | --- | --- | --- |
| `lightweight_chat` | `hi`、`你好`、`thanks` | 1 条历史，不附加包上下文 | 关闭 |
| `package_overview` | “当前应用的基本信息”、“分析一下” | 2 条历史，不预附加包摘要 | 关闭 |
| `focused_static_analysis` | “这个字符串如何被使用”、“入口逻辑怎么走” | 4 条历史，包摘要 7000 字符 | 关闭 |
| `static_fast_path` | “破解这个 secretKey 校验”、“复算这个 hash” | 4 条历史，包摘要 4000 字符 | 关闭 |
| `device_runtime` | “安装并截图”、“继续设备验证” | 10 条历史，包摘要 8000 字符 | 开启 |
| `general_static` | 其他静态问题 | 8 条历史，包摘要 8000 字符 | 关闭 |

没有加载包时，静态模式返回本地回复 `当前没有加载分析包。`；轻量聊天和设备观察不要求 APK。

先识别当前请求中的明确限制和实际动作，再使用关键词辅助分类。“只看源码”“不做设备验证”等限制优先于历史中的设备建议；否定的动作不算执行请求。`crash`、`device`、`hash`、`key` 本身不能开启设备操作或求解校验流程。例如查看证书哈希属于静态分析，复算候选值的 hash 才属于求解。

修正与复盘类问题不会因为包含 `flag`、`破解`、`密码` 等词而进入静态快路径。`meta_review: true` 时先用证据定位最早的错误假设；仅在确有候选值和校验关系的任务中重跑完整 verifier。

## 上下文预算

预算限制的是附加给模型的材料，不是用户问题本身：

- `max_history_messages`：最多携带的历史消息数。
- `max_history_chars_per_message`：单条历史消息字符上限。
- `max_package_summary_chars`：包摘要字符上限。
- `max_entry_point_chars`：入口点材料字符上限。
- `max_file_list_chars`：文件列表字符上限。

预算为 `0` 表示该模式不应附加对应材料。轻量聊天不应携带包上下文；聚焦静态分析应优先带目标字符串、类、方法和入口点，而不是整个包。
包概览模式同样不预附加包摘要、入口点或文件列表；它通过 `analysis.*` 工具按需取证。这避免把大包上下文塞进普通“分析一下”请求。

## 操作建议

`analysis.route` 同时返回 `suggested_operations` 和 `suggested_skills`，但它们是候选顺序，不是自动执行许可：

`suggested_skills` 只推荐唯一入口 `android-workbench:android-workbench`；`suggested_references` 给出相对此 Skill 根目录的模式文档路径，不再推荐已合并的旧 Skill。

- 包概览先取 `analysis.overview`，再按问题补充 `manifest`、`resources`、`signature` 或 `snapshot`。
- 包概览的答案先用一句话给出功能级结论，再列最相关行为；包名、版本和签名只作为证据。不能用预写摘要代替本轮工具证据，也不能把未知项写成结论。
- 聚焦静态分析先取 `analysis.knowledge`、`analysis.strings`、`manifest`、`decompile` 和 `code`，再接 `apkrev.dex_strings`、`apkrev.find_refs`。
- 静态快路径先定位校验关系，再使用 `apkrev.dex_check_verifier`、`apkrev.dex_find_insn`、`analysis.python`、`analysis.strings`、`analysis.decompile` 和 `analysis.code`。
- 设备运行先用 `device.info` 记录设备事实，再使用 `device.scene`、`device.screenshot`、`device.observe` 或对应 `apkrev.*` 操作，并保留语义证据。
- 未列出的操作仍可按证据需要使用，但不能绕过 Workbench 队列。

`analysis.knowledge` 可以检索本地文档、目录和 URL；配置 embedding 端点后做语义检索。`analysis.python` 提供本地 Python 计算和可复用状态；`analysis.scratchpad` 维护笔记；`analysis.exec` 提供本地命令执行。受管通用执行锁住项目和工作目录；其他输入/输出分别用 `--read-path` / `--write-path` 声明。设备操作和共享环境修改必须使用对应登记操作。结果记录命令或代码、退出码、输出和超时。

## 静态快路径契约

求解、复算、编码、解码等明确动作可进入静态快路径；补丁请求也可使用该模式，但适用补丁验证契约，不强求候选值。仅提到 `hash`、`key`、证书或签名不能触发候选求解。

确有候选值及校验关系的任务必须满足：

1. **不手算常量。** 十六进制、大整数、模逆元和字符编码都由脚本计算。
2. **完整断言。** 脚本必须检查 `encode(candidate) == verifier`、`hash(candidate) == expected` 或等价完整关系。
3. **候选值必须落地。** 完整答案包含具体候选值，而不是只给公式。
4. **不留半成品。** 不能只给脚本让用户自己运行。
5. **完整复算。** 抽查、随机样本或部分片段不足以支撑最终候选值；必须完整校验。
6. **不倒退。** 不能把已验证候选值替换成后来的未验证猜测；只做静态证明时要标注设备验证未完成。
7. **样本无关。** 不把某个 CTF 样本的常量写进通用产品逻辑。

## 设备运行契约

明确安装、启动、截图、采集设备日志等动作进入设备运行；代码中的这些名称不是执行请求。最近一条助手消息提出“设备验证/运行时验证”且用户回答“继续/是/可以”时，也可进入设备运行；更早的设备讨论不能覆盖当前请求中的限制或新的静态任务。

必须满足：

1. **闭环验证。** 安装、启动、输入、状态变化和产物生成要形成一条可复现链路。
2. **语义证据。** 结论绑定到 UI 文本、日志、生成文件、业务状态或明确观测值。
3. **不把命令成功当业务成功。** `adb`、`input` 或安装命令返回 0 不等于目标分支已验证。
4. **现场绑定。** 指定设备、App、Activity 和时间窗；过期现场不能被另一页截图替代。
5. **不猜路径和控件。** 坐标、包名、存储路径、Toast 文案和产物名必须来自 UI、代码、日志或运行时证据；进程存活、输入回显和无崩溃都不是成功证明。
6. **矛盾即停。** 静态证据和运行时行为不一致时，不继续随机换输入；先复核常量和字节码语义，再做有针对性的运行时探针。
7. **区分替代验证。** 使用 verifier 补丁时，要区分“成功分支已验证”和“原始输入被精确送达”；完整秘密不能主要交给用户手工输入来验证。

## 与 Workbench 的关系

- `analysis.route` 只返回路由计划和契约，不直接调用模型。
- `analysis.overview`、`files`、`entry_points`、`manifest`、`resources`、`preview`、`code`、`signature`、`strings` 和 `snapshot` 是只读证据接口。
- `analysis.preview --extract` 只写调用方指定的输出文件，且受 `--max-bytes` 限制。
- 设备操作仍由 `device.*`、`apk.*`、`frida.*` 和项目扩展通过共享队列执行。
- 本组件不引入 HarmonyOS、`hyle`、Qt 或 ReArk 二进制。
