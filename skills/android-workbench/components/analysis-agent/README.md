<!-- android-workbench-routing -->
在含 `workbench.project.json` 的工作项目中，使用 Android Workbench 统一入口提交已登记操作。本仓库内的脚本会转交共享服务；不要绕过队列操作手机或改写共用环境。服务不可用时先检查 `python3 <Workbench目录>/scripts/workbench.py --project <项目目录> capabilities`，不能退回未协调的直接执行。原能力范围、输入校验和退出码含义仍以本文为准。
<!-- /android-workbench-routing -->

# Android 分析代理组件

这个组件吸收 ReArk 的非 HarmonyOS 交互模式，把它变成可排队、可审计的 Workbench 操作：

- 请求路由：把问题分成轻量聊天、包概览、聚焦静态分析、静态快路径、设备运行和通用静态分析，并返回上下文预算。
- 包概览：读取 APK/AAB 的 ZIP 结构、DEX、原生库、资源、签名入口和哈希。
- 文件列表：按名称过滤包内条目，返回大小、压缩大小和 CRC32。
- 入口点：在 `aapt2` 可用时提取包名、启动 Activity、标签和 SDK 信息。
- Manifest 组件：提取 Activity、Service、Receiver、Provider、权限、导出状态和 Intent Filter。
- 资源导航：列出 `res/`、`assets/` 和 `resources.arsc`，统计类型并定位应用图标。
- 包内预览：识别文本、JSON、XML、图片和常见媒体格式，输出二进制十六进制片段；可导出不超过预览上限的单个条目。
- 反编译：通过已安装的 JADX 生成 Java 源码树，并输出文件数、大小和命令证据。
- 源码导航：在已生成的 Java/Kotlin/Smali/原生源码目录中按文件名或内容检索，并读取有界片段。
- 签名摘要：在 `apksigner` 可用时提取证书 SHA-256、DN、算法和签名方案；在 `keytool` 可用时补充证书有效期。
- 字符串检索：按优先级扫描 DEX、Manifest、资源、原生库和文本，输出有界结果。
- 快照：组合上述证据，形成一次可保存的包级初始视图。
- 知识检索：索引本地文档、目录和 URL，支持词法检索；配置 OpenAI 兼容 embedding 端点后支持语义检索。
- Python 计算：执行内联代码或项目内脚本，记录命令、退出码、stdout/stderr 和超时状态；可加载、追加、替换或清空可复用 Python session 状态。
- Scratchpad：读取、追加、替换或清空持久分析笔记，保存候选值、证据、未解决偏移和下一步。
- 宿主执行：执行任意 shell 命令，记录命令、环境键、退出码、stdout/stderr 和超时状态。

