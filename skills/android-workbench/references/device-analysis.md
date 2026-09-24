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

`device.info` 是单独的只读操作，采集启动代次、型号/系统/ABI 属性、屏幕分辨率、内存和数据分区容量。存储值来自 `df -k /data`，同时保留实际返回的 filesystem 与 mount；部分 Magisk/ROM 组合可能返回镜像挂载路径，不能把它改写成 `/data`。运行时实验开始前先保存这份证据，便于报告复现；它不能证明 App 行为，语义结论仍要回到截图、日志、文件或状态证据。

检查点示例：

```json
{"action":"checkpoint","seconds":5,"budget":3,"max_insertions":1,"allow":["device.screenshot","device.observe"]}
```

只有原实验容许观察造成的时延和 Hook 触发时才添加检查点。连续取证或计时实验不插入。检查点之前的命令结束后，控制者执行兼容请求，再核验原活动、进程、设备代次与 Hook。第一版只插入一层观察，不支持任意脚本中途抢占。后台 Hook 仍存活时占用不会因“启动成功”而释放。

请求复用某场景时，在截图请求中传入该任务的 `scene`。只有接受该 Hook 环境才能设置 `accept_hooks:true`。场景结束或不满足条件时返回过期/失败，不悄悄另开页面。

用 `jobs_cancel` 请求停止；清理完成才释放。人工操作先用 `devices_manual_acquire`，等 `handed_over:true`；交还用 `devices_manual_release`。`devices_recover` 可核实只读故障与人工交还；未知修改、残留 Hook 或子进程仍需相应恢复步骤，不能把恢复接口当作清空锁。
