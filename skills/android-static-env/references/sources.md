# 官方来源与核对记录

核对日期：2026-09-16。具体 URL、版本、制品大小与上游校验值保存在 `assets/toolchain.lock.json`。版本是本次选用的基线，不是未来的“最新版本”承诺。

| 组件 | 官方来源 | 本次采用 / 需要保留的差异 |
| --- | --- | --- |
| Java | [Adoptium API](https://api.adoptium.net/) / [Temurin 21](https://github.com/adoptium/temurin21-binaries/releases) | JDK 21，锁定实际制品 URL 与 SHA-256；不改系统默认 Java |
| SDK | [sdkmanager](https://developer.android.com/tools/sdkmanager)、[官方仓库元数据](https://dl.google.com/android/repository/repository2-3.xml)、[apkanalyzer](https://developer.android.com/tools/apkanalyzer)、[AAPT2](https://developer.android.com/tools/aapt2) | Command-Line Tools 21.0、Build-Tools 36.0.0；`*_latest.zip` 中的 build ID 固定，不按字符串 latest 动态跟进 |
| JADX | [skylot/jadx](https://github.com/skylot/jadx)、[1.5.6](https://github.com/skylot/jadx/releases/tag/v1.5.6) | 区分运行发布包与源码构建所需 JDK；不能保证 100% 反编译 |
| Apktool | [安装文档](https://apktool.org/docs/install/)、[3.0.3](https://github.com/iBotPeaches/Apktool/releases/tag/v3.0.3) | 使用独立 wrapper，不向 `/usr/local/bin` 写文件 |
| smali/baksmali | [google/smali](https://github.com/google/smali)、[Google Maven 版本元数据](https://dl.google.com/android/maven2/com/android/tools/smali/smali/maven-metadata.xml) | 采用 Maven 3.0.10；GitHub release 标签与 Maven 已发布版本不一致，3.0.6 的 Maven 路径本次返回 404。发布 jar 不是可直接 `java -jar` 的 fat jar，需运行依赖与 Main class |
| dex2jar | [2.4](https://github.com/pxb1988/dex2jar/releases/tag/v2.4) | 该 release 元数据未提供 digest；首次下载 SHA-256 的证据等级需如实记录 |
| bundletool | [google/bundletool](https://github.com/google/bundletool)、[官方说明](https://developer.android.com/tools/bundletool) | 1.18.3 的 all jar；AAB/APKS 与第三方拆分容器格式分别处理 |
| Androguard | [官方仓库](https://github.com/androguard/androguard)、[PyPI](https://pypi.org/project/androguard/4.1.4/) | 4.1.4；版本元数据优先于旧 wiki 里的安装示例 |
| APKiD | [rednaga/APKiD](https://github.com/rednaga/APKiD)、[PyPI](https://pypi.org/project/apkid/3.1.0/) | 3.1.0，依赖 `yara-python-dex>=1.0.1`；单独 venv |
| Quark | [quark-engine](https://github.com/quark-engine/quark-engine)、[文档](https://quark-engine.readthedocs.io/)、[规则仓库](https://github.com/quark-engine/quark-rules) | 26.9.1、Python >=3.10；规则 commit 单独固定 |
| Semgrep | [文档](https://semgrep.dev/docs/)、[本地规则](https://semgrep.dev/docs/running-rules/) | 1.177.0；安装与本地规则 smoke 分开验证 |
| Rizin | [rizinorg/rizin](https://github.com/rizinorg/rizin/releases/tag/v0.9.1) | 0.9.1 Linux x86_64 static 包；支持架构不等于每个发布包都能在各主机运行 |
| Ghidra | [官方发布](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.3_build)、[运行时要求](https://github.com/NationalSecurityAgency/ghidra/blob/Ghidra_12.1.3_build/Ghidra/application.properties) | 12.1.3，JDK >=21；GUI / headless / 样本导入是不同验证层级 |
| MobSF | [官方仓库与 Docker 用法](https://github.com/MobSF/Mobile-Security-Framework-MobSF) | 官方文档示例使用 latest；本 Skill 要求具体版本或 digest，并在 pull 后记录 digest |
| ELF Python 工具 | [LIEF](https://github.com/lief-project/LIEF)、[pyelftools](https://github.com/eliben/pyelftools)、[Capstone](https://www.capstone-engine.org/) | 本次 PyPI 版本 LIEF 1.0.0、pyelftools 0.33、Capstone 5.0.9；独立环境验证 ELF 解析与 ARM/ARM64 解码 |
| checksec | [slimm609/checksec](https://github.com/slimm609/checksec) | Ubuntu 24.04 提供 Bash 版 2.6.0，使用 `--file=`；上游 Go 版 3.x 使用 `file ... --output`，不要混用参数 |
| YARA | [官方文档](https://yara.readthedocs.io/) | 通用 CLI 与 APKiD 自带 Python binding 分开管理 |

## 本次研究工具的使用

- **Exa**：搜索并读取 JADX、Apktool、APKiD、Rizin、smali、Ghidra、MobSF 的官方内容。
- **Tavily**：查询 Android Developers 的 SDK 工具说明，并提取 MobSF、Quark 文档。一次扩展搜索返回 429，随后官方页面提取成功；未把失败查询当作证据。
- **Context7**：解析 `/androguard/androguard` 并查询安装与 v4 APK/DEX API；解析 `/semgrep/semgrep-docs` 并查询本地规则扫描。查询 `/lief-project/lief` 核对只读 ELF 解析 API。Quark 查询没有匹配到正确库，改查其官方仓库/文档。
- **上游元数据交叉核对**：GitHub Release API、PyPI JSON、Google SDK XML 和 Google Maven POM/校验文件用于固定实际制品。没有依赖搜索摘要推测下载地址或哈希。

平台移植或升级时再次查相关官方文档即可，无需每次执行安装都重新运行三种搜索。