设计来源说明：交互模式参考了 [ReArk](https://github.com/lkimuk/ReArk) 的请求路由、上下文预算和证据接口；本组件没有复制 ReArk 源码，也不依赖 `hyle`、Qt 或 HarmonyOS 工具链。

## 使用

`COMPONENT_DIR` 指实际包含本文件的目录。所有命令输出 JSON；`--output` 可把同一 JSON 保存为证据文件。

```bash
python3 "$COMPONENT_DIR/scripts/analysis.py" route \
  --question "这个字符串如何被使用" --has-package

python3 "$COMPONENT_DIR/scripts/analysis.py" overview \
  --apk app.apk --output evidence/package-overview.json

python3 "$COMPONENT_DIR/scripts/analysis.py" files \
  --apk app.apk --query manifest --limit 200

python3 "$COMPONENT_DIR/scripts/analysis.py" entry-points \
  --apk app.apk

python3 "$COMPONENT_DIR/scripts/analysis.py" manifest \
  --apk app.apk --component-limit 500

python3 "$COMPONENT_DIR/scripts/analysis.py" resources \
  --apk app.apk --query icon --limit 200

python3 "$COMPONENT_DIR/scripts/analysis.py" preview \
  --apk app.apk --entry res/mipmap-mdpi/ic_launcher.png \
  --extract evidence/icon.png

python3 "$COMPONENT_DIR/scripts/analysis.py" decompile \
  --apk app.apk --output-dir evidence/decompiled --output evidence/decompile.json

python3 "$COMPONENT_DIR/scripts/analysis.py" code \
  --root decompiled-output --query "secretKey" --limit 200

python3 "$COMPONENT_DIR/scripts/analysis.py" signature \
  --apk app.apk

python3 "$COMPONENT_DIR/scripts/analysis.py" strings \
  --apk app.apk --query example.com --limit 120

python3 "$COMPONENT_DIR/scripts/analysis.py" snapshot \
  --apk app.apk --output evidence/package-snapshot.json

python3 "$COMPONENT_DIR/scripts/analysis.py" knowledge \
  --root docs --query "signature verification" \
  --index evidence/knowledge-index.json --output evidence/knowledge.json

python3 "$COMPONENT_DIR/scripts/analysis.py" python \
  --code "print(1 + 1)" --output evidence/python.json

python3 "$COMPONENT_DIR/scripts/analysis.py" python \
  --prelude "value = 41" --code "print(value + 1)" \
  --save-session evidence/python-session.py

python3 "$COMPONENT_DIR/scripts/analysis.py" python \
  --session evidence/python-session.py --code "print(value + 1)"

python3 "$COMPONENT_DIR/scripts/analysis.py" scratchpad \
  --path evidence/scratchpad.md --text "candidate=workbench" --mode append

python3 "$COMPONENT_DIR/scripts/analysis.py" exec \
  --command "uname -a" --output evidence/host.json
```

在已登记项目中，对应操作是：

| Operation | 用途 |
| --- | --- |
| `analysis.route` | 分类请求并返回上下文预算 |
| `analysis.overview` | APK/AAB 包级概览 |
| `analysis.files` | 包内文件导航 |
| `analysis.entry_points` | 启动入口提取 |
| `analysis.manifest` | Manifest 与组件清单 |
| `analysis.resources` | 资源与应用图标导航 |
| `analysis.preview` | 包内单文件预览和有界导出 |
| `analysis.decompile` | JADX 反编译源码树 |
| `analysis.code` | 反编译源码目录检索与片段读取 |
| `analysis.signature` | 签名摘要 |
| `analysis.strings` | 有界字符串检索 |
| `analysis.snapshot` | 组合快照 |
| `analysis.knowledge` | 本地/远程知识检索，可选 embedding |
| `analysis.python` | 任意 Python 计算，可带前置状态代码和可保存 session 状态 |
| `analysis.scratchpad` | 持久分析笔记，可读取、追加、替换或清空 |
| `analysis.exec` | 任意宿主命令执行 |

## 工具依赖

- ZIP 结构、文件列表、字符串检索、资源导航、源码导航和基础签名入口不依赖外部工具。
- `aapt2` 可用时，`overview`、`manifest`、`entry_points`、`resources`、`preview` 和 `snapshot` 会补充包名、版本、权限、SDK、启动 Activity、二进制 XML 和图标信息。
- `jadx` 可用时，`decompile` 生成源码树；默认查找 PATH 和项目 `.android-static/bin/jadx`。输出目录非空时必须显式传 `--overwrite`。
- `apksigner` 可用时，`overview`、`signature` 和 `snapshot` 会补充证书摘要、DN、算法和签名方案。
- `keytool` 可用时，`overview`、`signature` 和 `snapshot` 会补充证书有效期。
- `preview` 的 `--extract` 只导出不超过 `--max-bytes` 的条目；更大文件保留哈希和格式信息，但不会完整写出。
- 未找到工具时结果中标记 `available: false` 和 `uncertain: true`，不会伪造字段。
- `analysis.knowledge` 默认做本地词法检索；提供 `--embedding-endpoint` 和 `--embedding-model` 后使用 OpenAI 兼容 embeddings 接口做语义检索。向量随 `--index` 持久保存，后续查询只重新计算 query 向量。API key 只从 `--embedding-api-key-env` 指定的环境变量读取，不会写入结果。
- `analysis.python` 和 `analysis.exec` 直接提供任意计算与宿主执行能力；`analysis.python --prelude` 可先恢复常量、公式和候选值再执行新代码，`--session` 可加载已保存状态，`--save-session` 支持 `replace`/`append`，`--clear-session` 可清空状态，适合长链路逆向。返回值包含代码或命令、退出码、输出、超时和 session 哈希，用于复核逆向结论。
- `analysis.scratchpad` 把候选值、脚本结果、未解决偏移和下一步保存在项目文件中；`--mode append` 可持续追加，`--clear` 可在换样本或结论失效时清空。

## 分析契约

静态快路径必须遵守：

1. 不手算十六进制或大整数常量。
2. 用脚本断言完整的 `encode(candidate) == verifier` 或等价校验。
3. 完整答案必须包含具体候选值和断言结果。
4. 不能只留一个脚本让用户自己运行。

设备运行必须遵守：

1. 运行时验证是闭环，不是命令返回成功。
2. 命令成功不等于业务成功。
3. 结论必须绑定到语义 UI、日志、文件或状态证据。

详见 [请求路由与证据契约](references/agent-routing.md)。
