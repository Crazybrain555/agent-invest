# adapters/parsers/mineru — MinerU 适配与 NormalizedIR v2

管道：`mineru_process.py`（subprocess 调 CLI，剥离代理 env，timeout→ParserTimeoutError）
→ `artifact_reader.py`（定位/读 content_list 及同 stem 可选 model，形状不符
→ParserOutputContractError）→ `table_reconciler.py`（同页唯一 bbox + logical-cell 串接证据，
把 MinerU 跨页聚合/空 ghost 恢复为 page-local HTML）→ `mapper_to_ir.py`
（content_list → normalized_ir.v2）→ `parser.py`（组装 ParserResult）。
投关/业绩说明会另由相邻 `parsers/native_text.py` 读取 PDF 原生文本层，作为可选
`native_text` shadow；MinerU 仍是表格/版面/locator 真源，业务恢复只在 unit builder 中发生。

IR v2 关键契约（schema：contracts/normalized_ir/normalized_ir.v2.json）：

- kind ∈ {text, heading, table, image, equation, page_furniture, unknown}；
  `type=list` 且 `list_items` 为至少一个非空的纯字符串列表时，按原顺序以换行连接成
  `kind=text, raw_kind=list`；空/混合/嵌套等不稳定形状与其他未映射 raw type → unknown +
  原样 raw_kind，**禁止丢弃**
- heading 判定：raw type=='text' 且 int(text_level)>=1；heading_level 同步
- 表结构化：rowspan/colspan 感知的 HTML→grid；**headers 只在 `<th>` 证据时非空**
  （MinerU 输出纯 td，headers 通常为空、全网格在 rows；表头提升是 05-S5 的
  业务规则，合并后执行——不要在 mapper 里猜表头，实测会错标续表/KV 表）
- 非空 HTML 解析出零 cell → table_parse_failed=true（防静默空网格）
- model 对账的正向等价证明不得依赖标题/科目词：仅支持已验证的 pipeline/VLM schema、同页唯一 bbox
  （归一化坐标最大差 3/1000），且 content 聚合表的 logical cell 文本、`th/td`、rowspan、colspan 必须精确等于
  各页 model 表串接（允许续页重复首行被抑制）才算 proven。carrier 之间只允许 mapper/S1
  必然丢弃的页码，或跨至少两页精确重复、且不是报表 caption/结构标题的 running header/footer；
  普通正文和唯一页眉仍视为 nonadjacent。分页 expanded 列宽一致、续页 caption/footnote 必须语义为空
  （缺失、`[]` 或仅空字符串均可，恢复时统一为 `[]`）、续页不得含 `<th>`，bbox 坐标必须为
  finite number，以证明 S5 会重并为一个内容不变的逻辑表；受控报表 caption/结构标题仅可作
  否决恢复的安全护栏，不能补足任何正向证据；否则保留 aggregate+empty ghost，绝不制造多个 ok
  碎片。缺失、歧义、坏 schema、不等价或不兼容均 fail closed；包括 model absent 在内始终在
  `parser_diagnostics.table_reconciliation` 记录 `mineru-aggregate-table-restore.v3`、实际依赖的
  专用 `table-builder-semantics` 版本（builder 入场时 fail-loud 校验，不绑定全部 RULES_VERSION）、model hash、
  restored/locator-only/unresolved 等原因计数，model relpath
  进入 parser_artifacts。
- `native_text`（可选）只保存 extractor/version/hash/逐页文本；不得在 parser/mapper 猜标题、
  问答或替换 MinerU table。仅 full-PDF run 且标题有记录/问答/实录证据时生成，并受同一
  parser timeout 的剩余预算约束；预期的 PDF/IO/子进程/预算失败只降级该 shadow，保留 MinerU
  主产物并写 `parser_diagnostics.native_text_shadow=unavailable + error_code`，未知异常仍显式失败；
  builder 仅在高置信官方表单 family 中使用，shadow unavailable/empty 时 fail closed 为 needs_review。
- mapper 不做业务判断（单位识别/表头提升/保留跳过都在 05 builder）

golden fixtures 再生成：`scripts/regen_phase00_fixtures.py` 不带参数时从本地 source artifact
重跑 reader→reconciler→mapper→builder；仅 builder 变化才用 `--units-only` 从 committed IR
重建 units（协议见 fixture policy；document_id 必须保持 phase00_<key>）。source artifact 不在
clean checkout 时，parser/reconciler 由真实样本测试或 `make test-mineru-smoke` 覆盖，不得把
builder-only fixture 更新当成 parser 回归。
