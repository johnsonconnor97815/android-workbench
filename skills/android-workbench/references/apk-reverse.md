# APK 逆向与补丁流程

适用对象是已授权的 APK、AAB、DEX 或 SO 分析，包括去广告、客户端限制判断、手术级补丁、重打包、签名和安装验证。不要把这份流程用于未授权目标。

参考上游是 [newliver666/apk-reverse](https://github.com/newliver666/apk-reverse)。完整组件、原始参考文档和可运行脚本在 `components/apk-reverse/README.md`。它的价值不是新增一套工具链，而是把已有工具组织成一套经验驱动的逆向流程；Workbench 已有的静态环境、APK 导出、Frida 和设备调度能力仍优先使用。

## 适用边界

- 只在任务目标是修改 APK、DEX 或 SO，重打包，去广告，判断客户端限制，或分析复杂加固样本时读取本文。
- 仅导出 APK、静态阅读代码、权限审计、环境搭建或普通设备观察时，不要加载本文，避免把流程变成负担。
- 如果任务只是判断“能不能做”，先完成交付物和归属层分类；不要默认启动完整逆向流程。

## 是否需要接入

- Workbench 已覆盖工具链安装、APK 导出、静态分析、Frida 和设备队列，不需要再复制一份同类工具。
- 需要补充的是“先分类、再修改、最后验证”的流程约束，尤其是重打包后的安装、启动和目标功能验证。
- 已整包导入上游脚本、参考文档和证据；57 个 `apkrev.*` 操作覆盖全部 50 个上游 Python 脚本，Frida JS 模板由对应入口调用。
- 上游自己的能力矩阵区分 `ok`、`partial`、`blocked`。不要把 `partial` 或 `blocked` 当成已验证能力，也不要把文档推断当成实测结论。

## 四个准确性检查点

1. **交付物**：用一句可测试的话写清目标形态，例如“未 root 设备可安装并运行”。Root 辅助、Hook、主机代理和补丁机上的结果只能算 fallback，不能冒充最终交付物。
2. **环境**：先确认工具和设备状态。缺少 APKTool、JADX、Apksigner、Frida 或合适 ABI 时，先补环境，不要把环境故障归因于补丁。
3. **归属层**：确定目标逻辑属于 Java/DEX、Native、Dart/Unity、配置还是服务端。改错层的常见表现是“构建成功但没有效果”，不是一定报错。
4. **基线与对照**：保留原样本和控制构建；每次只改一个变量。重打包后必须安装、启动、触发目标功能，并用哈希确认设备上运行的确实是新构建。

## 推荐吸收的脚本

| 上游脚本 | 用途 |
| --- | --- |
| `doctor.py` | 列出当前主机和设备能力，区分缺失、部分可用和可用 |
| `preflight.py` | 检查设备、代理、端口转发和时钟漂移，避免误判补丁故障 |
| `dex_find_insn.py`、`dex_patch_bytes.py`、`dex_check_verifier.py` | 等长 DEX 修改、指令定位和校验器合法性检查 |
| `repack.py`、`install_test.py` | 重打包、对齐、签名、安装和运行验证 |
| `probe_api.py`、`tls_check.py` | API 请求复放和 TLS/证书链排查 |
| `run_probe.py`、`spawn_patch_detach.py` | Frida 注入、进程恢复和运行时观察 |
| `dex_dump_validate.py`、`dex_mem_scan.py` | Dump 出来的 DEX 去重、结构检查和骨架识别 |

这些脚本已内置并接入 Workbench 队列；仍要按能力矩阵区分 `ok`、`partial` 和 `blocked`，不要把未验证路线当成已验证结论。

## 已登记操作

- 环境与能力：`apkrev.doctor`、`apkrev.capabilities`、`apkrev.device_shell`、`apkrev.preflight`、`apkrev.rasc_build`
- APK 与 DEX：`apkrev.apk_diff`、`apkrev.blob_decode`、`apkrev.datastore_inject`、`apkrev.dex_*`、`apkrev.dexutil`、`apkrev.find_refs`、`apkrev.repack`、`apkrev.so_constpatch`
- 模块脚手架：`apkrev.lsposed_scaffold`
- 设备与运行时：`apkrev.coldstart`、`apkrev.frida_rpc_serve`、`apkrev.grab_crash`、`apkrev.install_test`、`apkrev.lib_map`、`apkrev.run_probe`、`apkrev.sig_probe`、`apkrev.snap`、`apkrev.spawn_patch_detach`
- Native、协议与服务端：`apkrev.elf_plt`、`apkrev.java2c_probe`、`apkrev.native_crash`、`apkrev.probe_api`、`apkrev.protobuf_decode`、`apkrev.stalker_report`、`apkrev.svc_scan`、`apkrev.tls_check`
- Dart、KernelSU 与 VMP：`apkrev.dart_*`、`apkrev.kernel_*`、`apkrev.vmp_*`
- 维护：`apkrev.scan_leaks`

## Workbench 接入规则

- 优先使用本插件的 `static.*`、`apk.*`、`frida.*` 和 `device.*` 操作；不要为了一个新脚本绕过共享队列。
- 涉及设备、ADB 或 Frida 的脚本必须先登记为项目扩展，并保留原参数、输出路径和退出码含义。
- 离线脚本可以登记为 `compute` 操作；重打包、安装和设备验证要明确输出目录，避免多个任务写同一产物。
- 原始 APK、反编译目录、补丁产物和安装日志分开保存；记录样本 SHA-256、来源和工具版本。
- 不把项目专用补丁、样本、设备序列号或证据写进本插件。
