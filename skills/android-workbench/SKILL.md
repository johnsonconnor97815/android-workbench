---
name: android-workbench
description: 统一组织 Android 环境准备、APK 导出、静态分析、Frida 定制和设备场景，并通过共享服务协调多个 Session 的手机与工具环境占用。用于需要组合这些能力、查询队列或解决设备操作冲突的分析任务。
license: MIT
metadata:
  author: johnsonconnor97815
  version: "0.2.0"
---

# Android Workbench

这是 Android 分析插件的唯一入口 Skill。先读取 [共享调度与路由规则](references/workbench-routing.md)，再按任务读取一个必要的模式文档；不要为了普通请求加载全部组件。

| 任务 | 读取 |
| --- | --- |
| 多能力组合、队列、冲突与恢复 | `references/workbench-routing.md` |
| 设备信息、截图、页面操作、状态观测、受管 Hook | `references/device-analysis.md` |
| 静态环境、分析 MCP、工具链版本 | `components/static-env/README.md` |
| 分析请求路由、包概览、Manifest/资源/预览、JADX 反编译、源码导航、签名、字符串、知识检索、Python 计算/session 状态、持久 scratchpad 与宿主执行 | `components/analysis-agent/README.md` |
| APK 补丁、方法级 DEX 补丁、USB 联网代理、重打包与复杂逆向 | `components/apk-reverse/README.md` |
| Frida 选版、修改、构建、部署 | `components/frida/README.md` |
| 已安装 App 的 APK 导出与校验 | `components/apk-export/README.md` |

插件中的 Claude Agent 只是窄域分工入口，不是资源所有者。设备、ADB、Frida、共享分析工程和输出路径都通过 Workbench MCP 或已登记操作协调；服务不可用时报告阻塞，不退回裸 ADB/Frida。

项目专用扩展仍以 `workbench.project.json` 显式登记为准。不要把 APK、证据、设备序列号、凭据或生成的工具环境写进本插件。
