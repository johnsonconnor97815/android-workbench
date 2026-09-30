# Android 设备场景

使用共享 MCP 或 `<插件目录>/scripts/workbench.py`，先查询设备和占用。`jobs_submit` 的请求由服务校验；不能用自报的只读标签缩小资源范围。

简单截图：

```json
{
  "operation": "device.screenshot",
  "device": "设备登记 ID",
  "request_key": "本次截图的稳定标识",
  "app": "com.example.app",
  "activity": "com.example.app/.MainActivity",
  "estimate": 2,
  "queue_timeout": 30,
  "accept_hooks": false
}
```

`device.observe` 读取当前活动、进程和设备运行代次。缓存状态带观测时间；要求新状态时提交任务。截图会验证 PNG 和采集前后的 Activity／进程；需要指定业务页面时，增加 `ui_expect`，例如 `[{"resource-id":"com.example.app:id/title","text":"订单详情"}]`。采集前后都必须找到匹配节点；XML 会留作证据。它仍不能替代对截图内容的检查，UI 转储也要计入检查点预算。

`device.scene` 接受连续步骤。可用动作：`observe`、`screenshot`、`ui_dump`、`logcat`、`launch`、`tap`、`swipe`、`input`、`keyevent`、`wait`、`checkpoint`、`hook_attach`、`hook_unload`。启动用明确 `component`，点击用 `x/y`，输入用 `text`，等待用 `seconds`。日志步骤用 `lines`、`buffer`、`level` 和可选 `clear`；默认读取 `main` buffer 最近 500 行，`clear:true` 会先清空指定 buffer，因此该场景不是只读。Hook 使用项目内已审查脚本的绝对路径及明确 `package`；仅附加已运行进程，不自行启动 Frida 服务。

`device.info` 是单独的只读操作，采集启动代次、型号/系统/ABI 属性、屏幕分辨率、内存、数据分区容量、系统版本、网络状态、Wi‑Fi 状态和 Google Play 登录推断。存储值来自 `df -k /data`，同时保留实际返回的 filesystem 与 mount；部分 Magisk/ROM 组合可能返回镜像挂载路径，不能把它改写成 `/data`。网络部分只读系统状态，不发起外网连通性探测；`internet_reachable` 保持 `null` 且 `internet_probe_performed` 为 `false`。Google Play 登录由 Play 包已安装且存在 Google 账户推断，结果不包含账号名、邮箱或其他账号标识。运行时实验开始前先保存这份证据，便于报告复现；它不能证明 App 行为，语义结论仍要回到截图、日志、文件或状态证据。

`devices_state` / `devices_list` 的 `device_status` 持久保存最近的受管预检：手机模式、系统、当前启动槽、开机标识、ADB 授权、电量、存储、Frida 进程和 `root_access`。后者记录 Root 检查结果、执行身份 `caller_uid`、`su_path`、`su_version` 和检查时间。`stale:false` 且 `root_access.status:granted` 表示最近验证过该身份可用 Root，普通操作不用再提醒授权；手机仍会检查当前权限，缓存不能代替命令结果，也不能证明授权永久有效。

普通 `device_manager.preflight` 不请求 Root 权限，只读取执行身份及 `su` 工具的路径、版本。已有有效授权且开机标识、系统、执行身份、工具路径和版本都相同，才复用原结果，并标记 `cached:true`；原验证时间 `checked_at` 和任务 `verified_job` 不会被这次普通检查刷新。没有可复用记录时显示 `not_checked`。任务确需验证 Root 时加 `--check-root`，这才会执行可能触发手机授权的检查。`su_version` 是命令工具报告的版本，不能当作管理 App 的版本或永久授权政策。

刷机、重新 Root、模式检查中的重启、人工接管、已经启动的 Frida 安装失败或未确认的预检会让旧记录失效；预检或 `device.info` 发现开机标识/系统变更时，也会让另一份旧记录失效。`device_status` 和 `last_device_info` 都保留时间与 `stale`；刷新预检不能让旧手机详细信息恢复有效，应分别用 `preflight` 和 `device.info` 刷新。记录绑定登记的手机，位于共享服务的 SQLite 数据库，跨 Session 和服务重启保留；不会因旧任务证据清理而丢失。没有预检记录时返回 `null`，需提交 `device_manager.preflight`，不能凭型号或旧研究档案认定已授权。

检查点示例：

```json
{"action":"checkpoint","seconds":5,"budget":3,"max_insertions":1,"allow":["device.screenshot","device.observe"]}
```

只有原实验容许观察造成的时延和 Hook 触发时才添加检查点。连续取证或计时实验不插入。检查点之前的命令结束后，控制者执行兼容请求，再核验原活动、进程、设备代次与 Hook。第一版只插入一层观察，不支持任意脚本中途抢占。后台 Hook 仍存活时占用不会因“启动成功”而释放。

请求复用某场景时，在截图请求中传入该任务的 `scene`。只有接受该 Hook 环境才能设置 `accept_hooks:true`。场景结束或不满足条件时返回过期/失败，不悄悄另开页面。

用 `jobs_cancel` 请求停止；清理完成才释放。人工操作先用 `devices_manual_acquire`，等 `handed_over:true`；交还用 `devices_manual_release`。`devices_recover` 可核实只读故障与人工交还；未知修改、残留 Hook 或子进程仍需相应恢复步骤，不能把恢复接口当作清空锁。
