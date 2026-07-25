# adapters/parsers/mineru — MinerU 适配与 NormalizedIR v3

管道：`mineru_process.py`（subprocess 调 CLI，剥离代理 env，HTTP 内并发显式限额；
timeout/worker stop 先 SIGINT 走 MinerU 官方临时 API cleanup，超时才强杀）
→ `artifact_reader.py`（定位/读 content_list 及同 stem 可选 model，形状不符
→ParserOutputContractError）→ `table_reconciler.py`（同页唯一 bbox + logical-cell 串接证据，
证明 MinerU 跨页 aggregate/empty ghost 同组，保持物理 carrier 并只给 root 附逐页 locator）→ `mapper_to_ir.py`
（content_list → normalized_ir.v3）→ `parser.py`（组装 ParserResult）。MinerU 是表格、版面和
locator 的唯一解析真源；parser/mapper 不做投关问答或业务内容替换。

IR v3 当前契约（schema：`contracts/normalized_ir/normalized_ir.v3.json`；v2 仅供既有 artifact/
fixture 兼容读取，禁止新写）：

- kind ∈ {text, heading, table, image, equation, page_furniture, unknown}；
  `type=list` 且 `list_items` 为至少一个非空的纯字符串列表时，按原顺序以换行连接成
  `kind=text, raw_kind=list`；空/混合/嵌套等不稳定形状与其他未映射 raw type → unknown +
  原样 raw_kind，**禁止丢弃**
- heading 判定：raw type=='text' 且 int(text_level)>=1；heading_level 同步
- 表结构化：rowspan/colspan 感知的 HTML→grid；**headers 只在 `<th>` 证据时非空**
  （MinerU 输出纯 td，headers 通常为空、全网格在 rows；用户 2026-07-16 裁决
  **不做表头提升**——无 th 证据时 headers 保持空、忠实落盘，表头解释归 L2/视图层；
  不要在任何层猜表头，实测会错标续表/KV 表）
- 非空 HTML 解析出零 cell → table_parse_failed=true（防静默空网格）
- model 对账的正向同组证明不得依赖标题/科目词：仅支持已验证的 pipeline/VLM schema、同页唯一 bbox
  （归一化坐标最大差 3/1000），且 content 聚合表的 logical cell 文本、`th/td`、rowspan、colspan 必须精确等于
  各页 model 表串接（允许续页重复首行被抑制）才算 proven。carrier 之间只允许 mapper/S1
  必然丢弃的页码，或跨至少两页精确重复、且不是报表 caption/结构标题的 running header/footer；
  普通正文和唯一页眉仍视为 nonadjacent。proven 后仍保持 aggregate+empty ghosts 的物理形态，
  只给 root 写完整逐页 locator，绝不把 model HTML 写回 page-local carrier；网格相等不足以证明
  下游 unit 边界与 hash 等价。缺失、歧义、坏 schema、不等价或不兼容均 fail closed；
  包括 model absent 在内始终在 `parser_diagnostics.table_reconciliation` 记录
  `mineru-aggregate-table-locator.v4`、model hash 与 locator-only/unresolved 计数；v4 是
  parser 自身不可变的定位证明，不绑定 S5 或全部 RULES_VERSION。BuildUnits 先把合法旧算法
  分类为必须重 parse，再对当前 v4 的完整 shape/cross-object 关系做 fail-loud 校验；
  locator 五字段还须与 diagnostics 的 group/table/model counts 对账；model relpath 进入 parser_artifacts。
- mapper 不做业务判断（单位识别/保留跳过在 05 builder；表头提升已整体废除）

golden fixtures 再生成：`scripts/regen_phase00_fixtures.py` 不带参数时从本地 source artifact
重跑 reader→reconciler→mapper→builder；仅 builder 变化才用 `--units-only` 从 committed IR
重建 units（当前 committed v2 fixture 属兼容输入；协议见 fixture policy；document_id 必须保持
phase00_<key>）。source artifact 不在
clean checkout 时，parser/reconciler 由真实样本测试或 `make test-mineru-smoke` 覆盖，不得把
builder-only fixture 更新当成 parser 回归。
