sample: ir_activity.pdf
parser_version: MinerU 3.4.0
backend: pipeline
parsed_pages: {'start_page_no': 1, 'end_page_no': 24, 'full_pdf': True}
result:
  text_units_ok: yes
  table_units_ok: yes
  source_structure_units_ok: yes
  qa_projection_ok: n/a
issues:
  - MinerU 输出中第一页活动记录表包含一个大 table，第一条 Q&A 位于 table_body；后续问答按源结构保留为有序 text/table evidence blocks。
  - L1 不再根据问句或固定栏目词面改写边界；L2 命中任一同节成员后通过 evidence cluster 取得完整上下文并抽取 Q&A。
action: pass_l1_evidence_preservation
