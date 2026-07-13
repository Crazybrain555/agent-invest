# adapters/sources/cninfo — CNINFO 双通道 source adapter

```text
client.py            WebAPI HTTP 客户端：token 缓存+过期刷新(resultcode 404/405)、
                     令牌桶(全请求共桶)、退避(base=1s/factor=2/cap=30s full jitter)、
                     审计脱敏(redact_params)；非 JSON 响应(网关 403 HTML 间歇拦截)
                     = non_json_response 可重试——实测行为，勿改成 fail-fast
source.py            主通道 CninfoSource：p_info3015 索引按 ≤30 天分片(TEXTID 去重合并)、
                     p_stock2100 档案(ORGID/USCC)、p_info3005 分类名(失败退
                     category_names_fallback.json 快照)；filing_type/report_period
                     在此映射后随 AnnouncementRef 出去(use case 不做 provider 判断)
web_source.py        免凭据兜底通道 CninfoWebSource：官网 hisAnnouncement/query +
                     szse_stock.json(code→orgId，query 必须成对传)；无档案(profile=None，
                     resolver 按"无名称主张"处理)、无 F006V(filing_type 从标题走同一规则包)；
                     已实测 announcementId==TEXTID、adjunctSize==F005N(KB)——
                     去重键与文件签名跨通道通用
mapper.py            p_info3015/p_stock2100 → DTO(TEXTID 即 provider_document_id)、
                     F006V 多段拆分映射、report_period 标题推导(07 §3.2 封闭规则)
filing_type_map.json 规则包(版本化，当前 r8)；**intermediary_report 必须排最前**(carrier 载体
                     判定优先——"激励计划法律意见书"是意见书不是激励公告)；
                     **performance_briefing 与 inquiry(双向语序两条)必须排在
                     semiannual/annual/quarterly 之前**(北交所"年度报告业绩说明会预告"
                     与"年度报告问询函回复"被定期报告子串抢注且伪造 report_period，实测)；
                     **semiannual 必须排在 annual 之前**(子串遮蔽，实测踩过)；
                     topic_rules 段=title_topic 追加规则(有码无码都追加命中 class 并给
                     下载资格，补 provider 码盲区；关键词是纯子串禁含 %)；
                     noise_rules 段=title_noise 负向规则(标题命中=绝对不下载不解析，
                     覆盖不能翻；match=all 按序 % 连接=语序敏感)
```

词表工程原则(2026-07-13 泛化审计定案，加删词必须遵守；决策与证据见
docs/implementation/reviews/vocab-generalization-2026-07-13.md)：

1. **语料证据强制**：每条前缀/关键词携带分层命中记录写入 note(池内+池外，标注留出集)；
   零命中规则要么删除、要么显式标注"占位待语料"并列入复审。禁止凭官方码表或单公司孤例入表
   (死词表 010309/01211160 的教训)。
2. **池外+留出集双段核验**：先在池外多行业语料跑全命中清单逐条判读，再用未参与推导的
   留出集复验；match=all 关键词语序必须与证据标题语序逐条一致(SQL LIKE 有序——语序变体
   是实测最大漏杀源：长城/北交所/中远海控三家各击穿过一条连排锚)。
3. **双轨互检指标化**：码与标题的分歧即告警——"码零命中而标题高值"=码盲区、"title 兜底
   撑起 process 分类但无下载资格"=资格裂缝(基线 432 行)、"前缀在语料零出现"=死词表；
   按 doctor/审计对账指标持续监控，不等下次人工审计。

硬规则：凭据只从 settings 进(构造注入)，query_params/日志一律先脱敏；
provider 词表 cninfo:p_info3015 / p_stock2100 / hisAnnouncement / download_pdf
定义在 application/use_cases/sync_disclosure_index.py（08 队列视图按此过滤）；
CLI 换通道：`pipeline sync --channel api|web`。
