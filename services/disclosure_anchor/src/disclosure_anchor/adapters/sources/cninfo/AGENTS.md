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
filing_type_map.json 规则包(版本化)；**intermediary_report 必须排最前**(carrier 载体
                     判定优先——"激励计划法律意见书"是意见书不是激励公告)；
                     **semiannual 必须排在 annual 之前**(子串遮蔽，实测踩过)；
                     topic_rules 段=title_topic 追加规则(有码无码都追加命中 class，
                     补 provider 码盲区：销售简报/经营数据/发电量走 012305 而非 010309)；
                     noise_rules 段=title_noise 负向规则(标题命中=绝对不下载不解析，
                     覆盖不能翻；加词必须先跑全库+候选层误伤核验并写 note)
```

硬规则：凭据只从 settings 进(构造注入)，query_params/日志一律先脱敏；
provider 词表 cninfo:p_info3015 / p_stock2100 / hisAnnouncement / download_pdf
定义在 application/use_cases/sync_disclosure_index.py（08 队列视图按此过滤）；
CLI 换通道：`pipeline sync --channel api|web`。
