# 运行记录和未知问题

运行阶段必须找到同项目的真实受管任务，并确认任务成功、清理成功、时间和发布文件一致。任务归属核对沿用 `Service.shared()` 的同项目只读规则；返回数据不包含原执行者的 Session token 或摘要。核对只能证明文件与任务的联系，具体声明仍需审阅截图、日志或状态内容。

运行记录示例（占位符不是有效任务或证明）：

```json
{
  "runtime_verified": true,
  "device": "REGISTERED_DEVICE_ID",
  "job_id": "ACTUAL_MANAGED_JOB_ID",
  "observation_window": {"started_at": 100.1, "finished_at": 100.8},
  "observations": [
    {"kind": "ui", "path": "/absolute/published/screen.png", "sha256": "ACTUAL_64_HEX_SHA256"}
  ]
}
```

`kind` 可为 `ui`、`log`、`file`、`state`；路径在记录中可相对该记录文件，提交给服务前转为完整路径。每项必须声明实际 SHA-256。窗口必须精确等于任务真实开始时间 `job.started` 至 worker 结束时间 `job.result.finished_at`。当前没有可靠的逐文件采集时间，不能用狭小窗口声称某截图发生在那个时刻；返回 `window_scope: "job_execution"`。`runtime_verified: true` 是要求核对的声明，不是通过核对的证书。

`jobs.verify_runtime` 经已认证会话读取真实 Store 任务；请求仅包含 `job_id`、`device`、`observation_window` 和 `observations`。调用者不能提交 job JSON、owner、project 或任意 state 目录代替服务记录。独立调用 `semantic_records`、`update_plan`、`check_plan` 没有 verifier callback 时，运行阶段不会完整通过；静态流程仍可使用。重新 plan-check 会重新查询，不复用旧证书。

发布文件必须同时出现在任务发布清单和身份旁置文件 `raw_results` 中；服务用 Store 保存的 `result.analysis_evidence` 摘要核验旁置文件，再核验其内容身份、任务 ID、操作和最终结果。因此事后只修改 `artifacts.json` 加入任意文件不能产生新的可信观察。旧任务没有这个绑定时返回未核验原因，不升级为成功证明。

每次核对最多 1,000 项观察，单文件最多 32 MiB，总观察最多 64 MiB，旁置文件和发布清单分别最多 8 MiB。全部观察先检查字节预算再散列；文件中途增长也受已批准大小限制。超预算返回 `reason: "resource_limit"`，不会返回部分通过。预算控制读取量，不能证明慢存储上的固定完成时间。

当前允许 `device.screenshot`、`device.observe`、`device.info`；`device.scene` 必须含观察步骤。适配器范围是 `frida.inspect_device`、`frida.probe_native`、`frida.probe_uncrackable`、`apkrev.coldstart`、`apkrev.grab_crash`、`apkrev.install_test`、`apkrev.lib_map`、`apkrev.run_probe`、`apkrev.sig_probe`、`apkrev.snap`，并要求受管登记确实绑定设备 serial。它不是任意 Frida、shell、安装或刷机操作的通行证。发布清单、任务配置、结果状态和 `analysis-evidence.json` 身份旁置文件不能替代观察文件。真实来源也不能自动证明文件支持用户的具体问题。

未知问题通过 `analysis_ledger.py` 的 `unknown-record`、`unknown-update`、`unknown-check` 管理。问题 ID 由规范化描述生成；`required` 表示是否阻止这次交付。修改必须带当前 `expected_revision`；进程锁内读取、校验、写入，旧版本更新会冲突。原子替换保留有顺序的修订历史。

`resolved` 必须有具体理由和非空证据文件。证据变更或消失后检查返回 `effective_status: stale`，不会自动推断新答案。计划通过 `unknown_ledger: {"path": "/absolute/unknowns.json"}` 关联；只有 required 且未有效解决的问题阻止完整通过。旧计划没有 ledger 时保持原检查规则。修订摘要用于发现记录变化，不是数字签名，也不证明答案正确。

本轮离线验收命令：`python3 -m unittest tests.test_runtime_evidence tests.test_unknown_ledger tests.test_analysis_workflow`。31 项通过，涵盖错误身份/设备/项目、非最终或失败任务、清理不确定、非法或伪窄时间、未发布或改变文件、篡改清单追加文件、替换旁置文件、读取预算、旧记录缺 verifier、两进程同 revision 竞争、已解决证据 stale 和 optional 不阻止交付。均为临时文件和合成任务，没有真机验证。
