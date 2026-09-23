<!-- android-workbench-routing -->
在含 `workbench.project.json` 的工作项目中，使用 Android Workbench 统一入口提交已登记操作。本仓库内的脚本会转交共享服务；不要绕过队列操作手机或改写共用环境。服务不可用时先检查 `python3 <Workbench目录>/scripts/workbench.py --project <项目目录> capabilities`，不能退回未协调的直接执行。原能力范围、输入校验和退出码含义仍以本文为准。
<!-- /android-workbench-routing -->


# Android Frida 魔改组件

从目标设备和所需能力开始。当前附带 `17.18.0` 的明确版本案例；其他版本先检查上游，再适配补丁和构建方法。

以 Agent 加载本文件时提供的目录为组件根目录；本文及引用文档中的 `scripts/`、`references/`、`assets/` 均相对于该目录。执行脚本时使用解析后的绝对路径，不假设安装在某个 Agent 的固定目录。

## 工作流程

1. **确认设备和需求。** 区分 Android 设备与构建主机。优先使用用户指定的序列号；多台设备且目标不明时询问。运行 `scripts/inspect_device.py`，记录 Android/API、ROM、ABI、root/SELinux、主机环境；涉及 Java 时补充可取得的 ART 信息。区分 server/Gadget，列出必需和可选能力；没有目标 App 时保留其 ABI、启动和集成行为未测的限制。
2. **选择版本。** 按 [版本选择依据](references/version-selection.md) 查询官方 release、源码/修复记录、具体 issue 和相关实测。给出短候选列表、依据与回退顺序；用户指定版本优先验证。Android 大版本不对应唯一最佳版本。候选耗尽或重复同一未定位故障时，回到最早的不确定阶段。
3. **下载、检查构建条件并建立原版基线。** 用 `scripts/fetch.py` 获取明确版本的源码或官方产物，保留来源、commit、子模块与哈希。客户端使用匹配 Frida 版本，frida-tools 按依赖配套。确认该版本的工具链可取得，在重度修改前检查配置/最小构建。按所需能力验证注入、RPC、Hook、App spawn、Java、ABI 和运行时。探针/API 不适配记为测试受阻；已定位的上游缺陷可保留失败基线后作为独立修复项处理。
4. **确定修改点。** 按意图分为兼容性修复、运行名称修改、跨组件协议/资源修改，写明依赖、目标行为及验证办法。检查源码、生成的 DEX/ELF、嵌入资源、主机/设备产物和实际运行状态。用户已明确的范围直接执行；只有关键歧义才询问。
5. **按版本修改、构建。** 保持干净上游与修改树可区分。已有补丁匹配具体 commit；跨版本重新定位相关实现，`git apply` 成功不证明语义正确。需要客户端协同时一起构建。`17.18.0` 使用 [案例与构建适配](references/case-17.18.0.md)，其他版本按 [新增适配](references/adapting-versions.md) 处理。输出独立锁定清单和产物哈希。
6. **对照验证、部署、交付。** 在同机、相同权限、相同 App 输入和测试语义下比较原版与改版，按功能标记通过、失败、未测或受阻。正式部署后核对运行文件哈希、身份、参数并复测。必需功能、指定修改项、部署复测和临时资源清理全部通过，才报告完整成功。

命令和阶段证据组织见 [操作步骤](references/workflow.md)；设备恢复前先读 [部署与恢复](references/deployment.md)。测试 App 的固定来源与场景见 [App 回归](references/app-regression.md)。

## 执行约束

- 在使用者工作目录保存每次任务的环境、需求、选版依据、阶段结果和原始证据；不写进插件目录。给每次尝试新的输出路径，保留失败记录。
- 同设备的版本试验串行。先记录已有服务和端口；仅停止或清理本次拥有、或明确纳入替换范围的资源。设备脚本使用进程锁，部署子检查继承父任务的锁。
- 续作核对源码、补丁、探针、客户端、工具链/构建参数、已有产物哈希和设备实际状态。`scripts/provenance.py` 可核验源码输入；输入变化后重做受影响阶段。不得仅凭文件存在复用缓存。
- 保留主错误和清理错误。清理失败不能报告整体通过；恢复旧文件还需验证原服务。中断/断连时先读取部署记录，再核实实际状态，不盲目重试。
- root 启动可能触发 Frida 的 SELinux 策略处理；`Enforcing` 不证明策略未变。记录不能自动恢复的状态。关闭 SELinux、重启、APK 重签名均按实际任务范围处理。
- 分别报告功能结果、标识修改结果和目标 App 检测结果。native spawn 不代表 App spawn；Gadget harness 不代表 APK 内集成；ADB/root 视角不替代 App 可见痕迹验证。
- Java 类初始化时序等案例保留适用条件。现有 `17.18.0` 补丁和 Android 12 证据不构成 Android 10 或任意 App 的兼容性承诺。

原创脚本与指引采用 [MIT](assets/licenses/LICENSE-MIT)。附带 Frida 源码补丁沿用上游 [wxWindows Library Licence 3.1](assets/licenses/Frida-COPYING) 及其引用的 [GNU Library General Public License v2](assets/licenses/Frida-COPYING.LIB)，包括上游例外条款。
