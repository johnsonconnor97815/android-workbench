# APK 代码索引与行为分析

适用于整包代码核对，以及保活、守护、拉起、强停后启动、通知/浮层/Activity 弹出和 Native/VM 的分析。先建立固定样本的导航与范围，再追踪具体逻辑；运行验证用来回答明确剩余问题。用户只要某个行为时，按该行为追踪，不扩大到全部业务或 SDK。

登记项目中的执行与设备调度遵守 [共享路由](workbench-routing.md)。下面的离线脚本没有 ADB/Frida 或工具安装能力。在登记的共享项目中，通过已有 `analysis.exec` / `analysis.python` 执行，声明读写路径；设备与共用环境仍走专门的受管操作。这里没有新增名为 `analysis.code_index` 的 MCP operation。

## 固定输入、范围与工作记录

固定包名、版本、base/config split、每个 APK SHA-256、ABI、全部 DEX（包括 assets 中的 DEX）、ELF、Manifest 组件、资源及解释型资产。不要混用更新版 APK、旧反编译结果、重打包样本和未知来源的下载程序。分析输出留在独立项目，不写入 Workbench Git。

范围至少区分：App 命名代码、混淆辅助/合并 callback、App 的 SDK 接口与选定 case、SDK 内部、平台、未取得的外部代码、运行验证。排除 SDK 内部时仍保留 App 的输入、资格、顺序、失败处理和显示/关闭回调；SDK 命名类可能包含 R8 合入的 App case。短类名、包名、无直接 caller 和零字符串命中都不足以证明整个类的归属或不可达。用具体调用者、构造参数/selector、字段/继承关系、JNI 导出/装载类和原代码支持归属；未归属的边界明确记录。

持续追加项目内 `process.jsonl`：问题、假设、索引/源码位置、缺少的功能、实现决定、命令/工具源哈希、验证结果、结论修订、下一步和剩余限制。报告引用的当前文件变动前保存内容哈希快照；原证据保持不变。用户明确要求 Goal 时才创建；验收范围与结束条件写清楚，不用任务数、扫描数或字节覆盖率冒充语义完成。

## 原字节索引与快速导航

仓库提供 [`scripts/apk_code_index.py`](../../../scripts/apk_code_index.py) 与 [`scripts/apk_navigation.py`](../../../scripts/apk_navigation.py)。设置 `WORKBENCH_DIR` 为登记的实际 checkout，`ANALYSIS_ROOT` 为项目内专用输出目录。以下是离线 CLI；在共享登记项目中，将实际路径组成的命令交给 `analysis.exec`，不要绕过设备或环境队列。

```bash
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" prepare \
  --apk base.apk --apk split_config.arm64_v8a.apk
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" build \
  --app-prefix 'Lcom/example/app/' --native-focus lib/arm64-v8a/libexample.so
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" extend-types
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" extend-fields
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" extend

python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" method \
  'Lcom/example/app/Entry;->show(Landroid/content/Context;)V' --depth 1 --limit 40
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" native \
  libexample.so 0x12340 --depth 6 --limit 60
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" constant 'example literal'
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" type 'Lcom/example/app/Worker;'
python3 "$WORKBENCH_DIR/scripts/apk_code_index.py" --root "$ANALYSIS_ROOT" field \
  'Lcom/example/app/State;->enabled:Z'
```

DEX 索引只依赖标准 Python。ELF 元数据/指令索引需要现有 `pyelftools` / `capstone`；可在子命令前用 `--native-env <已准备依赖目录>` 显式选择。缺失依赖按静态环境组件准备，不通过分析脚本隐式安装。`build` 可重复 `--app-prefix`；这是导航提示，不是所有权证明。未传 `--native-focus` 时 ELF 只收集符号/FDE/接口元数据；重点指令扫描当前只支持 little-endian ARM64。多 ABI 时用完整 `lib/<abi>/<name>.so` 避免同名库混选。

`prepare` 输出输入哈希、去重后的 DEX/ELF 清单、每个 APK/entry origin 和提取原文件；已存在输入清单时拒绝覆盖。非标准/加密代码文件保留在 `inventory/unsupported-code.json`，不能无声跳过。它是代码输入准备器，Manifest/资源/签名仍用 analysis-agent 现有能力收集。DEX 标准容器以解析器支持的原布局为准，不把不支持的 container/odex 当作普通 DEX。

`code-index.sqlite3` 中方法身份为 `DEX_SHA:m:原method_idx`。定位保留完整 class/name/descriptor、code_item、指令单位 PC、文件 offset、原字节、原池索引和输入 SHA。不要把旧 call graph ID 当原始 method_idx；签名查找保持大小写，防止 `A9` / `a9` 混淆。MUTF-8 按 UTF-16 单位读取，保留原编码；不要用普通 UTF-8 解码抹掉 NUL、代理项或非 ASCII 名称。

直接 DEX 调用、type/field/string uses、ELF 符号/FDE、PLT 重定位、JNI 名称匹配和间接调用分别标记。自动匹配 JNI export 只是结构绑定；短 JNI 导出名对应多个 overload 时留为未解。`native/jni/<elf_id>/map.json` 可按项目提供有出处的 export/registration 候选表，候选表形状不证明 RegisterNatives 执行。

