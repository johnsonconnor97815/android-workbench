# 项目扩展

`workbench.project.json` 是可信的本地操作清单。内置操作的脚本来自唯一入口 Skill 内的 `@workbench/skills/android-workbench/components/...`；扩展脚本使用分析项目内的相对路径。调用者只能提交登记操作，不能自行降低它需要的锁。

离线脚本示例，添加到 `operations`：

```json
{
  "project.inspect": {
    "script": "scripts/inspect.py",
    "source": "scripts",
    "readonly": true,
    "compute": true,
    "outputs": ["--output"]
  }
}
```

`script` 必须在 `source` 内。`source` 中的代码会留存快照并检查是否在提交后变化。输出路径参与排队；不要将整个样本目录作为代码来源。

| 字段 | 作用 |
| --- | --- |
| `python` | 此操作专用的 Python 路径，相对项目或使用绝对路径；保留虚拟环境路径，不解析为系统 Python。缺失时在入队前报错 |
| `serial` | 设备序列号参数，如 `--serial`；也可用位置索引。设置后必须明确选择登记设备 |
| `subcommand` | 强制脚本子命令，例如 `pull` |
| `readonly` | 说明操作是否只观察，用于失败与清理处理；不是免排队标记 |
| `mutates_environment` | 独占登记的共享工具环境和 ADB 服务 |
| `compute` | 占用重计算并发名额 |
| `uses_mcp` | 保留共享分析工程，允许受管嵌套调用复用父任务授权 |
| `nested_sources` | 额外需要固定版本的脚本目录，可使用 `@workbench/` |
| `outputs` | 输出参数名数组；对应路径参与写入互斥 |
| `default_outputs` | 缺少输出参数时，使用任务证据目录内的默认子目录，如 `{"--output":"result"}` |
| `adb_exclusive` | 对明确需要修改 ADB 服务的适配器使用独占锁 |
| `recovery` / `recovery_for` | 恢复适配器使用；必须登记可修复的 operation 名称列表和 `result_file`。脚本须完成并验证这些操作的实际清理，不可仅清除状态。共享服务拒绝不匹配的故障来源和仍存活的旧进程；本地恢复适配器还必须独占声明它要核验的共享资源（如 `adb_exclusive`） |
| `resources` | 额外项目路径锁，如 `[{"path":"workspace","mode":"write"}]` |

特定样本的输入哈希、版本限制和恢复步骤由扩展自身验证。涉及设备的新脚本默认整段独占；没有检查点适配时不能中途插入其他操作。

可通过 `scripts/integrate_entries.py /path/to/analysis` 给登记的项目脚本加入排队入口；它只修改项目内代码，保留修改前副本。现存脚本已有旧入口时应审阅并迁移，不依靠目录名自动猜测新路径。CLI 的 `run` 与 MCP 的 `jobs_submit` 始终可用于登记操作。

本仓库不会自动发现或捆绑 `apps/` 中的任何样本。`scripts/configure_project.py --update` 保留自定义操作与环境，并更新内置入口 Skill 组件的引用。
