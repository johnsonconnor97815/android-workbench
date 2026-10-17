# REA 机制借鉴：来源与实现边界

上一阶段研究日期：2026-10-09。使用 Exa、Tavily、Context7；该阶段仅查文档和固定源码，没有安装引擎、运行目标或操作设备。本文保留当时的设计依据，不声称 Workbench 已实现 REA 的所有机制。本次 Tools 扩展的来源登记另列在文末，安装与真实引擎验收结果见 [实施记录](rea-adoption-plan.md)。

## 来源固定方式

REA 本地核验 `git rev-parse HEAD` 为 `6875d4a64c0d77c4f90ebbec36705f191eb7cdc6`。所有实现判断以[这个固定提交](https://github.com/morluto/rea/tree/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6)为准；Tavily 返回的 `main` 页面和 issue 仅用于定位，不作为固定提交的行为证据。本机对比报告属于研究产物，不随插件分发。

| 工具 | 实际查询与来源 | 用途和边界 |
| --- | --- | --- |
| Exa search + crawl | [Chrome DevTools Network](https://developer.chrome.com/docs/devtools/network/reference)、[Jan Odvarko HAR 1.2](https://www.softwareishard.com/blog/har-12-spec/)、[SQLite URI](https://www.sqlite.org/uri.html) | 查询当日网页；HAR 作者页面明确是冻结的 1.2 格式，不能称为现行 W3C 标准 |
| Tavily search | `site:github.com/morluto/rea "cache" "identity" "unknown"`，找到浏览器观察、provider profile ADR 和 AGENTS 页面 | 搜索摘录仅用于发现文件；ADR 在固定 checkout 不存在，未据此声称该提交具有相关文档或行为 |
| Context7 resolve → query | 先解析 `Python` 得到 `/python/cpython`，再查询 `sqlite3` 的只读 URI、参数绑定和 authorizer；返回 [CPython sqlite3 文档源](https://github.com/python/cpython/blob/main/Doc/library/sqlite3.rst) 与 [只读 URI 测试](https://github.com/python/cpython/blob/main/Lib/test/test_sqlite3/test_dbapi.py) | 官方 CPython 当前文档摘录，未固定 Python 版本；只读接口语义另与 SQLite 官方 URI 文档核对 |

Exa 同时返回了 [W3C 仓库中的 HAR 历史稿](https://w3c.github.io/web-performance/specs/HAR/Overview.html)，其首页明确说明从未由 W3C 工作组发布且已放弃。本文采用作者的冻结 HAR 1.2 文档，不把历史稿写成标准背书。

## 固定源码中可复用的机制

1. **记录输入和工具身份。** [analysisProfile.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/domain/analysisProfile.ts) 将 provider 名称、版本和分析参数一起摘要；[analysisSnapshot.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/domain/analysisSnapshot.ts) 的查询身份包括 target、binding、operation、parameters。建议 Workbench 的结果同时携带输入 SHA-256、引擎版本、会改变结果的选项和结果格式版本。不同输入或工具版本不得共用缓存。身份摘要只证明内容对应关系，不证明分析结论正确。

2. **缓存只复用稳定结果。** [AnalysisSnapshotCache.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/application/binary/AnalysisSnapshotCache.ts) 排除健康检查、当前位置、导航等状态操作；要求明确 document/address，排除 live policy、修改产物和权限变化；换 target/profile 时清空。建议把 freshness 分成 `fresh`、`stale`、`unknown` 并给出原因；运行观察不能因静态输入相同就重放成“刚发生”。时间戳本身不是运行证明。引擎版本不明时禁用持久缓存或明确标注不可确认。

3. **类型化查询和分页。** [JS 查询契约](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/domain/javascript/javascriptSemanticQuerySchemas.ts) 区分 literal/function/property/endpoint 等 seed，结果区分 `no-match`、`ambiguous`、`partial`、`unsupported`；[分析视图契约](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/contracts/analysisViewToolContracts.ts) 对已完成证据选择有正数 limit 的稳定页。建议 Workbench 在固定输入摘要及固定排序下分页，返回 total、offset/next、truncated、coverage；分页不能改变原始分析身份。完整覆盖下的未匹配才支持“未找到”，部分结果不能支持“没有”。

4. **未知问题可以修订。** [residualUnknown.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/domain/residualUnknown.ts) 有 revision、前一修订摘要、支持/反对证据、需要的证明和下一步；[EvidenceLedger.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/application/investigation/EvidenceLedger.ts) 拒绝 expected_revision 不匹配的更新。建议项目状态目录持久化，并原子更新或事务写入，避免两个 Session 覆盖；resolved 要明确区分已验证、撤回、范围外，不把人工关闭当成功证明。摘要链不是数字签名。

5. **已有 HAR 是历史文件，不是当前运行证明。** [webNetworkCapture.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/domain/webNetworkCapture.ts) 明确 `runtime_attribution: "unknown"`。建议运行结果另绑定真实 task_id、实际执行操作、输入身份和观察时间；导入旧 HAR 不伪造任务来源、不自动重新请求 URL。

## HAR 和离线 JavaScript 的具体条件

[Chrome 官方文档](https://developer.chrome.com/docs/devtools/network/reference)说明 sanitized HAR 默认移除 Cookie、Set-Cookie、Authorization 等敏感头，但不能据此宣称任意输入都已去除全部秘密。HAR 有 URL、queryString、postData、响应正文及扩展字段，应用自己的令牌仍可能在其中。建议默认摘要只显示方法、去凭据后的 URL/参数名、状态和 MIME；正文、查询值和自定义头只按明确输出选择暴露，并告知其为保留的原始文件内容。不要把原始 HAR 或解析出的秘密提交 Git。

固定 REA 的 [CaptureRedaction.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/browser/history/CaptureRedaction.ts) 识别 Authorization、Proxy-Authorization、Cookie、Set-Cookie、cookie value 和 URL userinfo；另有 caller 指定 sensitive_values。它并未保证识别所有应用秘密。敏感值不能通过错误文本、摘要、base64 或 hash 再泄漏。

[HAR 1.2 作者格式](https://www.softwareishard.com/blog/har-12-spec/)区分 producer 解码后的文本和 `content.encoding: "base64"`；[HarCapture.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/browser/history/HarCapture.ts) 保留该区别和数字原字面量。建议保留缺失/未知状态，严格校验 base64；不要将重新编码的文本声称为原始网络字节，不对已解码 content 再无条件解压；未知 encoding 返回明确限制。

REA 的预算为 32 MiB 输入、96 MiB 回复、64 层嵌套、30 秒命令期限，超限不返回伪装完整的部分 capture；这些是 REA 的具体选择，Workbench 应定义自己的预算。建议同时限制输入、记录数、单条正文、总解码字节、嵌套深度、输出和执行时间。解析器应拒绝 NaN/Infinity 等非 JSON 数字，并正确处理 UTF-8 BOM。分页只缩短输出，不能代替解析资源限制。

[LocalWebScriptArtifacts.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/browser/assets/LocalWebScriptArtifacts.ts) 核验已保存脚本的 SHA-256 与字节数；[webScriptToolContracts.ts](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/src/contracts/webScriptToolContracts.ts) 则说明完整 module resolver 另需受信 Chromium stub。建议先交付纯离线文本/结构路径，绝不 eval/import 捕获的脚本，不拉取 source map 或 import URL，不把文件名直接用于写出路径。没有语法解析器时只能称文本观察，不能将正则匹配写成 AST、完整调用图或执行关系。

## SQLite 与缓存键：仅适用于实际用到的路径

官方 [Python sqlite3 文档](https://github.com/python/cpython/blob/main/Doc/library/sqlite3.rst)和 [SQLite URI](https://www.sqlite.org/uri.html)支持 `file:...?...mode=ro` 与 `uri=True` 打开只读连接；缺失文件应失败而不是创建。应先校验项目读路径，正确转义文件 URI，用参数绑定传递查询值。不要给外部调用者自由 SQL；固定查询、允许的表和列、正数 limit 更容易明确权限和资源预算。必要时用 authorizer 拒绝 ATTACH 等操作。只读不等于不可读取其他文件或不会耗尽资源。

`immutable=1` 会跳过锁与变化检测；SQLite 官方说明文件若仍变化可能返回错误结果或损坏错误，不能用于活跃服务数据库。读取实时任务库应使用普通 `mode=ro` 和短事务。原始索引缓存键应提交实际输入内容、索引格式/算法、工具版本和分析选项；修改时刻或路径只能帮助发现变化，不足以证明缓存仍对应相同内容。

## 许可证

固定提交的 [REA LICENSE](https://github.com/morluto/rea/blob/6875d4a64c0d77c4f90ebbec36705f191eb7cdc6/LICENSE)是 MIT，版权人为 `Copyright (c) 2026 morluto`。上一阶段仅参考设计，没有复制实现代码。如后续复制代码或实质性文档，应保留版权和许可全文，并登记来源仓库、完整 SHA、原路径、文件摘要、修改说明。REA 使用的第三方库需分别核对许可证，不能因主仓库是 MIT 就统一视为 MIT。

## 本次 Tools 扩展：来源登记与验收边界

用户本次授权加入工具并统一管理。工具来源、固定版本与使用条件集中登记在 [Tools/catalog.json](../Tools/catalog.json)，API/CLI 核对说明、锁文件与安装限制见 [Tools/README.md](../Tools/README.md)。这里引用本仓库的工具登记，不将上一阶段 REA 机制研究当作新引擎安装或运行证据。

本次可执行接线覆盖 Babel parser/traverse、Wakaru、Binwalk、Unblob 和既有 native-python 的 ELF 读取，另复用原静态工具链与 Frida 约束。七个管理/分析入口及当前问题的路由条件见 [使用说明](rea-adoption-usage.md#路由与统一工具入口)。`analysis.offline_js` 继续只提供词法线索；`tools.js_ast` 的真实解析、`tools.js_deobfuscate` 的恢复源码和扫描器产物分别保留身份与限制。固件扫描不代表任意解包、固件重建或运行，其他平台/商业工具仅登记来源不代表已接入。

源码接线、依赖锁、已安装版本、合成固定样本、真实分析样本、插件缓存和运行服务的一致性分别验收。本次工具安装、固定样本和 501 项全量测试已通过，原六阶段与 476 项测试保持为历史证据。固定样本不代表真实应用准确率或真机兼容性；部署状态用当次内容一致性检查确认。