Native 反向导航用有界 BFS，沿 `bl`、指向函数入口的 `b` 候选和 `code-index/reviewed-edges.json` 中有出处的异步边追溯到 JNI/Java。`refresh-edges` 导入后者，校验引用文件 SHA；src/pc/dst 是地址，kind、condition、status、source、source_sha256 必须明确。条件分支、函数内部跳转和间接目标不猜成调用。循环和节点/结果截断显式输出。已有 `review_records` / `method_reviews` / `native_reviews`、局部契约和 `native_interface_sites` 可附到查询；工具不会自动生成这些语义审查表。

建立索引后以代表性签名、Worker 的 const-class、字段写入者、JNI 和 pthread→JNI→Java 路径验证导航。大文件哈希按不可变文件缓存，JSONL 用行号与行哈希引用；不在每个方法查询时重算同一大文件，也不向上下文打印整张图。索引完成后返回精确查询与有限正文。

## 完整局部代码与重点行为

用 JADX/Java/Kotlin 了解意图，以原 DEX 的官方反汇编或原 ELF 指令定论。反编译字段类型、寄存器和异常区域可能错。对保留方法读取完整本体，检查签名/header 与 code_item 一致、全部指令边界、调用池、type/field、分支 true/false、switch key/default、try 顺序/范围/handler、所有正常/失败返回。源码行号用实际文件行数，EOF 换行不是额外一行。

当复杂方法需要结构化契约时，用基本块输入 frame 表示未知合流值，按指令记录赋值、宽值的两个字、call 参数与结果、字段/数组副作用和退出条件。保留协程 state/resume、循环、异常与分配/平台边界。操作数解析只读取对应格式的寄存器位置；方法名里的 `v0` 不是寄存器。不可达对齐 NOP 可紧邻 switch 数据 payload，数据不作可执行 fallthrough；若入口或 handler 可达 payload，拒绝该解析并排查。图和模板本身不形成全方法/全程序语义证明，数值行为遵守固定 DEX 规范，不能借 Python 无限整数偷换 Android 整数。

用第二套实现核对原字节/官方反汇编/局部契约，注明是否同一分析者。项目专用 ARM 解释或有界模型仅在需要时编写，记录实际执行的工具源及依赖、API/网络/分配/线程替身、病例输入、比较依据、未覆盖异常。解释原指令不等于执行目标 Native/ART；合成输入或手动调用不标成自然发生。

对账要同时检查 App 命名方法、原 DEX 的全部直接目标、构造/回调类上下文、继承入口和既有审查中的混淆辅助代码。仅用旧 smali 直接引用清单会漏构造 overload 或合并类。既有卡只有 selector/PC 窗口时，补完整局部体或保留明确的窗口范围；文件级引用不提升为全方法完成。

## 保活、守护与强停

从 Application、Provider、Service、Receiver、Worker 和生命周期入口反查初始化及触发。逐条记录：谁安排，何时，资格/权限，API 类型（一次性或周期任务、Alarm/Handler/Job/WorkManager），唯一名称与 KEEP/REPLACE，时间单位与 clamp，取消/修复，onTaskRemoved/onDestroy/restart 返回值，异常是否到共同续排尾部。类名含 Periodic 不代表创建 PeriodicWorkRequest；const-class 和库内动态注册也要索引。

区分 Java Thread/Coroutine、pthread、子进程、同 UID 额外进程、外部服务和独立守护。pthread_detach 只改变线程资源回收方式；popen/Runtime.exec 要找实际命令、参数、读取、wait/destroy/close、触发与退出。SDK 内部 fork/clone/execlp 导入不等于 App 已实现守护；仅搜 App 包名前缀也会漏子进程和混淆入口。查 UID、父子 PID、进程名与启动组件，并保留有限观察条件。

强停、SIGKILL、滑掉任务、HOME、Recents 点击、通知点击、系统配置重建分别建路径。要证明“强停完成后自行复活”，需要强停发起/完成的先后、stopped 状态、PID/UID、Job/bind/取消 IPC、窗口内用户/外部启动动作和新进程启动方。只有强停入口附近新 PID 不能证明这一结论。用匹配 Android 版本的固定平台代码解释机制，注明厂商差异；历史缺失的完成时序不能靠 APK 恢复。受管 Hook 失败、有限阴性观察或模拟调度通过都不证明实际兼容/全局不复活。

## 拉起与弹窗

从 `startActivity/startService/PendingIntent/notify/Dialog/WindowManager/Compose/Fragment` 等实际接口反查到触发者及 gating，再前向跟到显示内容、关闭和清理。表中分别写出通知、显式 Activity、透明引导 Activity、系统权限页面、系统对话框、overlay 与广告显示边界；fake_notification 的名称不代表 NotificationManager 行为。Manifest 与权限声明只是条件，要核对实际调用及窗口 flag/layout/theme。

