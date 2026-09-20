# 部署与恢复

`deploy_server.py` 面向已授权的、独立运行的 Android server。传入明确的设备、本地产物、远端文件路径、版本和目标架构；它会保存旧文件和原启动信息，检查实际运行哈希与真实注入。

```bash
"$RUN/client/bin/python" "$SKILL/scripts/deploy_server.py" --serial "$SERIAL" \
  --binary "$SERVER" --version "$VERSION" --remote "$REMOTE_SERVER" --arch arm64 \
  --record "$RUN/deploy.json"
```

首次使用前核对当前服务归属及启动机制。受服务管理器自动拉起、依赖额外环境变量/命名空间、认证/TLS 或特殊 SELinux 启动上下文的服务，应按实际启动机制编写适配器，不能当作普通后台进程直接替换。

## 已实现的恢复边界

- 通过 `/proc/*/exe` 的精确路径寻找旧服务，拒绝同路径多个实例、目标符号链接、不可恢复的运行文件和不支持的 UID。
- 记录旧文件哈希、进程 starttime、UID、SELinux context、argv、cwd 及版本。旧服务存在时，先用匹配客户端执行 native 基线；原服务本就不可验证时停止自动替换，保留诊断结果。
- 默认沿用旧 argv，新 server 以 root 启动。需要明确改变参数时重复传 `--server-arg=VALUE`；监听端口对应 `--port`。旧端口不同时用 `--previous-port`。
- 跨版本替换需要独立客户端环境：`--python` 为新版客户端 Python，`--previous-python` 为旧版客户端 Python。配套 wheel 的安装和回退由各自 venv 隔离，不改全局 Python 环境。保留 venv 的 `bin/python` 路径；对这个符号链接使用 `realpath`/`Path.resolve()` 可能改为调用系统 Python，导致已安装的 Frida 不可见。
- 上传后核对哈希及 `--version`，替换后核对运行文件、UID、argv，执行注入、RPC、Hook 和清理检查。子检查保存在相邻 JSON 文件中。
- 候选启动/验证失败时恢复旧文件；原来有服务时，恢复 UID/argv/cwd，核对运行哈希及 SELinux context，并重新验证旧服务功能。回滚通过不改变本次部署失败的结论。
- 每个可能改动服务的阶段都写本地记录。临时上传文件清理失败会使整体失败。远端备份和启动日志作为恢复材料保留，路径写在 `retained_backup`，不作为遗留临时文件静默删除。

原 UID 为 `2000` 时，恢复依赖普通 ADB shell 仍为 `2000`；root adbd 无法用此通用启动器重建该身份。更复杂的启动环境必须单独适配。

## 中断、断连及人工续作

先读 `phase`、`previous`、`candidate_pid`、`backup`、`staged`、`rollback` 和错误。重连后核对这些 PID 的 starttime、实际 `/proc/<pid>/exe`、运行哈希、argv、UID 和端口。PID 已复用时不能继续发信号；状态不明时不直接重跑部署命令。

只在确认旧进程或候选进程身份后停止它，从备份恢复已记录的旧文件和启动配置，再使用旧版客户端复测。无法自动恢复的状态明确记录。root 启动可能改变 SELinux 策略；恢复二进制、UID/context 或看到 `Enforcing` 都不能证明策略恢复到原样。

Gadget 的 APK 集成需要自己的加载点、配置、ABI、签名与卸载策略，不能用这个 server 部署器代替。独立 harness 检查见 [操作步骤](workflow.md)。
