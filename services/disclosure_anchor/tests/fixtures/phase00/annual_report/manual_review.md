sample: annual_report.pdf
parser_version: MinerU 3.4.0
backend: pipeline
parsed_pages: {'start_page_no': 1, 'end_page_no': 209, 'full_pdf': True}
result:
  text_units_ok: yes
  table_units_ok: yes
  qa_units_ok: n/a
counts:
  elements: 2697 {'text': 1805, 'header': 209, 'page_number': 209, 'table': 473, 'image': 1}
  document_units: 1617 {'text': 1144, 'table': 473}
issues:
  - 全量 209 页可解析并生成 fixture；年报表格密集，后续正式 mapper 需要保留原始表格字符串并另做数值规范化。
  - 当前 Phase 00 heading_path 是启发式提取，足够做 feasibility/golden fixture，但不是最终章节切分规则。
action: pass