追踪延迟 Runnable、重复启动、retry Job、默认参数 mask、Bundle/Intent extra 的实际类型和继承的 onCreate。参数为 long 0 的调用不能拿 int 默认值或另一条默认 3000 ms 路径解释。合并 callback 的构造 selector、原 switch payload、实际 target PC 与返回路径要连起来；SDK 命名类中的 App case 仍纳入。

核对 ready/preload 是否等待、pending 保存/读过期/消费短路、每日/前后台/HOME/锁屏资格、统计回调和清键顺序、timeout/dismiss/onUserLeaveHint/onDestroy、mutex 释放。回调被构造后可能被覆盖，构造不证明注册，注册不证明派发；调用显示接口正常返回也不证明窗口实际显示。无锁本体不能承诺并发去重。

## Native、加载与 VM

先定位 loadLibrary/dlopen、JNI export、RegisterNatives 候选及原 Java 声明，用原 relocation/GOT/PLT 和 receiver 数据流识别接口。相同 slot offset 只有在 receiver 类型明确时才对应 JNIEnv/JavaVM/C++ vtable；具体间接目标与运行绑定仍分开。补 FDE/符号遗漏的代码区间，保留候选/数据/零填充状态；线性扫描可恢复范围不等于所有函数语义或执行可达。

将入口参数、Java CAS/字段、Native global、JNI 异常、线程 tuple、pthread 返回和 C++ throw/unwind 连起来。全局地址要跨 GOT relocation、直接 ADRP+offset 和其它线程写入查证；本函数无 reset 不等于全局永不 reset。JNI GetEnv/Attach 的错误与 null 输出要独立跟踪。Java catch(Throwable) 不自动覆盖 C++ thread-create 错误，普通内存标志不自动具有 CAS/持久化语义。

对影响保活/拉起/窗口的 Native 代码，跟踪请求参数/环境检查、短路与低位结果、HTTP 返回/超时、文件读写/权限/清理、摘要、加解密、ZIP 选择、ByteBuffer/dexElements/loader 和外部 init 签名备用。下载真值、部分读取、摘要通过、加载成功与 Java init 执行是不同门。外部类在固定 APK 不存在且载荷未取得时，交付已恢复接口与有限未知；不要虚构其 UI/VM 效果或无限重复手机试验。

VM 判断分别回答平台 JavaVM/ART 与自定义字节码解释器。寻找真实 bytecode stream、取指/解码、dispatch、虚拟寄存器/栈、handler 和 Java/native 桥接证据；标准 JavaVM、混淆跳转或寄存器保存/恢复不单独证明自定义 VM。未找到的结论注明检索范围；没有直接 SVC 或某符号不意味着不存在任何系统调用。若库直接从 APK 映射，按 ZIP entry 数据起点 + ELF PT_LOAD + maps offset/长度识别，不能只搜 `.so` 文本。

## 当前交付、验收与流程沉淀

每项结论具有原文件 SHA 与完整签名/code_item/PC 或 ELF 地址、源码摘要、调用时机、调用方→条件→动作→正常/异常/关闭路径、验证方式、适用版本与限制。调用边区分 decoded / observed / inferred / unknown；触发方未确定时不标为自然自启动。对旧报告建立修订对应表：明确撤回错误，完成后的旧 residual 不再重复当成未审事项，原证据仍保留。

可用 [`scripts/audit_behavior_report.py`](../../../scripts/audit_behavior_report.py) 检查报告 JSON、fact registry 和方法声明中的哈希/位置/引用/未审字段：

```bash
python3 "$WORKBENCH_DIR/scripts/audit_behavior_report.py" --root "$ANALYSIS_ROOT" \
  --report delivery/claims.json --facts delivery/facts.jsonl \
  --declarations delivery/method-declarations.jsonl --output delivery/source-audit-01.json
```

它检查结构和来源，不自动证明断言正确；原断言/状态变化要显式修改事实 registry，不能为通过审计把 pending 随意变成 reviewed 或把来源改成不相干的有效文件。实际 schema 以脚本与 [证据格式](behavior-evidence-schema.md) 为准。审计输出拒绝覆盖，迭代用新编号。

验收同时看：输入身份、保留方法的完整局部代码与语义/调用边界、特定行为链、每条证据引用、真实运行与模型区分、旧结论修订和自审。局部静态完成、运行验证完成和任意输入的全程序证明是不同层次。未知远程程序或丢失历史事实可作为有出处的交付限制；仍可由固定代码回答的条件/参数/分支、未对账的保留方法则是待完成工作。

没有进展的重复设备实验先回到缺口：是索引功能、原源码、scope、接口语义、静态数据流、需要新运行证据还是无法恢复的历史事实。索引漏功能时补齐并验证，再继续具体逻辑；设备试验只有新增可测假设/条件才重跑。分析验收后才把已验证通用流程和工具写到仓库唯一 Skill，目标专用原指令模型/样本留在项目。修改仓库后运行 `python3 scripts/check.py`、`python3 scripts/build_plugin.py --check`，可用时 `claude plugin validate .`；不要把模拟测试当成真实 Hook 兼容性。
