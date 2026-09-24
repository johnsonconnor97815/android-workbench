# 请求路由与证据契约

这个文档把 ReArk 的 Agent 路由思想转换成 Workbench 的本地证据接口。它不选择模型，不执行模型，也不拥有设备；它只返回一个可复现的路由计划和上下文预算。

## 路由模式

| 模式 | 触发例子 | 上下文预算 | 设备工具 |
| --- | --- | --- | --- |
| `lightweight_chat` | `hi`、`你好`、`thanks` | 1 条历史，不附加包上下文 | 关闭 |
| `package_overview` | “当前应用的基本信息”、“分析一下” | 8 条历史，包摘要 8000 字符 | 关闭 |
| `focused_static_analysis` | “这个字符串如何被使用”、“入口逻辑怎么走” | 4 条历史，包摘要 4000 字符 | 关闭 |
| `static_fast_path` | “破解这个 secretKey 校验”、“复算这个 hash” | 4 条历史，包摘要 4000 字符 | 关闭 |
| `device_runtime` | “安装并截图”、“继续设备验证” | 8 条历史，包摘要 8000 字符 | 开启 |
| `general_static` | 其他静态问题 | 8 条历史，包摘要 8000 字符 | 关闭 |

没有加载包时，除轻量聊天外返回本地回复 `当前没有加载分析包。`，不调用模型。

修正与复盘类问题不会因为包含 `flag`、`破解`、`密码` 等词而进入静态快路径。`meta_review: true` 时必须把旧候选视为未验证，先用证据定位最早的错误假设，再重跑完整 verifier。

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

- 包概览先取 `analysis.overview`，再按问题补充 `manifest`、`resources`、`signature` 或 `snapshot`。
- 包概览的答案先用一句话给出功能级结论，再列最相关行为；包名、版本和签名只作为证据。不能用预写摘要代替本轮工具证据，也不能把未知项写成结论。
- 聚焦静态分析先取 `analysis.knowledge`、`analysis.strings`、`manifest`、`decompile` 和 `code`，再接 `apkrev.dex_strings`、`apkrev.find_refs`。
- 静态快路径先定位校验关系，再使用 `apkrev.dex_check_verifier`、`apkrev.dex_find_insn`、`analysis.python`、`analysis.strings`、`analysis.decompile` 和 `analysis.code`。
- 设备运行先用 `device.info` 记录设备事实，再使用 `device.scene`、`device.screenshot`、`device.observe` 或对应 `apkrev.*` 操作，并保留语义证据。
- 未列出的操作仍可按证据需要使用，但不能绕过 Workbench 队列。

`analysis.knowledge` 可以检索本地文档、目录和 URL；配置 embedding 端点后做语义检索。`analysis.python` 执行任意 Python 计算，并可用 `--prelude` 恢复常量、公式和候选值，或用 `--session`/`--save-session` 维护可复用状态；`analysis.scratchpad` 维护持久分析笔记；`analysis.exec` 执行任意宿主命令。计算和执行结果返回命令或代码、退出码、输出和超时，帮助验证逆向结果；不额外限制用途。

## 静态快路径契约

触发词包含 `secret`、`encode`、`verifier`、`checksum`、`hash`、`crack`、`key`、`口令`、`密钥`、`校验`、`编码`、`解码`、`破解` 等。

必须满足：

1. **不手算常量。** 十六进制、大整数、模逆元和字符编码都由脚本计算。
2. **完整断言。** 脚本必须检查 `encode(candidate) == verifier`、`hash(candidate) == expected` 或等价完整关系。
3. **候选值必须落地。** 完整答案包含具体候选值，而不是只给公式。
4. **不留半成品。** 不能只给脚本让用户自己运行。
5. **完整复算。** 抽查、随机样本或部分片段不足以支撑最终候选值；必须完整校验。
6. **不倒退。** 不能把已验证候选值替换成后来的未验证猜测；只做静态证明时要标注设备验证未完成。
7. **样本无关。** 不把某个 CTF 样本的常量写进通用产品逻辑。

## 设备运行契约

触发词包含 `install`、`launch`、`screenshot`、`device`、`runtime`、`安装`、`启动`、`截图`、`设备`、`真机`、`运行时` 等。历史中刚出现“设备验证/运行时验证”且用户回答“继续/是/可以”时，也路由到设备运行。

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
