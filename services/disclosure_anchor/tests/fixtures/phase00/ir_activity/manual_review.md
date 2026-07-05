sample: ir_activity.pdf
parser_version: MinerU 3.4.0
backend: pipeline
parsed_pages: {'start_page_no': 1, 'end_page_no': 24, 'full_pdf': True}
result:
  text_units_ok: yes
  table_units_ok: yes
  qa_units_ok: needs_review
issues:
  - MinerU 输出中第一页活动记录表包含一个大 table，第一条 Q&A 被嵌在 table_body 里；后续 Q&A 可由文本规则初步拆分。
action: needs_rule_adjustment
