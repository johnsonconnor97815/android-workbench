# 工具范围与选型

默认 full 覆盖常见 APK 的多层静态分析。以下“内置”表示安装器有实现；“按需”表示根据平台和样本安装，不声称内置一键完成。

| 分析任务 | 工具 | 安装范围 / 判断依据 |
| --- | --- | --- |
| ZIP、文件类型、检索、JSON、证书 | unzip、zip、7z、file、strings、ripgrep、jq、OpenSSL | full 系统包；SHA-256 由 Python 标准库计算 |
| Manifest、资源表、权限、DEX 清单 | apkanalyzer、aapt/aapt2、dexdump、Apktool | core；SDK Command-Line Tools + Build-Tools，无需模拟器 |
| 签名与对齐验证 | apksigner、zipalign | core；使用 verify/check，不重签原样本 |
| Java/Kotlin 反编译 | JADX CLI/GUI | core；反编译错误保留为证据，不能当作完整原始源码 |
| DEX 汇编与反汇编 | Google smali / baksmali | core；从 Google Maven 下载固定版本及运行依赖，使用完整 classpath |
| 大型 APK 的类与引用快速定位 | Droid ASC | full，独立 venv；输入为 APK/ZIP 容器，不直接接受裸 DEX。先用于 `listclass`、`getclass`、`getmanifest`、`findrefs` 快速缩小范围，重要命中仍需 JADX、Smali、Androguard 或数据流工具复核；类名优先使用完整 Dalvik 描述符，`findrefs` 查询按正则处理，字面量需转义 `.*?[](){}|^$` 等元字符 |
| DEX → JAR 交叉验证 | dex2jar | full；提供 d2j-dex2jar 等入口，不替代 Smali 语义核对 |
| AAB / APKS | bundletool、unzip、JADX | core；dump/validate 用于检查；需要生成 APK 时保存派生产物和参数 |
| XAPK/APKM/拆分 APK | unzip/7z、逐 APK 的 apkanalyzer/apksigner、split 元数据 | 不把任意 ZIP 当 bundletool APKS；保留 base 和全部 splits，不能只分析 base 后声称覆盖整个应用 |
| Python 批量 DEX/APK 分析、调用关系 | Androguard | core，独立 venv；`androguard-python` 指向这个环境 |
| 编译器、加固、混淆特征识别 | APKiD | full，独立 venv；验证 YARA 的 dex 模块能编译，命中是特征证据，不等于成功脱壳 |
| 自定义二进制规则 | YARA CLI | full 系统包，与 APKiD 的 Python YARA 分开；规则的来源、版本和误报需记录 |
| Android 行为规则 | Quark Engine + quark-rules | full；引擎固定版本、规则固定 commit，使用显式 `-r` 路径，避免隐式更新 |
| Java/Kotlin/XML/JS 模式扫描 | Semgrep | full，独立 venv；使用本地配置并关闭 metrics/version-check，反编译源码可能丢失语义 |
| 原生 ELF、符号、反汇编 | readelf、objdump、LLVM、Rizin/rz-bin/rz-asm | full；确认 ARM/ARM64 支持，不用主机架构选项误解码 APK 内 `.so` |
| ELF/DWARF 与指令的脚本化分析 | LIEF、pyelftools、Capstone | full 的 native-python 独立 venv；`so-info` 只读解析，支持 ARM/ARM64，ARM 的 Thumb 模式不能猜测 |
| `.so` 保护配置 | checksec、readelf | full；检查 RELRO、NX、Canary、RPATH/RUNPATH；ET_DYN 不能单独证明 PIE，未导入 canary helper 不能直接推出漏洞 |
| 原生反编译、交叉引用、批处理 | Ghidra + analyzeHeadless | full；锁定版本要求 JDK 21，GUI 和 headless 分开验证 |
| 综合本地静态报告 | MobSF | 容器选项；检查 Docker 可用性、固定镜像 digest、服务健康和报告生成 |
| 另一种 Java 反编译结果 | CFR、JD-GUI | 按需；交叉核对失败方法，不默认堆叠所有反编译器 |
| 图形化原生分析 | Cutter；用户已有 IDA/JEB/Binary Ninja | 按需；Cutter 基于 Rizin；商业工具不自动下载、注册或处理许可 |
| 源码审计 | Android Lint、detekt、mobsfscan、项目已有 SAST | 按需；需要对应源码/构建上下文，不能声称 APK 可恢复原始构建信息 |
| 资源、嵌入脚本、JS bundle | Apktool、7z、strings、Semgrep、格式专用解析器 | 按输入选择；压缩、加密内容先恢复格式链 |
| Flutter AOT / Dart | blutter、Dart SDK 的匹配工具 | 按需；以实际 Flutter/Dart 版本、目标架构和产物格式匹配，普通 JADX 看不到全部 Dart 逻辑 |
| Unity IL2CPP | Il2CppDumper、Cpp2IL | 按需；匹配 `libil2cpp.so`、`global-metadata.dat` 和 Unity 版本 |
| React Native Hermes | 匹配 Hermes 版本的 hbcdump/相关解析工具 | 按需；检查 bundle 是否为 HBC，字节码版本不匹配不得强行解释 |
| 跨版本比对 | diff、JADX 输出比对、rz-diff、Ghidra Version Tracking | 按需；相同反编译设置、区分改名/资源重排与逻辑变化 |

## Agent / MCP 接入

JADX、Apktool、PyGhidra 与 Semgrep 的安装、客户端配置和实际调用验证见 [MCP 文档](mcp.md)。MCP 是调用接口，不能补回加固后缺失的代码或自动扩大引擎的样本覆盖。

## 最小闭环

- 包层：样本来源与 SHA-256 → ZIP/Manifest/签名 → 资源和 DEX 清单。
- 字节码层：Smali/DEX → JADX 与备选反编译 → 方法级对照；源码还原不是语义证明。
- 原生层：ELF 架构/符号/导入 → 反汇编和反编译 → JNI 对应关系。
- 扫描层：固定工具 + 固定本地规则 → 原始结果 → 人工定位证据；报告不直接把规则命中写成漏洞成立。

Frida、Objection、ADB 设备操作、模拟器、流量代理、动态解密和脱壳属于另外的分析阶段，不作为此 Skill 的安装成功条件。

## `.so` 分析的边界

保留 APK 中 `lib/<abi>/` 的路径和每个 `.so` 的 SHA-256，记录架构、动态依赖、导入/导出、重定位、`.init_array` 和 JNI 入口。`Java_*`、`JNI_OnLoad` 是入口线索；动态 `RegisterNatives` 需要交叉引用定位，不能只查导出名后声称 JNI 映射完整。符号被 strip、控制流混淆和运行时解密会限制静态结果。

不对样本使用 `ldd`、`dlopen` 或执行入口来探测依赖；使用 ELF 的 `DT_NEEDED`。生成的 C/C++ 伪代码只用于辅助核对。按需增加 Ghidra 脚本/数据类型、用户已有 IDA/JEB、Ghidra Version Tracking 或 BinDiff；这些专项扩展没有包含在通用安装器中。
