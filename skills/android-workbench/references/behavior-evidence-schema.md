# 行为报告的来源审计格式

配合 [`code-first-behavior.md`](code-first-behavior.md) 使用。这里定义可检查的记录，不替代断言的语义审查。项目生成数据留在分析工程，Workbench 只保存工具与格式。

`scripts/audit_behavior_report.py` 接收：

| 输入 | 形状 |
| --- | --- |
| report JSON | `schema: "android-workbench.behavior-review/v1"`，`completion: partial/complete`，`claims: []` |
| facts JSONL | 每行 `fact_id`、`value`、`status`、原 canonical `sources`（artifact 与 sha256） |
| declarations JSONL | 引用原 DEX 的唯一 `dex_id/class/name/descriptor`、`code_offset`、`source`、`source_sha256` |

每条 claim：

```json
{
  "id": "popup.entry",
  "assertion": "与 fact.value 完全一致的有限断言",
  "status": "decoded",
  "sources": [
    {
      "id": "original",
      "artifact": "relative/original-code.txt",
      "sha256": "原文件完整 SHA-256",
      "locator": {"kind": "lines", "start": 10, "end": 30}
    }
  ],
  "code": {"state": "reviewed", "summary": "具体动作与参数", "source_ids": ["original"]},
  "trigger": {"state": "reviewed", "summary": "调用者与资格", "source_ids": ["original"]},
  "flow": {
    "state": "reviewed",
    "summary": "入口条件、动作、异常/关闭边界",
    "source_ids": ["original"],
    "nodes": [{"id": "entry", "source_ids": ["original"]}, {"id": "body", "source_ids": ["original"]}],
    "edges": [{"from": "entry", "to": "body", "kind": "direct", "status": "decoded", "summary": "具体条件", "source_ids": ["original"]}]
  },
  "review": {"state": "reviewed", "summary": "如何从原字节/源码复核", "source_ids": ["original"]},
  "runtime": {"state": "pending", "summary": "自然派发未观察；静态结论不变"},
  "limits": ["限定当前方法与被调用接口，不声明显示效果"],
  "temporal_scope": "固定版本/哈希和所列条件"
}
```

`code/trigger/flow/review.state` 为 reviewed / pending / not_applicable。reviewed 必须引用非空 source_ids；not_applicable 给 `reason`。complete 报告不能含这四项的 pending。runtime 为 observed / pending / not_applicable；静态交付可有 runtime pending，但 observed claim 要有 observed runtime、来源、conditions、trigger_mode；有限阴性观察还需 `negative_observation: true` 与正 `window_ms`。

source path 需在分析根目录内，文件 SHA、line 范围、JSON pointer 与方法身份必须一致。支持 locator：

| kind | 字段 |
| --- | --- |
| whole_artifact | 整个文件；只能支持确实按文件审查的内容 |
| lines | start/end 均为实际 1-based 行号 |
| json_pointer | pointer 为 RFC 6901 路径 |
| bytecode | method 为 dex_id、class、name、descriptor、code_offset、instruction_bytes_sha256 |
| elf | virtual_address、size；同时需要真实 ELF 指令/地址语义审查，当前审计器不替代该核验 |

bytecode 的 instruction hash 是 `code_item+16` 起、原 insns_size×2 个字节。方法声明来源是原 DEX，不能用旧 smali 模型索引代替。引用 `.dexdump.txt` 时脚本核对对应 code_item header 与完整签名。switch_case 的 locator 还需 selector、dispatch_pc、target_pc 和 payload_source_ids；这些字段的存在不证明选定 case 的含义。

保留 fact 的 canonical 来源，不允许用另一份不相干但哈希有效的文件替代。逐方法 JSONL 可另附原 code_item、行号、行哈希和完整源码契约；这种覆盖与结构审计 pass 不能自动提升为全方法或任意输入的全程序证明。
