# CLI Agent 的 Linux 沙箱排查

`codex exec` 能连接模型，不代表它的本地命令已经能执行。若每条 shell 命令都在执行前报 `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`，先检查宿主沙箱，不要让实现 Agent 反复改业务代码。

## 定位

在实际 `execution_root` 中，用与后端相同的 Codex 可执行文件运行：

```sh
codex sandbox -c 'sandbox_mode="workspace-write"' -- /usr/bin/true
```

这是 Codex CLI 0.162.1 的语法；其他版本先读取 `codex sandbox --help`。保留完整命令、工作目录、退出码和错误。失败后检查内核日志中的 AppArmor 拒绝记录，以及 `kernel.apparmor_restrict_unprivileged_userns`。单凭报错或该开关为 `1`，不能直接认定原因。

本机实际日志显示 `/usr/bin/bwrap` 创建用户命名空间后进入 `unprivileged_userns`，随后被拒绝 `setpcap` 和 `net_admin`。后者使隔离网络的 loopback 地址无法建立，目标命令尚未执行。

## 修复范围

[OpenAI 官方沙箱说明](https://learn.chatgpt.com/docs/sandboxing#prerequisites) 建议 Ubuntu 24.04 加载发行版提供的 `bwrap-userns-restrict` 配置。它允许 Bubblewrap 建立隔离环境，同时继续限制其子进程的能力；不要用全局关闭 AppArmor 或 `danger-full-access` 代替修复。

共享宿主上的安装、配置加载和回滚必须通过 Workbench 队列。先检查已有文件和已加载规则；存在自定义规则时保留并核对，不能直接覆盖。修复应保存规则来源、哈希、原状态和执行结果。可以从 Ubuntu 软件包提取所需规则，无需因此升级全部系统软件。

本机于 2026-10-15 通过登记的临时项目操作加载了 Ubuntu `apparmor-profiles` 软件包中的原始配置：

- 目标文件：`/etc/apparmor.d/bwrap-userns-restrict`。
- 软件包版本：`4.0.1really4.0.1-0ubuntu0.24.04.9`；只提取配置，没有安装该软件包或升级 AppArmor。
- 配置 SHA-256：`11d39094f044f0cda0febb3ad517b830301da6b2ce929664af09ee9e4dd264f9`。
- 用 `apparmor_parser -r` 加载，未重启服务或系统。全局用户命名空间限制修复前后均为 `1`。
- 原先没有目标文件或同名已加载规则；操作包含失败回滚。以后撤销时仍走受管操作，并先确认规则未被其他维护修改。

`use_legacy_landlock` 在本机 CLI 0.162.1 上无法替代这一修复：它会报 `filesystem-restricted execution requires bubblewrap to isolate app-server sockets`。保留这次失败，不将该开关列为已验证方案。

## 验证

修复后检查实际能力与限制：项目文件可读写；项目外指定测试文件写入被拒绝且内容不变；禁止联网仍生效；`read-only` 模式下项目文件也不可写。不要只看 `/usr/bin/true` 返回成功。

本机这四项检查均通过。命令以普通用户运行，实际 AppArmor 标签为 `bwrap//&unpriv_bwrap (enforce)`。随后真实 `gpt-6.1-sol` / `high` 经 `CodexBackend` 完成代码读取、写入和测试，再用准确线程 ID 恢复并执行追加检查。两个回合退出码均为 `0`，后端记录的模型/思考强度与请求一致；实际进程树停止后占用名额归零。这些检查不证明 Android App 的功能一致性。

本次原始执行目录为 `/tmp/awb-codex-sandbox-repair-5n597x68/`；原始日志、队列结果及验证产物另存于 `/home/conn/.local/state/android-workbench/app-replica/sandbox-repair-20261015-5n597x68/`。它们属于本机证据，不随插件分发。
