# adapters/parsers/mineru — MinerU 适配与 NormalizedIR v2

管道：`mineru_process.py`（subprocess 调 CLI，剥离代理 env，timeout→ParserTimeoutError）
→ `artifact_reader.py`（定位/读 content_list，形状不符→ParserOutputContractError）
→ `mapper_to_ir.py`（content_list → normalized_ir.v2）→ `parser.py`（组装 ParserResult）。

IR v2 关键契约（schema：contracts/normalized_ir/normalized_ir.v2.json）：

- kind ∈ {text, heading, table, image, equation, page_furniture, unknown}；
  未映射 raw type → unknown + 原样 raw_kind，**禁止丢弃**
- heading 判定：raw type=='text' 且 int(text_level)>=1；heading_level 同步
- 表结构化：rowspan/colspan 感知的 HTML→grid；**headers 只在 `<th>` 证据时非空**
  （MinerU 输出纯 td，headers 通常为空、全网格在 rows；表头提升是 05-S5 的
  业务规则，合并后执行——不要在 mapper 里猜表头，实测会错标续表/KV 表）
- 非空 HTML 解析出零 cell → table_parse_failed=true（防静默空网格）
- mapper 不做业务判断（单位识别/表头提升/保留跳过都在 05 builder）

golden fixtures 再生成：`scripts/regen_phase00_fixtures.py`（协议见 04R §6.4；
document_id 必须保持 phase00_<key>；真解析冒烟：`make test-mineru-smoke`）。
