# adapters/parsers/mineru — MinerU 适配与 NormalizedIR v2

管道：`mineru_process.py`（subprocess 调 CLI，剥离代理 env，timeout→ParserTimeoutError）
→ `artifact_reader.py`（定位/读 content_list，形状不符→ParserOutputContractError）
→ `mapper_to_ir.py`（content_list → normalized_ir.v2）→ `parser.py`（组装 ParserResult）。
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
- `native_text`（可选）只保存 extractor/version/hash/逐页文本；不得在 parser/mapper 猜标题、
  问答或替换 MinerU table。仅 full-PDF run 且标题有记录/问答/实录证据时生成，并受同一
  parser timeout 的剩余预算约束；builder 仅在高置信官方表单 family 中使用，失败则保持
  MinerU 产物并 fail closed 为 needs_review。
- mapper 不做业务判断（单位识别/表头提升/保留跳过都在 05 builder）

golden fixtures 再生成：`scripts/regen_phase00_fixtures.py`（协议见 04R §6.4；
document_id 必须保持 phase00_<key>；真解析冒烟：`make test-mineru-smoke`）。
